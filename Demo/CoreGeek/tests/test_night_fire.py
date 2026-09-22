"""P2 贴塔开火:黑夜人已在塔旁且射程内有怪,必须 attack。"""

from agent.brain import decide
from agent.protocol import Pos, distance

from tests.helpers import (
    GATLING,
    PIONEER,
    RAILGUN,
    ROCKET,
    WORKER_1,
    WORKER_2,
    action_of,
    fresh,
    move_pos,
    place,
)

VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}


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


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS
        if command["action"] == "attack":
            assert isinstance(command.get("controllerId"), str)


def test_adjacent_hero_fires_gatling(make_payload):
    """工人贴着加特林、射程内有机器人 → attack 挂在塔 ID 上。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    place(payload, WORKER_1, 8, 24, backpack=[], health=220)
    place(payload, WORKER_2, 20, 10, backpack=[], health=220)
    place(payload, PIONEER, 20, 12, backpack=["Medicine"], health=200)
    payload["robot"] = _robots((7, 24))
    response = decide(payload)
    _validate(response, payload)
    command = response[str(GATLING)]
    assert command["action"] == "attack"
    assert command["controllerId"] == str(WORKER_1)


def test_attack_key_is_weapon_not_hero(make_payload):
    """开火指令的 key 是武器 ID,英雄自己不再挂 attack。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    place(payload, WORKER_1, 8, 24, backpack=[], health=220)
    place(payload, WORKER_2, 20, 10, backpack=[], health=220)
    place(payload, PIONEER, 20, 12, backpack=["Medicine"], health=200)
    payload["robot"] = _robots((7, 24))
    response = decide(payload)
    _validate(response, payload)
    assert action_of(response, GATLING) == "attack"
    hero_cmd = response.get(str(WORKER_1))
    if hero_cmd is not None:
        assert hero_cmd["action"] != "attack"


def test_rocket_cooldown_skips_attack(make_payload):
    """火箭冷却中不得 attack。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    place(payload, WORKER_1, 8, 26, backpack=[], health=220)
    place(payload, WORKER_2, 20, 10, backpack=[], health=220)
    place(payload, PIONEER, 20, 12, backpack=["Medicine"], health=200)
    place(payload, ROCKET, 9, 25, cooldown=2)
    payload["robot"] = _robots((8, 27))
    response = decide(payload)
    _validate(response, payload)
    rocket_cmd = response.get(str(ROCKET))
    if rocket_cmd is not None:
        assert rocket_cmd["action"] != "attack"


def test_no_robot_no_attack(make_payload):
    """黑夜贴塔但场上无机器人,不得空放 attack。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    place(payload, WORKER_1, 8, 24, backpack=[], health=220)
    place(payload, WORKER_2, 11, 25, backpack=[], health=220)
    place(payload, PIONEER, 8, 25, backpack=["Medicine"], health=200)
    payload["robot"] = {"roles": []}
    response = decide(payload)
    _validate(response, payload)
    assert all(cmd["action"] != "attack" for cmd in response.values())


def test_three_heroes_only_one_fires(make_payload):
    """黑夜即使三人都贴着塔,同一回合也只由一名炮手开一门。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    place(payload, WORKER_1, 8, 24, backpack=[], health=220)
    place(payload, WORKER_2, 11, 25, backpack=[], health=220)
    place(payload, PIONEER, 8, 25, backpack=["Medicine"], health=200)
    payload["robot"] = _robots((7, 23), (10, 26), (8, 26))
    response = decide(payload)
    _validate(response, payload)
    attacks = [cmd for cmd in response.values() if cmd["action"] == "attack"]
    assert len(attacks) == 1
    assert attacks[0]["controllerId"] == str(WORKER_1)
    assert action_of(response, WORKER_2) != "collect"


def test_gunner_on_pocket_rotates_rockets(make_payload):
    """人站在口袋空地上时,同一回合只开一门;冷却的那门换下一门。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    cells = ((12, 24), (12, 22), (13, 22))
    for unit_id, (x, y) in zip((GATLING, RAILGUN, ROCKET), cells):
        place(
            payload, unit_id, x, y,
            roleType="rocket", cooldown=0, level=1,
            attackRange=10, attackPower=20, health=1000,
        )
    place(payload, WORKER_1, 12, 23, backpack=[], health=220)
    place(payload, WORKER_2, 4, 4, backpack=[], health=220)
    place(payload, PIONEER, 5, 5, backpack=["Medicine"], health=200)
    payload["robot"] = _robots((20, 16))
    first = decide(payload)
    _validate(first, payload)
    attacks = [(key, cmd) for key, cmd in first.items() if cmd["action"] == "attack"]
    assert len(attacks) == 1
    first_id, first_cmd = attacks[0]
    assert first_cmd["controllerId"] == str(WORKER_1)
    assert action_of(first, WORKER_1) != "move"

    for role in payload["teamOur"]["roles"]:
        if str(role["id"]) == first_id:
            role["cooldown"] = 3
    payload["roundNo"] = 86
    second = decide(payload)
    _validate(second, payload)
    attacks = [(key, cmd) for key, cmd in second.items() if cmd["action"] == "attack"]
    assert len(attacks) == 1
    second_id, second_cmd = attacks[0]
    assert second_cmd["controllerId"] == str(WORKER_1)
    assert second_id != first_id


