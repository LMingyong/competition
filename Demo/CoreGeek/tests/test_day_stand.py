"""白天中段不得停在炮手站位;采矿优先近处。"""

import agent.tasks as tasks_mod
from agent.brain import _gun_stand, _tower_sites, decide
from agent.jobs import KIND_MAN_TOWER, KIND_MINE, KIND_TOWER, KIND_WALL, Job
from agent.protocol import Pos, distance
from agent.world import World

from tests.helpers import (
    PIONEER,
    WORKER_1,
    WORKER_2,
    action_of,
    drop_walls,
    fill_walls,
    fresh,
    move_pos,
    place,
)

STAND = Pos(12, 23)
OUTER = Pos(13, 22)
NEAR_STONE = Pos(4, 24)
FAR_COPPER = Pos(22, 26)

VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS


def _strip_weapons(payload):
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") not in ("gatling", "railgun", "rocket")
    ]


def _add_rocket(payload, unit_id, x, y):
    payload["teamOur"]["roles"].append(
        {
            "id": unit_id,
            "pos": {"x": x, "y": y},
            "roleType": "rocket",
            "health": 1000,
            "attackPower": 20,
            "attackRange": 10,
            "level": 1,
            "backPackCapability": 0,
            "backpack": [],
        }
    )


def _two_rockets(payload):
    _strip_weapons(payload)
    _add_rocket(payload, 30001, 12, 24)
    _add_rocket(payload, 30002, 12, 22)


def _three_rockets(payload):
    _two_rockets(payload)
    _add_rocket(payload, 30003, 13, 22)


def _stand_worker(payload, round_no, gold, backpack=None):
    payload["roundNo"] = round_no
    payload["phaseTask"] = ""
    payload["teamOur"]["goldNum"] = gold
    place(payload, WORKER_1, STAND.x, STAND.y, backpack=list(backpack or []))
    place(payload, WORKER_2, 4, 4, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])


def _left_stand(response):
    assert action_of(response, WORKER_1) is not None
    assert action_of(response, WORKER_1) != "attack"
    step = move_pos(response, WORKER_1)
    if action_of(response, WORKER_1) == "move":
        assert step is not None
        assert step != STAND
    else:
        assert action_of(response, WORKER_1) in {"move", "collect", "build"}
        assert step != STAND
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    if job is not None:
        assert job.kind != KIND_MAN_TOWER
    return step, job


def test_pocket_coordinates_unchanged(make_payload):
    """样例炮位仍是朝敌口袋,没有改到基地背后。"""
    world = World.load(make_payload(roundNo=40))
    assert _tower_sites(world) == (Pos(12, 24), Pos(12, 22), OUTER)
    assert _gun_stand(world) == STAND


def test_midday_worker_on_stand_leaves_to_build_third(make_payload):
    """前两门已建成、人站在 (12,23)、白天中段:离开站位去建第三门。"""
    payload = fresh(make_payload(roundNo=40))
    _two_rockets(payload)
    fill_walls(payload)
    _stand_worker(payload, 40, gold=25)
    assert World.load(payload).round_in_day == 40
    response = decide(payload)
    _validate(response, payload)
    step, job = _left_stand(response)
    assert action_of(response, WORKER_1) == "move"
    assert step is not None
    assert job is not None
    assert job.kind == KIND_TOWER
    assert job.target == OUTER
    assert distance(step, OUTER) <= distance(STAND, OUTER)


def test_midday_worker_on_stand_leaves_to_wall_or_stone(make_payload):
    """三门已齐、墙还缺、人站在站位:离开去采石或砌墙,不占站位。"""
    payload = fresh(make_payload(roundNo=40))
    _three_rockets(payload)
    drop_walls(payload)
    _stand_worker(payload, 40, gold=0)
    response = decide(payload)
    _validate(response, payload)
    step, job = _left_stand(response)
    assert action_of(response, WORKER_1) == "move"
    assert step is not None
    assert job is not None
    assert job.kind == KIND_WALL
    assert job.name in {"stone", "wall"}
    assert distance(step, STAND) >= 1


def test_failed_outer_rocket_does_not_idle_on_stand(make_payload):
    """最外侧火箭指令失败后,不得停在站位上再 build;改去采石或砌墙。"""
    payload = fresh(make_payload(roundNo=40))
    _two_rockets(payload)
    drop_walls(payload)
    _stand_worker(payload, 40, gold=25)
    payload["lastRoundRoleActionResults"] = {str(WORKER_1): False}
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_TOWER, target=OUTER, name="rocket", started=39,
    )
    response = decide(payload)
    _validate(response, payload)
    command = response[str(WORKER_1)]
    assert command["action"] != "build" or command.get("name") != "rocket"
    step, job = _left_stand(response)
    assert action_of(response, WORKER_1) == "move"
    assert step is not None and step != STAND
    assert job is not None
    assert job.kind in {KIND_WALL, KIND_MINE}
    assert job.name == "stone" or job.kind == KIND_WALL


def test_idle_worker_walks_to_near_mine_not_far_copper(make_payload):
    """一近一远两座矿:空闲工人走向近的石头,不去远处铜矿。"""
    payload = fresh(make_payload(roundNo=10))
    fill_walls(payload)
    payload["teamOur"]["goldNum"] = 0
    start = Pos(6, 22)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    place(payload, WORKER_2, 4, 4, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "move"
    step = move_pos(response, WORKER_1)
    assert step is not None
    assert distance(step, NEAR_STONE) < distance(start, NEAR_STONE)
    assert distance(step, FAR_COPPER) >= distance(start, FAR_COPPER)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None
    assert job.kind == KIND_MINE
    assert job.target == NEAR_STONE
    assert job.target != FAR_COPPER


def test_locked_far_mine_switches_when_near_mine_exists(make_payload):
    """锁住的远矿还要走超过 8 格,身边 8 格内有矿时改去近的。"""
    payload = fresh(make_payload(roundNo=12))
    fill_walls(payload)
    payload["teamOur"]["goldNum"] = 0
    start = Pos(6, 22)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    place(payload, WORKER_2, 4, 4, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_MINE, target=FAR_COPPER, name="copper", started=10,
    )
    assert distance(start, FAR_COPPER) > 8
    assert distance(start, NEAR_STONE) <= 8
    response = decide(payload)
    _validate(response, payload)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None
    assert job.kind == KIND_MINE
    assert job.target == NEAR_STONE
    step = move_pos(response, WORKER_1)
    assert step is not None
    assert distance(step, NEAR_STONE) < distance(start, NEAR_STONE)


def test_partial_backpack_keeps_mining_near_not_shop(make_payload):
    """挖了一下背包未满:继续挖近处,不跨图去商店。"""
    payload = fresh(make_payload(roundNo=15))
    fill_walls(payload)
    payload["teamOur"]["goldNum"] = 200
    place(payload, WORKER_1, 5, 23, backpack=["stone"])
    place(payload, WORKER_2, 4, 4, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_MINE, target=NEAR_STONE, name="stone", started=14,
    )
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "collect"
    target = response[str(WORKER_1)]["targetPos"][0]
    assert (target["x"], target["y"]) == (NEAR_STONE.x, NEAR_STONE.y)
    assert action_of(response, WORKER_1) != "buy"
