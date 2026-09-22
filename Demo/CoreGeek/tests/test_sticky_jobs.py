"""跨回合任务连续:采矿不换目标、失败重试、工种锁矿、开拓者冷却等待、夜间人塔配对粘住。"""

import agent.tasks as tasks_mod
from agent.brain import _tower_sites, decide
from agent.jobs import KIND_ACCEPT, KIND_MINE, KIND_MAN_TOWER, KIND_SHOP, KIND_TOWER, Job
from agent.protocol import Pos, distance
from agent.world import World

from tests.helpers import (
    GATLING,
    PIONEER,
    WORKER_1,
    WORKER_2,
    action_of,
    fill_walls,
    fresh,
    move_pos,
    place,
)

COPPER_NEAR = Pos(7, 2)
COPPER_FAR = Pos(22, 26)
SHOP = Pos(25, 20)
TASK_1 = Pos(14, 14)

VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS


def _park_economy(payload):
    fill_walls(payload)
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])


def _robots(*cells):
    roles = []
    for index, (x, y) in enumerate(cells, start=1):
        roles.append(
            {
                "id": 30000 + index,
                "pos": {"x": x, "y": y},
                "roleType": "smallRobot",
                "health": 40,
                "abnormalState": "",
                "targetTeam": "challenger",
            }
        )
    return {"roles": roles}


def test_miner_keeps_copper_when_gold_still_short(make_payload):
    """金币一直不够买武器券时,采矿 Job 不得中途换矿。"""
    payload = fresh(make_payload(roundNo=10))
    payload["teamOur"]["goldNum"] = 20
    _park_economy(payload)
    start = Pos(16, 8)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    first = decide(payload)
    _validate(first, payload)
    assert action_of(first, WORKER_1) == "move"
    step1 = move_pos(first, WORKER_1)
    assert step1 is not None
    assert distance(step1, COPPER_NEAR) < distance(start, COPPER_NEAR)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None
    assert job.kind == KIND_MINE
    assert job.target == COPPER_NEAR

    place(payload, WORKER_1, step1.x, step1.y, backpack=[])
    payload["roundNo"] = 11
    payload["teamOur"]["goldNum"] = 40
    second = decide(payload)
    _validate(second, payload)
    stuck = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert stuck is not None
    assert stuck.kind == KIND_MINE
    assert stuck.target == COPPER_NEAR
    assert action_of(second, WORKER_1) != "buy"
    step2 = move_pos(second, WORKER_1)
    if step2 is not None:
        assert distance(step2, COPPER_NEAR) < distance(step1, COPPER_NEAR)
        assert distance(step2, SHOP) >= distance(step1, SHOP)


def test_en_route_mine_is_not_replaced_by_shop(make_payload):
    """走到矿之前金币突然够买券:仍走向同一座矿,不改去商店。"""
    payload = fresh(make_payload(roundNo=10))
    payload["teamOur"]["goldNum"] = 20
    _park_economy(payload)
    start = Pos(16, 8)
    place(payload, WORKER_1, start.x, start.y, backpack=[])
    first = decide(payload)
    _validate(first, payload)
    assert action_of(first, WORKER_1) == "move"
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None
    assert job.kind == KIND_MINE

    step1 = move_pos(first, WORKER_1)
    assert step1 is not None
    place(payload, WORKER_1, step1.x, step1.y, backpack=[])
    payload["roundNo"] = 11
    payload["teamOur"]["goldNum"] = 200
    second = decide(payload)
    _validate(second, payload)
    stuck = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert stuck is not None
    assert stuck.kind == KIND_MINE
    assert stuck.target == COPPER_NEAR
    assert action_of(second, WORKER_1) != "buy"
    step2 = move_pos(second, WORKER_1)
    if step2 is not None:
        assert distance(step2, COPPER_NEAR) < distance(step1, COPPER_NEAR)
        assert distance(step2, SHOP) >= distance(step1, SHOP)


def test_locked_mine_collects_once_adjacent(make_payload):
    """人已经贴着锁定的矿时,下一回合 collect,而不是因为刷新状态改去别处。"""
    payload = fresh(make_payload(roundNo=10))
    payload["teamOur"]["goldNum"] = 20
    _park_economy(payload)
    place(payload, WORKER_1, 16, 8, backpack=[])
    first = decide(payload)
    _validate(first, payload)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None and job.target == COPPER_NEAR

    place(payload, WORKER_1, 8, 2, backpack=[])
    payload["roundNo"] = 11
    payload["teamOur"]["goldNum"] = 200
    second = decide(payload)
    _validate(second, payload)
    assert action_of(second, WORKER_1) == "collect"
    target = second[str(WORKER_1)]["targetPos"][0]
    assert (target["x"], target["y"]) == (COPPER_NEAR.x, COPPER_NEAR.y)


def test_failed_collect_walks_off_blocked_adjacent_mine(make_payload):
    """09:00 采矿:贴着铜矿但上回合 collect 失败时改走下一座,不得原地站着重试。"""
    payload = fresh(make_payload(roundNo=10))
    _park_economy(payload)
    place(payload, WORKER_1, 8, 2, backpack=[])
    payload["lastRoundRoleActionResults"] = {str(WORKER_1): False}
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "move"
    step = move_pos(response, WORKER_1)
    assert step is not None
    assert step != Pos(8, 2)


