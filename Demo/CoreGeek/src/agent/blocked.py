"""连续两回合被堵住就换方向。

被堵住指下面任一情况，连续累计满 2 回合：
- 发出的 move 落点等于当前格
- 上回合 move 在 lastRoundRoleActionResults 里失败
- 决策时原目标周围落脚点被占、越界或被建筑/单位挡住，实际走不了原方向
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .grid import next_step
from .protocol import Pos, Unit, distance
from .world import World

_LAST_ROUND = -1


@dataclass
class MoveBlock:
    streak: int = 0
    goal: Pos | None = None
    failed_step: Pos | None = None
    avoid: frozenset[Pos] = field(default_factory=frozenset)
    blocked_adj: frozenset[Pos] = field(default_factory=frozenset)
    issued: Pos | None = None
    pos: Pos | None = None
    decision_blocked: bool = False
    probe_blocked: bool = False
    probe_goal: Pos | None = None
    probe_failed: Pos | None = None
    probe_adj: frozenset[Pos] = field(default_factory=frozenset)
    pending_goal: Pos | None = None
    accounted_round: int = -1
    rerouted: bool = False


def _live_memory():
    from . import tasks as tasks_mod
    return tasks_mod.MEMORY


def _table(memory=None) -> dict[int, MoveBlock]:
    if memory is None:
        memory = _live_memory()
    table = getattr(memory, "move_block", None)
    if not isinstance(table, dict):
        table = {}
        memory.move_block = table
    return table


def block_record(unit_id: int) -> MoveBlock:
    table = _table()
    rec = table.get(unit_id)
    if not isinstance(rec, MoveBlock):
        rec = MoveBlock()
        table[unit_id] = rec
    return rec


def _brain():
    from . import brain
    return brain


def account_blocked_turns(turn: World, memory) -> None:
    """用上一回合的结果累计被堵住。同一回合只记一次。"""
    global _LAST_ROUND
    table = _table(memory)
    if turn.round_no < _LAST_ROUND:
        table.clear()
    _LAST_ROUND = turn.round_no
    for role in turn.controllable():
        rec = table.get(role.unit_id)
        if not isinstance(rec, MoveBlock) or rec.accounted_round == turn.round_no:
            continue
        noop = (
            rec.issued is not None
            and rec.pos is not None
            and rec.issued == rec.pos
            and role.pos == rec.pos
        )
        move_failed = (
            rec.issued is not None
            and rec.pos is not None
            and rec.issued != rec.pos
            and turn.last_ok(role.unit_id) is False
        )
        arrived = (
            rec.issued is not None
            and rec.pos is not None
            and rec.issued != rec.pos
            and role.pos == rec.issued
            and turn.last_ok(role.unit_id) is not False
        )
        if arrived:
            rec.streak = 0
            rec.failed_step = None
        elif noop or move_failed or rec.decision_blocked:
            rec.streak += 1
            if noop or move_failed:
                rec.failed_step = rec.issued
        elif rec.issued is None and not rec.decision_blocked:
            rec.streak = 0
        rec.decision_blocked = False
        rec.accounted_round = turn.round_no


def remember_issued_moves(
    turn: World, memory, commands: dict[int, dict[str, Any]],
) -> None:
    """记下本回合发出的 move，供下一回合判断有没有被堵住。"""
    brain = _brain()
    alive = {role.unit_id for role in turn.controllable()}
    table = _table(memory)
    for unit_id in list(table):
        if unit_id not in alive:
            table.pop(unit_id, None)
    for role in turn.controllable():
        rec = block_record(role.unit_id)
        command = commands.get(role.unit_id)
        cell = None
        if command and command.get("action") == "move":
            cell = brain._command_cell(command)
        if cell is not None and cell != role.pos:
            if rec.pending_goal is not None and rec.goal not in (None, rec.pending_goal):
                rec.streak = 0
                rec.failed_step = None
                rec.avoid = frozenset()
                rec.blocked_adj = frozenset()
            if rec.pending_goal is not None:
                rec.goal = rec.pending_goal
            rec.issued = cell
            rec.pos = role.pos
            rec.decision_blocked = False
            rec.probe_blocked = False
        elif cell is not None and cell == role.pos:
            rec.issued = cell
            rec.pos = role.pos
            rec.failed_step = cell
            rec.decision_blocked = True
            rec.probe_blocked = False
        else:
            rec.issued = None
            rec.pos = role.pos
            if rec.probe_blocked:
                rec.decision_blocked = True
                if rec.probe_goal is not None:
                    if rec.goal not in (None, rec.probe_goal):
                        rec.streak = 0
                        rec.failed_step = None
                        rec.avoid = frozenset()
                    rec.goal = rec.probe_goal
                rec.blocked_adj = rec.probe_adj
                if rec.failed_step is None:
                    rec.failed_step = rec.probe_failed
            else:
                rec.decision_blocked = False
            rec.probe_blocked = False
        rec.pending_goal = None


def _blocked_goal_neighbours(
    turn: World, role: Unit, goal: Pos, claimed: set[Pos],
) -> frozenset[Pos]:
    brain = _brain()
    blocked = turn.blocked(role)
    bad: list[Pos] = []
    for pos in brain._neighbours(goal):
        if pos == role.pos:
            continue
        if not turn.land(pos) or pos in blocked or pos in claimed:
            bad.append(pos)
    return frozenset(bad)


def note_direction_blocked(
    turn: World, role: Unit, goal: Pos, claimed: set[Pos],
) -> None:
    rec = block_record(role.unit_id)
    adj = _blocked_goal_neighbours(turn, role, goal, claimed)
    rec.probe_blocked = True
    rec.probe_goal = goal
    rec.probe_adj = adj
    if adj:
        rec.probe_failed = min(
            adj, key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
        )
    else:
        rec.probe_failed = None


def _cells_others_will_build(
    commands: dict[int, dict[str, Any]] | None, except_id: int,
) -> set[Pos]:
    brain = _brain()
    cells: set[Pos] = set()
    if not commands:
        return cells
    for unit_id, command in commands.items():
        if unit_id == except_id or command.get("action") != "build":
            continue
        pos = brain._command_cell(command)
        if pos is not None:
            cells.add(pos)
    return cells


def _reroute_forbidden(
    turn: World,
    role: Unit,
    goal: Pos,
    rec: MoveBlock,
    claimed: set[Pos],
    travel_avoid: set[Pos],
    commands: dict[int, dict[str, Any]] | None,
) -> set[Pos]:
    brain = _brain()
    bad = set(travel_avoid)
    bad.update(rec.avoid)
    bad.update(rec.blocked_adj)
    bad.update(_blocked_goal_neighbours(turn, role, goal, claimed))
    if rec.failed_step is not None:
        bad.add(rec.failed_step)
    bad.add(role.pos)
    if role.kind == "pioneer":
        bad.update(brain._tower_sites(turn))
        stand = brain._gun_stand(turn)
        if stand is not None:
            bad.add(stand)
        bad.update(_cells_others_will_build(commands, role.unit_id))
    elif brain._opening_rockets_pending(turn):
        bad.update(brain._unbuilt_rocket_sites(turn))
        stand = brain._gun_stand(turn)
        if stand is not None:
            bad.add(stand)
    return bad


def _can_enter(
    turn: World, role: Unit, pos: Pos, forbidden: set[Pos], claimed: set[Pos],
) -> bool:
    if pos == role.pos or pos in forbidden or pos in claimed:
        return False
    if not turn.land(pos) or pos in turn.blocked(role):
        return False
    return True


def _approach_stands(
    turn: World, role: Unit, goal: Pos, forbidden: set[Pos], claimed: set[Pos],
) -> list[Pos]:
    brain = _brain()
    seen: set[Pos] = set()
    stands: list[Pos] = []

    def add(pos: Pos) -> None:
        if pos in seen:
            return
        seen.add(pos)
        if _can_enter(turn, role, pos, forbidden, claimed):
            stands.append(pos)

    for pos in brain._neighbours(goal):
        add(pos)
    sites = tuple(brain._tower_sites(turn))
    if goal in sites:
        for site in sites:
            if site == goal:
                continue
            for pos in brain._neighbours(site):
                add(pos)
    return stands


def _pick_other_side(
    turn: World,
    role: Unit,
    goal: Pos,
    claimed: set[Pos],
    forbidden: set[Pos],
    failed: Pos | None,
) -> Pos | None:
    here = distance(role.pos, goal)
    best: Pos | None = None
    best_key: tuple | None = None
    for stand in _approach_stands(turn, role, goal, forbidden, claimed):
        step = next_step(turn, role, stand, forbidden)
        if step is None or not _can_enter(turn, role, step, forbidden, claimed):
            continue
        if distance(step, goal) >= here:
            continue
        away = -distance(step, failed) if failed is not None else 0
        key = (distance(step, goal), away, step.x, step.y)
        if best_key is None or key < best_key:
            best_key = key
            best = step
    if best is not None:
        return best
    brain = _brain()
    laterals = [
        pos for pos in brain._neighbours(role.pos)
        if _can_enter(turn, role, pos, forbidden, claimed)
    ]
    if not laterals:
        return None
    return min(
        laterals,
        key=lambda pos: (
            distance(pos, goal),
            -distance(pos, failed) if failed is not None else 0,
            pos.x,
            pos.y,
        ),
    )


def _finish_reroute(
    rec: MoveBlock, goal: Pos, failed: Pos | None, adj: frozenset[Pos],
) -> None:
    sticky = set(rec.avoid)
    sticky.update(adj)
    if failed is not None:
        sticky.add(failed)
    rec.streak = 0
    rec.goal = goal
    rec.failed_step = None
    rec.blocked_adj = adj
    rec.avoid = frozenset(sticky)
    rec.probe_blocked = False
    rec.pending_goal = goal
    rec.rerouted = True


def reroute_if_blocked_two_turns(
    turn: World,
    role: Unit,
    goal: Pos,
    claimed: set[Pos],
    travel_avoid: set[Pos] | None = None,
    commands: dict[int, dict[str, Any]] | None = None,
) -> tuple[bool, Pos | None]:
    """单位本应移动，但连续两回合被堵住，就换个方向走。

    未满两回合返回 (False, None)，调用方继续原方向。
    满两回合不再朝同一个失败步走，避开该格和原目标上被挡的邻格，
    改选另一侧走得到、仍靠近原任务的空格（另一门炮、另一个空邻格，或绕开队友）。
    没有更近的格子就先迈到空着的侧向格离开堵点。
    换向后把失败步从候选里拿掉，计数清掉。
    开拓者不会踩进火箭格、站位、或别人本回合要 build 的格子。
    """
    rec = block_record(role.unit_id)
    if rec.streak < 2 or (rec.goal is not None and rec.goal != goal):
        return False, None
    failed = rec.failed_step
    adj = _blocked_goal_neighbours(turn, role, goal, claimed)
    if rec.blocked_adj:
        adj = frozenset(set(adj) | set(rec.blocked_adj))
    forbidden = _reroute_forbidden(
        turn, role, goal, rec, claimed, set(travel_avoid or ()), commands,
    )
    forbidden.update(adj)
    if failed is not None:
        forbidden.add(failed)
    step = _pick_other_side(turn, role, goal, claimed, forbidden, failed)
    if step is None:
        return True, None
    _finish_reroute(rec, goal, failed, adj)
    if failed is not None:
        turn.note(
            f"角色 {role.unit_id} 连续两回合被堵住，"
            f"不再走向 ({failed.x},{failed.y})，改走 ({step.x},{step.y})"
        )
    else:
        turn.note(
            f"角色 {role.unit_id} 连续两回合被堵住，改走 ({step.x},{step.y})"
        )
    return True, step


def keep_rerouted_pioneer_clear(
    turn: World, memory, commands: dict[int, dict[str, Any]],
) -> None:
    """开拓者换向后若踩上火箭、站位或别人要建的格子，再挑一格。"""
    brain = _brain()
    pioneer = turn.pioneer()
    if pioneer is None:
        return
    rec = _table(memory).get(pioneer.unit_id)
    if not isinstance(rec, MoveBlock) or not rec.rerouted:
        return
    rec.rerouted = False
    command = commands.get(pioneer.unit_id)
    if not command or command.get("action") != "move":
        return
    step = brain._command_cell(command)
    if step is None:
        return
    goal = rec.goal or step
    forbidden = _reroute_forbidden(turn, pioneer, goal, rec, set(), set(), commands)
    if step not in forbidden:
        return
    claimed: set[Pos] = set()
    for unit_id, other in commands.items():
        if unit_id == pioneer.unit_id or other.get("action") != "move":
            continue
        pos = brain._command_cell(other)
        if pos is not None:
            claimed.add(pos)
    alt = _pick_other_side(turn, pioneer, goal, claimed, forbidden, step)
    if alt is None or alt in forbidden:
        commands.pop(pioneer.unit_id, None)
        return
    commands[pioneer.unit_id] = brain.move_command(alt)
    rec.avoid = frozenset(set(rec.avoid) | {step})
    rec.pending_goal = goal
