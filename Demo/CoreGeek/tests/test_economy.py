"""开局经济:先建塔,再采矿升塔,第 30 回合起才砌墙。"""

from agent.brain import decide
from agent.protocol import Pos, distance

from tests.helpers import (
    GATLING,
    PIONEER,
    RAILGUN,
    ROCKET,
    WORKER_1,
    WORKER_2,
    action_of,
    drop_walls,
    fill_walls,
    fresh,
    move_pos,
    park_other_worker_building,
    place,
)

VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}

SHOP = Pos(25, 20)
COPPER_NEAR = Pos(7, 2)
STONE_NEAR = Pos(14, 3)


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS


def _opening_towers_no_walls(payload, gold: int) -> None:
    drop_walls(payload)
    payload["teamOur"]["goldNum"] = gold
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    place(payload, WORKER_2, 16, 18, backpack=[])


def test_early_day_mines_copper_while_walls_missing(make_payload):
    """回合 20、三塔已齐、墙未建、金币不够升塔:贴铜矿应 collect 铜,不去采石砌墙。"""
    payload = fresh(make_payload(roundNo=20))
    _opening_towers_no_walls(payload, gold=0)
    start = Pos(8, 2)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    response = decide(payload)
    _validate(response, payload)
    command = response.get(str(WORKER_1))
    assert command is not None
    assert command["action"] == "collect"
    target = command["targetPos"][0]
    assert (target["x"], target["y"]) == (COPPER_NEAR.x, COPPER_NEAR.y)


def test_early_day_does_not_build_wall(make_payload):
    """回合 20、工人紧邻墙位且包里有石头:仍不得建墙,先去经营。"""
    payload = fresh(make_payload(roundNo=20))
    _opening_towers_no_walls(payload, gold=0)
    place(payload, WORKER_1, 12, 21, backpack=["stone", "stone", "stone", "stone"])
    response = decide(payload)
    _validate(response, payload)
    command = response.get(str(WORKER_1))
    assert command is not None
    assert not (
        command["action"] == "build" and command.get("name") == "wall"
    ), command


def test_early_day_buys_weapon_voucher_when_gold_enough(make_payload):
    """回合 20、墙未齐但金币够:已在武器商店旁应买 WeaponUpgradeVoucher1。"""
    payload = fresh(make_payload(roundNo=20))
    _opening_towers_no_walls(payload, gold=120)
    place(payload, WORKER_1, 24, 20, backpack=[])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "buy"
    assert command.get("name") == "WeaponUpgradeVoucher1"