def test_workers_lock_distinct_mine_targets(make_payload):
    """墙已齐、两人同时采矿:Job 目标不得是同一座矿。"""
    payload = fresh(make_payload(roundNo=10))
    fill_walls(payload)
    place(payload, WORKER_1, 15, 12, backpack=[])
    place(payload, WORKER_2, 16, 12, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    job1 = tasks_mod.MEMORY.jobs.get(WORKER_1)
    job2 = tasks_mod.MEMORY.jobs.get(WORKER_2)
    assert job1 is not None and job2 is not None
    assert job1.kind == KIND_MINE
    assert job2.kind == KIND_MINE
    assert job1.target != job2.target


def test_pioneer_waits_on_task_cooldown_not_shop(make_payload):
    """任务点冷却中即使金够买券,开拓者仍停在任务点旁,不得去商店。"""
    payload = fresh(make_payload(roundNo=10, phaseTask=""))
    payload["teamOur"]["goldNum"] = 200
    payload["teamOur"]["playerTasks"] = [
        {
            "taskType": "自进化类1",
            "taskPosition": {"x": 14, "y": 14},
            "coldDownRounds": 12,
            "scoreReward": 50,
            "goldReward": 30,
            "isValid": False,
        },
        {
            "taskType": "自进化类2",
            "taskPosition": {"x": 17, "y": 17},
            "coldDownRounds": 12,
            "scoreReward": 50,
            "goldReward": 30,
            "isValid": False,
        },
    ]
    fill_walls(payload)
    place(payload, PIONEER, 13, 14, backpack=["Medicine"])
    place(payload, WORKER_1, 8, 24, backpack=[])
    place(payload, WORKER_2, 11, 25, backpack=[])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, PIONEER) not in {"buy", "sell", "acceptTask"}
    step = move_pos(response, PIONEER)
    if step is not None:
        assert distance(step, TASK_1) <= 1
        assert step != TASK_1
    job = tasks_mod.MEMORY.jobs.get(PIONEER)
    assert job is not None
    assert job.kind == KIND_ACCEPT


def test_night_weapon_assignment_stays_on_first_tower(make_payload):
    """黑夜先分到加特林后,即使另一人更近,下一回合仍由原操作者开火。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    place(payload, WORKER_1, 8, 24, backpack=[], health=220)
    place(payload, WORKER_2, 20, 10, backpack=[], health=220)
    place(payload, PIONEER, 20, 12, backpack=["Medicine"], health=200)
    payload["robot"] = _robots((6, 24))
    first = decide(payload)
    _validate(first, payload)
    assert first[str(GATLING)]["action"] == "attack"
    assert first[str(GATLING)]["controllerId"] == str(WORKER_1)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None
    assert job.kind == KIND_MAN_TOWER
    assert job.tower_id == GATLING

    place(payload, WORKER_1, 7, 24, backpack=[], health=220)
    place(payload, WORKER_2, 8, 24, backpack=[], health=220)
    payload["roundNo"] = 86
    second = decide(payload)
    _validate(second, payload)
    stuck = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert stuck is not None
    assert stuck.kind == KIND_MAN_TOWER
    assert stuck.tower_id == GATLING
    gatling = second.get(str(GATLING))
    if gatling is not None and gatling.get("action") == "attack":
        assert gatling["controllerId"] == str(WORKER_1)
    else:
        assert action_of(second, WORKER_1) == "move"
        step = move_pos(second, WORKER_1)
        assert step is not None
        assert distance(step, Pos(9, 24)) < distance(Pos(7, 24), Pos(9, 24))


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


def test_opening_workers_all_get_commands(make_payload):
    """开局 75 金、无塔无墙:两名工人都必须有指令,不得空闲站岗。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=1)))
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) in {"move", "build"}
    assert action_of(response, WORKER_2) in {"move", "build"}
    assert action_of(response, PIONEER) in {"move", "acceptTask"}


def test_opening_both_workers_walk_toward_towers(make_payload):
    """09:00 移动:开局两名工人都走近/建造塔,第二人不得被拆去远处铜矿。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=1)))
    starts = {WORKER_1: Pos(5, 23), WORKER_2: Pos(10, 16)}
    sites = _tower_sites(World.load(payload))
    response = decide(payload)
    _validate(response, payload)
    for uid, start in starts.items():
        action = action_of(response, uid)
        assert action in {"move", "build"}, f"{uid} 开局应去建塔,得到 {action}"
        if action == "move":
            step = move_pos(response, uid)
            assert step is not None
            assert min(distance(step, site) for site in sites) < min(
                distance(start, site) for site in sites
            ), f"{uid} 应从 {start} 走近塔,实际走到 {step}"
        else:
            raw = response[str(uid)]["targetPos"][0]
            built = Pos(int(raw["x"]), int(raw["y"]))
            assert built in sites
            assert response[str(uid)]["name"] in {"gatling", "railgun", "rocket"}


def test_locked_tower_spend_updates_gold_for_next_worker(make_payload):
    """同一回合第一人锁定建塔花掉 25 金后,第二人不得按旧账再建一座。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=10)))
    payload["teamOur"]["goldNum"] = 25
    sites = _tower_sites(World.load(payload))
    assert len(sites) >= 2
    place(payload, WORKER_1, 13, 24, backpack=[])
    place(payload, WORKER_2, 11, 21, backpack=[])
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_TOWER, target=sites[0], name="rocket", started=10,
    )
    tasks_mod.MEMORY.jobs[WORKER_2] = Job(
        kind=KIND_TOWER, target=sites[1], name="rocket", started=10,
    )
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "build"
    built = response[str(WORKER_1)]["targetPos"][0]
    assert (built["x"], built["y"]) == (sites[0].x, sites[0].y)
    assert response[str(WORKER_1)]["name"] == "rocket"
    assert action_of(response, WORKER_2) != "build"
