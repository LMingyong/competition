"""连续两回合走不动就换方向，不再挤同一个失败步。"""

import agent.tasks as tasks_mod
from agent.blocked import MoveBlock, block_record, reroute_if_blocked_two_turns
from agent.brain import _gun_stand, _tower_sites, decide
from agent.jobs import KIND_MINE, Job
from agent.protocol import Pos
from agent.world import World

from tests.helpers import (
    PIONEER,
    WORKER_1,
    WORKER_2,
    action_of,
    fresh,
    move_pos,
    place,
)

MINE = Pos(14, 3)
START = Pos(16, 8)
JAM = Pos(11, 22)


def _pocket(payload):
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") not in ("gatling", "railgun", "rocket")
    ]
    for index, (x, y) in enumerate(((9, 24), (9, 22), (8, 22))):
        payload["teamOur"]["roles"].append(
            {
                "id": 30001 + index,
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


def _scene(payload, round_no):
    fresh(payload)
    payload["roundNo"] = round_no
    payload["phaseTask"] = ""
    payload["teamOur"]["goldNum"] = 0
    _pocket(payload)
    place(payload, WORKER_1, START.x, START.y, backpack=[])
    place(payload, WORKER_2, 30, 8, backpack=[])
    place(payload, PIONEER, 30, 5, backpack=["Medicine"])
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_MINE, target=MINE, name="stone", started=round_no,
    )


def _fail_move(payload, round_no):
    payload["roundNo"] = round_no
    payload["lastRoundRoleActionResults"] = {str(WORKER_1): False}


def test_two_failed_moves_leave_the_failed_step(make_payload):
    """同一目标连续两回合 move 失败后，下一回合不再走失败步，并离开堵点。"""
    payload = make_payload(roundNo=10)
    _scene(payload, 10)
    first = decide(payload)
    assert action_of(first, WORKER_1) == "move"
    failed = move_pos(first, WORKER_1)
    assert failed is not None
    assert failed != START

    _fail_move(payload, 11)
    second = decide(payload)
    assert move_pos(second, WORKER_1) == failed

    _fail_move(payload, 12)
    third = decide(payload)
    step = move_pos(third, WORKER_1)
    assert step is not None
    assert step != failed
    assert step != START
    assert action_of(third, WORKER_1) == "move"


def test_blocked_stands_leave_the_jam_after_two_turns(make_payload):
    """原目标周围落脚点连续两回合被挡住后，下一回合离开堵点，不踩失败格。"""
    payload = make_payload(roundNo=10)
    _scene(payload, 10)
    robots = []
    for index, (x, y) in enumerate(
        (
            (13, 2), (14, 2), (15, 2),
            (13, 3), (15, 3),
            (13, 4), (14, 4), (15, 4),
        ),
        start=1,
    ):
        robots.append(
            {
                "id": 50000 + index,
                "pos": {"x": x, "y": y},
                "roleType": "smallRobot",
                "health": 40,
                "abnormalState": "",
                "targetTeam": "challenger",
            }
        )
    payload["robot"] = {"roles": robots}
    first = decide(payload)
    assert move_pos(first, WORKER_1) is None
    rec = tasks_mod.MEMORY.move_block[WORKER_1]
    assert isinstance(rec, MoveBlock)
    assert rec.decision_blocked is True
    failed = rec.failed_step

    payload["roundNo"] = 11
    second = decide(payload)
    assert move_pos(second, WORKER_1) is None

    payload["roundNo"] = 12
    third = decide(payload)
    step = move_pos(third, WORKER_1)
    assert step is not None
    assert step != START
    assert step != failed
    assert step != JAM
    assert action_of(third, WORKER_1) == "move"


def test_pioneer_reroute_avoids_rocket_stand_and_build(make_payload):
    """开拓者换向时不踩火箭格、站位，也不踩别人本回合要 build 的格子。"""
    payload = fresh(make_payload(roundNo=10))
    payload["phaseTask"] = ""
    _pocket(payload)
    here = Pos(10, 21)
    place(payload, PIONEER, here.x, here.y, backpack=["Medicine"])
    place(payload, WORKER_1, 4, 4, backpack=[])
    place(payload, WORKER_2, 5, 4, backpack=[])
    turn = World.load(payload)
    pioneer = turn.pioneer()
    assert pioneer is not None
    rockets = set(_tower_sites(turn))
    stand = _gun_stand(turn)
    assert rockets == {Pos(9, 24), Pos(9, 22), Pos(8, 22)}
    assert stand == Pos(9, 23)
    build_at = Pos(10, 20)
    failed = Pos(11, 21)
    rec = block_record(pioneer.unit_id)
    rec.streak = 2
    rec.goal = MINE
    rec.failed_step = failed
    commands = {
        WORKER_1: {
            "action": "build",
            "targetPos": [{"x": build_at.x, "y": build_at.y}],
            "name": "wall",
        }
    }
    changed, step = reroute_if_blocked_two_turns(
        turn, pioneer, MINE, set(), set(), commands,
    )
    assert changed is True
    assert step is not None
    assert step != here
    assert step != failed
    assert step not in rockets
    assert step != stand
    assert step != build_at
