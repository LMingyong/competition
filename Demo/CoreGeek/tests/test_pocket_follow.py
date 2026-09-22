"""贴着矿就挖；两门火箭落地后继续第三门。目标跨回合不丢。"""

import copy

import agent.tasks as tasks_mod
from agent.brain import decide
from agent.protocol import Pos, distance
from agent.world import World

from tests.helpers import (
    PIONEER,
    WORKER_1,
    WORKER_2,
    action_of,
    drop_walls,
    fresh,
    move_pos,
    place,
)
from tests.sandbox.world import WORKER_1 as DEF_W1
from tests.sandbox.world import WORKER_2 as DEF_W2
from tests.sandbox.world import PIONEER as DEF_PIONEER
from tests.sandbox.world import new_game
from tests.test_day_stand import _three_rockets, _two_rockets
from tests.test_opening_motion import (
    CHALLENGER_ROCKETS,
    CHALLENGER_STAND,
    DEFENDER_ROCKETS,
    DEFENDER_STAND,
    _apply,
    _weapons,
)

OUTER = CHALLENGER_ROCKETS[2]
DEF_OUTER = DEFENDER_ROCKETS[2]
STONE = Pos(4, 24)
DEF_STONE = Pos(32, 6)


def _challenger(make_payload, round_no, gold, rockets):
    payload = fresh(make_payload(roundNo=round_no))
    payload["teamOur"]["goldNum"] = gold
    payload["phaseTask"] = ""
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("station", "worker", "pioneer")
    ]
    if rockets == 2:
        _two_rockets(payload)
    else:
        _three_rockets(payload)
    drop_walls(payload)
    place(payload, PIONEER, 20, 4, backpack=["Medicine"])
    return payload


def _defender(round_no, gold, rockets):
    payload = new_game()
    payload["roundNo"] = round_no
    payload["teamOur"]["goldNum"] = gold
    roles = [
        role for role in payload["teamOur"]["roles"]
        if role["roleType"] not in ("gatling", "railgun", "rocket")
    ]
    built = DEFENDER_ROCKETS[:rockets]
    for index, pos in enumerate(built):
        roles.append(
            {
                "id": 30001 + index,
                "pos": pos.dump(),
                "roleType": "rocket",
                "health": 1000,
                "level": 1,
                "attackPower": 20,
                "attackRange": 10,
                "backPackCapability": 0,
                "backpack": [],
            }
        )
    payload["teamOur"]["roles"] = roles
    for role in roles:
        if role["id"] == DEF_PIONEER:
            role["pos"] = {"x": 2, "y": 4}
            role["backpack"] = []
    return payload


def _cell(command):
    raw = (command.get("targetPos") or [None])[0]
    if not isinstance(raw, dict):
        return None
    return Pos(int(raw["x"]), int(raw["y"]))


def test_near_worker_keeps_third_rocket_when_far_worker_locked_it(make_payload):
    """远处的人先锁了第三门时，贴着第二门的人仍要走向第三门的空邻格，不能改去挖矿。"""
    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _challenger(make_payload, 12, 25, 2)
    place(payload, WORKER_1, 30, 2, backpack=[])
    place(payload, WORKER_2, 10, 22, backpack=[])
    response = decide(payload)
    command = response[str(WORKER_2)]
    assert command["action"] == "move"
    step = _cell(command)
    assert step is not None
    assert step not in CHALLENGER_ROCKETS
    assert step != CHALLENGER_STAND
    assert distance(step, OUTER) < distance(Pos(10, 22), OUTER)
    job = tasks_mod.MEMORY.jobs[WORKER_2]
    assert job.kind == "tower"
    assert job.target == OUTER
    assert job.big == "建炮"
    assert job.small == "walk"
    assert action_of(response, WORKER_1) in {"move", "build"}


def test_adjacent_to_third_rocket_builds_it(make_payload):
    """已经贴着第三门的空邻格就 build，不停在第二门旁边，也不踩未建成的炮格。"""
    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _challenger(make_payload, 12, 25, 2)
    place(payload, WORKER_1, 30, 2, backpack=[])
    place(payload, WORKER_2, 9, 21, backpack=[])
    response = decide(payload)
    command = response[str(WORKER_2)]
    assert command["action"] == "build"
    assert command.get("name") == "rocket"
    assert _cell(command) == OUTER


def test_defender_near_second_rocket_goes_to_third():
    """守方两门已成：近处工人走向 (33,11) 的空邻格，不踩站位 (32,10)，也不把炮格当落脚点。"""
    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _defender(12, 25, 2)
    for role in payload["teamOur"]["roles"]:
        if role["id"] == DEF_W1:
            role["pos"] = {"x": 20, "y": 20}
            role["backpack"] = []
        elif role["id"] == DEF_W2:
            role["pos"] = {"x": 31, "y": 11}
            role["backpack"] = []
    response = decide(payload)
    command = response[str(DEF_W2)]
    assert command["action"] in {"move", "build"}
    step = _cell(command)
    assert step is not None
    assert step not in DEFENDER_ROCKETS
    assert step != DEFENDER_STAND
    if command["action"] == "build":
        assert step == DEF_OUTER
    else:
        assert distance(step, DEF_OUTER) < distance(Pos(31, 11), DEF_OUTER)
        job = tasks_mod.MEMORY.jobs[DEF_W2]
        assert job.target == DEF_OUTER
        assert job.big == "建炮"
        assert job.small == "walk"


