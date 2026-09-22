"""开局不要挤在同一门炮上，开拓者也不踩建造格。"""

import agent.tasks as tasks_mod
from agent.brain import _gun_stand, _tower_sites, decide
from agent.jobs import KIND_TOWER
from agent.protocol import Pos, WEAPON_BUILD_COST, distance
from agent.world import World

from tests.helpers import PIONEER, WORKER_1, WORKER_2, action_of, fresh, move_pos, place

ROCKETS = (Pos(9, 24), Pos(9, 22), Pos(8, 22))
STAND = Pos(9, 23)


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


def _cell(command):
    raw = (command.get("targetPos") or [None])[0]
    if not isinstance(raw, dict):
        return None
    return Pos(int(raw["x"]), int(raw["y"]))


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
            dest = _cell(command)
            unit = next(unit for unit in world.ours if unit.unit_id == int(key))
            others = set(occupied) - {cur}
            if (
                dest is not None
                and dest not in world.blocked(unit)
                and dest not in others
                and world.land(dest)
                and distance(cur, dest) == 1
            ):
                role["pos"] = dest.dump()
                occupied.discard(cur)
                occupied.add(dest)
        elif action == "build" and command.get("name") != "wall":
            target = _cell(command)
            occupied_build = World.load(payload).occupied_for_build()
            if (
                target is not None
                and role["roleType"] == "worker"
                and cur != target
                and distance(cur, target) <= 1
                and target not in occupied_build
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


def _opening(make_payload, pioneer):
    payload = fresh(make_payload(roundNo=1, phaseTask=""))
    payload["teamOur"]["goldNum"] = 75
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("station", "worker", "pioneer")
    ]
    place(payload, WORKER_1, 5, 23, backpack=[])
    place(payload, WORKER_2, 10, 16, backpack=[])
    place(payload, PIONEER, pioneer.x, pioneer.y, backpack=[])
    payload["robot"] = {"roles": []}
    return payload


def test_opening_turns_keep_pioneer_off_pocket_and_split_builds(make_payload):
    """连续开局回合：开拓者不踩火箭格和站位，工人不对同一格又 build 又 move。"""
    payload = _opening(make_payload, Pos(10, 25))
    world = World.load(payload)
    assert _tower_sites(world) == ROCKETS
    assert _gun_stand(world) == STAND
    tasks_mod.MEMORY = tasks_mod.Memory()
    pocket = set(ROCKETS) | {STAND}
    for round_no in range(1, 7):
        if len(_weapons(payload)) >= 3:
            break
        payload["roundNo"] = round_no
        response = decide(payload)
        builds = set()
        moves = []
        for key, command in response.items():
            pos = _cell(command)
            if command.get("action") == "build" and pos is not None:
                builds.add(pos)
            if command.get("action") == "move" and pos is not None:
                moves.append((int(key), pos))
        clash = [item for item in moves if item[1] in builds]
        assert clash == [], (round_no, clash, builds)
        pioneer = response.get(str(PIONEER))
        if pioneer and pioneer.get("action") == "move":
            step = move_pos(response, PIONEER)
            assert step not in pocket, (round_no, step)
        for role in _workers(payload):
            action = action_of(response, role["id"])
            assert action in {"move", "build"}, (round_no, role["id"], action)
        _apply(payload, response)


def test_blocked_cannon_sends_second_worker_to_another(make_payload):
    """一门炮的邻格被队友和机器人占住时，另一名工人改去别的炮，不原地不动。"""
    payload = _opening(make_payload, Pos(18, 18))
    payload["roundNo"] = 4
    place(payload, WORKER_1, 8, 24, backpack=[])
    place(payload, WORKER_2, 7, 24, backpack=[])
    robots = []
    for index, (x, y) in enumerate(((8, 23), (8, 25), (9, 25), (10, 25))):
        robots.append(
            {
                "id": 81000 + index,
                "pos": {"x": x, "y": y},
                "roleType": "smallRobot",
                "health": 40,
                "abnormalState": "",
                "targetTeam": "challenger",
            }
        )
    payload["robot"] = {"roles": robots}
    response = decide(payload)
    assert action_of(response, WORKER_1) == "build"
    assert _cell(response[str(WORKER_1)]) == Pos(9, 24)
    action = action_of(response, WORKER_2)
    assert action in {"move", "build"}, action
    step = _cell(response[str(WORKER_2)])
    assert step is not None
    assert step != Pos(7, 24)
    assert step != Pos(9, 24)
    assert step not in {STAND, Pos(9, 24)}
    job = tasks_mod.MEMORY.jobs.get(WORKER_2)
    others = {Pos(9, 22), Pos(8, 22)}
    if action == "build":
        assert step in others
    else:
        assert job is not None and job.kind == KIND_TOWER
        assert job.target in others
        assert step not in set(ROCKETS)
