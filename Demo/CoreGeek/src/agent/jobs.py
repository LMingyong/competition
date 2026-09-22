"""跨回合粘性任务(Job)。

HTTP 每回合无状态重入,进程级 MEMORY 记住每个角色正在做的事,
避免 if 链每回合重选目标导致中途改去商店/另一座矿。
"""

from __future__ import annotations

from dataclasses import dataclass
from .protocol import Pos

KIND_MINE = "mine"
KIND_WALL = "wall"
KIND_TOWER = "tower"
KIND_SHOP = "shop"
KIND_SELL = "sell"
KIND_RECALL = "recall"
KIND_ACCEPT = "accept_task"
KIND_PHASE = "phase_task"
KIND_TREASURE = "treasure"
KIND_MAN_TOWER = "man_tower"
KIND_HOLD = "hold"

# 任务单把已领取的大任务放在这些表上时，interrupt_task 会摘掉。
_ORDER_TABLES = ("orders", "big_tasks", "task_orders", "assignments")

ROLE_BUILDER = "builder"
ROLE_MINER = "miner"
ROLE_PIONEER = "pioneer"

FAIL_LIMIT = 3
SELL_THRESHOLD = 8


@dataclass
class Job:
    kind: str
    target: Pos | None = None
    name: str = ""
    started: int = 0
    fail_streak: int = 0
    tower_id: int = 0
    # 跨回合接着做同一件小任务，不要走两步就另选目标。
    big: str = ""
    small: str = ""
    progress: int = 0


def assign_roles(turn, memory) -> None:
    """工号较小的工人砌墙建塔,其余专职采矿;孤身工人两头都扛。"""
    alive = {unit.unit_id for unit in turn.controllable()}
    owners = getattr(memory, "ticket_owner", None)
    hold = getattr(memory, "ticket_hold", None)
    for unit_id in list(memory.roles):
        if unit_id not in alive:
            memory.roles.pop(unit_id, None)
            memory.jobs.pop(unit_id, None)
            if owners is not None:
                owners.pop(unit_id, None)
            if hold is not None:
                hold.discard(unit_id)
    workers = list(turn.workers())
    if len(workers) == 1:
        memory.roles[workers[0].unit_id] = ROLE_BUILDER
    elif len(workers) >= 2:
        memory.roles[workers[0].unit_id] = ROLE_BUILDER
        for worker in workers[1:]:
            memory.roles[worker.unit_id] = ROLE_MINER
    pioneer = turn.pioneer()
    if pioneer is not None:
        memory.roles[pioneer.unit_id] = ROLE_PIONEER


def is_builder(memory, unit_id: int) -> bool:
    return memory.roles.get(unit_id) != ROLE_MINER


def is_edge_miner(turn, memory, unit_id: int) -> bool:
    """三人齐时矿工夜间/回防窗口去地图边缘采矿,另外两人回塔。"""
    if memory.roles.get(unit_id) != ROLE_MINER:
        return False
    return len(turn.controllable()) >= 3


def update_fail_streaks(turn, memory) -> None:
    """上回合失败累加;连续失败则丢掉 Job,允许重选。"""
    for unit_id, job in list(memory.jobs.items()):
        ok = turn.last_ok(unit_id)
        if ok is False:
            job.fail_streak += 1
        elif ok is True:
            job.fail_streak = 0
        if job.fail_streak >= FAIL_LIMIT:
            memory.jobs.pop(unit_id, None)
            # 连续失败才允许丢掉这张大任务,改领别的。自进化不在这套工人单里。
            owners = getattr(memory, "ticket_owner", None)
            if owners is not None and owners.get(unit_id) not in (None, "自进化"):
                owners.pop(unit_id, None)


def claimed_targets(memory, except_id: int) -> set[Pos]:
    taken: set[Pos] = set()
    for unit_id, job in memory.jobs.items():
        if unit_id == except_id or job.target is None:
            continue
        taken.add(job.target)
    return taken