def test_both_workers_finish_third_rocket_without_idling(make_payload):
    """从第二门旁边出发，两名工人每回合都是 move 或 build，直到第三门落地。"""
    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _challenger(make_payload, 1, 50, 2)
    place(payload, WORKER_1, 30, 2, backpack=[])
    place(payload, WORKER_2, 10, 22, backpack=[])
    built = False
    for round_no in range(1, 16):
        if len(_weapons(payload)) >= 3:
            built = True
            break
        payload["roundNo"] = round_no
        response = decide(payload)
        missing = {
            pos for pos in CHALLENGER_ROCKETS
            if pos not in {Pos(role["pos"]["x"], role["pos"]["y"]) for role in _weapons(payload)}
        }
        for unit_id in (WORKER_1, WORKER_2):
            command = response.get(str(unit_id))
            assert command is not None, (round_no, unit_id)
            assert command["action"] in {"move", "build"}
            step = _cell(command)
            assert step != CHALLENGER_STAND
            if command["action"] == "move":
                assert step not in missing
        _apply(payload, response)
    assert built or len(_weapons(payload)) >= 3


def test_adjacent_stone_is_collected_and_kept(make_payload):
    """贴着石矿必须 collect。走两步目标不变。站在矿上先走开，下一回合再挖。"""
    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _challenger(make_payload, 12, 0, 3)
    place(payload, WORKER_1, 7, 21, backpack=[])
    place(payload, WORKER_2, 30, 2, backpack=[])
    seen = []
    for round_no in range(12, 16):
        payload["roundNo"] = round_no
        response = decide(payload)
        command = response[str(WORKER_1)]
        job = tasks_mod.MEMORY.jobs[WORKER_1]
        seen.append((command["action"], _cell(command), job.target, job.small, job.big, job.progress))
        assert job.target == STONE
        assert job.small == "collect"
        assert job.big == "建墙"
        assert job.progress == len(
            next(role["backpack"] for role in payload["teamOur"]["roles"] if role["id"] == WORKER_1)
        )
        _apply(payload, response)
        if command["action"] == "collect":
            role = next(role for role in payload["teamOur"]["roles"] if role["id"] == WORKER_1)
            role.setdefault("backpack", []).append("stone")
    actions = [item[0] for item in seen]
    assert actions[:2] == ["move", "move"]
    assert actions[2] == "collect"
    assert all(item[1] != STONE or item[0] == "collect" for item in seen)

    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _challenger(make_payload, 20, 0, 3)
    place(payload, WORKER_1, STONE.x, STONE.y, backpack=[])
    place(payload, WORKER_2, 30, 2, backpack=[])
    first = decide(payload)
    assert action_of(first, WORKER_1) == "move"
    assert tasks_mod.MEMORY.jobs[WORKER_1].target == STONE
    step = move_pos(first, WORKER_1)
    assert step is not None and step != STONE
    _apply(payload, first)
    payload["roundNo"] = 21
    second = decide(payload)
    assert action_of(second, WORKER_1) == "collect"
    assert _cell(second[str(WORKER_1)]) == STONE
    assert tasks_mod.MEMORY.jobs[WORKER_1].target == STONE
    assert tasks_mod.MEMORY.jobs[WORKER_1].small == "collect"


def test_wall_ticket_keeps_collecting_once_against_the_mine(make_payload):
    """建墙要一次采够：贴着石矿连续 collect，挖之前不发呆，目标不换。"""
    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _challenger(make_payload, 18, 0, 3)
    place(payload, WORKER_1, 5, 23, backpack=[])
    place(payload, WORKER_2, 30, 2, backpack=[])
    target = None
    for round_no in range(18, 23):
        payload["roundNo"] = round_no
        response = decide(payload)
        command = response[str(WORKER_1)]
        assert command["action"] == "collect", (round_no, command)
        assert _cell(command) == STONE
        job = tasks_mod.MEMORY.jobs[WORKER_1]
        assert job.small == "collect"
        assert job.big == "建墙"
        assert job.progress == round_no - 18
        if target is None:
            target = job.target
        assert job.target == target == STONE
        role = next(role for role in payload["teamOur"]["roles"] if role["id"] == WORKER_1)
        role.setdefault("backpack", []).append("stone")


def test_defender_wall_builder_collects_adjacent_stone():
    """三门齐了之后，守方建墙的人贴着石矿就挖。"""
    tasks_mod.MEMORY = tasks_mod.Memory()
    payload = _defender(15, 0, 3)
    for role in payload["teamOur"]["roles"]:
        if role["id"] == DEF_W1:
            role["pos"] = {"x": 31, "y": 7}
            role["backpack"] = []
        elif role["id"] == DEF_W2:
            role["pos"] = {"x": 2, "y": 2}
            role["backpack"] = []
    response = decide(payload)
    command = response[str(DEF_W1)]
    assert command["action"] == "collect"
    assert _cell(command) == DEF_STONE
    job = tasks_mod.MEMORY.jobs[DEF_W1]
    assert job.target == DEF_STONE
    assert job.small == "collect"
    assert job.big == "建墙"
    world = World.load(payload)
    assert distance(world.workers()[0].pos, DEF_STONE) <= 1
