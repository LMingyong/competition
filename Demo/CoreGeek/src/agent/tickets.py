"""工人任务单和开拓者任务单。两套不混用。

大任务里的小任务按顺序做:要多少料一次算清,采够再连续建造。
炮位、墙格都从 brain 里现成的计划函数读,这里不写死坐标。
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import WEAPON_BUILD_COST, WALL_MATERIAL
from .world import World, count_item

BUILD_TOWER = "建炮"
BUILD_WALL = "建墙"
UPGRADE_TOWER = "升级炮"
UPGRADE_WALL = "升级墙"
EVOLVE = "自进化"

WORKER_TICKETS = (BUILD_TOWER, BUILD_WALL, UPGRADE_TOWER, UPGRADE_WALL)
WORKER_BACKPACK = 100
TOWER_CAP = 3
TOWER_STEP_GOLD = {1: 100, 2: 150}
WALL_STEP_GOLD = {1: 20, 2: 30}
_BASE_RANK = {
    BUILD_TOWER: 0,
    BUILD_WALL: 1,
    UPGRADE_TOWER: 2,
    UPGRADE_WALL: 3,
}


@dataclass(frozen=True)
class SmallTask:
    """大任务里的一步。kind 为 collect / sell / build / buy / upgrade / answer / walk。"""

    kind: str
    count: int = 0
    material: str = ""


@dataclass(frozen=True)
class BigPlan:
    """一张大任务:要建或升多少、要多少料、小任务按顺序执行。"""

    name: str
    count: int
    material: str
    material_count: int
    steps: tuple[SmallTask, ...]
    claimable: bool


def worker_ticket_order(penalty: dict[str, int] | None = None) -> list[str]:
    """当前优先级从高到低。领取时排序值 +2,在四项里下移两位。"""
    extra = penalty or {}
    return sorted(
        WORKER_TICKETS,
        key=lambda name: (_BASE_RANK[name] + extra.get(name, 0), _BASE_RANK[name]),
    )


def plan_big_task(turn: World, name: str) -> BigPlan:
    """按当前场面算一张大任务要的数量、物料和小任务。不改场面。"""
    if name == BUILD_TOWER:
        return _plan_build_tower(turn)
    if name == BUILD_WALL:
        return _plan_build_wall(turn)
    if name == UPGRADE_TOWER:
        return _plan_upgrade_tower(turn)
    if name == UPGRADE_WALL:
        return _plan_upgrade_wall(turn)
    return BigPlan(name, 0, "", 0, (), False)


def plan_pioneer_task(turn: World) -> BigPlan:
    """开拓者只有自进化:停在当前任务点做题并提交,再走向下一个任务点。"""
    points = turn.own_task_zones() or tuple(task.pos for task in turn.player_tasks)
    steps = (
        SmallTask("answer", 1, "task"),
        SmallTask("walk", 1, "next_task"),
    )
    return BigPlan(EVOLVE, len(points), "", 0, steps, True)


def claim_next_ticket(turn: World, memory, unit_id: int) -> str | None:
    """空闲工人领当前最高、还做得了、且没被别人领走的大任务。领完优先级 +2。"""
    if unit_id in memory.ticket_owner:
        return memory.ticket_owner[unit_id]
    taken = set(memory.ticket_owner.values())
    for name in worker_ticket_order(memory.ticket_penalty):
        if name in taken or name == EVOLVE:
            continue
        plan = plan_big_task(turn, name)
        if not plan.claimable:
            continue
        memory.ticket_owner[unit_id] = name
        memory.ticket_penalty[name] = memory.ticket_penalty.get(name, 0) + 2
        return name
    return None


def release_ticket(memory, unit_id: int) -> None:
    memory.ticket_owner.pop(unit_id, None)
    hold = getattr(memory, "ticket_hold", None)
    if hold is not None:
        hold.discard(unit_id)


def _plan_build_tower(turn: World) -> BigPlan:
    from .brain import _tower_sites

    standing = {unit.pos for unit in turn.weapons()}
    missing = [pos for pos in _tower_sites(turn) if pos not in standing]
    cap_left = max(0, TOWER_CAP - len(turn.weapons()))
    count = min(len(missing), cap_left)
    gold = count * WEAPON_BUILD_COST
    if count <= 0:
        return BigPlan(BUILD_TOWER, 0, "gold", 0, (), False)
    if turn.gold >= gold:
        steps = (SmallTask("build", count, "rocket"),)
    else:
        short = gold - turn.gold
        steps = (
            SmallTask("collect", short, "gold"),
            SmallTask("sell", short, "gold"),
            SmallTask("build", count, "rocket"),
        )
    return BigPlan(BUILD_TOWER, count, "gold", gold, steps, True)


def _opening_rockets_pending(turn: World) -> bool:
    """三门火箭还没齐、场上武器也不满三座:这时先不领升级单。"""
    from .brain import _tower_sites

    if len(turn.weapons()) >= TOWER_CAP:
        return False
    standing = {unit.pos for unit in turn.weapons()}
    return any(pos not in standing for pos in _tower_sites(turn))


def _plan_build_wall(turn: World) -> BigPlan:
    from .brain import _gun_stand, _tower_sites, _wall_order

    blocked = set(_tower_sites(turn))
    stand = _gun_stand(turn)
    if stand is not None:
        blocked.add(stand)
    standing = {unit.pos for unit in turn.walls()}
    missing = [
        pos for pos in _wall_order(turn)
        if pos not in standing and pos not in blocked
    ]
    count = len(missing)
    if count <= 0:
        return BigPlan(BUILD_WALL, 0, WALL_MATERIAL, 0, (), False)
    steps: list[SmallTask] = []
    left = count
    while left > 0:
        batch = min(left, WORKER_BACKPACK)
        steps.append(SmallTask("collect", batch, WALL_MATERIAL))
        steps.append(SmallTask("build", batch, "wall"))
        left -= batch
    return BigPlan(BUILD_WALL, count, WALL_MATERIAL, count, tuple(steps), True)


def _plan_upgrade_tower(turn: World) -> BigPlan:
    if _opening_rockets_pending(turn):
        return BigPlan(UPGRADE_TOWER, 0, "gold", 0, (), False)
    rockets = [
        unit for unit in turn.weapons()
        if unit.kind == "rocket" and unit.level < 3
    ]
    return _plan_upgrade(UPGRADE_TOWER, rockets, TOWER_STEP_GOLD, turn.gold)


def _plan_upgrade_wall(turn: World) -> BigPlan:
    from .brain import _on_incoming_side

    if _opening_rockets_pending(turn):
        return BigPlan(UPGRADE_WALL, 0, "gold", 0, (), False)
    walls = [
        unit for unit in turn.walls()
        if unit.level < 3 and _on_incoming_side(unit.pos, turn)
    ]
    return _plan_upgrade(UPGRADE_WALL, walls, WALL_STEP_GOLD, turn.gold)


def _plan_upgrade(name: str, units, prices: dict[int, int], gold: int) -> BigPlan:
    count = len(units)
    if count <= 0:
        return BigPlan(name, 0, "gold", 0, (), False)
    need = sum(prices.get(unit.level, 0) for unit in units)
    steps: list[SmallTask] = []
    if gold < need:
        short = need - gold
        steps.append(SmallTask("collect", short, "gold"))
        steps.append(SmallTask("sell", short, "gold"))
    steps.append(SmallTask("buy", count, "gold"))
    steps.append(SmallTask("upgrade", count, "gold"))
    return BigPlan(name, count, "gold", need, tuple(steps), True)


def stone_goal(role, missing: int) -> int:
    """这一批要拿在自己背包里的石头。装不下就先采满。"""
    cap = role.capacity if role.capacity else WORKER_BACKPACK
    have = count_item(role, WALL_MATERIAL)
    others = len(role.backpack) - have
    room = max(0, cap - others)
    return min(missing, room)
