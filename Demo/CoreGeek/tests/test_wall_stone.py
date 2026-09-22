"""P3 缺墙采石:墙未齐时不得去采高价铜/铁,必须走向石矿。"""

from agent.brain import decide
from agent.protocol import Pos, distance

from tests.helpers import (
    PIONEER,
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

STONE_NEAR_COPPER_WORKER = Pos(14, 3)
STONE_NEAR_CENTER_WORKER = Pos(14, 3)


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS


def test_missing_walls_collect_stone_not_copper(make_payload):
    """砌墙阶段(回合 35)墙未齐:贴着铜矿的工人不得 collect 铜。"""
    payload = fresh(make_payload(roundNo=35))
    drop_walls(payload)
    park_other_worker_building(payload)
    start = Pos(8, 2)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response.get(str(WORKER_1))
    assert command is not None
    if command["action"] == "collect":
        target = command["targetPos"][0]
        assert (target["x"], target["y"]) != (7, 2)
        assert (target["x"], target["y"]) in {(4, 24), (14, 3)}
    else:
        assert command["action"] == "move"
        step = move_pos(response, WORKER_1)
        assert distance(step, STONE_NEAR_COPPER_WORKER) < distance(start, STONE_NEAR_COPPER_WORKER)


def test_missing_walls_walk_to_nearest_stone_mine(make_payload):
    """砌墙阶段墙未齐、不贴任何矿:应走向最近石矿,而不是更近的铜/铁。"""
    payload = fresh(make_payload(roundNo=35))
    drop_walls(payload)
    park_other_worker_building(payload)
    start = Pos(20, 15)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "move"
    step = move_pos(response, WORKER_1)
    assert distance(step, STONE_NEAR_CENTER_WORKER) < distance(start, STONE_NEAR_CENTER_WORKER)


def test_has_stone_builds_instead_of_more_ore(make_payload):
    """砌墙阶段墙未齐、计划所需石头已经在背包里:直接建墙,不再去挖矿。"""
    from agent.brain import _wall_order
    from agent.world import World

    payload = fresh(make_payload(roundNo=35))
    drop_walls(payload)
    need = len(_wall_order(World.load(payload)))
    place(payload, WORKER_1, 12, 21, backpack=["stone"] * need)
    place(payload, PIONEER, 8, 24, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "build"
    assert command.get("name") == "wall"


def test_walls_complete_allows_copper(make_payload):
    """墙已按 _wall_order 建齐后,贴铜矿允许 collect 铜。"""
    payload = fresh(make_payload(roundNo=20))
    fill_walls(payload)
    place(payload, WORKER_1, 8, 2, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] == "collect"
    target = command["targetPos"][0]
    assert (target["x"], target["y"]) == (7, 2)
