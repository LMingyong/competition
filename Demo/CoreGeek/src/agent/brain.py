from typing import Any

from .combat import attack_positions, bomb_center
from .debuglog import set_extra, write_round_log
from .grid import next_step
from .jobs import (
    KIND_ACCEPT,
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
    get_job,
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
    next_task_command,
    observe,
    should_ask_model,
    task_prompt,
    treasure_prompt,
    treasure_ready,
)
from .world import HERO_MAX_HP, World, backpack_item, count_item

TOWER_LOADOUT = ("rocket", "rocket", "rocket")
STONE_KEEP = 4
RECALL_ROUNDS = 5
RECALL_FROM = DAY_ROUNDS - RECALL_ROUNDS + 1
WALL_FROM = 30
EDGE_MINE_MAX = 4
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


def decide(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    turn = World.load(payload)
    memory = observe(turn)
    assign_roles(turn, memory)
    commands: dict[int, dict[str, Any]] = {}
    prompt = ""
    execute_cmd = ""
    if turn.is_day:
        execute_cmd, prompt = _day(turn, memory, commands)
    else:
        execute_cmd, prompt = _night(turn, memory, commands)
    _fill_idle(turn, memory, commands)
    scan_idle(turn, commands)
    set_extra(prompt, execute_cmd)
    write_round_log(turn, commands, prompt, execute_cmd)
    return {str(key): value for key, value in commands.items()}


def _day(
    turn: World,
    memory,
    commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    sites = _tower_sites(turn)
    order = _wall_order(turn)
    standing_towers = {unit.pos for unit in turn.weapons()}
    standing_walls = {unit.pos for unit in turn.walls()}
    occupied = turn.occupied_for_build()
    towers_missing = [pos for pos in sites if pos not in standing_towers]
    walls_missing = [pos for pos in order if pos not in standing_walls]
    free_towers = [pos for pos in towers_missing if pos not in occupied]
    free_walls = [pos for pos in walls_missing if pos not in occupied]
    claimed: set[Pos] = set()
    gold_left = turn.gold
    builds_left = max(0, 3 - len(turn.weapons()))
    execute_cmd = ""
    prompt = ""

    pioneer = turn.pioneer()
    if pioneer is not None:
        execute_cmd, prompt = _pioneer_day(
            turn, pioneer, memory, claimed, commands,
        )

    for role in turn.workers():
        if role.unit_id in commands:
            continue
        gold_left, builds_left = _worker_day(
            turn, role, sites, free_towers, free_walls, claimed,
            commands, gold_left, builds_left, memory,
        )
    return execute_cmd, prompt


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
    if _try_heal(role, commands):
        return gold_left, builds_left
    if _try_upgrade_or_fix(turn, role, commands):
        return gold_left, builds_left
    upgrade = _next_upgrade(turn, role)
    if upgrade is not None:
        _, target = upgrade
        if not _adjacent_building(turn, role, target):
            step = _step_toward(turn, role, target.pos, claimed)
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return gold_left, builds_left
    if _should_home(turn, role, memory):
        if walls_missing and count_item(role, WALL_MATERIAL):
            for site in list(walls_missing):
                if site in claimed or distance(role.pos, site) > 1 or role.pos == site:
                    continue
                if _build_or_walk(turn, role, site, WALL, claimed, commands):
                    if (
                        role.unit_id in commands
                        and commands[role.unit_id]["action"] == "build"
                    ):
                        walls_missing.remove(site)
                    return gold_left, builds_left
        _keep_job(
            memory, role, KIND_RECALL, target=_recall_target(turn),
            round_no=turn.round_no,
        )
        _recall_to_tower(turn, role, claimed, commands)
        return gold_left, builds_left

    # 09:00 逻辑:两名工人都去建塔/走近塔,不按 builder/miner 拆开。
    # 优先建前两个塔位，第三个只有在前面两个都满了之后才建。
    # 三塔齐后先采矿换钱、能升塔就升塔; 第 30 回合起砌墙,第一天把墙建齐。
    # 建完后升级顺序:武器 > 朝向敌人的围墙(从地图中心向外) > 其余围墙 > 基地。
    num_standing_towers = len(turn.weapons())
    if towers_missing and gold_left >= WEAPON_BUILD_COST and builds_left > 0:
        for index, site in enumerate(sites):
            if site not in towers_missing or site in claimed:
                continue
            if num_standing_towers < 2 and index >= 2:
                continue
            if _build_or_walk(
                turn, role, site, TOWER_LOADOUT[index], claimed, commands,
            ):
                if (
                    role.unit_id in commands
                    and commands[role.unit_id]["action"] == "build"
                ):
                    gold_left -= WEAPON_BUILD_COST
                    builds_left -= 1
                    if site in towers_missing:
                        towers_missing.remove(site)
                    clear_job(memory, role.unit_id)
                else:
                    _keep_job(
                        memory, role, KIND_TOWER, target=site,
                        name=TOWER_LOADOUT[index], round_no=turn.round_no,
                    )
                return gold_left, builds_left
    shopped = _try_weapon_upgrade_shop(
        turn, role, claimed, commands, gold_left, memory,
    )
    if shopped is not None:
        return shopped, builds_left
    if walls_missing and _wall_phase(turn):
        if _build_walls(turn, role, walls_missing, claimed, commands, memory):
            return gold_left, builds_left

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


def _in_recall(turn: World) -> bool:
    return bool(turn.is_day) and turn.round_in_day >= RECALL_FROM


def _wall_phase(turn: World) -> bool:
    """开局前 29 回合只建塔/经营; 第 30 回合起才进入砌墙阶段。"""
    return turn.round_no >= WALL_FROM


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
    return _in_recall(turn)


def _should_edge_mine(turn: World, role: Unit, memory) -> bool:
    return False


def _edge_distance(turn: World, pos: Pos) -> int:
    return min(pos.x, pos.y, turn.width - 1 - pos.x, turn.height - 1 - pos.y)


def _is_edge_mine(turn: World, pos: Pos) -> bool:
    return _edge_distance(turn, pos) <= EDGE_MINE_MAX


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
        if pos in blocked:
            continue
        if kind in ("copper", "iron"):
            metals.append((pos, kind))
        else:
            others.append((pos, kind))
    pool = metals or others
    if not pool:
        return None
    center = _map_center(turn)
    return min(
        pool,
        key=lambda item: (
            _edge_distance(turn, item[0]),
            -turn.vendor_price(item[1]),
            -distance(item[0], center),
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
    """回防窗口与黑夜:矿工只在地图边缘采矿,不穿过中央去小贩。"""
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
        and _is_edge_mine(turn, job.target)
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
        return prev
    started = round_no
    if prev is not None and prev.kind == kind and prev.tower_id == tower_id and prev.name == name:
        started = prev.started or round_no
    return set_job(
        memory,
        role.unit_id,
        Job(
            kind=kind,
            target=target,
            name=name,
            started=started,
            fail_streak=prev.fail_streak if prev and prev.kind == kind else 0,
            tower_id=tower_id,
        ),
    )


def _recall_target(turn: World) -> Pos | None:
    weapons = turn.weapons()
    if weapons:
        return weapons[0].pos
    station = turn.station()
    return station.pos if station is not None else None


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
        if role.backpack_full or num_ores(role) >= SELL_THRESHOLD:
            return False
        mines = dict(turn.all_mines())
        if job.target is None or job.target not in mines:
            return False
        if job.name and mines[job.target] != job.name:
            return False
        want = _wanted_item(turn, role, gold_left, memory)
        if _is_weapon_upgrade(want):
            return False
        if (
            walls_missing
            and _wall_phase(turn)
            and is_builder(memory, role.unit_id)
            and mines[job.target] != WALL_MATERIAL
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
        _recall_to_tower(turn, role, claimed, commands)
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
    )
    if dest is not None:
        pos, kind = dest
        _keep_job(
            memory, role, KIND_MINE, target=pos, name=kind,
            round_no=turn.round_no,
        )
    return gold_left, builds_left


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
    if job.target is None:
        return gold_left, builds_left
    if _build_or_walk(turn, role, job.target, job.name, claimed, commands):
        if role.unit_id in commands and commands[role.unit_id]["action"] == "build":
            gold_left -= WEAPON_BUILD_COST
            builds_left -= 1
            if job.target in towers_missing:
                towers_missing.remove(job.target)
            clear_job(memory, role.unit_id)
    return gold_left, builds_left


def _recall_to_tower(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
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
) -> bool:
    """墙未建齐时,优先负责建墙:先采/走向石头矿,有石头则建墙。"""
    stones = count_item(role, WALL_MATERIAL)
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
        for site in sites:
            if site in claimed:
                continue
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
    if turn.phase_task:
        _keep_job(memory, role, KIND_PHASE, round_no=turn.round_no)
        return _run_task(turn, role, memory, claimed, commands)
    if _try_heal(role, commands):
        return "", _maybe_prompt(turn, memory)
    if _should_home(turn, role, memory):
        _keep_job(
            memory, role, KIND_RECALL, target=_recall_target(turn),
            round_no=turn.round_no,
        )
        _recall_to_tower(turn, role, claimed, commands)
        return "", _maybe_prompt(turn, memory)
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
    step_before = memory.task_step
    last_before = memory.last_execute
    wait_before = memory.model_wait
    execute_cmd, answer = next_task_command(turn, memory)
    if answer:
        commands[role.unit_id] = submit_answer_command(answer)
        if not _near_own_task(turn, role):
            execute_cmd = ""
    elif not _near_own_task(turn, role):
        # 人还没站到任务点，这条沙盒命令发不出去，不能把一次性求解的进度记掉。
        memory.task_step = step_before
        memory.last_execute = last_before
        memory.model_wait = wait_before
        _walk_to_task(turn, role, claimed, commands)
        execute_cmd = ""
    prompt = ""
    moving = commands.get(role.unit_id, {}).get("action") == "move"
    if should_ask_model(turn, execute_cmd, answer) and not moving:
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
        _recall_to_tower(turn, role, claimed, commands)
        return "", _maybe_prompt(turn, memory)
    return "", _maybe_prompt(turn, memory)


def _sticky_weapon_pairs(
    heroes: list[Unit],
    weapons: tuple[Unit, ...],
    memory,
    turn: World,
) -> list[tuple[Unit, Unit]]:
    pairs: list[tuple[Unit, Unit]] = []
    used_h: set[int] = set()
    used_w: set[int] = set()
    tower_map = {tower.unit_id: tower for tower in weapons}
    for hero in heroes:
        job = get_job(memory, hero.unit_id)
        if job is None or job.kind != KIND_MAN_TOWER:
            continue
        tower = tower_map.get(job.tower_id)
        if tower is None or tower.unit_id in used_w:
            continue
        pairs.append((hero, tower))
        used_h.add(hero.unit_id)
        used_w.add(tower.unit_id)
    rest_h = [hero for hero in heroes if hero.unit_id not in used_h]
    rest_w = tuple(tower for tower in weapons if tower.unit_id not in used_w)
    pairs.extend(_assign_weapons(rest_h, rest_w))
    for hero, tower in pairs:
        prev = get_job(memory, hero.unit_id)
        if (
            prev is not None
            and prev.kind == KIND_MAN_TOWER
            and prev.tower_id == tower.unit_id
        ):
            prev.target = tower.pos
            continue
        set_job(
            memory,
            hero.unit_id,
            Job(
                kind=KIND_MAN_TOWER,
                target=tower.pos,
                tower_id=tower.unit_id,
                started=turn.round_no,
            ),
        )
    return pairs


def _night(
    turn: World, memory, commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    claimed: set[Pos] = set()
    busy: set[int] = set()
    execute_cmd = ""
    prompt = ""
    if turn.phase_task:
        pioneer = turn.pioneer()
        if pioneer is not None:
            execute_cmd, answer = next_task_command(turn, memory)
            if answer:
                commands[pioneer.unit_id] = submit_answer_command(answer)
                execute_cmd = ""
            # 不把开拓者标成忙碌：黑夜必须继续操塔。任务 prompt 也不发，避免 503 丢掉开火。
    for role in turn.controllable():
        if role.unit_id in busy:
            continue
        if _try_heal(role, commands):
            busy.add(role.unit_id)
            continue
        if _try_night_item(turn, role, commands):
            busy.add(role.unit_id)
    free = [role for role in turn.controllable() if role.unit_id not in busy]
    for role, tower in _sticky_weapon_pairs(free, turn.weapons(), memory, turn):
        if distance(role.pos, tower.pos) <= 1:
            if tower.cooldown > 0:
                turn.note(
                    f"武器 {tower.unit_id} 仍在冷却 cooldown={tower.cooldown}，本回合不能 attack"
                )
                continue
            targets = attack_positions(turn, tower)
            if targets:
                commands[tower.unit_id] = attack_commands(role.unit_id, targets)
            else:
                turn.note(
                    f"角色 {role.unit_id} 已贴塔 {tower.unit_id} 待命（射程内无目标）"
                )
            continue
        step = _step_toward(turn, role, tower.pos, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
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


def _fill_idle(turn: World, memory, commands: dict[int, dict[str, Any]]) -> None:
    """真正没事做的英雄走近最近的塔,避免站桩。贴塔开火/任务点待命的不算空闲。"""
    claimed: set[Pos] = set()
    for command in commands.values():
        if command.get("action") != "move":
            continue
        raw = (command.get("targetPos") or [None])[0]
        if isinstance(raw, dict):
            claimed.add(Pos.load(raw))
    busy = set(commands) | _controller_ids(commands)
    for role in turn.controllable():
        if role.unit_id in busy:
            continue
        if any(distance(role.pos, tower.pos) <= 1 for tower in turn.weapons()):
            continue
        if turn.is_day and role.kind == "pioneer" and _near_own_task(turn, role):
            continue
        _recall_to_tower(turn, role, claimed, commands)


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
        key=lambda item: (
            -turn.vendor_price(item[1]),
            distance(role.pos, item[0]),
            item[0].x,
            item[0].y,
        ),
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
    if turn.adjacent_to_zone(role, pos):
        commands[role.unit_id] = collect_command(pos)
        claimed.add(pos)
        return True
    return _walk_adjacent(turn, role, pos, claimed, commands)


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


def _mine(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    extra: set[Pos] | frozenset[Pos] | tuple = (),
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
    ranked = sorted(
        (
            (pos, kind) for pos, kind in turn.all_mines()
            if pos not in blocked
        ),
        key=lambda item: (
            -turn.vendor_price(item[1]),
            distance(role.pos, item[0]),
            item[0].x,
            item[0].y,
        ),
    )
    failed = turn.last_ok(role.unit_id) is False
    for pos, kind in ranked:
        if failed and distance(role.pos, pos) <= 1:
            continue
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
    if role.pos != target and distance(role.pos, target) <= 1:
        commands[role.unit_id] = build_command(target, name)
        claimed.add(target)
        return True
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _walk_adjacent(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if role.pos != target and distance(role.pos, target) <= 1:
        return False
    step = _step_toward(turn, role, target, claimed)
    if step is None:
        return False
    commands[role.unit_id] = move_command(step)
    return True


def _step_toward(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    *,
    inside_only: bool = False,
) -> Pos | None:
    for stand in _stand_cells(turn, role, target, claimed, inside_only):
        if stand == role.pos:
            return None
        step = next_step(turn, role, stand)
        if step is None or step in claimed:
            continue
        claimed.add(step)
        return step
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
    cells = [
        pos for pos in _neighbours(target)
        if turn.land(pos)
        and pos not in blocked
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


def _tower_sites(turn: World) -> tuple[Pos, ...]:
    station = turn.station()
    if station is None:
        return ()
    footprint = station_footprint(station.pos)
    center = _map_center(turn)
    cells = [
        pos for pos in _cells_at_distance(station.pos, 1) if turn.land(pos)
    ]
    station_x = station.pos.x
    front_half = [p for p in cells if p.x >= station_x]
    back_half = [p for p in cells if p.x < station_x]
    front_half.sort(key=lambda pos: (distance(pos, center), pos.x, pos.y))
    back_half.sort(key=lambda pos: (distance(pos, center), pos.x, pos.y))
    result = []
    while len(result) < 3 and (front_half or back_half):
        if len(result) < 2 and front_half:
            result.append(front_half.pop(0))
        elif back_half:
            result.append(back_half.pop(0))
        elif front_half:
            result.append(front_half.pop(0))
    return tuple(result)


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


def _wall_order(turn: World) -> tuple[Pos, ...]:
    """第一天目标墙位:按左右朝向地图中心的约一半围墙,由近到远建造。

    来敌方向只看东西。左上挑战者砌东半圈,右下防守者砌西半圈;
    南北边只保留靠中心的那一半,远离中心的半圈留作出入口。
    """
    center = _map_center(turn)
    chosen = [pos for pos in _wall_ring(turn) if _on_incoming_side(pos, turn)]
    if not chosen:
        ring = list(_wall_ring(turn))
        ring.sort(key=lambda pos: (distance(pos, center), pos.x, pos.y))
        quota = max(1, (len(ring) + 1) // 2) if ring else 0
        return tuple(ring[:quota])
    chosen.sort(key=lambda pos: (distance(pos, center), pos.x, pos.y))
    return tuple(chosen)


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


def _assign_weapons(
    heroes: list[Unit], weapons: tuple[Unit, ...],
) -> list[tuple[Unit, Unit]]:
    pairs: list[tuple[Unit, Unit]] = []
    used_h: set[int] = set()
    used_w: set[int] = set()
    options = sorted(
        (
            (distance(hero.pos, tower.pos), hero.unit_id, tower.unit_id)
            for hero in heroes
            for tower in weapons
        )
    )
    hero_map = {hero.unit_id: hero for hero in heroes}
    tower_map = {tower.unit_id: tower for tower in weapons}
    for _, hid, tid in options:
        if hid in used_h or tid in used_w:
            continue
        used_h.add(hid)
        used_w.add(tid)
        pairs.append((hero_map[hid], tower_map[tid]))
    return pairs


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