def test_recall_gunner_walks_toward_pocket(make_payload):
    """口袋三门火箭已建成时,回防窗口炮手往中间空地走。"""
    payload = fresh(make_payload(roundNo=66, phaseTask=""))
    cells = ((12, 24), (12, 22), (13, 22))
    for unit_id, (x, y) in zip((GATLING, RAILGUN, ROCKET), cells):
        place(
            payload, unit_id, x, y,
            roleType="rocket", cooldown=0, level=1,
            attackRange=10, attackPower=20, health=1000,
        )
    place(payload, WORKER_1, 5, 5, backpack=[])
    place(payload, WORKER_2, 6, 6, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    _validate(response, payload)
    step = move_pos(response, WORKER_2)
    assert step is not None
    stand = Pos(12, 23)
    assert distance(step, stand) < distance(Pos(6, 6), stand)


def test_night_only_one_fires_and_others_stay_off_stand(make_payload):
    """夜里仍只有一人开火;另外两人走向站位背后,不占中间空地。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    cells = ((12, 24), (12, 22), (13, 22))
    for unit_id, (x, y) in zip((GATLING, RAILGUN, ROCKET), cells):
        place(
            payload, unit_id, x, y,
            roleType="rocket", cooldown=0, level=1,
            attackRange=10, attackPower=20, health=1000,
        )
    place(payload, WORKER_1, 12, 23, backpack=[], health=220)
    place(payload, WORKER_2, 4, 4, backpack=[], health=220)
    place(payload, PIONEER, 5, 5, backpack=["Medicine"], health=200)
    payload["robot"] = _robots((20, 16))
    response = decide(payload)
    _validate(response, payload)
    attacks = [cmd for cmd in response.values() if cmd["action"] == "attack"]
    assert len(attacks) == 1
    assert attacks[0]["controllerId"] == str(WORKER_1)
    stand = Pos(12, 23)
    backs = (Pos(9, 24), Pos(9, 23))
    for uid, start in ((WORKER_2, Pos(4, 4)), (PIONEER, Pos(5, 5))):
        step = move_pos(response, uid)
        assert step != stand
        assert step is not None
        assert min(distance(step, cell) for cell in backs) < min(
            distance(start, cell) for cell in backs
        )


def test_night_edge_miner_parks_behind_instead_of_mining(make_payload):
    """夜里即使上一回合还在采边缘矿,也改停到基地背后,不占站位。"""
    import agent.tasks as tasks_mod
    from agent.jobs import KIND_MINE, Job

    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    cells = ((12, 24), (12, 22), (13, 22))
    for unit_id, (x, y) in zip((GATLING, RAILGUN, ROCKET), cells):
        place(
            payload, unit_id, x, y,
            roleType="rocket", cooldown=0, level=1,
            attackRange=10, attackPower=20, health=1000,
        )
    place(payload, WORKER_1, 12, 23, backpack=[], health=220)
    place(payload, WORKER_2, 8, 3, backpack=[], health=220)
    place(payload, PIONEER, 5, 5, backpack=["Medicine"], health=200)
    tasks_mod.MEMORY.jobs[WORKER_2] = Job(
        kind=KIND_MINE, target=Pos(7, 2), name="copper", started=80,
    )
    payload["robot"] = _robots((20, 16))
    response = decide(payload)
    _validate(response, payload)
    attacks = [cmd for cmd in response.values() if cmd["action"] == "attack"]
    assert len(attacks) == 1
    assert attacks[0]["controllerId"] == str(WORKER_1)
    assert action_of(response, WORKER_2) != "collect"
    stand = Pos(12, 23)
    backs = (Pos(9, 24), Pos(9, 23))
    start = Pos(8, 3)
    step = move_pos(response, WORKER_2)
    assert step is not None and step != stand
    assert min(distance(step, cell) for cell in backs) < min(
        distance(start, cell) for cell in backs
    )
    assert move_pos(response, PIONEER) != stand


def test_night_pioneer_with_phase_task_does_not_take_the_gun(make_payload):
    """有工人能操炮时,开拓者不必开火;同一回合仍然只有一门攻击。"""
    payload = fresh(make_payload(roundNo=85, phaseTask="请阅读task_1_alpha.md，获取任务信息"))
    place(payload, WORKER_1, 8, 24, backpack=[], health=220)
    place(payload, WORKER_2, 11, 25, backpack=[], health=220)
    place(payload, PIONEER, 8, 25, backpack=["Medicine"], health=200)
    payload["robot"] = _robots((7, 24))
    response = decide(payload)
    _validate(response, payload)
    attacks = [cmd for cmd in response.values() if cmd["action"] == "attack"]
    assert len(attacks) == 1
    assert attacks[0]["controllerId"] != str(PIONEER)
    assert action_of(response, PIONEER) not in {"acceptTask"}
