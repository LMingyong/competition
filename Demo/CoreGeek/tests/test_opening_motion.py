"""开局三门火箭没齐之前,两名工人每回合都要移动或建造。"""

import copy

import agent.tasks as tasks_mod
from agent.brain import _gun_stand, _tower_sites, decide
from agent.protocol import Pos, WEAPON_BUILD_COST, distance
from agent.world import World

from tests.helpers import PIONEER, WORKER_1, WORKER_2, fresh, place
from tests.sandbox.world import WORKER_1 as DEF_W1
from tests.sandbox.world import WORKER_2 as DEF_W2
from tests.sandbox.world import new_game

CHALLENGER_ROCKETS = (Pos(9, 24), Pos(9, 22), Pos(8, 22))
CHALLENGER_STAND = Pos(9, 23)
DEFENDER_ROCKETS = (Pos(32, 9), Pos(32, 11), Pos(33, 11))
DEFENDER_STAND = Pos(32, 10)


def _workers(payload):
    return [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") == "worker"
    ]


def _weapons(payload):
    return [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("rocket", "gatling", "railgun")
    ]


def _apply(payload, commands):
    world = World.load(payload)
    occupied = {unit.pos for unit in world.ours if unit.health > 0}
    heroes = {
        role["id"]: role
        for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("worker", "pioneer")
    }
    for key, command in sorted(commands.items(), key=lambda item: int(item[0])):
        role = heroes.get(int(key))
        if role is None:
            continue
        cur = Pos(role["pos"]["x"], role["pos"]["y"])
        action = command.get("action")
        if action == "move":
            raw = command["targetPos"][0]
            dest = Pos(int(raw["x"]), int(raw["y"]))
            unit = next(unit for unit in world.ours if unit.unit_id == int(key))
            blocked = set(world.blocked(unit))
            others = set(occupied) - {cur}
            if (
                dest not in blocked
                and dest not in others
                and world.land(dest)
                and distance(cur, dest) == 1
            ):
                role["pos"] = dest.dump()
                occupied.discard(cur)
                occupied.add(dest)
        elif action == "build" and command.get("name") != "wall":
            raw = command["targetPos"][0]
            target = Pos(int(raw["x"]), int(raw["y"]))
            occ = World.load(payload).occupied_for_build()
            if (
                role["roleType"] == "worker"
                and cur != target
                and distance(cur, target) <= 1
                and target not in occ
                and payload["teamOur"]["goldNum"] >= WEAPON_BUILD_COST
                and len(_weapons(payload)) < 3
            ):
                payload["teamOur"]["goldNum"] -= WEAPON_BUILD_COST
                payload["teamOur"]["roles"].append(
                    {
                        "id": 50000 + target.x * 40 + target.y,
                        "pos": target.dump(),
                        "roleType": command.get("name") or "rocket",
                        "health": 1000,
                        "level": 1,
                        "attackPower": 20,
                        "attackRange": 10,
                        "backpack": [],
                    }
                )
    payload["lastRoundRoleActionResults"] = {key: True for key in commands}


def _opening_challenger(make_payload):
    payload = fresh(make_payload(roundNo=1, phaseTask=""))
    payload["teamOur"]["goldNum"] = 75
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("station", "worker", "pioneer")
    ]
    place(payload, WORKER_1, 5, 23, backpack=[])
    place(payload, WORKER_2, 10, 16, backpack=[])
    place(payload, PIONEER, 10, 12, backpack=[])
    payload["robot"] = {"roles": []}
    return payload


def _assert_workers_keep_acting(payload, stand: Pos, rounds: int = 12):
    payload = copy.deepcopy(payload)
    tasks_mod.MEMORY = tasks_mod.Memory()
    idle = []
    for round_no in range(1, rounds + 1):
        if len(_weapons(payload)) >= 3:
            break
        payload["roundNo"] = round_no
        response = decide(payload)
        for role in _workers(payload):
            command = response.get(str(role["id"]))
            action = None if command is None else command.get("action")
            if action not in {"move", "build"}:
                idle.append((round_no, role["id"], role["pos"], action))
                continue
            if action == "move":
                raw = command["targetPos"][0]
                step = Pos(int(raw["x"]), int(raw["y"]))
                assert step != stand, (round_no, role["id"], step)
        _apply(payload, response)
    assert idle == [], idle
    return payload


def test_battery_stays_behind_the_base(make_payload):
    """背后炮位不能被改回应敌方向。"""
    challenger = World.load(_opening_challenger(make_payload))
    assert _tower_sites(challenger) == CHALLENGER_ROCKETS
    assert _gun_stand(challenger) == CHALLENGER_STAND
    defender = World.load(new_game())
    assert _tower_sites(defender) == DEFENDER_ROCKETS
    assert _gun_stand(defender) == DEFENDER_STAND


def test_defender_opening_workers_move_or_build_until_three_rockets():
    """守方出生点:至少前 8 回合,火箭没齐时两名工人都不空过、不原地采集。"""
    payload = new_game()
    done = _assert_workers_keep_acting(payload, DEFENDER_STAND, rounds=20)
    assert len(_weapons(done)) == 3
    assert {role["id"] for role in _workers(payload)} == {DEF_W1, DEF_W2}


def test_challenger_opening_workers_move_or_build_until_three_rockets(make_payload):
    """挑战者开局:两名工人连续多回合都在走向火箭或建造,第 3 回合也不停。"""
    payload = _opening_challenger(make_payload)
    done = _assert_workers_keep_acting(payload, CHALLENGER_STAND, rounds=20)
    assert len(_weapons(done)) == 3
