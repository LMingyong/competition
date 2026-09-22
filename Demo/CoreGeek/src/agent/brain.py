from dataclasses import dataclass
from typing import Any

from .blocked import (
    account_blocked_turns as _account_blocked_turns,
    block_record as _block_record,
    keep_rerouted_pioneer_clear as _keep_rerouted_pioneer_clear,
    note_direction_blocked as _note_direction_blocked,
    remember_issued_moves as _remember_issued_moves,
    reroute_if_blocked_two_turns,
)
from .combat import attack_positions, bomb_center
from .debuglog import set_extra, write_round_log
from .grid import next_step
from .jobs import (
    KIND_ACCEPT,
    KIND_HOLD,
    KIND_MAN_TOWER,
    KIND_MINE,
    KIND_PHASE,
    KIND_RECALL,
    KIND_SELL,
    KIND_SHOP,
    KIND_TOWER,
    KIND_TREASURE,
    KIND_WALL,
    SELL_THRESHOLD,
    Job,
    assign_roles,
    claimed_targets,
    clear_job,
    drop_claimed_orders,
    get_job,
    has_claimed_order,
    interrupt_task,
    is_builder,
    set_job,
)
from .monitor import scan_idle
from .protocol import (
    DAY_ROUNDS,
    Pos,
    TOWER_TYPES,
    Unit,
    WALL,
    WALL_MATERIAL,
    WEAPON_BUILD_COST,
    accept_task_command,
    attack_commands,
    build_command,
    buy_command,
    collect_command,
    distance,
    drop_command,
    move_command,
    sell_command,
    station_footprint,
    submit_answer_command,
    summon_treasure_command,
    use_command,
)
from .tasks import (
    can_prompt,
    consume_names,
    mark_prompt,
    missing_treasure_items,
    MEMORY,
    next_task_command,
    observe,
    task_prompt,
    treasure_prompt,
    treasure_ready,
)
from .tickets import (
    BUILD_TOWER,
    BUILD_WALL,
    UPGRADE_TOWER,
    UPGRADE_WALL,
    claim_next_ticket,
    plan_big_task,
    release_ticket,
)
from .world import HERO_MAX_HP, World, backpack_item, count_item

TOWER_LOADOUT = ("rocket", "rocket", "rocket")
STONE_KEEP = 4
RECALL_ROUNDS = 7
RECALL_FROM = DAY_ROUNDS - RECALL_ROUNDS + 1
WALL_FROM = 30
EDGE_MINE_MAX = 4
MINE_NEAR = 8
_NEIGHBOUR_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)
UPGRADE_MAP = {
    "WeaponUpgradeVoucher1": (TOWER_TYPES, 1),
    "WeaponUpgradeVoucher2": (TOWER_TYPES, 2),
    "WallUpgradeVoucher1": ((WALL,), 1),
    "WallUpgradeVoucher2": ((WALL,), 2),
    "StationUpgradeVoucher1": (("station",), 1),
    "StationUpgradeVoucher2": (("station",), 2),
}
# 本回合已经决定要 build 的格子 -> 建造者。其他人不能 move 上去。
_PLANNED_BUILDS: dict[Pos, int] = {}
# 绕到炮位的路比直线远过这么多，就当成走不到，改去另一门。
_DETOUR_SLACK = 4