def set_job(memory, unit_id: int, job: Job) -> Job:
    memory.jobs[unit_id] = job
    return job


def clear_job(memory, unit_id: int) -> None:
    memory.jobs.pop(unit_id, None)


def get_job(memory, unit_id: int) -> Job | None:
    return memory.jobs.get(unit_id)


def _unit_id_of(role) -> int:
    if hasattr(role, "unit_id"):
        return int(role.unit_id)
    return int(role)


def _order_owner(order) -> int | None:
    if isinstance(order, dict):
        for key in ("assignee", "unit_id", "owner", "role_id"):
            value = order.get(key)
            if isinstance(value, int):
                return value
        return None
    for attr in ("assignee", "unit_id", "owner", "role_id"):
        value = getattr(order, attr, None)
        if isinstance(value, int):
            return value
    return None


def _unassign(order) -> None:
    if isinstance(order, dict):
        if "assignee" in order:
            order["assignee"] = None
        if "claimed" in order:
            order["claimed"] = False
        return
    if hasattr(order, "assignee"):
        order.assignee = None
    if hasattr(order, "claimed"):
        order.claimed = False


def _release_table(table, unit_id: int) -> None:
    if isinstance(table, dict):
        table.pop(unit_id, None)
        for key, order in list(table.items()):
            if _order_owner(order) != unit_id:
                continue
            if key == unit_id:
                table.pop(key, None)
                continue
            _unassign(order)
        return
    if isinstance(table, list):
        for order in table:
            if _order_owner(order) == unit_id:
                _unassign(order)


def has_claimed_order(memory, unit_id: int) -> bool:
    """这个人身上是否还挂着已领取的大任务。没有任务单时为 False。"""
    owners = getattr(memory, "ticket_owner", None)
    if isinstance(owners, dict) and unit_id in owners:
        return True
    for name in _ORDER_TABLES:
        table = getattr(memory, name, None)
        if isinstance(table, dict):
            if unit_id in table:
                return True
            if any(_order_owner(order) == unit_id for order in table.values()):
                return True
        elif isinstance(table, list):
            if any(_order_owner(order) == unit_id for order in table):
                return True
    return False


def drop_claimed_orders(memory, unit_id: int) -> None:
    """摘掉已领取的大任务（建炮/建墙/升级炮/升级墙）。没有任务单时什么都不做。

    字典按 unit_id 存的条目直接删掉；按任务编号存、身上带 assignee 的，只解除领取，
    单子留在池里，下一回合可以再派。memory.clear_order(unit_id) 若存在也会被调用。
    """
    for name in _ORDER_TABLES:
        _release_table(getattr(memory, name, None), unit_id)
    # 工人任务单：ticket_owner 里领走的建炮/建墙/升级炮/升级墙。
    release = getattr(memory, "clear_order", None)
    if callable(release):
        release(unit_id)
    try:
        from .tickets import release_ticket
    except ImportError:
        release_ticket = None
    if release_ticket is not None and hasattr(memory, "ticket_owner"):
        release_ticket(memory, unit_id)


def interrupt_task(memory, role, reason: str) -> None:
    """打断这个人当前领走的大任务和正在执行的小任务/Job。

    不查看工单锁，锁也挡不住这次清除。清除后下一回合可以重派。
    reason 只说明原因（dusk / night / 任务单主动调用），不改变清除范围。

    任务单以后直接调用本函数即可，不必自己拆锁。
    调用点在 brain.decide：入夜前 7 个白天回合，以及每一个黑夜回合。
    """
    unit_id = _unit_id_of(role)
    drop_claimed_orders(memory, unit_id)
    clear_job(memory, unit_id)
    if reason:
        log = getattr(memory, "interrupts", None)
        if log is None:
            memory.interrupts = []
            log = memory.interrupts
        log.append((unit_id, reason))