def test_uses_weapon_voucher_on_tower_before_walls(make_payload):
    """背包已有武器升级券且贴着 1 级塔:先 use 升塔,不建墙。"""
    payload = fresh(make_payload(roundNo=20))
    _opening_towers_no_walls(payload, gold=0)
    place(payload, WORKER_1, 8, 24, backpack=["WeaponUpgradeVoucher1"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "use"
    assert command.get("name") == "WeaponUpgradeVoucher1"


def test_after_round_30_builds_wall_when_cannot_upgrade(make_payload):
    """第 35 回合、金币不够升塔、工人墙边有石头:开始建墙。"""
    payload = fresh(make_payload(roundNo=35))
    drop_walls(payload)
    payload["teamOur"]["goldNum"] = 0
    place(payload, WORKER_1, 12, 21, backpack=["stone", "stone", "stone", "stone"])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "build"
    assert command.get("name") == "wall"


def test_after_round_30_upgrade_still_beats_wall(make_payload):
    """第 35 回合即使该砌墙,金币够升塔时仍先买武器升级券。"""
    payload = fresh(make_payload(roundNo=35))
    _opening_towers_no_walls(payload, gold=120)
    place(payload, WORKER_1, 24, 20, backpack=[])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "buy"
    assert command.get("name") == "WeaponUpgradeVoucher1"


def test_after_round_30_missing_walls_walk_to_stone(make_payload):
    """砌墙阶段墙未齐、金币不够升塔:不贴矿的工人应走向石矿。"""
    payload = fresh(make_payload(roundNo=35))
    drop_walls(payload)
    park_other_worker_building(payload)
    payload["teamOur"]["goldNum"] = 0
    start = Pos(20, 15)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "move"
    step = move_pos(response, WORKER_1)
    assert step is not None
    assert distance(step, STONE_NEAR) < distance(start, STONE_NEAR)


def test_walls_complete_still_buys_upgrade(make_payload):
    """墙已齐、金币够:商店旁仍买武器升级券。"""
    payload = fresh(make_payload(roundNo=20))
    fill_walls(payload)
    payload["teamOur"]["goldNum"] = 120
    place(payload, WORKER_1, 24, 20, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "buy"
    assert command.get("name") == "WeaponUpgradeVoucher1"


def _set_levels(payload, kinds: set[str], level: int) -> None:
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") in kinds:
            role["level"] = level


def _stand_beside(payload, target: Pos) -> Pos:
    from agent.world import World

    world = World.load(payload)
    blocked = {Pos(role["pos"]["x"], role["pos"]["y"]) for role in payload["teamOur"]["roles"]}
    options = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            pos = Pos(target.x + dx, target.y + dy)
            if pos in blocked or not world.land(pos):
                continue
            options.append(pos)
    assert options, f"没有可贴着 {target} 的空地"
    return options[0]


def test_cheap_gold_does_not_buy_wall_before_weapons(make_payload):
    """塔还是 1 级、金币只够买围墙券:不得买墙券,武器优先。"""
    payload = fresh(make_payload(roundNo=40))
    fill_walls(payload)
    payload["teamOur"]["goldNum"] = 30
    place(payload, WORKER_1, 24, 20, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert not (
        command["action"] == "buy" and "Wall" in str(command.get("name"))
    ), command
    assert not (
        command["action"] == "buy" and "Station" in str(command.get("name"))
    ), command


def test_maxed_weapons_buy_wall_voucher_not_station(make_payload):
    """武器已满级、朝向敌人的墙还是 1 级:买围墙升级券,不买基地券。"""
    payload = fresh(make_payload(roundNo=140))
    fill_walls(payload)
    _set_levels(payload, {"gatling", "railgun", "rocket"}, 3)
    payload["teamOur"]["goldNum"] = 30
    place(payload, WORKER_1, 24, 20, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "buy"
    assert command.get("name") == "WallUpgradeVoucher1"


def test_maxed_weapons_and_walls_buy_station(make_payload):
    """武器和围墙都满级:才买基地升级券。"""
    payload = fresh(make_payload(roundNo=140))
    fill_walls(payload)
    _set_levels(payload, {"gatling", "railgun", "rocket", "wall"}, 3)
    payload["teamOur"]["goldNum"] = 120
    place(payload, WORKER_1, 24, 20, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "buy"
    assert command.get("name") == "StationUpgradeVoucher1"


def test_wall_voucher_used_on_center_facing_wall(make_payload):
    """围墙券用在朝向敌人、离地图中心最近的那段墙上。"""
    from agent.brain import _map_center, _wall_order
    from agent.world import World

    payload = fresh(make_payload(roundNo=140))
    fill_walls(payload)
    _set_levels(payload, {"gatling", "railgun", "rocket"}, 3)
    world = World.load(payload)
    center = _map_center(world)
    facing = list(_wall_order(world))
    assert len(facing) >= 2
    nearest = min(facing, key=lambda pos: (distance(pos, center), pos.x, pos.y))
    stand = _stand_beside(payload, nearest)
    place(payload, WORKER_1, stand.x, stand.y, backpack=["WallUpgradeVoucher1"])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "use"
    assert command.get("name") == "WallUpgradeVoucher1"
    target = command["targetPos"][0]
    assert (target["x"], target["y"]) == (nearest.x, nearest.y)


def test_wall_voucher_walks_to_center_not_far_wall(make_payload):
    """人贴着远离中心的墙时,仍走向离中心最近的那段朝向墙,不就近升级。"""
    from agent.brain import _map_center, _wall_order
    from agent.world import World

    payload = fresh(make_payload(roundNo=140))
    fill_walls(payload)
    _set_levels(payload, {"gatling", "railgun", "rocket"}, 3)
    world = World.load(payload)
    center = _map_center(world)
    facing = list(_wall_order(world))
    nearest = min(facing, key=lambda pos: (distance(pos, center), pos.x, pos.y))
    farthest = max(facing, key=lambda pos: (distance(pos, center), pos.x, pos.y))
    stand = _stand_beside(payload, farthest)
    assert distance(stand, nearest) > 1
    place(payload, WORKER_1, stand.x, stand.y, backpack=["WallUpgradeVoucher1"])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "move", command
    step = move_pos(response, WORKER_1)
    assert step is not None
    assert distance(step, nearest) < distance(stand, nearest)


def test_weapon_voucher_beats_adjacent_wall(make_payload):
    """同时带着武器券和围墙券、人贴着墙不贴塔:先去升武器。"""
    from agent.brain import _map_center, _wall_order
    from agent.world import World

    payload = fresh(make_payload(roundNo=140))
    fill_walls(payload)
    world = World.load(payload)
    center = _map_center(world)
    nearest = min(
        _wall_order(world),
        key=lambda pos: (distance(pos, center), pos.x, pos.y),
    )
    stand = _stand_beside(payload, nearest)
    place(
        payload, WORKER_1, stand.x, stand.y,
        backpack=["WeaponUpgradeVoucher1", "WallUpgradeVoucher1"],
    )
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command.get("name") != "WallUpgradeVoucher1"
    if command["action"] == "use":
        assert command.get("name") == "WeaponUpgradeVoucher1"
    else:
        assert command["action"] == "move"
        towers = [
            Pos(role["pos"]["x"], role["pos"]["y"])
            for role in payload["teamOur"]["roles"]
            if role.get("roleType") in {"gatling", "railgun", "rocket"}
        ]
        step = move_pos(response, WORKER_1)
        assert step is not None
        def _span(origin: Pos) -> int:
            return min(
                abs(origin.x - tower.x) + abs(origin.y - tower.y)
                for tower in towers
            )
        assert _span(step) < _span(stand)


def test_later_days_do_not_rebuild_rockets_and_keep_walling(make_payload):
    """第二天、第三天炮已在口袋:不再建造火箭;包里有石头就继续砌朝向敌人的墙。"""
    import agent.tasks as tasks_mod

    for round_no in (140, 270):
        tasks_mod.MEMORY = tasks_mod.Memory()
        payload = fresh(make_payload(roundNo=round_no))
        payload["teamOur"]["goldNum"] = 75
        drop_walls(payload)
        cells = ((9, 24), (9, 22), (8, 22))
        for unit_id, (x, y) in zip((GATLING, RAILGUN, ROCKET), cells):
            place(
                payload, unit_id, x, y,
                roleType="rocket", cooldown=0, level=1,
                attackRange=10, attackPower=20, health=1000,
            )
        place(
            payload, WORKER_1, 12, 21,
            backpack=["stone", "stone", "stone", "stone"],
        )
        place(payload, WORKER_2, 16, 18, backpack=[])
        place(payload, PIONEER, 8, 24, backpack=["Medicine"])
        response = decide(payload)
        _validate(response, payload)
        command = response[str(WORKER_1)]
        assert command["action"] == "build", (round_no, command)
        assert command.get("name") == "wall"
        assert all(
            not (
                cmd["action"] == "build"
                and cmd.get("name") in {"rocket", "gatling", "railgun"}
            )
            for cmd in response.values()
        )

