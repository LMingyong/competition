"""连续回放:开局占炮格、贴着就干、第二门当回合去第三门。禁止 place 瞬移冒充多回合。"""

from agent.brain import _tower_sites, decide
from agent.jobs import KIND_TOWER
from agent.protocol import Pos, distance
from agent.world import World

import agent.tasks as tasks_mod

from tests.helpers import (
    PIONEER,
    WORKER_1,
    WORKER_2,
    action_of,
    apply_turn,
    drop_walls,
    fill_walls,
    fresh,
    move_pos,
    place,
    role_by_id,
)

VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}
COPPER_NEAR = Pos(7, 2)


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS


def _opening_no_buildings(payload):
    payload["teamOur"]["goldNum"] = 75
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("station", "worker", "pioneer")
    ]
    place(payload, WORKER_1, 5, 23, backpack=[])
    place(payload, WORKER_2, 10, 16, backpack=[])
    place(payload, PIONEER, 10, 12, backpack=["Medicine"])
    return payload


def _unit_pos(payload, unit_id: int) -> Pos:
    role = role_by_id(payload, unit_id)
    return Pos(int(role["pos"]["x"]), int(role["pos"]["y"]))


def _build_cell(response, unit_id: int) -> Pos | None:
    command = response.get(str(unit_id))
    if not command or command.get("action") != "build":
        return None
    raw = command["targetPos"][0]
    return Pos(int(raw["x"]), int(raw["y"]))


def test_opening_five_turns_workers_move_or_build(make_payload):
    """开局连续至少 5 回合:两工人每回合都有 move/build,且不挤同一门。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=1)))
    sites = _tower_sites(World.load(payload))
    assert len(sites) == 3
    reserved = set(sites)

    for _ in range(5):
        starts = {
            WORKER_1: _unit_pos(payload, WORKER_1),
            WORKER_2: _unit_pos(payload, WORKER_2),
        }
        pioneer_start = _unit_pos(payload, PIONEER)
        response = decide(payload)
        _validate(response, payload)
        targets: list[Pos] = []
        for uid, start in starts.items():
            action = action_of(response, uid)
            assert action in {"move", "build"}, f"{uid} 第{payload['roundNo']}回合空过: {action}"
            if action == "move":
                step = move_pos(response, uid)
                assert step is not None
                assert step != start
                assert step not in reserved, f"{uid} 走到未建炮格 {step}"
                job = tasks_mod.MEMORY.jobs.get(uid)
                if job is not None and job.kind == KIND_TOWER and job.target is not None:
                    targets.append(job.target)
            else:
                built = _build_cell(response, uid)
                assert built in sites
                targets.append(built)
        if len(targets) == 2:
            assert targets[0] != targets[1], f"两工人挤同一门: {targets}"
        pioneer_step = move_pos(response, PIONEER)
        if pioneer_step is not None:
            assert pioneer_step not in reserved, f"开拓者踩未建炮 {pioneer_step}"
            assert pioneer_step != pioneer_start
        apply_turn(payload, response)


def test_walk_to_mine_then_collect_next_beat(make_payload):
    """贴矿:从远处走过去,下一拍必须 collect,不得空过或改去更贵的远矿。"""
    payload = fresh(make_payload(roundNo=20))
    payload["teamOur"]["goldNum"] = 0
    fill_walls(payload)
    start = Pos(9, 4)
    assert distance(start, COPPER_NEAR) == 2
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])

    first = decide(payload)
    _validate(first, payload)
    assert action_of(first, WORKER_1) == "move"
    step = move_pos(first, WORKER_1)
    assert step is not None
    assert distance(step, COPPER_NEAR) < distance(start, COPPER_NEAR)
    apply_turn(payload, first)

    here = _unit_pos(payload, WORKER_1)
    assert here == step
    if distance(here, COPPER_NEAR) > 1:
        second = decide(payload)
        _validate(second, payload)
        assert action_of(second, WORKER_1) == "move"
        step2 = move_pos(second, WORKER_1)
        assert step2 is not None
        assert distance(step2, COPPER_NEAR) <= 1
        apply_turn(payload, second)
        here = _unit_pos(payload, WORKER_1)

    assert here != COPPER_NEAR
    assert distance(here, COPPER_NEAR) <= 1
    last = decide(payload)
    _validate(last, payload)
    assert action_of(last, WORKER_1) == "collect"
    raw = last[str(WORKER_1)]["targetPos"][0]
    assert (int(raw["x"]), int(raw["y"])) == (COPPER_NEAR.x, COPPER_NEAR.y)
    apply_turn(payload, last)
    assert "copper" in role_by_id(payload, WORKER_1)["backpack"]


def test_second_door_same_turn_other_goes_third(make_payload):
    """第二门当回合 build 完成:另一人必须走向第三门,不得改去挖矿。"""
    payload = fresh(make_payload(roundNo=5))
    drop_walls(payload)
    payload["teamOur"]["goldNum"] = 75
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("station", "worker", "pioneer")
        or role.get("id") == 10020
    ]
    sites = _tower_sites(World.load(payload))
    first, second, third = sites
    for role in payload["teamOur"]["roles"]:
        if role.get("id") == 10020:
            role["pos"] = {"x": first.x, "y": first.y}
            role["roleType"] = "rocket"
            break
    else:
        payload["teamOur"]["roles"].append(
            {
                "id": 10020,
                "pos": {"x": first.x, "y": first.y},
                "roleType": "rocket",
                "health": 1000,
                "level": 1,
                "attackPower": 20,
                "attackRange": 10,
                "backpack": [],
            }
        )
    stand = Pos(second.x + 1, second.y)
    far = Pos(8, 16)
    place(payload, WORKER_1, stand.x, stand.y, backpack=[])
    place(payload, WORKER_2, far.x, far.y, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])

    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "build"
    assert _build_cell(response, WORKER_1) == second
    assert action_of(response, WORKER_2) == "move"
    step = move_pos(response, WORKER_2)
    assert step is not None
    assert distance(step, third) < distance(far, third)
    job = tasks_mod.MEMORY.jobs.get(WORKER_2)
    assert job is not None
    assert job.kind == KIND_TOWER
    assert job.target == third
    apply_turn(payload, response)
    weapons = [
        Pos(int(role["pos"]["x"]), int(role["pos"]["y"]))
        for role in payload["teamOur"]["roles"]
        if role.get("roleType") in {"gatling", "railgun", "rocket"}
    ]
    assert first in weapons and second in weapons
    assert payload["teamOur"]["goldNum"] == 50
