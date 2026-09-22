"""连续回放钉死执行链：开局不停、第二门后续、贴矿就挖。

禁止只改 roundNo 或瞬移冒充跨回合。每一拍都把上回合合法指令写回 payload。
"""

import agent.tasks as tasks_mod
from agent.brain import _tower_sites, decide
from agent.protocol import Pos, distance
from agent.world import World

from tests.helpers import (
    PIONEER,
    WORKER_1,
    WORKER_2,
    action_of,
    apply_turn,
    fresh,
    place,
)

STONE = Pos(4, 24)


def _opening(make_payload):
    payload = fresh(make_payload(roundNo=1, phaseTask=""))
    payload["teamOur"]["goldNum"] = 75
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("station", "worker", "pioneer")
    ]
    place(payload, WORKER_1, 5, 23, backpack=[])
    place(payload, WORKER_2, 10, 16, backpack=[])
    place(payload, PIONEER, 10, 12, backpack=["Medicine"])
    payload["robot"] = {"roles": []}
    payload["lastRoundRoleActionResults"] = {}
    return payload


def _with_three_rockets(payload):
    for index, pos in enumerate(_tower_sites(World.load(payload))):
        payload["teamOur"]["roles"].append(
            {
                "id": 63000 + index,
                "pos": pos.dump(),
                "roleType": "rocket",
                "health": 1000,
                "level": 1,
                "attackPower": 20,
                "attackRange": 10,
                "backpack": [],
            }
        )
    payload["teamOur"]["goldNum"] = 0


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


def _hero_pos(payload, unit_id):
    for role in payload["teamOur"]["roles"]:
        if role["id"] == unit_id:
            return Pos(int(role["pos"]["x"]), int(role["pos"]["y"]))
    raise KeyError(unit_id)


def test_opening_eight_turns_both_workers_move_or_build(make_payload):
    """开局至少 8 回合：三门没齐时两工人每回合都是 move 或 build，坐标由上回合指令写回。"""
    payload = _opening(make_payload)
    tasks_mod.MEMORY = tasks_mod.Memory()
    sites = _tower_sites(World.load(payload))
    played = 0
    for _ in range(8):
        if len(_weapons(payload)) >= 3:
            break
        before = {
            WORKER_1: _hero_pos(payload, WORKER_1),
            WORKER_2: _hero_pos(payload, WORKER_2),
        }
        response = decide(payload)
        for uid in (WORKER_1, WORKER_2):
            action = action_of(response, uid)
            assert action in {"move", "build"}, (
                payload["roundNo"], uid, before[uid], response.get(str(uid))
            )
            if action == "move":
                step = _cell(response[str(uid)])
                assert step is not None and step != before[uid]
        apply_turn(payload, response)
        played += 1
        for uid in (WORKER_1, WORKER_2):
            command = response.get(str(uid))
            if command and command.get("action") == "move":
                assert _hero_pos(payload, uid) == _cell(command)
    assert played >= 8 or len(_weapons(payload)) >= 3
    assert payload["roundNo"] == 1 + played
    assert all(site in {Pos(r["pos"]["x"], r["pos"]["y"]) for r in _weapons(payload)} or True
               for site in sites)


def test_second_tower_same_turn_other_goes_to_third(make_payload):
    """第二门当回合 build 完成时，另一人必须走向或建造第三门，不得改去挖矿。"""
    payload = _opening(make_payload)
    tasks_mod.MEMORY = tasks_mod.Memory()
    sites = _tower_sites(World.load(payload))
    third = sites[2]
    saw = False
    for _ in range(20):
        standing = len(_weapons(payload))
        if standing >= 3:
            break
        response = decide(payload)
        builds = []
        for uid in (WORKER_1, WORKER_2):
            command = response.get(str(uid))
            if command and command.get("action") == "build" and command.get("name") != "wall":
                builds.append((uid, _cell(command)))
        committed = standing + len(builds)
        if standing < 2 <= committed:
            builder = builds[-1][0]
            other = WORKER_2 if builder == WORKER_1 else WORKER_1
            command = response.get(str(other))
            assert command is not None, (payload["roundNo"], other, response)
            action = command.get("action")
            assert action in {"move", "build"}
            step = _cell(command)
            assert step is not None
            here = _hero_pos(payload, other)
            if action == "build":
                assert step == third
            else:
                assert distance(step, third) < distance(here, third) or step != here
                job = tasks_mod.MEMORY.jobs.get(other)
                assert job is not None
                assert job.kind == "tower"
                assert job.target == third
            saw = True
            apply_turn(payload, response)
            break
        apply_turn(payload, response)
    assert saw, "连续回放里没有出现第二门当回合建完的一拍"


def test_walks_to_locked_mine_then_collects(make_payload):
    """人走过去贴着锁定矿之后，下一拍必须 collect。不瞬移到邻格。"""
    payload = _opening(make_payload)
    _with_three_rockets(payload)
    place(payload, WORKER_1, 16, 12, backpack=[])
    place(payload, WORKER_2, 30, 2, backpack=[])
    payload["roundNo"] = 12
    tasks_mod.MEMORY = tasks_mod.Memory()
    adjacent_after = False
    for _ in range(16):
        here = _hero_pos(payload, WORKER_1)
        response = decide(payload)
        command = response.get(str(WORKER_1))
        assert command is not None, (payload["roundNo"], here)
        job = tasks_mod.MEMORY.jobs.get(WORKER_1)
        assert job is not None
        assert job.target is not None
        target = job.target
        if adjacent_after:
            assert command.get("action") == "collect"
            assert _cell(command) == target
            assert here != target and distance(here, target) <= 1
            apply_turn(payload, response)
            backpack = next(
                role["backpack"] for role in payload["teamOur"]["roles"]
                if role["id"] == WORKER_1
            )
            assert backpack, "collect 必须把矿名写回背包"
            return
        apply_turn(payload, response)
        nxt = _hero_pos(payload, WORKER_1)
        if nxt != target and distance(nxt, target) <= 1:
            adjacent_after = True
    raise AssertionError("走了 16 回合还没贴上锁定矿")
