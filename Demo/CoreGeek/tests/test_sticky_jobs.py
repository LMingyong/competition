"""跨回合任务连续:采矿不换目标、失败重试、工种锁矿、开拓者冷却等待、夜间人塔配对粘住。"""

import agent.tasks as tasks_mod
from agent.brain import _gun_stand, _tower_sites, decide
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
NEAR_STONE = Pos(14, 3)
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
    """金币一直不够买武器券时,已选中的近矿不得中途换矿。"""
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
    assert distance(step1, NEAR_STONE) < distance(start, NEAR_STONE)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None
    assert job.kind == KIND_MINE
    assert job.target == NEAR_STONE

    place(payload, WORKER_1, step1.x, step1.y, backpack=[])
    payload["roundNo"] = 11
    payload["teamOur"]["goldNum"] = 40
    second = decide(payload)
    _validate(second, payload)
    stuck = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert stuck is not None
    assert stuck.kind == KIND_MINE
    assert stuck.target == NEAR_STONE
    assert action_of(second, WORKER_1) != "buy"
    step2 = move_pos(second, WORKER_1)
    if step2 is not None:
        assert distance(step2, NEAR_STONE) < distance(step1, NEAR_STONE)
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
    assert stuck.target == NEAR_STONE
    assert action_of(second, WORKER_1) != "buy"
    step2 = move_pos(second, WORKER_1)
    if step2 is not None:
        assert distance(step2, NEAR_STONE) < distance(step1, NEAR_STONE)
        assert distance(step2, SHOP) >= distance(step1, SHOP)


def test_locked_mine_collects_once_adjacent(make_payload):
    """人已经贴着锁定的近矿时,下一回合 collect,而不是因为刷新状态改去别处。"""
    payload = fresh(make_payload(roundNo=10))
    payload["teamOur"]["goldNum"] = 20
    _park_economy(payload)
    place(payload, WORKER_1, 16, 8, backpack=[])
    first = decide(payload)
    _validate(first, payload)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None and job.target == NEAR_STONE

    place(payload, WORKER_1, 14, 4, backpack=[])
    payload["roundNo"] = 11
    payload["teamOur"]["goldNum"] = 200
    second = decide(payload)
    _validate(second, payload)
    assert action_of(second, WORKER_1) == "collect"
    target = second[str(WORKER_1)]["targetPos"][0]
    assert (target["x"], target["y"]) == (NEAR_STONE.x, NEAR_STONE.y)


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
    """开局 75 金:两名工人都去建火箭。建墙单可以先领着,但这一回合不能去采石。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=1)))
    start1 = Pos(5, 23)
    start2 = Pos(10, 16)
    sites = _tower_sites(World.load(payload))
    response = decide(payload)
    _validate(response, payload)
    for uid, start in ((WORKER_1, start1), (WORKER_2, start2)):
        action = action_of(response, uid)
        assert action in {"move", "build"}, (uid, action)
        if action == "move":
            step = move_pos(response, uid)
            assert step is not None
            assert min(distance(step, site) for site in sites) < min(
                distance(start, site) for site in sites
            ), (uid, start, step)
        else:
            raw = response[str(uid)]["targetPos"][0]
            assert Pos(int(raw["x"]), int(raw["y"])) in sites
    assert tasks_mod.MEMORY.ticket_owner[WORKER_1] == "建炮"
    assert tasks_mod.MEMORY.ticket_owner[WORKER_2] == "建墙"


def test_opening_six_turns_workers_move_or_build(make_payload):
    """开局连续 8 回合:两名工人每回合都在移动或建造,不能空过,也不能踩炮手站位。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=1)))
    where = {
        WORKER_1: Pos(5, 23),
        WORKER_2: Pos(10, 16),
        PIONEER: Pos(10, 12),
    }
    stand = _gun_stand(World.load(payload))
    next_id = 61000
    for round_no in range(1, 9):
        payload["roundNo"] = round_no
        response = decide(payload)
        _validate(response, payload)
        for uid in (WORKER_1, WORKER_2):
            action = action_of(response, uid)
            assert action in {"move", "build"}, (
                f"第 {round_no} 回合 {uid} 停在 {where[uid]}: {response.get(str(uid))}"
            )
            if action == "move":
                step = move_pos(response, uid)
                assert step is not None and step != where[uid]
                assert step != stand
                where[uid] = step
                place(payload, uid, step.x, step.y)
            else:
                raw = response[str(uid)]["targetPos"][0]
                assert response[str(uid)]["name"] in {"gatling", "railgun", "rocket"}
                payload["teamOur"]["roles"].append({
                    "id": next_id,
                    "pos": {"x": int(raw["x"]), "y": int(raw["y"])},
                    "roleType": response[str(uid)]["name"],
                    "health": 1000,
                    "attackPower": 20,
                    "attackRange": 4,
                    "level": 1,
                    "backPackCapability": 0,
                    "backpack": [],
                })
                next_id += 1
                payload["teamOur"]["goldNum"] -= 25
        pioneer = response.get(str(PIONEER))
        if pioneer and pioneer.get("action") == "move":
            step = move_pos(response, PIONEER)
            if step is not None:
                where[PIONEER] = step
                place(payload, PIONEER, step.x, step.y)
        payload["lastRoundRoleActionResults"] = {
            str(uid): True for uid in response
        }
    built = {
        (role["pos"]["x"], role["pos"]["y"])
        for role in payload["teamOur"]["roles"]
        if role.get("roleType") == "rocket"
    }
    assert built == {(9, 24), (9, 22), (8, 22)}


def test_worker_on_unbuilt_rocket_steps_off(make_payload):
    """人站在还没建成的最外侧火箭上时,必须走开,不能空过。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=4)))
    payload["teamOur"]["goldNum"] = 25
    for index, (x, y) in enumerate(((9, 24), (9, 22))):
        payload["teamOur"]["roles"].append({
            "id": 62000 + index,
            "pos": {"x": x, "y": y},
            "roleType": "rocket",
            "health": 1000,
            "attackPower": 20,
            "attackRange": 4,
            "level": 1,
            "backPackCapability": 0,
            "backpack": [],
        })
    place(payload, WORKER_1, 8, 22, backpack=[])
    place(payload, WORKER_2, 4, 4, backpack=[])
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, WORKER_1) == "move"
    step = move_pos(response, WORKER_1)
    assert step is not None
    assert step != Pos(8, 22)
    assert step != Pos(9, 23)


def test_locked_tower_spend_updates_gold_for_next_worker(make_payload):
    """同一回合第一人锁定建塔花掉 25 金后,第二人不得按旧账再建一座。"""
    payload = _opening_no_buildings(fresh(make_payload(roundNo=10)))
    payload["teamOur"]["goldNum"] = 25
    sites = _tower_sites(World.load(payload))
    assert len(sites) >= 2
    place(payload, WORKER_1, 8, 24, backpack=[])
    place(payload, WORKER_2, 10, 22, backpack=[])
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
