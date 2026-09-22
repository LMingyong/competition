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
    task_prompt,
    treasure_prompt,
    treasure_ready,
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
    _keep_pioneer_on_task(turn, memory, commands)
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
    if _should_home(turn, role, memory):
        _recall_to_tower(turn, role, claimed, commands, memory)
        return gold_left, builds_left
    # 回防窗口优先于已锁的白天工单,但只拉走炮手。
    # 另外两人改去边缘采矿,不继续往敌人一侧砌墙,也不占站位。
    if _should_edge_mine(turn, role, memory):
        _run_day_edge(turn, role, claimed, commands, memory)
        return gold_left, builds_left

    # 最外侧火箭在站位上建失败时,不要死站着空过,改去采石或砌已能砌的墙。
    if _outer_build_failed(turn, role, towers_missing):
        job = get_job(memory, role.unit_id)
        if job is not None and job.kind == KIND_TOWER:
            clear_job(memory, role.unit_id)
        if _recover_failed_outer(
            turn, role, walls_missing, claimed, commands, memory,
        ):
            return gold_left, builds_left

    # 已派的单子没做完就继续:走到同一格,到了才 collect/build。
    # 金币变了、旁边出现更贵的矿,都不换目标,避免每回合重新寻路。
    # 例外:锁住的矿还要走超过 8 格,且 8 格内另有能挖的矿,可以改去近的。
    continued = _continue_locked_job(
        turn, role, sites, towers_missing, walls_missing, claimed,
        commands, gold_left, builds_left, memory,
    )
    if continued is not None:
        return continued

    upgrade = _next_upgrade(turn, role)
    if upgrade is not None:
        _, target = upgrade
        if not _adjacent_building(turn, role, target):
            step = _step_toward(turn, role, target.pos, claimed)
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return gold_left, builds_left

    # 两名工人都去建三座火箭。炮位在基地背后、靠近地图边缘:
    # 先建前两座,前两座落地再补最外侧那座。
    # 三塔齐后先采矿换钱、能升塔就升塔; 第 30 回合起沿圈连续砌墙。
    # 建完后升级顺序:武器 > 朝向敌人的围墙 > 其余围墙 > 基地。
    num_standing_towers = len(turn.weapons())
    if towers_missing and gold_left >= WEAPON_BUILD_COST and builds_left > 0:
        for index, site in enumerate(sites):
            if site not in towers_missing or site in claimed:
                continue
            if num_standing_towers < 2 and index >= 2:
                continue
            if (
                _outer_build_failed(turn, role, towers_missing)
                and site == _outer_site(turn)
            ):
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
    """入夜前的回防窗口里,只有选定的那一名炮手回家。"""
    return _in_recall(turn) and _is_gunner(turn, role, memory)


def _should_edge_mine(turn: World, role: Unit, memory) -> bool:
    """回防窗口里不操炮的两人去边缘采矿。夜里改停在基地朝敌一侧,不再下矿。"""
    if not _in_recall(turn):
        return False
    return not _is_gunner(turn, role, memory)


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
    """卖不了就停在边缘矿旁,不往地图中央走,也不去炮位。"""
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
    if role.backpack_full:
        if _sell_in_safe_zone(turn, role, claimed, commands, memory):
            return True
        return _hold_near_edge(turn, role, claimed, commands, memory)
    return _run_edge_mine(turn, role, claimed, commands, memory)


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
        if pos in blocked or not _is_edge_mine(turn, pos):
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
    return _run_worker_job(
        turn, role, job, sites, towers_missing, walls_missing,
        claimed, commands, gold_left, builds_left, memory,
    )


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
    blocked = turn.blocked(role)
    options = [
        pos for pos in _neighbours(stand)
        if turn.land(pos) and pos not in blocked and pos not in claimed
    ]
    if not options:
        return False
    stone = _nearest_mine(turn, role, WALL_MATERIAL, claimed)
    target = min(
        options,
        key=lambda pos: (
            distance(pos, stone) if stone is not None else 0,
            pos.x,
            pos.y,
        ),
    )
    return _walk_onto(turn, role, target, claimed, commands)


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
        return (
            job.target is not None
            and job.target in towers_missing
            and gold_left >= WEAPON_BUILD_COST
        )
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
            return _park_behind(turn, role, claimed, commands)
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


def _walk_onto(
    turn: World,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    """走到目标格上。已经站在上面则不再移动。"""
    if role.pos == target:
        claimed.add(target)
        return True
    step = next_step(turn, role, target)
    if step is None or step in claimed:
        turn.note(
            f"角色 {role.unit_id} 无法走上 ({target.x},{target.y})"
        )
        return False
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
        # 任务还在就不要把开拓者选成炮手，也不要停去朝敌一侧。
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
        # 夜里只有炮手占背后站位。其余人停在基地朝敌一侧,不占新火箭和新站位。
        _park_behind(turn, role, claimed, commands)
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
            if not _must_leave_stand(turn, role):
                continue
        if role.kind == "pioneer" and (
            turn.phase_task or _just_accepted(turn, role, memory)
        ):
            continue
        if turn.is_day and role.kind == "pioneer" and _near_own_task(turn, role):
            continue
        if turn.is_day and not _in_recall(turn):
            if _must_leave_stand(turn, role):
                _step_off_stand(turn, role, claimed, commands)
                continue
            if _pattern_ready(turn):
                continue
            _recall_to_tower(turn, role, claimed, commands, memory)
            continue
        if not _is_gunner(turn, role, memory):
            if not turn.is_day:
                _park_behind(turn, role, claimed, commands)
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
    if turn.adjacent_to_zone(role, pos):
        if _must_leave_stand(turn, role):
            return _step_off_stand(turn, role, claimed, commands)
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
        key=lambda item: _mine_rank(turn, role, item),
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
    if (
        role.pos != target
        and distance(role.pos, target) <= 1
        and not _must_leave_stand(turn, role)
    ):
        commands[role.unit_id] = build_command(target, name)
        claimed.add(target)
        return True
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    if _must_leave_stand(turn, role):
        return _step_off_stand(turn, role, claimed, commands)
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


def _back_cells(turn: World) -> tuple[Pos, ...]:
    """炮已在基地背后。不操炮的人停在朝向敌人的一侧,避开新火箭和新站位。"""
    station = turn.station()
    if station is None:
        return ()
    sx, sy = station.pos.x, station.pos.y
    if _center_facing_east(turn):
        raw = (Pos(sx + 2, sy), Pos(sx + 2, sy - 1))
    else:
        raw = (Pos(sx - 1, sy), Pos(sx - 1, sy - 1))
    footprint = set(station_footprint(station.pos))
    rockets, stand = _battery_cells(turn)
    blocked = set(rockets)
    if stand is not None:
        blocked.add(stand)
    return tuple(
        pos for pos in raw
        if turn.land(pos) and pos not in footprint and pos not in blocked
    )


def _park_behind(
    turn: World,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    cells = [pos for pos in _back_cells(turn) if pos == role.pos or pos not in claimed]
    if not cells:
        return False
    if role.pos in cells:
        claimed.add(role.pos)
        return True
    target = min(cells, key=lambda pos: (distance(role.pos, pos), pos.x, pos.y))
    return _walk_onto(turn, role, target, claimed, commands)


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
