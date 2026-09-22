"""入夜前 7 回合:只派一名炮手回口袋,另外两人去边缘采矿。"""

import pytest

import agent.tasks as tasks_mod
from agent.brain import RECALL_FROM, RECALL_ROUNDS, _is_edge_mine, decide
from agent.jobs import KIND_MAN_TOWER, KIND_MINE, KIND_WALL, Job
from agent.protocol import DAY_ROUNDS, Pos, distance
from agent.world import World

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
    place,
)

EDGE_COPPER = Pos(7, 2)
VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS


def test_round_60_still_mines_not_recall(make_payload):
    """回合 60 距入夜还有 10 回合:贴铜矿的建造工仍应 collect,不得提前回塔。"""
    payload = fresh(make_payload(roundNo=60))
    fill_walls(payload)
    place(payload, WORKER_1, 8, 2, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "collect"
    target = response[str(WORKER_1)]["targetPos"][0]
    assert (target["x"], target["y"]) == (EDGE_COPPER.x, EDGE_COPPER.y)


def _arm_pocket(payload):
    cells = ((12, 24), (12, 22), (13, 22))
    for unit_id, (x, y) in zip((GATLING, RAILGUN, ROCKET), cells):
        place(
            payload, unit_id, x, y,
            roleType="rocket", cooldown=0, level=1,
            attackRange=10, attackPower=20, health=1000,
        )


def test_midday_nobody_sent_to_gun_stand(make_payload):
    """白天中段口袋已就绪:没人被派去站位,贴着边缘矿的工人继续 collect。"""
    payload = fresh(make_payload(roundNo=40, phaseTask=""))
    payload["teamOur"]["goldNum"] = 0
    _arm_pocket(payload)
    fill_walls(payload)
    place(payload, WORKER_1, 8, 2, backpack=[])
    place(payload, WORKER_2, 8, 27, backpack=[])
    place(payload, PIONEER, 13, 14, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    stand = Pos(12, 23)
    assert action_of(response, WORKER_1) == "collect"
    assert all(cmd["action"] != "attack" for cmd in response.values())
    for uid, start in (
        (WORKER_1, Pos(8, 2)),
        (WORKER_2, Pos(8, 27)),
        (PIONEER, Pos(13, 14)),
    ):
        assert move_pos(response, uid) != stand
        job = tasks_mod.MEMORY.jobs.get(uid)
        if job is None:
            continue
        assert job.kind != KIND_MAN_TOWER
        step = move_pos(response, uid)
        if step is not None and job.target is not None:
            assert distance(step, job.target) < distance(start, job.target)


def test_round_63_still_before_recall_window(make_payload):
    """白天长 70 时,第 63 回合还差一回合才进入入夜前 7 回合。"""
    assert RECALL_ROUNDS == 7
    assert RECALL_FROM == DAY_ROUNDS - RECALL_ROUNDS + 1 == 64
    payload = fresh(make_payload(roundNo=63, phaseTask=""))
    payload["teamOur"]["goldNum"] = 0
    _arm_pocket(payload)
    fill_walls(payload)
    place(payload, WORKER_1, 8, 2, backpack=[])
    place(payload, WORKER_2, 8, 27, backpack=[])
    place(payload, PIONEER, 13, 14, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "collect"
    for uid in (WORKER_1, WORKER_2, PIONEER):
        job = tasks_mod.MEMORY.jobs.get(uid)
        if job is not None:
            assert job.kind != KIND_MAN_TOWER
        assert move_pos(response, uid) != Pos(12, 23)


@pytest.mark.parametrize("round_no", [64, 194, 324])
def test_last_seven_rounds_one_gunner_others_edge_mine(make_payload, round_no):
    """前三天入夜前 7 回合都一样:恰好一人走向站位,另外两人去边缘矿。"""
    payload = fresh(make_payload(roundNo=round_no, phaseTask=""))
    _arm_pocket(payload)
    drop_walls(payload)
    gunner_start = Pos(15, 20)
    miner_start = Pos(8, 2)
    pioneer_start = Pos(13, 14)
    place(payload, WORKER_1, gunner_start.x, gunner_start.y, backpack=[])
    place(
        payload, WORKER_2, miner_start.x, miner_start.y,
        backpack=["stone", "stone", "stone", "stone"],
    )
    place(payload, PIONEER, pioneer_start.x, pioneer_start.y, backpack=["Medicine"])
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_MINE, target=Pos(22, 26), name="copper", started=round_no,
    )
    tasks_mod.MEMORY.jobs[WORKER_2] = Job(
        kind=KIND_WALL, target=Pos(13, 21), name="wall", started=round_no,
    )
    response = decide(payload)
    _validate(response, payload)
    stand = Pos(12, 23)
    world = World.load(payload)
    assert all(cmd["action"] != "attack" for cmd in response.values())
    assert action_of(response, WORKER_1) == "move"
    assert action_of(response, WORKER_2) != "build"
    assert action_of(response, PIONEER) != "acceptTask"
    gunner_step = move_pos(response, WORKER_1)
    assert gunner_step is not None
    assert distance(gunner_step, stand) < distance(gunner_start, stand)
    gunner_job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert gunner_job is not None and gunner_job.kind == KIND_MAN_TOWER

    for uid, start in ((WORKER_2, miner_start), (PIONEER, pioneer_start)):
        job = tasks_mod.MEMORY.jobs.get(uid)
        assert job is not None
        assert job.kind == KIND_MINE
        assert job.kind != KIND_MAN_TOWER
        assert job.target is not None and _is_edge_mine(world, job.target)
        assert job.target != stand
        step = move_pos(response, uid)
        assert step != stand
        if step is not None:
            assert distance(step, job.target) < distance(start, job.target)
        else:
            assert action_of(response, uid) == "collect"
    assert sum(
        1 for uid in (WORKER_1, WORKER_2, PIONEER)
        if (tasks_mod.MEMORY.jobs.get(uid) or Job(kind="")).kind == KIND_MAN_TOWER
    ) == 1


def test_full_backpack_does_not_cross_center_to_vendor(make_payload):
    """回防窗口背包已满:在边缘待命或走向边缘矿,不去地图中央的小贩。"""
    payload = fresh(make_payload(roundNo=RECALL_FROM, phaseTask=""))
    _arm_pocket(payload)
    fill_walls(payload)
    place(payload, WORKER_1, 15, 20, backpack=[])
    place(
        payload, WORKER_2, 8, 2,
        backpack=["copper", "copper", "copper", "copper"],
        backPackCapability=4,
    )
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_2) != "sell"
    job = tasks_mod.MEMORY.jobs.get(WORKER_2)
    assert job is not None and job.kind == KIND_MINE
    assert job.target is not None
    world = World.load(payload)
    assert _is_edge_mine(world, job.target)
    assert job.target != Pos(20, 16)
    step = move_pos(response, WORKER_2)
    assert step != Pos(12, 23)
    if step is not None:
        assert distance(step, job.target) < distance(Pos(8, 2), job.target)


def test_late_day_pioneer_with_phase_task_stays(make_payload):
    """已有 phaseTask 时,回防也不得离开任务点周围一格。"""
    payload = fresh(make_payload(roundNo=66, phaseTask="请阅读task_1_alpha.md"))
    task = Pos(14, 14)
    place(payload, PIONEER, 13, 14, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, PIONEER) != "acceptTask"
    step = move_pos(response, PIONEER)
    if step is not None:
        assert distance(step, task) <= 1
        assert step != task


def test_early_day_worker_still_mines_or_builds(make_payload):
    """回合 10 非回防:墙已齐且贴铜矿时允许 collect,禁止全天回塔。"""
    payload = fresh(make_payload(roundNo=10))
    fill_walls(payload)
    place(payload, WORKER_1, 8, 2, backpack=[])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "collect"
    target = response[str(WORKER_1)]["targetPos"][0]
    assert (target["x"], target["y"]) == (7, 2)


def test_recall_does_not_emit_attack_in_day(make_payload):
    """回防仍是白天,整表不得出现 attack。"""
    payload = fresh(make_payload(roundNo=66))
    drop_walls(payload)
    place(payload, WORKER_1, 10, 10, backpack=[])
    response = decide(payload)
    _validate(response, payload)
    assert all(cmd["action"] != "attack" for cmd in response.values())


def test_night_miner_mans_tower_not_edge(make_payload):
    """黑夜矿工即使贴着边缘铜矿也要去操塔,不得 collect。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    place(payload, WORKER_1, 8, 24, backpack=[], health=220)
    place(payload, WORKER_2, 8, 2, backpack=[], health=220)
    place(payload, PIONEER, 8, 25, backpack=["Medicine"], health=200)
    payload["robot"] = {
        "roles": [
            {
                "id": 30001,
                "pos": {"x": 7, "y": 24},
                "roleType": "smallRobot",
                "health": 40,
                "abnormalState": "",
                "targetTeam": "challenger",
            }
        ]
    }
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_2) != "collect"
    gatling = response.get(str(GATLING))
    assert gatling is not None
    assert gatling["action"] == "attack"
    assert gatling["controllerId"] == str(WORKER_1)
    miner = response.get(str(WORKER_2))
    if miner is not None:
        assert miner["action"] == "move"