def decide(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _PLANNED_BUILDS.clear()
    turn = World.load(payload)
    memory = observe(turn)
    assign_roles(turn, memory)
    _account_blocked_turns(turn, memory)
    # 入夜前 7 回合，以及入夜后的每一个黑夜回合：打断白天建炮/建墙/采矿/升级。
    # interrupt_task 不看工单锁。炮手随后只回背后站位，其余人只进边缘安全区。
    if _in_recall(turn) or not turn.is_day:
        _interrupt_night_shift(turn, memory, "dusk" if turn.is_day else "night")
    commands: dict[int, dict[str, Any]] = {}
    prompt = ""
    execute_cmd = ""
    if turn.is_day:
        execute_cmd, prompt = _day(turn, memory, commands)
    else:
        execute_cmd, prompt = _night(turn, memory, commands)
    _fill_idle(turn, memory, commands)
    _keep_pioneer_on_task(turn, memory, commands)
    _keep_rerouted_pioneer_clear(turn, memory, commands)
    _untangle_feet(turn, memory, commands)
    _remember_issued_moves(turn, memory, commands)
    scan_idle(turn, commands)
    set_extra(prompt, execute_cmd)
    write_round_log(turn, commands, prompt, execute_cmd)
    return {str(key): value for key, value in commands.items()}


@dataclass
class _DayGaps:
    """计划层产物：本回合还缺什么。不发令。"""

    sites: tuple[Pos, ...]
    towers_missing: list[Pos]
    walls_missing: list[Pos]
    gold_left: int
    builds_left: int


def _plan_day_gaps(turn: World) -> _DayGaps:
    """计划层：只回答缺口。不给单位发令，不写 commands。"""
    sites = _tower_sites(turn)
    order = _wall_order(turn)
    standing_towers = {unit.pos for unit in turn.weapons()}
    standing_walls = {unit.pos for unit in turn.walls()}
    occupied = turn.occupied_for_build()
    towers_missing = [pos for pos in sites if pos not in standing_towers]
    walls_missing = [pos for pos in order if pos not in standing_walls]
    free_towers = [pos for pos in towers_missing if pos not in occupied]
    free_walls = [pos for pos in walls_missing if pos not in occupied]
    return _DayGaps(
        sites=sites,
        towers_missing=free_towers,
        walls_missing=free_walls,
        gold_left=turn.gold,
        builds_left=max(0, 3 - len(turn.weapons())),
    )


def _day(
    turn: World,
    memory,
    commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    memory.ticket_hold.clear()
    # 计划 / 领取 / 执行必须分开。计划只算缺口；工人循环里先领后做。
    gaps = _plan_day_gaps(turn)
    sites = gaps.sites
    free_towers = gaps.towers_missing
    free_walls = gaps.walls_missing
    claimed: set[Pos] = set()
    gold_left = gaps.gold_left
    builds_left = gaps.builds_left
    execute_cmd = ""
    prompt = ""
    _pin_recall_gunner(turn, memory)
    handled: set[int] = set()
    # 回防窗口先走炮手,占住站位;另外两人再去边缘,避免跟着抢炮位。
    if _in_recall(turn):
        gunner = _select_gunner(turn, list(turn.controllable()), memory)
        if gunner is not None and gunner.kind == "worker":
            gold_left, builds_left = _worker_day(
                turn, gunner, sites, free_towers, free_walls, claimed,
                commands, gold_left, builds_left, memory,
            )
            handled.add(gunner.unit_id)

    # 先占住本回合就要建的火箭格，开拓者排在工人前面也不会踩进去。
    _reserve_adjacent_rocket_builds(turn, claimed)
    pioneer = turn.pioneer()
    if pioneer is not None and pioneer.unit_id not in handled:
        execute_cmd, prompt = _pioneer_day(
            turn, pioneer, memory, claimed, commands,
        )

    for role in turn.workers():
        if role.unit_id in commands or role.unit_id in handled:
            continue
        gold_left, builds_left = _worker_day(
            turn, role, sites, free_towers, free_walls, claimed,
            commands, gold_left, builds_left, memory,
        )
    _ensure_opening_worker_actions(turn, memory, commands)
    _enforce_locked_acts(turn, memory, commands)
    return execute_cmd, prompt


def _unbuilt_rocket_sites(turn: World) -> list[Pos]:
    have = {unit.pos for unit in turn.weapons()}
    return [site for site in _tower_sites(turn) if site not in have]


def _opening_rockets_pending(turn: World) -> bool:
    """开局三门火箭没齐,而且场上武器还不满三座。入夜窗口不再抢这条规则。"""
    if not turn.is_day or _in_recall(turn):
        return False
    if len(turn.weapons()) >= 3:
        return False
    return bool(_unbuilt_rocket_sites(turn))


def _command_cell(command: dict[str, Any]) -> Pos | None:
    raw = (command.get("targetPos") or [None])[0]
    if not isinstance(raw, dict) or "x" not in raw:
        return None
    return Pos(int(raw["x"]), int(raw["y"]))


def _opening_command_ok(turn: World, command: dict[str, Any]) -> bool:
    action = command.get("action")
    if action == "build":
        return True
    if action != "move":
        return False
    pos = _command_cell(command)
    if pos is None:
        return False
    stand = _gun_stand(turn)
    if stand is not None and pos == stand:
        return False
    if pos in _unbuilt_rocket_sites(turn):
        return False
    if pos in _PLANNED_BUILDS:
        return False
    return True


def _move_avoid(turn: World, target: Pos) -> set[Pos]:
    """白天路径不踩火箭格、站位，也不踩本回合要 build 的格子。"""
    avoid: set[Pos] = set(_PLANNED_BUILDS)
    if _night_shift(turn):
        if target != _gun_stand(turn):
            avoid.update(_battery_block(turn))
        return avoid
    if _day_avoids_stand(turn):
        stand = _gun_stand(turn)
        if stand is not None and stand != target:
            avoid.add(stand)
        avoid.update(_tower_sites(turn))
    return avoid


def _path_cost(
    turn: World, role: Unit, goal: Pos, avoid: set[Pos], start: Pos | None = None,
) -> int | None:
    """与 next_step 同一套阻挡规则的步数。走不到返回 None。"""
    origin = role.pos if start is None else start
    if origin == goal:
        return 0
    from collections import deque

    blocked = set(turn.blocked(role))
    blocked.update(avoid)
    if start is not None:
        blocked.discard(start)
    seen = {origin}
    queue: deque[tuple[Pos, int]] = deque([(origin, 0)])
    while queue:
        current, cost = queue.popleft()
        for dx, dy in _NEIGHBOUR_STEPS:
            step = Pos(current.x + dx, current.y + dy)
            if step in seen or step in blocked or not turn.land(step):
                continue
            if step == goal:
                return cost + 1
            seen.add(step)
            queue.append((step, cost + 1))
    return None


def _straight_step(
    turn: World,
    role: Unit,
    landing: Pos,
    avoid: set[Pos],
    claimed: set[Pos],
) -> tuple[int, Pos] | None:
    """最短路上更直的第一步。同样步数时不先往斜向绕。"""
    cost = _path_cost(turn, role, landing, avoid)
    if cost is None:
        return None
    if cost == 0:
        return (0, role.pos)
    blocked = set(turn.blocked(role))
    blocked.update(avoid)
    best: tuple[tuple[int, int, int, int, int], Pos] | None = None
    for dx, dy in _NEIGHBOUR_STEPS:
        step = Pos(role.pos.x + dx, role.pos.y + dy)
        if step in blocked or step in claimed or not turn.land(step):
            continue
        remain = _path_cost(turn, role, landing, avoid, step)
        if remain is None or remain + 1 != cost:
            continue
        key = (
            cost,
            abs(step.x - landing.x) + abs(step.y - landing.y),
            distance(step, landing),
            abs(dx) + abs(dy),
            step.x + step.y,
        )
        if best is None or key < best[0]:
            best = (key, step)
    if best is None:
        return None
    return (cost, best[1])


def _rocket_approaches(
    turn: World,
    role: Unit,
    site: Pos,
    claimed: set[Pos],
    limit: int | None = None,
) -> list[tuple[int, Pos, Pos]]:
    """能走到的邻格：(路径步数, 下一步, 落脚点)。绕远的不算走得到。"""
    avoid = _move_avoid(turn, site)
    pads = set(_tower_sites(turn))
    found: list[tuple[int, Pos, Pos]] = []
    if limit is None:
        limit = distance(role.pos, site) + _DETOUR_SLACK
    for landing in _stand_cells(turn, role, site, claimed):
        if landing in pads or landing in _PLANNED_BUILDS or landing in avoid:
            continue
        if landing == role.pos:
            found.append((0, role.pos, landing))
            continue
        picked = _straight_step(turn, role, landing, avoid, claimed)
        if picked is None or picked[0] > limit or picked[1] in avoid or picked[1] == role.pos:
            continue
        cost, step = picked
        found.append((cost, step, landing))
    if not found and limit < 10**6:
        # 站位被排除后，绕远的空邻格也算走得到，不能因此当成没有路。
        return _rocket_approaches(turn, role, site, claimed, limit=10**6)
    found.sort(
        key=lambda item: (
            item[0],
            distance(role.pos, item[2]),
            abs(item[2].x - role.pos.x) + abs(item[2].y - role.pos.y),
            item[2].x,
            item[2].y,
        )
    )
    return found


def _note_cannot_reach(turn: World, role: Unit, site: Pos) -> None:
    turn.note(
        f"角色 {role.unit_id} 无法走向 ({site.x},{site.y})："
        "周围落脚点被占、越界、或被建筑/中立单位/机器人挡住"
    )


def _reserve_adjacent_rocket_builds(turn: World, claimed: set[Pos]) -> None:
    """已经贴着缺的火箭、这一回合就会 build 的格子，先留给建造者。"""
    if not turn.is_day or _in_recall(turn):
        return
    if turn.gold < WEAPON_BUILD_COST:
        return
    sites = _tower_sites(turn)
    missing = set(_unbuilt_rocket_sites(turn))
    if not missing:
        return
    slots = min(turn.gold // WEAPON_BUILD_COST, max(0, 3 - len(turn.weapons())))
    committed = len(turn.weapons())
    for role in turn.workers():
        if slots <= 0:
            break
        if _must_leave_stand(turn, role) or role.pos in missing:
            continue
        chosen: tuple[int, Pos] | None = None
        for index, site in enumerate(sites):
            if site not in missing or site in _PLANNED_BUILDS:
                continue
            if committed < 2 and index >= 2:
                continue
            if role.pos != site and distance(role.pos, site) <= 1:
                chosen = (index, site)
                break
        if chosen is None:
            continue
        _index, site = chosen
        _PLANNED_BUILDS[site] = role.unit_id
        claimed.add(site)
        missing.discard(site)
        committed += 1
        slots -= 1


def _assign_rocket_move(
    turn: World,
    role: Unit,
    site: Pos,
    step: Pos,
    index: int,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> None:
    claimed.add(step)
    commands[role.unit_id] = move_command(step)
    _block_record(role.unit_id).pending_goal = site
    _keep_job(
        memory, role, KIND_TOWER, target=site,
        name=TOWER_LOADOUT[index] if index < len(TOWER_LOADOUT) else "rocket",
        round_no=turn.round_no,
    )


def _best_rocket_move(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    missing: list[Pos],
    claimed: set[Pos],
    memory,
    *,
    committed: int,
) -> tuple[Pos, Pos, int] | None:
    """挑一门走得到、没被别人占的炮。最近那门走不到时改去另一门。"""
    taken = claimed_targets(memory, role.unit_id)
    job = get_job(memory, role.unit_id)
    own = job.target if job is not None and job.kind == KIND_TOWER else None
    ranked: list[tuple[int, int, int, int, Pos, Pos]] = []
    blocked_near: tuple[int, Pos] | None = None
    for index, site in enumerate(sites):
        if site not in missing or site in claimed:
            owner = _PLANNED_BUILDS.get(site)
            if not (owner == role.unit_id and site in missing):
                continue
        owner = _PLANNED_BUILDS.get(site)
        if owner is not None and owner != role.unit_id:
            continue
        # 还缺前两门时，别人锁住的炮位让开。最后一门谁近谁去，
        # 不能因为远处的人先记下目标，近处的人就改去挖矿。
        if site in taken and site != own and committed < 2:
            continue
        if _outer_build_failed(turn, role, missing) and site == _outer_site(turn):
            continue
        approaches = _rocket_approaches(turn, role, site, claimed)
        adjacent = (
            role.pos != site
            and distance(role.pos, site) <= 1
            and not _must_leave_stand(turn, role)
        )
        if not approaches and not adjacent:
            cheby = distance(role.pos, site)
            if blocked_near is None or (cheby, site.x, site.y) < (
                blocked_near[0], blocked_near[1].x, blocked_near[1].y,
            ):
                blocked_near = (cheby, site)
            continue
        step = role.pos if adjacent and not approaches else approaches[0][1]
        if not adjacent and (not approaches or step in claimed or step == role.pos):
            continue
        cost = 0 if adjacent else approaches[0][0]
        sticky = 0 if site == own and (approaches or adjacent) else 1
        wait_rank = 1 if committed < 2 and index >= 2 else 0
        ranked.append((sticky, wait_rank, cost, index, site, step))
    if not ranked:
        if blocked_near is not None:
            _note_cannot_reach(turn, role, blocked_near[1])
        return None
    ranked.sort()
    if blocked_near is not None and blocked_near[0] <= distance(role.pos, ranked[0][4]):
        _note_cannot_reach(turn, role, blocked_near[1])
    _sticky, _wait, _cost, index, site, step = ranked[0]
    return site, step, index


def _nudge_toward_rocket(
    turn: World,
    role: Unit,
    missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """落脚点被占时改去另一门炮或空邻格，不能空过，也不要朝走不到的炮越走越远。"""
    if not missing:
        return False
    sites = _tower_sites(turn)
    committed = _tower_committed(turn, commands)
    picked = _best_rocket_move(
        turn, role, sites, missing, claimed, memory, committed=committed,
    )
    if picked is not None and picked[1] != role.pos:
        site, step, index = picked
        _assign_rocket_move(
            turn, role, site, step, index, claimed, commands, memory,
        )
        return True
    open_sites = [
        site for site in missing
        if _PLANNED_BUILDS.get(site) != role.unit_id
        and _rocket_approaches(turn, role, site, claimed)
    ]
    anchors = open_sites or [
        site for site in missing if _PLANNED_BUILDS.get(site) != role.unit_id
    ] or list(missing)
    avoid = _move_avoid(turn, anchors[0])
    blocked = set(turn.blocked(role))
    blocked.update(avoid)
    choices: list[Pos] = []
    for dx, dy in _NEIGHBOUR_STEPS:
        pos = Pos(role.pos.x + dx, role.pos.y + dy)
        if pos in claimed or pos in blocked or not turn.land(pos):
            continue
        choices.append(pos)
    avoided = _block_record(role.unit_id).avoid
    if avoided:
        kept = [pos for pos in choices if pos not in avoided]
        if kept:
            choices = kept
    if not choices:
        return False
    current = min(distance(role.pos, site) for site in anchors)
    closer = [
        pos for pos in choices
        if min(distance(pos, site) for site in anchors) < current
    ]
    pool = closer or choices
    step = min(
        pool,
        key=lambda pos: (
            min(distance(pos, site) for site in anchors),
            pos.x,
            pos.y,
        ),
    )
    anchor = min(anchors, key=lambda site: (distance(step, site), site.x, site.y))
    index = next((i for i, site in enumerate(sites) if site == anchor), 0)
    _assign_rocket_move(
        turn, role, anchor, step, index, claimed, commands, memory,
    )
    return True


def _ensure_opening_worker_actions(
    turn: World, memory, commands: dict[int, dict[str, Any]],
) -> None:
    """三门火箭没齐之前,两名工人每回合都要 move 或 build，而且不互相堵死。"""
    if not _opening_rockets_pending(turn):
        return
    claimed: set[Pos] = set(_PLANNED_BUILDS)
    pending: list[Unit] = []
    missing = _unbuilt_rocket_sites(turn)
    for role in turn.workers():
        if _outer_build_failed(turn, role, missing):
            command = commands.get(role.unit_id)
            if command is not None and command.get("action") in {"move", "collect", "build"}:
                pos = _command_cell(command)
                if pos is not None and pos not in _PLANNED_BUILDS:
                    claimed.add(pos)
                continue
        command = commands.get(role.unit_id)
        job = get_job(memory, role.unit_id)
        # 走向矿的 move 不算在建第三门。最后一门还缺时要改去炮的空邻格。
        toward_rocket = (
            command is not None
            and _opening_command_ok(turn, command)
            and (
                command.get("action") == "build"
                or (job is not None and job.kind == KIND_TOWER)
            )
        )
        if toward_rocket:
            pos = _command_cell(command)
            if pos is not None:
                claimed.add(pos)
            continue
        commands.pop(role.unit_id, None)
        if job is not None and job.kind != KIND_TOWER:
            clear_job(memory, role.unit_id)
        pending.append(role)
    if not pending:
        return
    sites = _tower_sites(turn)
    spent = sum(
        1 for command in commands.values()
        if command.get("action") == "build" and command.get("name") in TOWER_TYPES
    )
    gold_left = turn.gold - spent * WEAPON_BUILD_COST
    builds_left = max(0, 3 - len(turn.weapons()) - spent)
    for role in pending:
        if gold_left >= WEAPON_BUILD_COST and builds_left > 0 and missing:
            gold_left, builds_left = _raise_pocket_towers(
                turn, role, sites, missing, claimed, commands,
                gold_left, builds_left, memory,
            )
        command = commands.get(role.unit_id)
        if command is not None and _opening_command_ok(turn, command):
            continue
        commands.pop(role.unit_id, None)
        if _nudge_toward_rocket(turn, role, missing, claimed, commands, memory):
            continue
        anchor = missing[0]
        step = _sidestep(turn, role, anchor, claimed, _move_avoid(turn, anchor))
        if step is None:
            continue
        index = next((i for i, site in enumerate(sites) if site == anchor), 0)
        _assign_rocket_move(
            turn, role, anchor, step, index, claimed, commands, memory,
        )


def _worker_day(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    """工人一回合：打断 → 领取 → 执行 → 可见补动作。执行层不再重算目标。"""
    towers_missing = _with_foot_site(turn, role, sites, towers_missing)
    if _interrupt_worker_turn(turn, role, towers_missing, walls_missing, claimed, commands, memory):
        return gold_left, builds_left
    if _opening_rockets_pending(turn):
        job = get_job(memory, role.unit_id)
        if job is not None and job.kind != KIND_TOWER:
            clear_job(memory, role.unit_id)
    _claim_worker_job(
        turn, role, sites, towers_missing, walls_missing, claimed,
        commands, gold_left, builds_left, memory,
    )
    gold_left, builds_left = _execute_worker_job(
        turn, role, sites, towers_missing, walls_missing, claimed,
        commands, gold_left, builds_left, memory,
    )
    if role.unit_id not in commands:
        gold_left, builds_left = _fallback_worker_action(
            turn, role, sites, towers_missing, walls_missing, claimed,
            commands, gold_left, builds_left, memory,
        )
    return gold_left, builds_left


def _interrupt_worker_turn(
    turn: World,
    role: Unit,
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """治疗/回防/入夜边缘。这些会打断白天工单，不属于执行层重规划。"""
    if _try_heal(role, commands):
        return True
    if not _in_recall(turn) and _try_upgrade_or_fix(turn, role, commands):
        return True
    if _should_home(turn, role, memory):
        _recall_to_tower(turn, role, claimed, commands, memory)
        return True
    if _should_edge_mine(turn, role, memory):
        _run_day_edge(turn, role, claimed, commands, memory)
        return True
    if _outer_build_failed(turn, role, towers_missing):
        job = get_job(memory, role.unit_id)
        if job is not None and job.kind == KIND_TOWER:
            clear_job(memory, role.unit_id)
        if _recover_failed_outer(
            turn, role, walls_missing, claimed, commands, memory,
        ):
            return True
    return False


def _claim_worker_job(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> None:
    """领取层：空闲才领一张 Job。已有未完成 Job 什么都不做。不发令。"""
    job = get_job(memory, role.unit_id)
    if _job_locked(
        turn, role, job, memory, walls_missing, towers_missing, gold_left,
    ):
        return
    if job is not None:
        clear_job(memory, role.unit_id)
    if memory.ticket_owner.get(role.unit_id) is None:
        claim_next_ticket(turn, memory, role.unit_id)
    if _claim_tower_job(
        turn, role, sites, towers_missing, claimed, commands, memory,
    ):
        return
    ticket = memory.ticket_owner.get(role.unit_id)
    if ticket == BUILD_TOWER:
        _claim_tower_job(
            turn, role, sites, towers_missing, claimed, commands, memory,
        )
        return
    if ticket == BUILD_WALL:
        return
    if ticket in {UPGRADE_TOWER, UPGRADE_WALL}:
        return
    _claim_economy_job(turn, role, claimed, gold_left, memory)


def _claim_tower_job(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """领取一门还没被别人占的火箭。满两门 committed 才领第三门。"""
    if not towers_missing:
        return False
    if not _opening_rockets_pending(turn) and memory.ticket_owner.get(role.unit_id) != BUILD_TOWER:
        return False
    committed = _tower_committed(turn, commands)
    picked = _best_rocket_move(
        turn, role, sites, towers_missing, claimed, memory, committed=committed,
    )
    if picked is None:
        return False
    site, _step, index = picked
    taken = claimed_targets(memory, role.unit_id)
    if site in taken:
        return False
    _keep_job(
        memory, role, KIND_TOWER, target=site,
        name=TOWER_LOADOUT[index] if index < len(TOWER_LOADOUT) else "rocket",
        round_no=turn.round_no,
    )
    return True


def _claim_wall_job(
    turn: World,
    role: Unit,
    walls_missing: list[Pos],
    claimed: set[Pos],
    memory,
) -> None:
    if _opening_rockets_pending(turn):
        return
    have = count_item(role, WALL_MATERIAL)
    cap = role.capacity if role.capacity else 100
    free = max(0, cap - len(role.backpack))
    if not _wall_phase(turn):
        if have > 0 and free <= 0:
            memory.ticket_hold.add(role.unit_id)
            return
        _claim_stone_mine(turn, role, claimed, memory)
        return
    if have > 0 and walls_missing:
        taken = claimed_targets(memory, role.unit_id)
        site = next((pos for pos in walls_missing if pos not in taken), walls_missing[0])
        _keep_job(
            memory, role, KIND_WALL, target=site, name=WALL, round_no=turn.round_no,
        )
        return
    _claim_stone_mine(turn, role, claimed, memory)


def _claim_stone_mine(turn: World, role: Unit, claimed: set[Pos], memory) -> None:
    taken = claimed_targets(memory, role.unit_id)
    target = _nearest_mine(turn, role, WALL_MATERIAL, claimed, taken)
    if target is None:
        return
    _keep_job(
        memory, role, KIND_WALL, target=target, name=WALL_MATERIAL,
        round_no=turn.round_no,
    )


def _claim_economy_job(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    gold_left: int,
    memory,
) -> None:
    if _opening_rockets_pending(turn):
        return
    want = _wanted_item(turn, role, gold_left, memory)
    shop = turn.weapon_shop_pos()
    if want and shop is not None and not _should_keep_digging(role):
        _keep_job(
            memory, role, KIND_SHOP, target=shop, name=want, round_no=turn.round_no,
        )
        return
    vendor = turn.vendor()
    if vendor is not None and (role.backpack_full or num_ores(role) >= SELL_THRESHOLD):
        _keep_job(memory, role, KIND_SELL, target=vendor, round_no=turn.round_no)
        return
    dest = _pick_mine(
        turn, role, claimed, extra=claimed_targets(memory, role.unit_id), memory=memory,
    )
    if dest is None:
        return
    pos, kind = dest
    _keep_job(
        memory, role, KIND_MINE, target=pos, name=kind, round_no=turn.round_no,
    )


def _execute_worker_job(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    """执行层：只做当前 Job 的 walk/collect/build。禁止在这里另选矿或另选门。"""
    job = get_job(memory, role.unit_id)
    ticket = memory.ticket_owner.get(role.unit_id)
    if job is not None and job.kind == KIND_TOWER:
        return _execute_tower(
            turn, role, job, towers_missing, claimed, commands,
            gold_left, builds_left, memory,
        )
    if job is not None and job.kind == KIND_MINE:
        _execute_mine(turn, role, job, claimed, commands)
        return gold_left, builds_left
    if ticket == BUILD_WALL:
        result = _run_wall_ticket(
            turn, role, walls_missing, claimed, commands, gold_left, builds_left, memory,
        )
        if result is None:
            return gold_left, builds_left
        return result
    if ticket in {UPGRADE_TOWER, UPGRADE_WALL}:
        result = _run_upgrade_ticket(
            turn, role, ticket, walls_missing, towers_missing, claimed,
            commands, gold_left, builds_left, memory,
        )
        if result is None:
            return gold_left, builds_left
        return result
    if ticket == BUILD_TOWER:
        result = _run_tower_ticket(
            turn, role, sites, towers_missing, walls_missing, claimed,
            commands, gold_left, builds_left, memory,
        )
        if result is None:
            return gold_left, builds_left
        return result
    if job is not None:
        return _run_worker_job(
            turn, role, job, sites, towers_missing, walls_missing,
            claimed, commands, gold_left, builds_left, memory,
        )
    return gold_left, builds_left


def _fallback_worker_action(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    """没发出指令时的可见补动作。禁止静默 return 空。"""
    job = get_job(memory, role.unit_id)
    target = job.target if job is not None else None
    turn.note(
        f"执行层无指令，补动作: 角色 {role.unit_id} "
        f"job={None if job is None else job.kind} "
        f"target={None if target is None else (target.x, target.y)}"
    )
    if _opening_rockets_pending(turn):
        missing = _unbuilt_rocket_sites(turn)
        if gold_left >= WEAPON_BUILD_COST and builds_left > 0 and missing:
            gold_left, builds_left = _raise_pocket_towers(
                turn, role, sites, missing, claimed, commands,
                gold_left, builds_left, memory,
            )
            if role.unit_id in commands:
                return gold_left, builds_left
        if _nudge_toward_rocket(turn, role, missing, claimed, commands, memory):
            return gold_left, builds_left
        if missing:
            step = _sidestep(turn, role, missing[0], claimed, _move_avoid(turn, missing[0]))
            if step is not None:
                index = next((i for i, site in enumerate(sites) if site == missing[0]), 0)
                _assign_rocket_move(
                    turn, role, missing[0], step, index, claimed, commands, memory,
                )
                return gold_left, builds_left
        _note_direction_blocked(turn, role, missing[0], claimed)
        return gold_left, builds_left
    rec = _block_record(role.unit_id)
    if job is not None and job.target is not None:
        if role.pos == job.target and _step_off_current(turn, role, claimed, commands):
            _stamp_job(memory, role, job)
            return gold_left, builds_left
        if rec.streak >= 2:
            step = _sidestep(
                turn, role, job.target, claimed, _move_avoid(turn, job.target),
            )
            if step is not None:
                claimed.add(step)
                commands[role.unit_id] = move_command(step)
                _stamp_job(memory, role, job)
                return gold_left, builds_left
            _note_direction_blocked(turn, role, job.target, claimed)
    return _economy_after_tickets(
        turn, role, sites, towers_missing, walls_missing, claimed,
        commands, gold_left, builds_left, memory,
    )


def _in_recall(turn: World) -> bool:
    return bool(turn.is_day) and turn.round_in_day >= RECALL_FROM


def _night_shift(turn: World) -> bool:
    """入夜前 7 个白天回合，以及整个黑夜。"""
    return (not turn.is_day) or _in_recall(turn)


def _wall_phase(turn: World) -> bool:
    """开局前 29 回合只建塔/经营; 第 30 回合起才进入砌墙阶段。"""
    return turn.round_no >= WALL_FROM


def _rockets_still_open(turn: World) -> bool:
    """口袋里的火箭还没凑满 3 门。这之前两名工人都不能空过。"""
    return _opening_rockets_pending(turn)


def _rockets_committed(turn: World, builds_left: int) -> int:
    """已经落地的火箭,加上本回合前面的人已经下达的建造。"""
    room = max(0, 3 - len(turn.weapons()))
    built_now = max(0, room - builds_left)
    return max(_tower_committed(turn), len(turn.weapons()) + built_now)


def _tower_committed(turn: World, commands: dict[int, dict[str, Any]] | None = None) -> int:
    """已落地 + 本回合已占用的 build（含预定格）才是 committed 炮数。"""
    standing = {unit.pos for unit in turn.weapons()}
    extra: set[Pos] = set()
    pads = set(_tower_sites(turn))
    for site in _PLANNED_BUILDS:
        if site in pads and site not in standing:
            extra.add(site)
    if commands:
        for command in commands.values():
            if command.get("action") != "build" or command.get("name") not in TOWER_TYPES:
                continue
            pos = _command_cell(command)
            if pos is not None and pos in pads and pos not in standing:
                extra.add(pos)
    return len(standing) + len(extra)


def _with_foot_site(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
) -> list[Pos]:
    """人正站在未建成的火箭格上时，这一格仍算待建。

    占用检查会把脚下这格从可建列表里拿掉，工单于是以为没有目标，不再移动。
    """
    standing = {unit.pos for unit in turn.weapons()}
    if role.pos in sites and role.pos not in standing and role.pos not in towers_missing:
        return [*towers_missing, role.pos]
    return towers_missing


def _keep_if_acted(role: Unit, commands: dict[int, dict[str, Any]], result, turn: World) -> bool:
    """有指令才结束这个人的回合。三门没齐时空返回要继续往下派动作。"""
    if result is None:
        return False
    if role.unit_id in commands:
        return True
    return not _rockets_still_open(turn)


def _is_weapon_upgrade(name: str | None) -> bool:
    return bool(name) and name.lower().startswith("weaponupgrade")


def _weapon_voucher_wishlist(turn: World) -> list[str]:
    names: list[str] = []
    if any(unit.kind in TOWER_TYPES and unit.level == 1 for unit in turn.ours):
        names.append("WeaponUpgradeVoucher1")
    if any(unit.kind in TOWER_TYPES and unit.level == 2 for unit in turn.ours):
        names.append("WeaponUpgradeVoucher2")
    return names


def _weapons_need_upgrade(turn: World) -> bool:
    return any(unit.kind in TOWER_TYPES and unit.level < 3 for unit in turn.ours)


def _wall_upgrade_wishlist(turn: World) -> list[str]:
    """朝向敌人的墙先升到 3 级,从中心那几段开始;背向的墙排在后面。"""
    facing_levels = {1: False, 2: False}
    other_levels = {1: False, 2: False}
    for wall in turn.walls():
        if wall.level not in (1, 2):
            continue
        bucket = facing_levels if _on_incoming_side(wall.pos, turn) else other_levels
        bucket[wall.level] = True
    names: list[str] = []
    if facing_levels[1]:
        names.append("WallUpgradeVoucher1")
    elif facing_levels[2]:
        names.append("WallUpgradeVoucher2")
    elif other_levels[1]:
        names.append("WallUpgradeVoucher1")
    elif other_levels[2]:
        names.append("WallUpgradeVoucher2")
    return names


def _station_voucher_wishlist(turn: World) -> list[str]:
    station = turn.station()
    if station is None:
        return []
    if station.level == 1:
        return ["StationUpgradeVoucher1"]
    if station.level == 2:
        return ["StationUpgradeVoucher2"]
    return []


def _upgrade_buy_list(turn: World) -> list[str]:
    """购买顺序:武器券 > 围墙券 > 基地券。武器未满级时不买墙和基地。"""
    weapons = _weapon_voucher_wishlist(turn)
    if weapons:
        return weapons
    return _wall_upgrade_wishlist(turn) + _station_voucher_wishlist(turn)


def _best_upgrade_unit(turn: World, role: Unit, voucher: str) -> Unit | None:
    spec = UPGRADE_MAP.get(voucher)
    if spec is None:
        return None
    kinds, level = spec
    candidates = [
        unit for unit in turn.ours
        if unit.kind in kinds and unit.level == level
    ]
    if not candidates:
        return None
    if WALL in kinds:
        center = _map_center(turn)
        facing = [unit for unit in candidates if _on_incoming_side(unit.pos, turn)]
        pool = facing or candidates
        return min(
            pool,
            key=lambda unit: (distance(unit.pos, center), unit.pos.x, unit.pos.y),
        )
    return min(
        candidates,
        key=lambda unit: (distance(role.pos, unit.pos), unit.unit_id),
    )


def _next_upgrade(turn: World, role: Unit) -> tuple[str, Unit] | None:
    """背包里按武器 > 围墙 > 基地挑下一张能用的升级券和目标。"""
    for voucher in (
        "WeaponUpgradeVoucher1",
        "WeaponUpgradeVoucher2",
        "WallUpgradeVoucher1",
        "WallUpgradeVoucher2",
        "StationUpgradeVoucher1",
        "StationUpgradeVoucher2",
    ):
        name = backpack_item(role, voucher)
        if name is None:
            continue
        target = _best_upgrade_unit(turn, role, voucher)
        if target is None:
            continue
        return name, target
    return None


def _try_weapon_upgrade_shop(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    memory,
) -> int | None:
    """金币够买武器升级券时先去商店,不把钱花在墙上。成功下达指令则返回新金币。"""
    want = _wanted_item(turn, role, gold_left, memory)
    if not _is_weapon_upgrade(want):
        return None
    if not _try_shop(turn, role, claimed, commands, gold_left, memory):
        return None
    if role.unit_id in commands and commands[role.unit_id]["action"] == "buy":
        item = turn.shop_item(commands[role.unit_id]["name"])
        if item:
            gold_left -= item.price
    _keep_job(
        memory, role, KIND_SHOP, target=turn.weapon_shop_pos(),
        name=want or "", round_no=turn.round_no,
    )
    return gold_left


def _should_home(turn: World, role: Unit, memory) -> bool:
    """入夜前的回防窗口里,只有选定的那一名炮手回家。"""
    return _in_recall(turn) and _is_gunner(turn, role, memory)


def _should_edge_mine(turn: World, role: Unit, memory) -> bool:
    """回防窗口里不操炮的人改去己方边缘安全区，不再往敌人一侧砌墙。"""
    if not _in_recall(turn):
        return False
    return not _is_gunner(turn, role, memory)


def _pioneer_on_evolution(turn: World, role: Unit, memory) -> bool:
    """停在己方任务点做自进化，或刚 accept 还没离开。拉开就会结束任务。"""
    if role.kind != "pioneer":
        return False
    if turn.phase_task:
        return True
    return _just_accepted(turn, role, memory)


def _interrupt_night_shift(turn: World, memory, reason: str) -> None:
    """入夜前 7 回合和黑夜：清掉白天那张单，并钉住这一名炮手。

    调用 interrupt_task(memory, role, reason)。工单锁不参与。
    开拓者正做自进化时不打断。炮手若已是操炮单，只摘大任务，人不换。
    安全区内的采矿/停留单保留，区外的建炮、建墙、采矿、购物一律清掉。
    """
    contenders = [
        role for role in turn.controllable()
        if not _pioneer_on_evolution(turn, role, memory)
    ]
    gunner = _select_gunner(turn, contenders, memory)
    if gunner is not None:
        job = get_job(memory, gunner.unit_id)
        if job is not None and job.kind == KIND_MAN_TOWER:
            drop_claimed_orders(memory, gunner.unit_id)
        else:
            interrupt_task(memory, gunner, reason)
            _bind_gunner(turn, memory, gunner)
    for role in turn.controllable():
        if gunner is not None and role.unit_id == gunner.unit_id:
            continue
        if _pioneer_on_evolution(turn, role, memory):
            continue
        if _sheet_should_break(turn, role, memory):
            interrupt_task(memory, role, reason)


def _sheet_should_break(turn: World, role: Unit, memory) -> bool:
    """白天的建炮/建墙/升级/区外采矿还挂在身上，就必须打断。"""
    if has_claimed_order(memory, role.unit_id):
        return True
    job = get_job(memory, role.unit_id)
    if job is None:
        return False
    if job.kind in {KIND_PHASE, KIND_ACCEPT, KIND_TREASURE}:
        return False
    if job.kind in {KIND_MINE, KIND_HOLD}:
        return job.target is None or not in_night_safe_zone(turn, job.target)
    if job.kind in {
        KIND_WALL, KIND_TOWER, KIND_SHOP, KIND_SELL, KIND_RECALL, KIND_MAN_TOWER,
    }:
        return True
    return False


def _bind_gunner(turn: World, memory, gunner: Unit) -> None:
    """打断之后立刻把这一名炮手钉回操炮，这 7 回合不再换人。"""
    if _pattern_ready(turn):
        _keep_gunner(memory, gunner, turn, _gun_stand(turn), 0)
        return
    weapons = turn.weapons()
    if weapons:
        tower = min(
            weapons,
            key=lambda unit: (distance(gunner.pos, unit.pos), unit.unit_id),
        )
        _keep_gunner(memory, gunner, turn, tower.pos, tower.unit_id)
        return
    _keep_gunner(memory, gunner, turn, _recall_target(turn), 0)


def _pin_recall_gunner(turn: World, memory) -> None:
    """回防一开始就定下炮手,这 7 回合不再换人。"""
    if not _in_recall(turn):
        return
    gunner = _select_gunner(turn, list(turn.controllable()), memory)
    if gunner is None:
        return
    if _pattern_ready(turn):
        _keep_gunner(memory, gunner, turn, _gun_stand(turn), 0)
        return
    weapons = turn.weapons()
    if weapons:
        tower = min(
            weapons,
            key=lambda unit: (distance(gunner.pos, unit.pos), unit.unit_id),
        )
        _keep_gunner(memory, gunner, turn, tower.pos, tower.unit_id)
        return
    _keep_gunner(memory, gunner, turn, _recall_target(turn), 0)


def _segment_crosses_center(turn: World, start: Pos, goal: Pos) -> bool:
    """去小贩的直线是否穿过地图中央。中央那一格本身也算穿过。"""
    center = _map_center(turn)
    if max(abs(goal.x - center.x), abs(goal.y - center.y)) <= 1:
        return True
    steps = max(abs(goal.x - start.x), abs(goal.y - start.y))
    for index in range(1, steps):
        x = start.x + (goal.x - start.x) * index // steps
        y = start.y + (goal.y - start.y) * index // steps
        if max(abs(x - center.x), abs(y - center.y)) <= 1:
            return True
    return False


def _sell_in_safe_zone(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """背包满了只在不穿过地图中央时卖矿。小贩在中央就不要去。"""
    vendor = turn.vendor()
    if vendor is None or _segment_crosses_center(turn, role.pos, vendor):
        return False
    ore = _sellable_ore(role, turn, 0)
    if ore is None:
        return False
    _keep_job(
        memory, role, KIND_SELL, target=vendor, round_no=turn.round_no,
    )
    if turn.adjacent_to_zone(role, vendor):
        name, num = ore
        commands[role.unit_id] = sell_command(name, num)
        return True
    return _walk_adjacent(turn, role, vendor, claimed, commands)


def _hold_near_edge(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """背包满了就停在安全区矿旁，不往地图中央走，也不去炮位。"""
    taken = claimed_targets(memory, role.unit_id)
    job = get_job(memory, role.unit_id)
    mines = dict(turn.all_mines())
    if (
        job is not None
        and job.kind == KIND_MINE
        and job.target is not None
        and job.target in mines
        and in_night_safe_zone(turn, job.target)
    ):
        target = job.target
    else:
        picked = _pick_edge_mine(turn, role, claimed, taken)
        if picked is None:
            return False
        target, kind = picked
        _keep_job(
            memory, role, KIND_MINE, target=target, name=kind,
            round_no=turn.round_no,
        )
    if role.pos != target and distance(role.pos, target) <= 1:
        return True
    return _walk_adjacent(turn, role, target, claimed, commands)


def _run_day_edge(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    return _assign_night_safe(turn, role, claimed, commands, memory)


def _assign_night_safe(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """非炮手的采矿和停留只落在己方边缘安全区。没有矿就停在基地背后的空地。"""
    if role.backpack_full:
        if _sell_in_safe_zone(turn, role, claimed, commands, memory):
            return True
        if _hold_near_edge(turn, role, claimed, commands, memory):
            return True
        return _park_in_safe_zone(turn, role, claimed, commands, memory)
    if _run_edge_mine(turn, role, claimed, commands, memory):
        return True
    return _park_in_safe_zone(turn, role, claimed, commands, memory)


def _edge_distance(turn: World, pos: Pos) -> int:
    return min(pos.x, pos.y, turn.width - 1 - pos.x, turn.height - 1 - pos.y)


def _is_edge_mine(turn: World, pos: Pos) -> bool:
    return _edge_distance(turn, pos) <= EDGE_MINE_MAX


def safe_zone_bounds(turn: World) -> tuple[int, int, int, int] | None:
    """己方夜间安全区，含端点，返回 xmin, xmax, ymin, ymax。

    只用基地背后、靠近地图外缘的一侧，不用敌人那一侧的边缘。
    横向用 EDGE_MINE_MAX：离左/右地图边界不超过这圈，并且不越过基地外缘朝向中央。
    纵向覆盖整张地图；真正落点再挑靠近基地背后的矿或空地。

    挑战者样例基地 (10,24)，占地左缘 x=10：x 为 0..4，y 为 0..31。
    守方样例基地 (30,10)，占地右缘 x=31：x 为 36..40，y 为 0..31。
    炮手站位在这圈之外、仍在基地背后，允许炮手留在站位。
    """
    bounds = _station_bounds(turn)
    if bounds is None:
        return None
    xmin, xmax, _ymin, _ymax = bounds
    top = turn.height - 1
    if _center_facing_east(turn):
        outer = min(xmin, EDGE_MINE_MAX)
        return 0, outer, 0, top
    inner = max(xmax, turn.width - 1 - EDGE_MINE_MAX)
    return inner, turn.width - 1, 0, top


def in_night_safe_zone(turn: World, pos: Pos) -> bool:
    """格子落在己方边缘安全区。朝向地图中心、或敌人一侧的边缘不算。"""
    span = safe_zone_bounds(turn)
    if span is None or pos is None:
        return False
    left, right, bottom, top = span
    return left <= pos.x <= right and bottom <= pos.y <= top


def _base_back_anchor(turn: World) -> Pos | None:
    """基地朝向地图外缘的那一侧，安全区落点往这里靠。"""
    bounds = _station_bounds(turn)
    if bounds is None:
        return None
    xmin, xmax, ymin, ymax = bounds
    mid_y = (ymin + ymax) // 2
    if _center_facing_east(turn):
        return Pos(xmin, mid_y)
    return Pos(xmax, mid_y)


def _pick_edge_mine(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    extra: set[Pos],
) -> tuple[Pos, str] | None:
    blocked = set(claimed)
    blocked.update(extra)
    metals: list[tuple[Pos, str]] = []
    others: list[tuple[Pos, str]] = []
    for pos, kind in turn.all_mines():
        if pos in blocked or not in_night_safe_zone(turn, pos):
            continue
        if kind in ("copper", "iron"):
            metals.append((pos, kind))
        else:
            others.append((pos, kind))
    pool = metals or others
    if not pool:
        return None
    anchor = _base_back_anchor(turn) or _map_center(turn)
    return min(
        pool,
        key=lambda item: (
            distance(item[0], anchor),
            _edge_distance(turn, item[0]),
            -turn.vendor_price(item[1]),
            distance(role.pos, item[0]),
            item[0].x,
            item[0].y,
        ),
    )


def _run_edge_mine(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """回防窗口与黑夜：只挖己方边缘安全区里的矿，不穿过中央去小贩。"""
    if role.backpack_full:
        return False
    taken = claimed_targets(memory, role.unit_id)
    job = get_job(memory, role.unit_id)
    mines = dict(turn.all_mines())
    if (
        job is not None
        and job.kind == KIND_MINE
        and job.target is not None
        and job.target in mines
        and in_night_safe_zone(turn, job.target)
    ):
        target = job
    else:
        picked = _pick_edge_mine(turn, role, claimed, taken)
        if picked is None:
            return False
        pos, kind = picked
        target = _keep_job(
            memory, role, KIND_MINE, target=pos, name=kind, round_no=turn.round_no,
        )
    return _execute_mine(turn, role, target, claimed, commands)


def _stamp_job(memory, role: Unit, job: Job) -> None:
    """记下当前大任务、小任务、目标格和已采数量，下一回合接着做。"""
    owners = getattr(memory, "ticket_owner", None)
    owner = ""
    if isinstance(owners, dict):
        owner = str(owners.get(role.unit_id) or "")
    if job.kind == KIND_TOWER:
        job.big = BUILD_TOWER
        beside = (
            job.target is not None
            and role.pos != job.target
            and distance(role.pos, job.target) <= 1
        )
        job.small = "build" if beside else "walk"
        return
    if job.kind == KIND_WALL and job.name == WALL_MATERIAL:
        job.big = BUILD_WALL
        job.small = "collect"
        job.progress = count_item(role, WALL_MATERIAL)
        return
    if job.kind == KIND_WALL:
        job.big = BUILD_WALL
        job.small = "build"
        return
    if job.kind == KIND_MINE:
        job.big = owner or job.big
        job.small = "collect"
        if job.name:
            job.progress = count_item(role, job.name)
        return
    if owner:
        job.big = owner


def _keep_job(
    memory, role: Unit, kind: str, *, target: Pos | None = None,
    name: str = "", tower_id: int = 0, round_no: int = 0,
) -> Job:
    prev = get_job(memory, role.unit_id)
    if (
        prev is not None
        and prev.kind == kind
        and prev.target == target
        and prev.name == name
        and prev.tower_id == tower_id
    ):
        _stamp_job(memory, role, prev)
        return prev
    started = round_no
    if prev is not None and prev.kind == kind and prev.tower_id == tower_id and prev.name == name:
        started = prev.started or round_no
    job = set_job(
        memory,
        role.unit_id,
        Job(
            kind=kind,
            target=target,
            name=name,
            started=started,
            fail_streak=prev.fail_streak if prev and prev.kind == kind else 0,
            tower_id=tower_id,
            big=prev.big if prev is not None else "",
            small=prev.small if prev is not None else "",
            progress=prev.progress if prev is not None and prev.target == target else 0,
        ),
    )
    _stamp_job(memory, role, job)
    return job


def _collect_quota_open(turn: World, role: Unit, job: Job) -> bool:
    """这一小任务还要接着挖：没装满，建墙的石头也还没一次采够。"""
    if role.backpack_full:
        return False
    cap = role.capacity if role.capacity else 100
    if len(role.backpack) >= cap:
        return False
    if job.kind == KIND_MINE:
        return num_ores(role) < SELL_THRESHOLD
    if job.kind == KIND_WALL and job.name == WALL_MATERIAL:
        standing = {unit.pos for unit in turn.walls()}
        missing = sum(1 for pos in _wall_order(turn) if pos not in standing)
        if missing <= 0:
            return False
        return count_item(role, WALL_MATERIAL) < missing
    return False


def _enforce_locked_acts(turn: World, memory, commands: dict[int, dict[str, Any]]) -> None:
    """贴着锁定目标必须 collect/build；站在矿/未建炮格上先走开。"""
    if not turn.is_day or _in_recall(turn):
        return
    claimed = _issued_cells(commands) | set(_PLANNED_BUILDS)
    missing = set(_unbuilt_rocket_sites(turn))
    opening = _opening_rockets_pending(turn)
    for role in turn.workers():
        job = get_job(memory, role.unit_id)
        if job is None or job.target is None:
            continue
        command = commands.get(role.unit_id)
        if role.pos == job.target:
            if command is not None and command.get("action") == "move":
                _stamp_job(memory, role, job)
                continue
            commands.pop(role.unit_id, None)
            if _step_off_current(turn, role, claimed, commands):
                _stamp_job(memory, role, job)
            continue
        if distance(role.pos, job.target) > 1 or _must_leave_stand(turn, role):
            continue
        if job.kind == KIND_TOWER and job.target in missing:
            owner = _PLANNED_BUILDS.get(job.target)
            if owner is not None and owner != role.unit_id:
                continue
            if command is not None and command.get("action") == "build":
                continue
            spent = sum(
                1 for item in commands.values()
                if item.get("action") == "build" and item.get("name") in TOWER_TYPES
            )
            committed = _tower_committed(turn, commands)
            index = next(
                (i for i, site in enumerate(_tower_sites(turn)) if site == job.target),
                0,
            )
            if turn.gold - spent * WEAPON_BUILD_COST < WEAPON_BUILD_COST:
                continue
            if index >= 2 and committed < 2:
                continue
            commands[role.unit_id] = build_command(job.target, job.name or "rocket")
            claimed.add(job.target)
            _PLANNED_BUILDS[job.target] = role.unit_id
            _stamp_job(memory, role, job)
            continue
        if job.kind == KIND_WALL and job.name == WALL:
            if command is not None and command.get("action") == "build":
                continue
            if count_item(role, WALL_MATERIAL) <= 0:
                continue
            commands[role.unit_id] = build_command(job.target, WALL)
            claimed.add(job.target)
            _stamp_job(memory, role, job)
            continue
        if opening:
            continue
        mining = job.kind == KIND_MINE or (
            job.kind == KIND_WALL and job.name == WALL_MATERIAL
        )
        if not mining:
            continue
        mines = dict(turn.all_mines())
        if mines.get(job.target) != job.name:
            continue
        if command is not None and command.get("action") == "build":
            continue
        if not _collect_quota_open(turn, role, job):
            continue
        commands[role.unit_id] = collect_command(job.target)
        claimed.add(job.target)
        _stamp_job(memory, role, job)


def _recall_target(turn: World) -> Pos | None:
    weapons = turn.weapons()
    if weapons:
        return weapons[0].pos
    station = turn.station()
    return station.pos if station is not None else None


def _continue_locked_job(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int] | None:
    """工单还在就只执行它。没有发出指令也不另派一条路。

    锁定成功时返回执行后的金币与建造额度,供同一回合下一名工人记账。
    """
    job = get_job(memory, role.unit_id)
    if not _job_locked(
        turn, role, job, memory, walls_missing, towers_missing, gold_left,
    ):
        return None
    result = _run_worker_job(
        turn, role, job, sites, towers_missing, walls_missing,
        claimed, commands, gold_left, builds_left, memory,
    )
    # 火箭还没齐时,锁着的那条路走不通也不能把这个人空过。
    if role.unit_id not in commands and _opening_rockets_pending(turn):
        return None
    return result


def _day_avoids_stand(turn: World) -> bool:
    """入夜前 7 回合之前,白天谁都不把炮手站位当成落脚点。"""
    return bool(turn.is_day) and not _in_recall(turn)


def _must_leave_stand(turn: World, role: Unit) -> bool:
    if not _day_avoids_stand(turn):
        return False
    stand = _gun_stand(turn)
    return stand is not None and role.pos == stand


def _outer_site(turn: World) -> Pos | None:
    """最外侧火箭:相对基地占地切比雪夫距离为 2 的那一门。坐标不改。"""
    sites = _tower_sites(turn)
    station = turn.station()
    if station is None or not sites:
        return None
    footprint = station_footprint(station.pos)
    for site in reversed(sites):
        if _footprint_distance(site, footprint) >= 2:
            return site
    return None


def _outer_build_failed(
    turn: World, role: Unit, towers_missing: list[Pos],
) -> bool:
    """人还站在炮位上,最外侧那门没建成,且上回合指令失败。"""
    if turn.last_ok(role.unit_id) is not False or not _must_leave_stand(turn, role):
        return False
    outer = _outer_site(turn)
    return outer is not None and outer in towers_missing


def _should_keep_digging(role: Unit) -> bool:
    """背包里已有矿石但没到出售门槛、也没装满:继续挖近处,不去商店。"""
    ores = num_ores(role)
    return ores > 0 and not role.backpack_full and ores < SELL_THRESHOLD


def _base_pos(turn: World) -> Pos:
    station = turn.station()
    if station is not None:
        return station.pos
    return _map_center(turn)


def _mine_rank(turn: World, role: Unit, item: tuple[Pos, str]) -> tuple:
    """白天正常时段:先离人近,再离基地近,价格放最后。"""
    pos, kind = item
    return (
        distance(role.pos, pos),
        distance(_base_pos(turn), pos),
        -turn.vendor_price(kind),
        pos.x,
        pos.y,
    )


def _closer_mine_nearby(turn: World, role: Unit, current: Pos, memory) -> bool:
    if distance(role.pos, current) <= MINE_NEAR:
        return False
    taken = claimed_targets(memory, role.unit_id)
    for pos, _kind in turn.all_mines():
        if pos == current or pos in taken:
            continue
        if distance(role.pos, pos) <= MINE_NEAR:
            return True
    return False


def _wall_needs_hands(turn: World, walls_missing: list[Pos], memory, unit_id: int) -> bool:
    """墙还没齐、又没有别人在采石或砌墙时,这个人不能继续去挖别的矿。"""
    if not walls_missing or not _wall_phase(turn) or _in_recall(turn):
        return False
    for other_id, job in memory.jobs.items():
        if other_id == unit_id or job is None:
            continue
        if job.kind == KIND_WALL:
            return False
        if job.kind == KIND_MINE and job.name == WALL_MATERIAL:
            return False
    return True


def _mine_lock_holds(
    turn: World,
    role: Unit,
    job: Job,
    memory,
    walls_missing: list[Pos],
) -> bool:
    if role.backpack_full or num_ores(role) >= SELL_THRESHOLD:
        return False
    mines = dict(turn.all_mines())
    if job.target is None or job.target not in mines:
        return False
    if job.name and mines[job.target] != job.name:
        return False
    if (
        turn.is_day
        and not _in_recall(turn)
        and _closer_mine_nearby(turn, role, job.target, memory)
    ):
        return False
    if (
        mines[job.target] != WALL_MATERIAL
        and _wall_needs_hands(turn, walls_missing, memory, role.unit_id)
    ):
        return False
    return True


def _step_off_stand(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """人已经站在炮位上:白天先迈出这一格,不能原地不动。"""
    stand = _gun_stand(turn)
    if stand is None or role.pos != stand:
        return False
    return _step_off_current(turn, role, claimed, commands)


def _step_off_current(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """站在矿格或未建炮格上时先走开。不踩预定建造格、未建炮和站位。"""
    avoid = set(_move_avoid(turn, role.pos))
    avoid.update(_unbuilt_rocket_sites(turn))
    avoid.update(_PLANNED_BUILDS)
    stand = _gun_stand(turn)
    if stand is not None:
        avoid.add(stand)
    anchor = _base_pos(turn)
    missing = _unbuilt_rocket_sites(turn)
    if missing:
        anchor = missing[0]
    step = _sidestep(turn, role, anchor, claimed, avoid)
    if step is None:
        step = _sidestep(turn, role, anchor, claimed, set(_PLANNED_BUILDS))
    if step is None or step == role.pos:
        return False
    claimed.add(step)
    commands[role.unit_id] = move_command(step)
    _block_record(role.unit_id).pending_goal = anchor
    return True


def _recover_failed_outer(
    turn: World,
    role: Unit,
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """最外侧火箭指令失败:离开站位去采石头,或砌已经能砌的墙。"""
    if walls_missing and _wall_phase(turn):
        if _build_walls(turn, role, walls_missing, claimed, commands, memory):
            return True
    taken = claimed_targets(memory, role.unit_id)
    stone = _nearest_mine(turn, role, WALL_MATERIAL, claimed, taken)
    if stone is None:
        return _step_off_stand(turn, role, claimed, commands)
    kind = KIND_WALL if walls_missing and _wall_phase(turn) else KIND_MINE
    _keep_job(
        memory, role, kind, target=stone, name=WALL_MATERIAL,
        round_no=turn.round_no,
    )
    if (
        role.pos != stone
        and distance(role.pos, stone) <= 1
        and not _must_leave_stand(turn, role)
    ):
        commands[role.unit_id] = collect_command(stone)
        claimed.add(stone)
        return True
    if _walk_adjacent(turn, role, stone, claimed, commands):
        return True
    return _step_off_stand(turn, role, claimed, commands)


def _job_locked(
    turn: World,
    role: Unit,
    job: Job | None,
    memory,
    walls_missing: list[Pos],
    towers_missing: list[Pos],
    gold_left: int,
) -> bool:
    """目标还在、这单还没做完,则锁住。商店变便宜或矿价变化不算做完。"""
    if job is None:
        return False
    if job.kind == KIND_MINE:
        return _mine_lock_holds(turn, role, job, memory, walls_missing)
    if job.kind == KIND_TOWER:
        if (
            _outer_build_failed(turn, role, towers_missing)
            and job.target == _outer_site(turn)
        ):
            return False
        if job.target is None or job.target not in towers_missing:
            return False
        if gold_left < WEAPON_BUILD_COST and not _opening_rockets_pending(turn):
            return False
        owner = _PLANNED_BUILDS.get(job.target)
        if owner is not None and owner != role.unit_id:
            return False
        beside = (
            role.pos != job.target
            and distance(role.pos, job.target) <= 1
            and not _must_leave_stand(turn, role)
        )
        if beside or role.pos == job.target:
            return True
        return bool(_rocket_approaches(turn, role, job.target, set())) or _opening_rockets_pending(turn)
    if job.kind == KIND_WALL:
        return bool(walls_missing) and _wall_phase(turn)
    if job.kind in {KIND_SHOP, KIND_SELL, KIND_RECALL}:
        return _worker_job_valid(
            turn, role, job, memory, walls_missing, towers_missing, gold_left,
        )
    return False


def _worker_job_valid(
    turn: World,
    role: Unit,
    job: Job,
    memory,
    walls_missing: list[Pos],
    towers_missing: list[Pos],
    gold_left: int,
) -> bool:
    if job.kind == KIND_MINE:
        if not _mine_lock_holds(turn, role, job, memory, walls_missing):
            return False
        want = _wanted_item(turn, role, gold_left, memory)
        if _is_weapon_upgrade(want) and not _should_keep_digging(role):
            return False
        mines = dict(turn.all_mines())
        if (
            walls_missing
            and _wall_phase(turn)
            and is_builder(memory, role.unit_id)
            and job.target is not None
            and mines.get(job.target) != WALL_MATERIAL
        ):
            return False
        return True
    if job.kind == KIND_WALL:
        if not walls_missing or not _wall_phase(turn):
            return False
        want = _wanted_item(turn, role, gold_left, memory)
        if _is_weapon_upgrade(want):
            return False
        return True
    if job.kind == KIND_TOWER:
        if job.target is None or job.target not in towers_missing:
            return False
        return gold_left >= WEAPON_BUILD_COST
    if job.kind == KIND_SHOP:
        shop = turn.weapon_shop_pos()
        if shop is None or role.backpack_full:
            return False
        if job.name:
            item = turn.shop_item(job.name)
            return item is not None and item.price <= gold_left
        return _wanted_item(turn, role, gold_left, memory) is not None
    if job.kind == KIND_SELL:
        keep = 1 if walls_missing and _wall_phase(turn) else 0
        return turn.vendor() is not None and _sellable_ore(role, turn, keep) is not None
    if job.kind == KIND_RECALL:
        return _should_home(turn, role, memory)
    return False


def _run_worker_job(
    turn: World,
    role: Unit,
    job: Job,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    if job.kind == KIND_TOWER and job.target is not None:
        gold_left, builds_left = _execute_tower(
            turn, role, job, towers_missing, claimed, commands,
            gold_left, builds_left, memory,
        )
        return gold_left, builds_left
    if job.kind == KIND_WALL:
        _build_walls(
            turn, role, walls_missing, claimed, commands, memory,
            preferred=job.target,
        )
        return gold_left, builds_left
    if job.kind == KIND_SHOP:
        gold_left = _execute_shop(
            turn, role, job, claimed, commands, gold_left, memory,
        )
        return gold_left, builds_left
    if job.kind == KIND_SELL:
        _execute_sell(turn, role, claimed, commands, walls_missing)
        return gold_left, builds_left
    if job.kind == KIND_MINE:
        _execute_mine(turn, role, job, claimed, commands)
        return gold_left, builds_left
    if job.kind == KIND_RECALL:
        _recall_to_tower(turn, role, claimed, commands, memory)
        return gold_left, builds_left
    return gold_left, builds_left


def _pick_worker_job(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    want = _wanted_item(turn, role, gold_left, memory)
    if want and _try_shop(turn, role, claimed, commands, gold_left, memory):
        if role.unit_id in commands and commands[role.unit_id]["action"] == "buy":
            item = turn.shop_item(commands[role.unit_id]["name"])
            if item:
                gold_left -= item.price
        _keep_job(
            memory, role, KIND_SHOP, target=turn.weapon_shop_pos(),
            name=want, round_no=turn.round_no,
        )
        return gold_left, builds_left
    if _try_sell(turn, role, claimed, commands, walls_missing):
        _keep_job(
            memory, role, KIND_SELL, target=turn.vendor(),
            round_no=turn.round_no,
        )
        return gold_left, builds_left
    dest = _mine(
        turn, role, claimed, commands,
        extra=claimed_targets(memory, role.unit_id),
        memory=memory,
    )
    if dest is not None:
        pos, kind = dest
        _keep_job(
            memory, role, KIND_MINE, target=pos, name=kind,
            round_no=turn.round_no,
        )
    return gold_left, builds_left


def _economy_after_tickets(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    """建造单都不可领时,才买券、卖矿、就近采矿。"""
    shopped = _try_weapon_upgrade_shop(
        turn, role, claimed, commands, gold_left, memory,
    )
    if shopped is not None:
        return shopped, builds_left
    job = get_job(memory, role.unit_id)
    if (
        job is not None
        and job.kind in {KIND_MINE, KIND_SHOP, KIND_SELL}
        and _worker_job_valid(
            turn, role, job, memory, walls_missing, towers_missing, gold_left,
        )
    ):
        gold_left, builds_left = _run_worker_job(
            turn, role, job, sites, towers_missing, walls_missing,
            claimed, commands, gold_left, builds_left, memory,
        )
        if role.unit_id in commands:
            return gold_left, builds_left
        clear_job(memory, role.unit_id)
    return _pick_worker_job(
        turn, role, sites, towers_missing, walls_missing,
        claimed, commands, gold_left, builds_left, memory,
    )


def _run_owned_ticket(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int] | None:
    name = memory.ticket_owner.get(role.unit_id)
    if name == BUILD_WALL:
        return _run_wall_ticket(
            turn, role, walls_missing, claimed, commands, gold_left, builds_left, memory,
        )
    if name == BUILD_TOWER:
        return _run_tower_ticket(
            turn, role, sites, towers_missing, walls_missing, claimed,
            commands, gold_left, builds_left, memory,
        )
    if name in {UPGRADE_TOWER, UPGRADE_WALL}:
        return _run_upgrade_ticket(
            turn, role, name, walls_missing, towers_missing, claimed,
            commands, gold_left, builds_left, memory,
        )
    return None


def _run_tower_ticket(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int] | None:
    plan = plan_big_task(turn, BUILD_TOWER)
    if not plan.claimable:
        release_ticket(memory, role.unit_id)
        return None
    # 买得起一门就先建,不要等三门的金币一次凑齐才肯动。
    if gold_left < WEAPON_BUILD_COST:
        _fund_shortfall(
            turn, role, walls_missing, towers_missing, claimed,
            commands, gold_left, memory,
        )
        return gold_left, builds_left
    job = get_job(memory, role.unit_id)
    if (
        job is not None
        and job.kind == KIND_TOWER
        and job.target in towers_missing
        and gold_left >= WEAPON_BUILD_COST
    ):
        gold_left, builds_left = _execute_tower(
            turn, role, job, towers_missing, claimed, commands,
            gold_left, builds_left, memory,
        )
        if role.unit_id in commands:
            return gold_left, builds_left
    return _raise_pocket_towers(
        turn, role, sites, towers_missing, claimed, commands,
        gold_left, builds_left, memory,
    )


def _raise_pocket_towers(
    turn: World,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    """只建口袋里还缺的火箭。落脚点被占就换一门，不和别人挤同一格。"""
    committed = _tower_committed(turn, commands)
    if not towers_missing or gold_left < WEAPON_BUILD_COST or builds_left <= 0:
        return gold_left, builds_left
    for index, site in enumerate(sites):
        if site not in towers_missing:
            continue
        if committed < 2 and index >= 2:
            continue
        owner = _PLANNED_BUILDS.get(site)
        if owner is not None and owner != role.unit_id:
            continue
        if _outer_build_failed(turn, role, towers_missing) and site == _outer_site(turn):
            continue
        beside = (
            role.pos != site
            and distance(role.pos, site) <= 1
            and not _must_leave_stand(turn, role)
        )
        if not beside:
            continue
        if site in claimed and owner != role.unit_id:
            continue
        commands[role.unit_id] = build_command(site, TOWER_LOADOUT[index])
        claimed.add(site)
        _PLANNED_BUILDS[site] = role.unit_id
        gold_left -= WEAPON_BUILD_COST
        builds_left -= 1
        if site in towers_missing:
            towers_missing.remove(site)
        clear_job(memory, role.unit_id)
        return gold_left, builds_left
    picked = _best_rocket_move(
        turn, role, sites, towers_missing, claimed, memory, committed=committed,
    )
    if picked is None or picked[1] == role.pos:
        return gold_left, builds_left
    site, step, index = picked
    _assign_rocket_move(
        turn, role, site, step, index, claimed, commands, memory,
    )
    return gold_left, builds_left


def _run_wall_ticket(
    turn: World,
    role: Unit,
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int] | None:
    """石头一次采够再沿墙连续砌。第 30 回合前只采不砌。"""
    plan = plan_big_task(turn, BUILD_WALL)
    if not plan.claimable:
        release_ticket(memory, role.unit_id)
        return None
    have = count_item(role, WALL_MATERIAL)
    cap = role.capacity if role.capacity else 100
    free = max(0, cap - len(role.backpack))
    target = min(len(walls_missing), have + free)
    job = get_job(memory, role.unit_id)
    building = job is not None and job.kind == KIND_WALL and job.name == WALL
    if not _wall_phase(turn):
        # 三门火箭还没齐时先去建炮。建墙单留着，齐了再一次把石头采够。
        if _rockets_still_open(turn):
            return None
        if have < target and free > 0:
            _mine_stone_for_wall(turn, role, claimed, commands, memory)
        else:
            memory.ticket_hold.add(role.unit_id)
        return gold_left, builds_left
    if building and have > 0:
        _lay_walls(turn, role, walls_missing, claimed, commands, memory)
        return gold_left, builds_left
    if have < target and free > 0:
        _mine_stone_for_wall(turn, role, claimed, commands, memory)
        return gold_left, builds_left
    if have > 0:
        _lay_walls(turn, role, walls_missing, claimed, commands, memory)
        return gold_left, builds_left
    _mine_stone_for_wall(turn, role, claimed, commands, memory)
    return gold_left, builds_left


def _mine_stone_for_wall(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """建墙的人只挖石头,挖够之前不改去砌墙,也不改去商店。"""
    job = get_job(memory, role.unit_id)
    target = None
    if job is not None and job.name == WALL_MATERIAL and _stone_mine_ok(
        turn, role, job, memory,
    ):
        target = job.target
    if target is None:
        taken = claimed_targets(memory, role.unit_id)
        target = _nearest_mine(turn, role, WALL_MATERIAL, claimed, taken)
    if target is None:
        return False
    _keep_job(
        memory, role, KIND_WALL, target=target, name=WALL_MATERIAL,
        round_no=turn.round_no,
    )
    if (
        role.pos != target
        and distance(role.pos, target) <= 1
        and not _must_leave_stand(turn, role)
    ):
        commands[role.unit_id] = collect_command(target)
        claimed.add(target)
        return True
    if _must_leave_stand(turn, role):
        return _step_off_stand(turn, role, claimed, commands)
    return _walk_adjacent(turn, role, target, claimed, commands)


def _stone_mine_ok(turn: World, role: Unit, job: Job, memory) -> bool:
    if job.target is None or role.backpack_full:
        return False
    mines = dict(turn.all_mines())
    if mines.get(job.target) != WALL_MATERIAL:
        return False
    if _closer_mine_nearby(turn, role, job.target, memory):
        return False
    return True


def _lay_walls(
    turn: World,
    role: Unit,
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    job = get_job(memory, role.unit_id)
    preferred = None
    if (
        job is not None
        and job.name == WALL
        and job.target is not None
        and job.target in walls_missing
    ):
        preferred = job.target
    return _build_walls(
        turn, role, walls_missing, claimed, commands, memory,
        preferred=preferred, allow_mine=False,
    )


def _run_upgrade_ticket(
    turn: World,
    role: Unit,
    name: str,
    walls_missing: list[Pos],
    towers_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int] | None:
    plan = plan_big_task(turn, name)
    if not plan.claimable:
        release_ticket(memory, role.unit_id)
        return None
    wall = name == UPGRADE_WALL
    vouchers = (
        ("WallUpgradeVoucher1", "WallUpgradeVoucher2")
        if wall
        else ("WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2")
    )
    if _pursue_voucher_target(turn, role, claimed, commands, vouchers):
        return gold_left, builds_left
    job = get_job(memory, role.unit_id)
    if job is not None and job.kind == KIND_MINE and _mine_lock_holds(
        turn, role, job, memory, walls_missing,
    ):
        _execute_mine(turn, role, job, claimed, commands)
        return gold_left, builds_left
    if (
        job is not None
        and job.kind == KIND_SHOP
        and _worker_job_valid(
            turn, role, job, memory, walls_missing, towers_missing, gold_left,
        )
    ):
        gold_left = _execute_shop(
            turn, role, job, claimed, commands, gold_left, memory,
        )
        return gold_left, builds_left
    if (
        job is not None
        and job.kind == KIND_SELL
        and _worker_job_valid(
            turn, role, job, memory, walls_missing, towers_missing, gold_left,
        )
    ):
        _execute_sell(turn, role, claimed, commands, walls_missing)
        return gold_left, builds_left
    voucher_name, price = _next_voucher_price(turn, wall)
    shop = turn.weapon_shop_pos()
    at_shop = shop is not None and turn.adjacent_to_zone(role, shop)
    afford_one = price > 0 and gold_left >= price
    afford_all = plan.material_count > 0 and gold_left >= plan.material_count
    # 人还没到商店、整张单的金币还没凑齐:继续就近挖,不要半路改去买券。
    if not afford_one or (not afford_all and not at_shop):
        _fund_shortfall(
            turn, role, walls_missing, towers_missing, claimed,
            commands, gold_left, memory,
        )
        return gold_left, builds_left
    if wall and voucher_name:
        gold_left, bought = _buy_named(
            turn, role, claimed, commands, gold_left, memory, voucher_name,
        )
        if bought:
            return gold_left, builds_left
    else:
        shopped = _try_weapon_upgrade_shop(
            turn, role, claimed, commands, gold_left, memory,
        )
        if shopped is not None:
            return shopped, builds_left
    if _should_keep_digging(role):
        _fund_shortfall(
            turn, role, walls_missing, towers_missing, claimed,
            commands, gold_left, memory,
        )
    return gold_left, builds_left


def _next_voucher_price(turn: World, wall: bool) -> tuple[str | None, int]:
    names = _wall_upgrade_wishlist(turn) if wall else _weapon_voucher_wishlist(turn)
    if not names:
        return None, 0
    item = turn.shop_item(names[0])
    if item is None:
        return names[0], 0
    return names[0], item.price


def _buy_named(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    memory,
    item_name: str,
) -> tuple[int, bool]:
    shop = turn.weapon_shop_pos()
    item = turn.shop_item(item_name)
    if shop is None or item is None or role.backpack_full or item.price > gold_left:
        return gold_left, False
    if backpack_item(role, item_name):
        return gold_left, False
    if turn.adjacent_to_zone(role, shop):
        commands[role.unit_id] = buy_command(item_name, 1)
        gold_left -= item.price
        _keep_job(
            memory, role, KIND_SHOP, target=shop, name=item_name,
            round_no=turn.round_no,
        )
        return gold_left, True
    if _should_keep_digging(role):
        return gold_left, False
    if _walk_adjacent(turn, role, shop, claimed, commands):
        _keep_job(
            memory, role, KIND_SHOP, target=shop, name=item_name,
            round_no=turn.round_no,
        )
        return gold_left, True
    return gold_left, False


def _pursue_voucher_target(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    vouchers: tuple[str, ...],
) -> bool:
    for voucher in vouchers:
        name = backpack_item(role, voucher)
        if name is None:
            continue
        target = _best_upgrade_unit(turn, role, voucher)
        if target is None:
            continue
        if _adjacent_building(turn, role, target):
            commands[role.unit_id] = use_command(name, target.pos)
            return True
        step = _step_toward(turn, role, target.pos, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return True
    return False


def _fund_shortfall(
    turn: World,
    role: Unit,
    walls_missing: list[Pos],
    towers_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    memory,
) -> None:
    """金不够时先采近处矿再卖,卖够差额再去建或买。采的路上不改去商店。"""
    job = get_job(memory, role.unit_id)
    if job is not None and job.kind == KIND_MINE and _mine_lock_holds(
        turn, role, job, memory, walls_missing,
    ):
        _execute_mine(turn, role, job, claimed, commands)
        return
    if (
        job is not None
        and job.kind == KIND_SELL
        and _worker_job_valid(
            turn, role, job, memory, walls_missing, towers_missing, gold_left,
        )
    ):
        _execute_sell(turn, role, claimed, commands, walls_missing)
        return
    if _try_sell(turn, role, claimed, commands, walls_missing):
        _keep_job(
            memory, role, KIND_SELL, target=turn.vendor(),
            round_no=turn.round_no,
        )
        return
    dest = _mine(
        turn, role, claimed, commands,
        extra=claimed_targets(memory, role.unit_id),
        memory=memory,
    )
    if dest is not None:
        pos, kind = dest
        _keep_job(
            memory, role, KIND_MINE, target=pos, name=kind,
            round_no=turn.round_no,
        )


def _execute_tower(
    turn: World,
    role: Unit,
    job: Job,
    towers_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    builds_left: int,
    memory,
) -> tuple[int, int]:
    """执行当前炮单：贴着就建，站在格上先走开，走不到不丢单。"""
    site = job.target
    if site is None:
        return gold_left, builds_left
    if role.pos == site:
        if not _step_off_current(turn, role, claimed, commands):
            turn.note(
                f"角色 {role.unit_id} 站在未建炮格 ({site.x},{site.y})，无侧向可走"
            )
            _note_direction_blocked(turn, role, site, claimed)
        else:
            _stamp_job(memory, role, job)
        return gold_left, builds_left
    beside = (
        distance(role.pos, site) <= 1
        and not _must_leave_stand(turn, role)
    )
    owner = _PLANNED_BUILDS.get(site)
    sites = _tower_sites(turn)
    index = next((i for i, pos in enumerate(sites) if pos == site), 0)
    committed = _tower_committed(turn, commands)
    can_build = (
        beside
        and gold_left >= WEAPON_BUILD_COST
        and builds_left > 0
        and site in towers_missing
        and (owner is None or owner == role.unit_id)
        and not (index >= 2 and committed < 2)
    )
    if can_build:
        commands[role.unit_id] = build_command(site, job.name or "rocket")
        claimed.add(site)
        _PLANNED_BUILDS[site] = role.unit_id
        gold_left -= WEAPON_BUILD_COST
        builds_left -= 1
        if site in towers_missing:
            towers_missing.remove(site)
        _stamp_job(memory, role, job)
        clear_job(memory, role.unit_id)
        return gold_left, builds_left
    if _must_leave_stand(turn, role) and _step_off_stand(turn, role, claimed, commands):
        _stamp_job(memory, role, job)
        return gold_left, builds_left
    step = _step_toward(turn, role, site, claimed)
    if step is not None and step != role.pos:
        commands[role.unit_id] = move_command(step)
        _stamp_job(memory, role, job)
        return gold_left, builds_left
    turn.note(
        f"执行层 walk 无步: 角色 {role.unit_id} 炮 ({site.x},{site.y})，保留工单"
    )
    _note_direction_blocked(turn, role, site, claimed)
    return gold_left, builds_left


def _recall_to_tower(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory=None,
) -> bool:
    # 入夜前不把人拉上站位。回防窗口里也只有炮手走上站位。
    if turn.is_day and not _in_recall(turn) and _pattern_ready(turn):
        return False
    if (
        turn.is_day
        and _in_recall(turn)
        and memory is not None
        and not _is_gunner(turn, role, memory)
    ):
        return False
    if memory is not None and _pattern_ready(turn):
        if _is_gunner(turn, role, memory) and (not turn.is_day or _in_recall(turn)):
            stand = _gun_stand(turn)
            if stand is None:
                return False
            return _walk_onto(turn, role, stand, claimed, commands)
        if not turn.is_day:
            return _assign_night_safe(turn, role, claimed, commands, memory)
        return False
    weapons = turn.weapons()
    if weapons:
        tower = min(
            weapons,
            key=lambda unit: (distance(role.pos, unit.pos), unit.unit_id),
        )
        target = tower.pos
    else:
        station = turn.station()
        if station is None:
            return False
        target = station.pos
    if role.pos != target and distance(role.pos, target) <= 1:
        return False
    return _walk_adjacent(turn, role, target, claimed, commands)


def _battery_block(turn: World) -> set[Pos]:
    """炮位和站位。非炮手赶去安全区时，路径不要踩上去。"""
    blocked = set(_tower_sites(turn))
    stand = _gun_stand(turn)
    if stand is not None:
        blocked.add(stand)
    return blocked


def _walk_onto(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    avoid: set[Pos] | None = None,
) -> bool:
    """走到目标格上。已经站在上面则不再移动。"""
    if role.pos == target:
        claimed.add(target)
        return True
    travel = set(avoid or ())
    travel.update(_block_record(role.unit_id).avoid)
    travel.update(_move_avoid(turn, target))
    if target not in _tower_sites(turn):
        travel.discard(target)
    changed, step = reroute_if_blocked_two_turns(
        turn, role, target, claimed, travel, commands,
    )
    if changed:
        if step is None or step in claimed or step == role.pos:
            step = _sidestep(turn, role, target, claimed, travel)
        if step is None or step in claimed or step == role.pos:
            _note_direction_blocked(turn, role, target, claimed)
            turn.note(
                f"角色 {role.unit_id} 无法走上 ({target.x},{target.y})"
            )
            return False
        claimed.add(step)
        claimed.add(target)
        commands[role.unit_id] = move_command(step)
        return True
    step = next_step(turn, role, target, travel or None)
    if step is None or step in claimed or step == role.pos:
        _note_direction_blocked(turn, role, target, claimed)
        turn.note(
            f"角色 {role.unit_id} 无法走上 ({target.x},{target.y})"
        )
        return False
    _block_record(role.unit_id).pending_goal = target
    claimed.add(step)
    claimed.add(target)
    commands[role.unit_id] = move_command(step)
    return True


def _nearest_mine(
    turn: World,
    role: Unit,
    kind: str,
    claimed: set[Pos],
    extra: set[Pos] | frozenset[Pos] | tuple = (),
) -> Pos | None:
    blocked = set(claimed)
    blocked.update(extra)
    candidates = [
        pos for pos, name in turn.all_mines()
        if name == kind and pos not in blocked
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
    )


def _build_walls(
    turn: World,
    role: Unit,
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory=None,
    preferred: Pos | None = None,
    allow_mine: bool = True,
) -> bool:
    """墙未建齐时沿计划砌。allow_mine 为假时石头不够就不改去挖矿。"""
    stones = count_item(role, WALL_MATERIAL)
    if allow_mine:
        mine = _adjacent_mine(turn, role, "stone")
        if mine is not None and stones < STONE_KEEP:
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            if memory is not None:
                _keep_job(
                    memory, role, KIND_WALL, target=mine,
                    name=WALL_MATERIAL, round_no=turn.round_no,
                )
            return True
    if stones:
        sites = list(walls_missing)
        if preferred is not None and preferred in walls_missing:
            sites = [preferred] + [site for site in walls_missing if site != preferred]
        adjacent = [
            site for site in sites
            if site not in claimed and role.pos != site and distance(role.pos, site) <= 1
        ]
        # 已经贴着计划墙就地砌,按顺时针取最早的那一格,避免绕去弧线另一头。
        ordered = adjacent or [
            site for site in sites if site not in claimed
        ]
        for site in ordered:
            if _build_or_walk(turn, role, site, WALL, claimed, commands):
                if (
                    role.unit_id in commands
                    and commands[role.unit_id]["action"] == "build"
                ):
                    walls_missing.remove(site)
                if memory is not None:
                    _keep_job(
                        memory, role, KIND_WALL, target=site,
                        name=WALL, round_no=turn.round_no,
                    )
                return True
    if not allow_mine:
        return False
    taken = claimed_targets(memory, role.unit_id) if memory is not None else set()
    stone = None
    if preferred is not None:
        for pos, name in turn.all_mines():
            if pos == preferred and name == WALL_MATERIAL and pos not in claimed:
                stone = preferred
                break
    if stone is None:
        stone = _nearest_mine(turn, role, WALL_MATERIAL, claimed, taken)
    if stone is not None:
        if memory is not None:
            _keep_job(
                memory, role, KIND_WALL, target=stone,
                name=WALL_MATERIAL, round_no=turn.round_no,
            )
        if role.pos != stone and distance(role.pos, stone) <= 1:
            commands[role.unit_id] = collect_command(stone)
            claimed.add(stone)
            return True
        return _walk_adjacent(turn, role, stone, claimed, commands)
    return False


def _pioneer_day(
    turn: World,
    role: Unit,
    memory,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    if _try_heal(role, commands):
        return "", _maybe_prompt(turn, memory)
    if turn.phase_task:
        _keep_job(memory, role, KIND_PHASE, round_no=turn.round_no)
        return _run_task(turn, role, memory, claimed, commands)
    if _just_accepted(turn, role, memory):
        # 接任务的下一回合题面可能还没写进 phaseTask，人也不能走开。
        return "", ""
    if _should_home(turn, role, memory):
        _recall_to_tower(turn, role, claimed, commands, memory)
        return "", _maybe_prompt(turn, memory)
    if _should_edge_mine(turn, role, memory):
        _run_day_edge(turn, role, claimed, commands, memory)
        return "", _maybe_prompt(turn, memory)
    job = get_job(memory, role.unit_id)
    if job is not None and job.target is not None and distance(role.pos, job.target) > 1:
        if job.kind in {KIND_ACCEPT, KIND_TREASURE, KIND_SHOP, KIND_SELL}:
            return _run_pioneer_job(turn, role, job, memory, claimed, commands)
    if _try_accept_task(turn, role, claimed, commands, memory):
        return "", _maybe_prompt(turn, memory)
    if _try_treasure(turn, role, memory, claimed, commands):
        _keep_job(
            memory, role, KIND_TREASURE, target=memory.treasure_pos,
            round_no=turn.round_no,
        )
        return "", _maybe_prompt(turn, memory)
    if _try_shop(turn, role, claimed, commands, turn.gold, memory):
        want = _wanted_item(turn, role, turn.gold, memory)
        _keep_job(
            memory, role, KIND_SHOP, target=turn.weapon_shop_pos(),
            name=want or "", round_no=turn.round_no,
        )
        return "", _maybe_prompt(turn, memory)
    if _try_sell(turn, role, claimed, commands, []):
        _keep_job(
            memory, role, KIND_SELL, target=turn.vendor(),
            round_no=turn.round_no,
        )
        return "", _maybe_prompt(turn, memory)
    _try_accept_task(turn, role, claimed, commands, memory)
    return "", _maybe_prompt(turn, memory)


def _run_task(
    turn: World,
    role: Unit,
    memory,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    if memory.abandon_task:
        _walk_to_other_task(turn, role, claimed, commands)
        return "", ""
    execute_cmd, answer = next_task_command(turn, memory)
    if memory.abandon_task:
        _walk_to_other_task(turn, role, claimed, commands)
        return "", ""
    if answer:
        commands[role.unit_id] = submit_answer_command(answer)
        execute_cmd = ""
    elif _near_own_task(turn, role):
        pass
    else:
        _walk_to_task(turn, role, claimed, commands)
        execute_cmd = ""
    prompt = ""
    if can_prompt(turn, memory):
        prompt = task_prompt(turn)
        mark_prompt(turn, memory)
    return execute_cmd, prompt


def _pioneer_job_valid(turn: World, role: Unit, job: Job, memory) -> bool:
    if job.kind == KIND_ACCEPT:
        return bool(turn.own_task_zones() or turn.player_tasks)
    if job.kind == KIND_TREASURE:
        return treasure_ready(turn, memory) and memory.treasure_pos is not None
    if job.kind == KIND_SHOP:
        if role.backpack_full or turn.weapon_shop_pos() is None:
            return False
        if job.name:
            item = turn.shop_item(job.name)
            return item is not None and item.price <= turn.gold
        return _wanted_item(turn, role, turn.gold, memory) is not None
    if job.kind == KIND_SELL:
        return turn.vendor() is not None and _sellable_ore(role, turn, 0) is not None
    if job.kind == KIND_PHASE:
        return bool(turn.phase_task)
    if job.kind == KIND_RECALL:
        return _should_home(turn, role, memory)
    return False


def _run_pioneer_job(
    turn: World,
    role: Unit,
    job: Job,
    memory,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    if job.kind == KIND_ACCEPT:
        _try_accept_task(turn, role, claimed, commands, memory, lock=job.target)
        return "", _maybe_prompt(turn, memory)
    if job.kind == KIND_TREASURE:
        _try_treasure(turn, role, memory, claimed, commands)
        return "", _maybe_prompt(turn, memory)
    if job.kind == KIND_SHOP:
        _execute_shop(turn, role, job, claimed, commands, turn.gold, memory)
        return "", _maybe_prompt(turn, memory)
    if job.kind == KIND_SELL:
        _execute_sell(turn, role, claimed, commands, [])
        return "", _maybe_prompt(turn, memory)
    if job.kind == KIND_PHASE:
        return _run_task(turn, role, memory, claimed, commands)
    if job.kind == KIND_RECALL:
        _recall_to_tower(turn, role, claimed, commands, memory)
        return "", _maybe_prompt(turn, memory)
    return "", _maybe_prompt(turn, memory)


def _pattern_ready(turn: World) -> bool:
    """三座火箭都落在口袋炮位上,炮手才能站进中间格轮流开。"""
    sites = _tower_sites(turn)
    if len(sites) < 3:
        return False
    have = {unit.pos for unit in turn.weapons()}
    return all(site in have for site in sites)


def _is_gunner(turn: World, role: Unit, memory) -> bool:
    gunner = _select_gunner(turn, list(turn.controllable()), memory)
    return gunner is not None and gunner.unit_id == role.unit_id


def _select_gunner(turn: World, heroes: list[Unit], memory) -> Unit | None:
    """只留一名炮手。已有操炮任务的人继续;否则优先贴着武器的工人。"""
    if not heroes:
        return None
    alive = {hero.unit_id for hero in heroes}
    for hero in heroes:
        job = get_job(memory, hero.unit_id)
        if (
            job is not None
            and job.kind == KIND_MAN_TOWER
            and hero.unit_id in alive
        ):
            return hero
    stand = _gun_stand(turn)
    pattern = _pattern_ready(turn)
    weapons = turn.weapons()

    def key(hero: Unit) -> tuple:
        worker = 0 if hero.kind == "worker" else 1
        if pattern and stand is not None:
            return (worker, distance(hero.pos, stand), hero.unit_id)
        if weapons:
            adjacent = 0 if any(
                distance(hero.pos, tower.pos) <= 1 for tower in weapons
            ) else 1
            near = min(distance(hero.pos, tower.pos) for tower in weapons)
            return (worker, adjacent, near, hero.unit_id)
        if stand is not None:
            return (worker, distance(hero.pos, stand), hero.unit_id)
        return (worker, hero.unit_id)

    return min(heroes, key=key)


def _keep_gunner(memory, role: Unit, turn: World, target: Pos | None, tower_id: int) -> None:
    prev = get_job(memory, role.unit_id)
    if tower_id == 0 and prev is not None and prev.kind == KIND_MAN_TOWER:
        tower_id = prev.tower_id
    set_job(
        memory,
        role.unit_id,
        Job(
            kind=KIND_MAN_TOWER,
            target=target,
            tower_id=tower_id,
            started=prev.started if prev is not None and prev.kind == KIND_MAN_TOWER else turn.round_no,
        ),
    )


def _rotate_weapons(weapons: list[Unit], last_id: int) -> list[Unit]:
    if not weapons or last_id <= 0:
        return weapons
    ids = [tower.unit_id for tower in weapons]
    if last_id not in ids:
        return weapons
    index = ids.index(last_id)
    rotated = weapons[index + 1:] + weapons[:index]
    return rotated + [weapons[index]]


def _fire_one(
    turn: World,
    role: Unit,
    memory,
    commands: dict[int, dict[str, Any]],
    anchor: Pos | None,
) -> bool:
    """同一回合只开一门已冷却、且射程内有目标的武器。"""
    sites = list(_tower_sites(turn))
    ready = [
        tower for tower in turn.weapons()
        if distance(role.pos, tower.pos) <= 1 and tower.cooldown <= 0
    ]

    def order(tower: Unit) -> tuple:
        try:
            index = sites.index(tower.pos)
        except ValueError:
            index = 99
        return (index, tower.unit_id)

    ready.sort(key=order)
    prev = get_job(memory, role.unit_id)
    last_id = prev.tower_id if prev is not None and prev.kind == KIND_MAN_TOWER else 0
    for tower in _rotate_weapons(ready, last_id):
        if tower.cooldown > 0:
            continue
        targets = attack_positions(turn, tower)
        if not targets:
            continue
        commands[tower.unit_id] = attack_commands(role.unit_id, targets)
        _keep_gunner(memory, role, turn, anchor or tower.pos, tower.unit_id)
        return True
    if ready:
        turn.note(f"炮手 {role.unit_id} 贴着武器,但射程内没有可打的机器人")
    elif any(distance(role.pos, tower.pos) <= 1 for tower in turn.weapons()):
        cooling = [
            tower for tower in turn.weapons()
            if distance(role.pos, tower.pos) <= 1 and tower.cooldown > 0
        ]
        if cooling:
            turn.note(
                "武器仍在冷却 "
                + ",".join(f"{tower.unit_id}:{tower.cooldown}" for tower in cooling)
            )
    _keep_gunner(memory, role, turn, anchor, last_id)
    return False


def _man_battery(
    turn: World,
    role: Unit,
    memory,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> None:
    stand = _gun_stand(turn)
    if stand is None:
        _man_single(turn, role, memory, claimed, commands)
        return
    if role.pos != stand:
        _keep_gunner(memory, role, turn, stand, 0)
        _walk_onto(turn, role, stand, claimed, commands)
        return
    claimed.add(stand)
    _fire_one(turn, role, memory, commands, stand)


def _man_single(
    turn: World,
    role: Unit,
    memory,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> None:
    """口袋还没建成时,一名炮手去贴最近的一座已有武器。"""
    weapons = turn.weapons()
    if not weapons:
        return
    job = get_job(memory, role.unit_id)
    tower = None
    if job is not None and job.kind == KIND_MAN_TOWER and job.tower_id:
        tower = next((item for item in weapons if item.unit_id == job.tower_id), None)
    if tower is None:
        tower = min(weapons, key=lambda item: (distance(role.pos, item.pos), item.unit_id))
    _keep_gunner(memory, role, turn, tower.pos, tower.unit_id)
    if distance(role.pos, tower.pos) <= 1:
        if tower.cooldown > 0:
            turn.note(
                f"武器 {tower.unit_id} 仍在冷却 cooldown={tower.cooldown}，本回合不能 attack"
            )
            return
        targets = attack_positions(turn, tower)
        if targets:
            commands[tower.unit_id] = attack_commands(role.unit_id, targets)
        else:
            turn.note(
                f"角色 {role.unit_id} 已贴塔 {tower.unit_id} 待命（射程内无目标）"
            )
        return
    step = _step_toward(turn, role, tower.pos, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)


def _night(
    turn: World, memory, commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    claimed: set[Pos] = set()
    busy: set[int] = set()
    execute_cmd = ""
    prompt = ""
    for role in turn.controllable():
        if _try_heal(role, commands):
            busy.add(role.unit_id)
    pioneer = turn.pioneer()
    if pioneer is not None and pioneer.unit_id not in busy and (
        turn.phase_task or _just_accepted(turn, pioneer, memory)
    ):
        # 任务还在就不要把开拓者选成炮手，也不要拉去安全区。
        if turn.phase_task:
            _keep_job(memory, pioneer, KIND_PHASE, round_no=turn.round_no)
            execute_cmd, prompt = _run_task(
                turn, pioneer, memory, claimed, commands,
            )
        busy.add(pioneer.unit_id)
    heroes = [role for role in turn.controllable() if role.unit_id not in busy]
    gunner = _select_gunner(turn, heroes, memory)
    if gunner is not None and turn.weapons():
        if _pattern_ready(turn):
            _man_battery(turn, gunner, memory, claimed, commands)
        else:
            _man_single(turn, gunner, memory, claimed, commands)
        busy.add(gunner.unit_id)
    for role in heroes:
        if role.unit_id in busy:
            continue
        if _try_night_item(turn, role, commands):
            busy.add(role.unit_id)
            continue
        # 夜里只有炮手留在背后站位。其余人去边缘安全区，不进地图中央。
        _assign_night_safe(turn, role, claimed, commands, memory)
    if not prompt:
        prompt = _maybe_prompt(turn, memory)
    return execute_cmd, prompt


def _controller_ids(commands: dict[int, dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for command in commands.values():
        if command.get("action") != "attack":
            continue
        raw = command.get("controllerId")
        if raw is None:
            continue
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _issued_cells(commands: dict[int, dict[str, Any]]) -> set[Pos]:
    cells: set[Pos] = set()
    for command in commands.values():
        pos = _command_cell(command)
        if pos is not None and command.get("action") in {"move", "build"}:
            cells.add(pos)
    return cells


def _untangle_feet(turn: World, memory, commands: dict[int, dict[str, Any]]) -> None:
    """同一回合不能一个人 build、另一个人 move 进同一格。开拓者白天不踩炮位。"""
    rockets = set(_tower_sites(turn))
    stand = _gun_stand(turn)
    build_cells: dict[Pos, int] = {}
    for uid, command in commands.items():
        if command.get("action") != "build":
            continue
        cell = _command_cell(command)
        if cell is None:
            continue
        build_cells[cell] = int(uid)
        _PLANNED_BUILDS.setdefault(cell, int(uid))
    roles = {role.unit_id: role for role in turn.controllable()}
    for uid, command in list(commands.items()):
        if command.get("action") != "move":
            continue
        step = _command_cell(command)
        role = roles.get(int(uid))
        if step is None or role is None:
            continue
        on_build = step in build_cells and build_cells[step] != int(uid)
        on_pocket = step in rockets or (stand is not None and step == stand)
        if role.kind == "pioneer":
            answering = _pioneer_on_evolution(turn, role, memory) or _just_accepted(
                turn, role, memory,
            )
            if answering or not turn.is_day or _in_recall(turn):
                if on_build:
                    commands.pop(int(uid), None)
                continue
            if not on_build and not on_pocket:
                continue
            commands.pop(int(uid), None)
            claimed = _issued_cells(commands)
            if not _try_accept_task(turn, role, claimed, commands, memory):
                _walk_to_task(turn, role, claimed, commands)
            continue
        if not on_build and not (turn.is_day and not _in_recall(turn) and on_pocket):
            continue
        if _outer_build_failed(turn, role, _unbuilt_rocket_sites(turn)):
            continue
        commands.pop(int(uid), None)
        if not turn.is_day or _in_recall(turn):
            continue
        missing = _unbuilt_rocket_sites(turn)
        if not missing:
            continue
        _nudge_toward_rocket(
            turn, role, missing, _issued_cells(commands), commands, memory,
        )


def _fill_idle(turn: World, memory, commands: dict[int, dict[str, Any]]) -> None:
    """真正没事做的英雄走近最近的塔,避免站桩。贴塔开火/任务点待命的不算空闲。"""
    claimed: set[Pos] = _issued_cells(commands) | set(_PLANNED_BUILDS)
    busy = set(commands) | _controller_ids(commands)
    opening = _opening_rockets_pending(turn)
    for role in turn.controllable():
        if role.unit_id in busy:
            continue
        if role.unit_id in memory.ticket_hold:
            continue
        job = get_job(memory, role.unit_id)
        if any(distance(role.pos, tower.pos) <= 1 for tower in turn.weapons()):
            if _must_leave_stand(turn, role):
                _step_off_stand(turn, role, claimed, commands)
                continue
            if opening:
                pass
            elif job is None or job.kind in {KIND_MAN_TOWER, KIND_HOLD}:
                continue
        if role.kind == "pioneer" and (
            turn.phase_task or _just_accepted(turn, role, memory)
        ):
            continue
        if turn.is_day and role.kind == "pioneer" and _near_own_task(turn, role):
            continue
        if turn.is_day and not _in_recall(turn) and role.kind == "pioneer":
            if not _try_accept_task(turn, role, claimed, commands, memory):
                _walk_to_task(turn, role, claimed, commands)
            continue
        if turn.is_day and not _in_recall(turn):
            if _must_leave_stand(turn, role):
                _step_off_stand(turn, role, claimed, commands)
                continue
            if opening:
                continue
            if _pattern_ready(turn):
                continue
            _recall_to_tower(turn, role, claimed, commands, memory)
            continue
        if not _is_gunner(turn, role, memory):
            if not turn.is_day or _in_recall(turn):
                job = get_job(memory, role.unit_id)
                if (
                    job is not None
                    and job.kind in {KIND_MINE, KIND_HOLD}
                    and job.target is not None
                    and in_night_safe_zone(turn, job.target)
                    and distance(role.pos, job.target) <= 1
                ):
                    continue
                _assign_night_safe(turn, role, claimed, commands, memory)
            continue
        _recall_to_tower(turn, role, claimed, commands, memory)


def _maybe_prompt(turn: World, memory) -> str:
    if turn.phase_task:
        return ""
    if memory.treasure_done or not can_prompt(turn, memory):
        return ""
    missing = memory.treasure_pos is None or not memory.treasure_items
    if not (
        turn.round_in_day == 1
        or turn.last_summon in (2, 3)
        or (missing and turn.round_in_day <= 2)
    ):
        return ""
    mark_prompt(turn, memory)
    return treasure_prompt(turn, memory)


def _try_heal(role: Unit, commands: dict[int, dict[str, Any]]) -> bool:
    cap = HERO_MAX_HP.get(role.kind)
    if cap is None or role.health >= cap:
        return False
    name = backpack_item(role, "Medicine")
    if name is None:
        return False
    commands[role.unit_id] = use_command(name)
    return True


def _try_upgrade_or_fix(
    turn: World, role: Unit, commands: dict[int, dict[str, Any]],
) -> bool:
    upgrade = _next_upgrade(turn, role)
    if upgrade is not None:
        name, target = upgrade
        if _adjacent_building(turn, role, target):
            commands[role.unit_id] = use_command(name, target.pos)
            return True
    fixer = backpack_item(role, "WallFixer")
    if fixer:
        for wall in turn.walls():
            if wall.health >= _wall_max(wall.level):
                continue
            if _adjacent_building(turn, role, wall):
                commands[role.unit_id] = use_command(fixer, wall.pos)
                return True
    return False


def _try_night_item(
    turn: World, role: Unit, commands: dict[int, dict[str, Any]],
) -> bool:
    center = bomb_center(turn)
    if center is None:
        return False
    for item_name in ("DizzyWeapon", "Bomb"):
        name = backpack_item(role, item_name)
        if name is None:
            continue
        commands[role.unit_id] = use_command(name, center)
        return True
    return False


def _try_sell(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    walls_missing: list[Pos],
) -> bool:
    vendor = turn.vendor()
    if vendor is None:
        return False
    keep_stone = 1 if walls_missing and _wall_phase(turn) else 0
    ore = _sellable_ore(role, turn, keep_stone)
    if ore is None:
        return False
    if turn.adjacent_to_zone(role, vendor):
        name, num = ore
        commands[role.unit_id] = sell_command(name, num)
        return True
    if role.backpack_full or num_ores(role) >= SELL_THRESHOLD:
        return _walk_adjacent(turn, role, vendor, claimed, commands)
    return False


def _try_shop(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    memory,
) -> bool:
    shop = turn.weapon_shop_pos()
    if shop is None or role.backpack_full:
        return False
    want = _wanted_item(turn, role, gold_left, memory)
    if want is None:
        return False
    if turn.adjacent_to_zone(role, shop):
        commands[role.unit_id] = buy_command(want, 1)
        return True
    if _should_keep_digging(role):
        return False
    urgent = (
        want.lower() in {item.lower() for item in memory.treasure_items}
        or "upgradevoucher" in want.lower()
    )
    if urgent or gold_left >= 100:
        return _walk_adjacent(turn, role, shop, claimed, commands)
    return False


def _wanted_item(turn: World, role: Unit, gold_left: int, memory) -> str | None:
    for name in _upgrade_buy_list(turn):
        if not name.startswith("Weapon") and _weapons_need_upgrade(turn):
            continue
        if backpack_item(role, name):
            if _best_upgrade_unit(turn, role, name) is not None:
                return None
            continue
        item = turn.shop_item(name)
        if item is None or item.price > gold_left:
            continue
        return name
    if treasure_ready(turn, memory) or memory.treasure_items:
        for name in missing_treasure_items(role, memory.treasure_items):
            item = turn.shop_item(name)
            if item and item.price <= gold_left:
                return item.name
    wishlist: list[str] = []
    cap = HERO_MAX_HP.get(role.kind, 0)
    if role.health < cap and backpack_item(role, "Medicine") is None:
        wishlist.append("Medicine")
    if any(wall.health < _wall_max(wall.level) for wall in turn.walls()):
        wishlist.append("WallFixer")
    wishlist.extend(("DizzyWeapon", "Bomb"))
    for name in wishlist:
        item = turn.shop_item(name)
        if item is None or item.price > gold_left:
            continue
        if backpack_item(role, item.name):
            continue
        if name.startswith("Weapon") and gold_left < item.price + 0:
            continue
        return name
    return None


def _try_accept_task(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory=None,
    lock: Pos | None = None,
) -> bool:
    valid = [task for task in turn.player_tasks if task.valid]
    points = list(turn.own_task_zones()) or [task.pos for task in turn.player_tasks]
    target = lock
    if target is None:
        if valid:
            target = min(valid, key=lambda task: distance(role.pos, task.pos)).pos
        elif points:
            target = min(points, key=lambda pos: distance(role.pos, pos))
    if target is None:
        turn.note(
            f"开拓者 {role.unit_id} 无法领任务：isValid=false 或冷却中/任务已做完"
        )
        return False
    if memory is not None:
        _keep_job(
            memory, role, KIND_ACCEPT, target=target, round_no=turn.round_no,
        )
    if _near_own_task(turn, role) and valid:
        commands[role.unit_id] = accept_task_command()
        if memory is not None:
            memory.accepted_round = turn.round_no
        return True
    if turn.adjacent_to_zone(role, target) and not valid:
        return True
    walked = _walk_adjacent(turn, role, target, claimed, commands)
    return walked or turn.adjacent_to_zone(role, target) or lock is not None


def _try_treasure(
    turn: World,
    role: Unit,
    memory,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if not treasure_ready(turn, memory) or memory.treasure_pos is None:
        return False
    missing = missing_treasure_items(role, memory.treasure_items)
    if missing:
        return False
    target = memory.treasure_pos
    if turn.adjacent_to_zone(role, target):
        items = consume_names(role, memory.treasure_items)
        commands[role.unit_id] = summon_treasure_command(target, items)
        return True
    return _walk_adjacent(turn, role, target, claimed, commands)


def _near_own_task(turn: World, role: Unit) -> bool:
    cells = turn.own_task_zones()
    if cells and turn.adjacent_to_any(role, cells):
        return True
    return any(
        turn.adjacent_to_zone(role, task.pos) for task in turn.player_tasks
    )


def _walk_to_task(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    cells = turn.own_task_zones() or tuple(task.pos for task in turn.player_tasks)
    if not cells:
        return False
    target = min(cells, key=lambda pos: distance(role.pos, pos))
    return _walk_adjacent(turn, role, target, claimed, commands)


def _just_accepted(turn: World, role: Unit, memory) -> bool:
    """上一回合刚 acceptTask 成功，这一回合必须还站在任务点上。"""
    accepted = int(getattr(memory, "accepted_round", 0) or 0)
    if accepted <= 0 or turn.round_no != accepted + 1:
        return False
    if turn.last_ok(role.unit_id) is False:
        return False
    return _near_own_task(turn, role)


def _keep_pioneer_on_task(turn: World, memory, commands: dict[int, dict[str, Any]]) -> None:
    """任务未结束时，抹掉会离开任务点周围一格的移动。"""
    if memory.abandon_task:
        return
    pioneer = turn.pioneer()
    if pioneer is None:
        return
    if not turn.phase_task and not _just_accepted(turn, pioneer, memory):
        return
    if not _near_own_task(turn, pioneer):
        return
    command = commands.get(pioneer.unit_id)
    if not command or command.get("action") != "move":
        return
    raw = (command.get("targetPos") or [None])[0]
    if not isinstance(raw, dict):
        return
    step = Pos(int(raw["x"]), int(raw["y"]))
    cells = turn.own_task_zones() or tuple(task.pos for task in turn.player_tasks)
    if cells and all(distance(step, cell) > 1 for cell in cells):
        commands.pop(pioneer.unit_id, None)


def _walk_to_other_task(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """连续失败后离开当前任务点，去另一个任务点。离开即结束本题。"""
    cells = list(turn.own_task_zones() or tuple(task.pos for task in turn.player_tasks))
    if not cells:
        return False
    here = [pos for pos in cells if distance(role.pos, pos) <= 1]
    others = [pos for pos in cells if pos not in here]
    if not others:
        anchor = here[0] if here else cells[0]
        for dx, dy in _NEIGHBOUR_STEPS:
            step = Pos(role.pos.x + dx, role.pos.y + dy)
            if not turn.land(step):
                continue
            if all(distance(step, cell) > 1 for cell in cells):
                commands[role.unit_id] = move_command(step)
                return True
        return _walk_adjacent(turn, role, anchor, claimed, commands)
    target = min(others, key=lambda pos: (distance(role.pos, pos), pos.x, pos.y))
    return _walk_adjacent(turn, role, target, claimed, commands)


def _adjacent_mine(turn: World, role: Unit, kind: str | None = None) -> Pos | None:
    mines = []
    for pos, name in turn.all_mines():
        if kind and name != kind:
            continue
        if role.pos != pos and distance(role.pos, pos) <= 1:
            mines.append(pos)
    mines.sort(key=lambda pos: (distance(role.pos, pos), pos.x, pos.y))
    return mines[0] if mines else None


def _pick_ranked_mine(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    memory,
) -> tuple[Pos, str] | None:
    taken = claimed_targets(memory, role.unit_id)
    ranked = sorted(
        (
            (pos, kind) for pos, kind in turn.all_mines()
            if pos not in claimed and pos not in taken
        ),
        key=lambda item: _mine_rank(turn, role, item),
    )
    return ranked[0] if ranked else None


def _execute_mine(
    turn: World,
    role: Unit,
    job: Job,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if role.backpack_full:
        extra = next(
            (
                item for item in role.backpack
                if item.lower() not in ("stone", "iron", "copper")
                and "voucher" not in item.lower()
            ),
            None,
        )
        if extra and turn.vendor() is None:
            commands[role.unit_id] = drop_command(extra)
            return True
        vendor = turn.vendor()
        if vendor is not None:
            return _walk_adjacent(turn, role, vendor, claimed, commands)
        return False
    pos = job.target
    if pos is None:
        return False
    if role.pos == pos:
        return _step_off_current(turn, role, claimed, commands)
    if turn.adjacent_to_zone(role, pos):
        if _must_leave_stand(turn, role):
            return _step_off_stand(turn, role, claimed, commands)
        commands[role.unit_id] = collect_command(pos)
        claimed.add(pos)
        return True
    if _walk_adjacent(turn, role, pos, claimed, commands):
        return True
    _note_direction_blocked(turn, role, pos, claimed)
    turn.note(
        f"角色 {role.unit_id} 无法走向矿 ({pos.x},{pos.y})，保留工单"
    )
    return False


def _execute_shop(
    turn: World,
    role: Unit,
    job: Job,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    gold_left: int,
    memory,
) -> int:
    shop = turn.weapon_shop_pos()
    want = job.name or _wanted_item(turn, role, gold_left, memory)
    if shop is None or want is None or role.backpack_full:
        return gold_left
    if turn.adjacent_to_zone(role, shop):
        commands[role.unit_id] = buy_command(want, 1)
        item = turn.shop_item(want)
        if item:
            gold_left -= item.price
        return gold_left
    _walk_adjacent(turn, role, shop, claimed, commands)
    return gold_left


def _execute_sell(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    walls_missing: list[Pos],
) -> bool:
    vendor = turn.vendor()
    keep_stone = 1 if walls_missing and _wall_phase(turn) else 0
    ore = _sellable_ore(role, turn, keep_stone)
    if vendor is None or ore is None:
        return False
    if turn.adjacent_to_zone(role, vendor):
        name, num = ore
        commands[role.unit_id] = sell_command(name, num)
        return True
    return _walk_adjacent(turn, role, vendor, claimed, commands)


def _pick_mine(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    extra: set[Pos] | frozenset[Pos] | tuple = (),
    memory=None,
) -> tuple[Pos, str] | None:
    """领取层选矿。已经贴着锁定矿时不得改去更贵的远矿。"""
    blocked = set(claimed)
    blocked.update(extra)
    if memory is not None:
        job = get_job(memory, role.unit_id)
        if (
            job is not None
            and job.kind == KIND_MINE
            and job.target is not None
            and job.target not in blocked
        ):
            mines = dict(turn.all_mines())
            kind = mines.get(job.target)
            if kind and (
                role.pos == job.target or turn.adjacent_to_zone(role, job.target)
            ):
                return job.target, kind
    ranked = sorted(
        (
            (pos, kind) for pos, kind in turn.all_mines()
            if pos not in blocked
        ),
        key=lambda item: _mine_rank(turn, role, item),
    )
    failed = turn.last_ok(role.unit_id) is False
    for pos, kind in ranked:
        if failed and distance(role.pos, pos) <= 1:
            continue
        return pos, kind
    return None


def _mine(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    extra: set[Pos] | frozenset[Pos] | tuple = (),
    memory=None,
) -> tuple[Pos, str] | None:
    blocked = set(claimed)
    blocked.update(extra)
    if role.backpack_full:
        extra_item = next(
            (
                item for item in role.backpack
                if item.lower() not in ("stone", "iron", "copper")
                and "voucher" not in item.lower()
            ),
            None,
        )
        if extra_item and turn.vendor() is None:
            commands[role.unit_id] = drop_command(extra_item)
            return (role.pos, extra_item)
        vendor = turn.vendor()
        if vendor is not None:
            if _walk_adjacent(turn, role, vendor, claimed, commands):
                return (vendor, "sell")
            return None
        return None
    picked = _pick_mine(turn, role, claimed, extra=extra, memory=memory)
    if picked is None:
        return None
    pos, kind = picked
    job = Job(kind=KIND_MINE, target=pos, name=kind)
    if _execute_mine(turn, role, job, claimed, commands):
        return (pos, kind)
    return None


def _build_or_walk(
    turn: World,
    role: Unit,
    target: Pos,
    name: str,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if (
        role.pos != target
        and distance(role.pos, target) <= 1
        and not _must_leave_stand(turn, role)
    ):
        owner = _PLANNED_BUILDS.get(target)
        if owner is not None and owner != role.unit_id:
            return False
        commands[role.unit_id] = build_command(target, name)
        claimed.add(target)
        if name in TOWER_TYPES:
            _PLANNED_BUILDS[target] = role.unit_id
        return True
    if role.pos == target:
        return _step_off_current(turn, role, claimed, commands)
    step = _step_toward(turn, role, target, claimed)
    if step is not None and step != role.pos:
        commands[role.unit_id] = move_command(step)
        claimed.add(step)
        return True
    if _must_leave_stand(turn, role):
        return _step_off_stand(turn, role, claimed, commands)
    step = _sidestep(turn, role, target, claimed, _move_avoid(turn, target))
    if step is None:
        return False
    claimed.add(step)
    commands[role.unit_id] = move_command(step)
    return True


def _walk_adjacent(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if role.pos != target and distance(role.pos, target) <= 1:
        turn.note(
            f"角色 {role.unit_id} 已贴着 ({target.x},{target.y})，"
            "下一步应 collect/build，不是走向失败"
        )
        return False
    step = _step_toward(turn, role, target, claimed)
    if step is None or step == role.pos:
        _note_direction_blocked(turn, role, target, claimed)
        return False
    commands[role.unit_id] = move_command(step)
    return True


def _sidestep(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    avoid: set[Pos],
) -> Pos | None:
    """换向或绕开站位之后仍要迈出一格。不踩炮位、站位和上一次失败步。"""
    blocked = set(turn.blocked(role))
    blocked.update(avoid)
    choices: list[Pos] = []
    for dx, dy in _NEIGHBOUR_STEPS:
        pos = Pos(role.pos.x + dx, role.pos.y + dy)
        if pos in claimed or pos in blocked or not turn.land(pos) or pos == role.pos:
            continue
        choices.append(pos)
    rec = _block_record(role.unit_id)
    if rec.failed_step is not None:
        kept = [pos for pos in choices if pos != rec.failed_step]
        if kept:
            choices = kept
    if rec.avoid:
        kept = [pos for pos in choices if pos not in rec.avoid]
        if kept:
            choices = kept
    if not choices:
        return None
    current = distance(role.pos, target)
    closer = [pos for pos in choices if distance(pos, target) < current]
    pool = closer or choices
    return min(pool, key=lambda pos: (distance(pos, target), pos.x, pos.y))


def _step_toward(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    *,
    inside_only: bool = False,
) -> Pos | None:
    # 人站上还没建成的火箭格就建不了,白天也不把站位当施工落脚点。
    avoid_set = _move_avoid(turn, target)
    rec = _block_record(role.unit_id)
    avoid_set.update(rec.avoid)
    if distance(role.pos, target) > 1:
        changed, alt = reroute_if_blocked_two_turns(
            turn, role, target, claimed, avoid_set, None,
        )
        if changed:
            if alt is None or alt in claimed or alt == role.pos:
                alt = _sidestep(turn, role, target, claimed, avoid_set)
            if alt is None or alt in claimed or alt == role.pos:
                alt = _sidestep(turn, role, target, claimed, set())
            if alt is not None and alt not in claimed and alt != role.pos:
                claimed.add(alt)
                rec.pending_goal = target
                return alt
            _note_direction_blocked(turn, role, target, claimed)
            turn.note(
                f"角色 {role.unit_id} 连续两回合被堵住，换向仍须移动，"
                f"但 ({target.x},{target.y}) 周围无合法落脚"
            )
            return None
    avoid = avoid_set or None
    pads = set(_tower_sites(turn))
    if target in pads and _day_avoids_stand(turn):
        best: tuple[tuple, Pos] | None = None
        for limit in (distance(role.pos, target) + _DETOUR_SLACK, 10**6):
            best = None
            for stand in _stand_cells(turn, role, target, claimed, inside_only):
                if stand == role.pos or stand in avoid_set or stand in pads:
                    continue
                picked = _straight_step(turn, role, stand, avoid_set, claimed)
                if picked is None or picked[0] > limit or picked[1] in avoid_set:
                    continue
                cost, step = picked
                rank = (
                    cost,
                    distance(role.pos, stand),
                    abs(stand.x - role.pos.x) + abs(stand.y - role.pos.y),
                    stand.x,
                    stand.y,
                )
                if best is None or rank < best[0]:
                    best = (rank, step)
            if best is not None:
                break
        if best is not None:
            rec.pending_goal = target
            claimed.add(best[1])
            return best[1]
        step = _sidestep(turn, role, target, claimed, avoid_set)
        if step is not None:
            rec.pending_goal = target
            claimed.add(step)
            return step
        _note_direction_blocked(turn, role, target, claimed)
        turn.note(
            f"角色 {role.unit_id} 无法走向 ({target.x},{target.y})："
            "周围落脚点被占、越界、或被建筑/中立单位/机器人挡住"
        )
        return None
    for stand in _stand_cells(turn, role, target, claimed, inside_only):
        if stand == role.pos:
            return None
        if avoid is not None and stand in avoid:
            continue
        step = next_step(turn, role, stand, avoid)
        if step is None or step in claimed or step in avoid_set:
            continue
        rec.pending_goal = target
        claimed.add(step)
        return step
    _note_direction_blocked(turn, role, target, claimed)
    turn.note(
        f"角色 {role.unit_id} 无法走向 ({target.x},{target.y})："
        "周围落脚点被占、越界、或被建筑/中立单位/机器人挡住"
    )
    return None


def _stand_cells(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    inside_only: bool = False,
) -> list[Pos]:
    station = turn.station()
    footprint = station_footprint(station.pos) if station else ()
    blocked = turn.blocked(role)
    avoid = _gun_stand(turn) if _day_avoids_stand(turn) else None
    cells = [
        pos for pos in _neighbours(target)
        if turn.land(pos)
        and pos not in blocked
        and pos != avoid
        and (pos == role.pos or pos not in claimed)
        and (
            not inside_only
            or _footprint_distance(pos, footprint) <= 1
        )
    ]
    cells.sort(key=lambda pos: (_footprint_distance(pos, footprint), pos.x, pos.y))
    return cells


def _map_center(turn: World) -> Pos:
    return Pos(turn.width // 2, turn.height // 2)


def _battery_cells(turn: World) -> tuple[tuple[Pos, ...], Pos | None]:
    """背敌口袋:三门火箭加中间一格空地,整组翻到基地外侧、靠近地图边缘。

    相对形状与原先朝敌口袋相同,只沿基地中线水平翻转。最外侧仍在切比雪夫距离 2。
    挑战者(来敌朝东,炮在西侧)相对基地左上角:
        火箭 基地 基地
        空地 基地 基地
        火箭 火箭 空地 空地
    防守者(来敌朝西,炮在东侧)是同一形状翻到东侧。人必须站在那格空地上,才能同时挨到三门炮。
    """
    station = turn.station()
    if station is None:
        return (), None
    sx, sy = station.pos.x, station.pos.y
    if _center_facing_east(turn):
        rockets = (Pos(sx - 1, sy), Pos(sx - 1, sy - 2), Pos(sx - 2, sy - 2))
        stand = Pos(sx - 1, sy - 1)
    else:
        rockets = (Pos(sx + 2, sy - 1), Pos(sx + 2, sy + 1), Pos(sx + 3, sy + 1))
        stand = Pos(sx + 2, sy)
    footprint = set(station_footprint(station.pos))
    rockets = tuple(
        pos for pos in rockets if turn.land(pos) and pos not in footprint
    )
    if not turn.land(stand) or stand in footprint:
        stand = None
    return rockets, stand


def _tower_sites(turn: World) -> tuple[Pos, ...]:
    rockets, _stand = _battery_cells(turn)
    return rockets


def _gun_stand(turn: World) -> Pos | None:
    _rockets, stand = _battery_cells(turn)
    return stand


def _pick_safe_hold(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    extra: set[Pos],
) -> Pos | None:
    """安全区里靠近基地背后的空地。不进中央，不占炮手站位。"""
    span = safe_zone_bounds(turn)
    anchor = _base_back_anchor(turn)
    if span is None or anchor is None:
        return None
    left, right, bottom, top = span
    blocked = set(claimed)
    blocked.update(extra)
    blocked.update(_tower_sites(turn))
    stand = _gun_stand(turn)
    if stand is not None:
        blocked.add(stand)
    station = turn.station()
    footprint = set(station_footprint(station.pos)) if station is not None else set()
    options: list[Pos] = []
    for x in range(left, right + 1):
        for y in range(bottom, top + 1):
            pos = Pos(x, y)
            if pos in blocked or pos in footprint:
                continue
            if not turn.land(pos):
                continue
            if pos != role.pos and pos in turn.blocked(role):
                continue
            options.append(pos)
    if not options:
        return None
    return min(
        options,
        key=lambda pos: (distance(pos, anchor), distance(role.pos, pos), pos.x, pos.y),
    )


def _park_in_safe_zone(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    memory,
) -> bool:
    """安全区没有可挖的矿时，停在靠近基地背后的空地。"""
    taken = claimed_targets(memory, role.unit_id)
    job = get_job(memory, role.unit_id)
    if (
        job is not None
        and job.kind == KIND_HOLD
        and job.target is not None
        and in_night_safe_zone(turn, job.target)
        and job.target not in taken
        and (job.target == role.pos or job.target not in turn.blocked(role))
    ):
        target = job.target
    else:
        target = _pick_safe_hold(turn, role, claimed, taken)
        if target is None:
            return False
        _keep_job(memory, role, KIND_HOLD, target=target, round_no=turn.round_no)
    if role.pos == target:
        claimed.add(target)
        return True
    return _walk_onto(
        turn, role, target, claimed, commands, avoid=_battery_block(turn),
    )


def _station_bounds(turn: World) -> tuple[int, int, int, int] | None:
    station = turn.station()
    if station is None:
        return None
    footprint = station_footprint(station.pos)
    xs = [pos.x for pos in footprint]
    ys = [pos.y for pos in footprint]
    return min(xs), max(xs), min(ys), max(ys)


def _wall_ring(turn: World) -> tuple[Pos, ...]:
    """基地占地切比雪夫距离 2 的完整一圈可建墙格(黄区外圈)。"""
    station = turn.station()
    if station is None:
        return ()
    return tuple(
        pos for pos in _cells_at_distance(station.pos, 2) if turn.land(pos)
    )


def _center_facing_east(turn: World) -> bool:
    """来敌方向只看左右:中心在基地东侧则砌东半圈,否则砌西半圈。"""
    bounds = _station_bounds(turn)
    if bounds is None:
        return True
    xmin, xmax, _, _ = bounds
    return _map_center(turn).x >= (xmin + xmax) / 2


def _center_facing_sides(turn: World) -> frozenset[str]:
    if _station_bounds(turn) is None:
        return frozenset()
    return frozenset({"east" if _center_facing_east(turn) else "west"})


def _on_incoming_side(pos: Pos, turn: World) -> bool:
    bounds = _station_bounds(turn)
    if bounds is None:
        return False
    xmin, xmax, _, _ = bounds
    mid_x = (xmin + xmax) / 2
    if _center_facing_east(turn):
        return pos.x > mid_x
    return pos.x < mid_x


def _ring_clockwise(turn: World) -> tuple[Pos, ...]:
    """围墙外圈按顺时针走一圈。y 向上时:上边向东,右边向南,下边向西,左边向北。"""
    ring = set(_wall_ring(turn))
    if not ring:
        return ()
    minx = min(pos.x for pos in ring)
    maxx = max(pos.x for pos in ring)
    miny = min(pos.y for pos in ring)
    maxy = max(pos.y for pos in ring)
    path: list[Pos] = []
    for x in range(minx, maxx + 1):
        path.append(Pos(x, maxy))
    for y in range(maxy - 1, miny - 1, -1):
        path.append(Pos(maxx, y))
    for x in range(maxx - 1, minx - 1, -1):
        path.append(Pos(x, miny))
    for y in range(miny + 1, maxy):
        path.append(Pos(minx, y))
    ordered: list[Pos] = []
    seen: set[Pos] = set()
    for pos in path:
        if pos in ring and pos not in seen:
            ordered.append(pos)
            seen.add(pos)
    for pos in ring:
        if pos not in seen:
            ordered.append(pos)
    return tuple(ordered)


def _contiguous_arc(ring: tuple[Pos, ...], keep: set[Pos]) -> tuple[Pos, ...]:
    """从缺口后面开始,沿环取出连续的一段。"""
    if not ring:
        return ()
    flags = [pos in keep for pos in ring]
    if not any(flags):
        return ()
    if all(flags):
        return ring
    n = len(ring)
    start = None
    for index, flag in enumerate(flags):
        if flag and not flags[(index - 1) % n]:
            start = index
            break
    if start is None:
        return ()
    arc: list[Pos] = []
    index = start
    while flags[index]:
        arc.append(ring[index])
        index = (index + 1) % n
        if len(arc) >= n:
            break
    return tuple(arc)


def _wall_order(turn: World) -> tuple[Pos, ...]:
    """朝向地图中心的半圈围墙,沿外圈顺时针连续砌。

    来敌方向只看东西。左上挑战者砌东半圈,右下防守者砌西半圈。
    落在火箭或站位上的格子留给炮,不再砌墙;炮翻到背后后,来袭半圈里空出的旧炮格重新砌上。
    """
    ring = _ring_clockwise(turn)
    incoming = {pos for pos in ring if _on_incoming_side(pos, turn)}
    if not incoming:
        center = _map_center(turn)
        cells = list(_wall_ring(turn))
        cells.sort(key=lambda pos: (distance(pos, center), pos.x, pos.y))
        quota = max(1, (len(cells) + 1) // 2) if cells else 0
        return tuple(cells[:quota])
    arc = _contiguous_arc(ring, incoming)
    blocked = set(_tower_sites(turn))
    stand = _gun_stand(turn)
    if stand is not None:
        blocked.add(stand)
    return tuple(pos for pos in arc if pos not in blocked)


def _cells_at_distance(station_pos: Pos, radius: int) -> tuple[Pos, ...]:
    footprint = station_footprint(station_pos)
    xs = [pos.x for pos in footprint]
    ys = [pos.y for pos in footprint]
    cells = []
    for x in range(min(xs) - radius, max(xs) + radius + 1):
        for y in range(min(ys) - radius, max(ys) + radius + 1):
            pos = Pos(x, y)
            if pos in footprint:
                continue
            if _footprint_distance(pos, footprint) == radius:
                cells.append(pos)
    return tuple(cells)


def _footprint_distance(pos: Pos, footprint: tuple[Pos, ...]) -> int:
    if not footprint:
        return 0
    return min(distance(pos, cell) for cell in footprint)


def _neighbours(pos: Pos) -> tuple[Pos, ...]:
    return tuple(
        Pos(pos.x + dx, pos.y + dy) for dx, dy in _NEIGHBOUR_STEPS
    )


def _adjacent_building(turn: World, role: Unit, unit: Unit) -> bool:
    return any(
        role.pos != cell and distance(role.pos, cell) <= 1
        for cell in turn.footprint(unit)
    )


def _wall_max(level: int) -> int:
    return {1: 1000, 2: 1500, 3: 2000}.get(max(level, 1), 1000)


def _sellable_ore(
    role: Unit, turn: World, keep_stone: int,
) -> tuple[str, int] | None:
    best: tuple[int, str, int] | None = None
    for name in ("copper", "iron", "stone"):
        have = count_item(role, name)
        if name == "stone":
            have -= keep_stone
        if have <= 0:
            continue
        actual = backpack_item(role, name)
        if actual is None:
            continue
        price = turn.vendor_price(name)
        score = (price, have)
        if best is None or score > (best[0], best[2]):
            best = (price, actual, have)
    if best is None:
        return None
    return best[1], best[2]


def num_ores(role: Unit) -> int:
    return sum(count_item(role, name) for name in ("stone", "iron", "copper"))
