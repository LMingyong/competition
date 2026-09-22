"""入夜前打断白天工单；夜里非炮手只在己方边缘安全区行动。"""

from types import SimpleNamespace

import agent.tasks as tasks_mod
from agent.brain import (
    _gun_stand,
    _tower_sites,
    decide,
    in_night_safe_zone,
    safe_zone_bounds,
)
from agent.jobs import KIND_HOLD, KIND_MAN_TOWER, KIND_MINE, KIND_WALL, Job, interrupt_task
from agent.protocol import DAY_ROUNDS, Pos, distance
from agent.tasks import Memory
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
    fresh,
    move_pos,
    place,
)

CENTER_COPPER = Pos(22, 26)
EDGE_COPPER = Pos(7, 2)
SAFE_STONE = Pos(4, 24)
STAND = Pos(9, 23)
WALL_SITE = Pos(13, 21)

VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}


def _validate(response, payload):
    unique = {str(role["id"]) for role in payload["teamOur"]["roles"]}
    assert set(response.keys()) <= unique
    for command in response.values():
        assert command["action"] in VALID_ACTIONS


def _arm_pocket(payload):
    cells = ((9, 24), (9, 22), (8, 22))
    for unit_id, (x, y) in zip((GATLING, RAILGUN, ROCKET), cells):
        place(
            payload, unit_id, x, y,
            roleType="rocket", cooldown=0, level=1,
            attackRange=10, attackPower=20, health=1000,
        )


def _move_station(payload, x, y):
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") == "station":
            role["pos"] = {"x": x, "y": y}
            return
    raise AssertionError("payload 里没有基地")


def test_safe_zone_matches_sample_bases(make_payload):
    """挑战者贴左缘，守方贴右缘。炮位仍是基地背后那一组。"""
    challenger = World.load(fresh(make_payload(roundNo=64, phaseTask="")))
    assert safe_zone_bounds(challenger) == (0, 4, 0, 31)
    assert in_night_safe_zone(challenger, SAFE_STONE)
    assert not in_night_safe_zone(challenger, EDGE_COPPER)
    assert not in_night_safe_zone(challenger, CENTER_COPPER)
    assert not in_night_safe_zone(challenger, STAND)
    assert _tower_sites(challenger) == (Pos(9, 24), Pos(9, 22), Pos(8, 22))
    assert _gun_stand(challenger) == STAND

    payload = fresh(make_payload(roundNo=64, phaseTask=""))
    _move_station(payload, 30, 10)
    defender = World.load(payload)
    assert safe_zone_bounds(defender) == (36, 40, 0, defender.height - 1)
    assert _tower_sites(defender) == (Pos(32, 9), Pos(32, 11), Pos(33, 11))
    assert _gun_stand(defender) == Pos(32, 10)
    assert in_night_safe_zone(defender, Pos(36, 10))
    assert in_night_safe_zone(defender, Pos(40, 31))
    assert not in_night_safe_zone(defender, Pos(32, 10))
    assert not in_night_safe_zone(defender, Pos(4, 24))
    assert not in_night_safe_zone(defender, CENTER_COPPER)


def test_interrupt_task_clears_job_and_claimed_order_without_lock():
    """工单锁不参与。大任务和小任务一起清掉。"""
    memory = Memory()
    memory.jobs[WORKER_1] = Job(
        kind=KIND_WALL, target=WALL_SITE, name="wall", started=40,
    )
    memory.orders = {
        WORKER_1: SimpleNamespace(kind="build_wall", assignee=WORKER_1, priority=4),
    }
    memory.ticket_owner = {WORKER_1: "建墙", WORKER_2: "升级墙"}
    memory.ticket_penalty = {"建墙": 2, "升级墙": 2}
    memory.ticket_hold = {WORKER_1}
    queued = SimpleNamespace(kind="upgrade_wall", assignee=WORKER_2, priority=1)
    memory.big_tasks = {"wall-upgrade": queued}
    interrupt_task(memory, SimpleNamespace(unit_id=WORKER_1), "dusk")
    interrupt_task(memory, SimpleNamespace(unit_id=WORKER_2), "night")
    assert WORKER_1 not in memory.jobs
    assert WORKER_1 not in memory.orders
    assert WORKER_1 not in memory.ticket_owner
    assert WORKER_2 not in memory.ticket_owner
    assert WORKER_1 not in memory.ticket_hold
    assert queued.assignee is None
    assert memory.interrupts == [(WORKER_1, "dusk"), (WORKER_2, "night")]


def test_round_63_keeps_wall_job(make_payload):
    """白天第 63 回合还不到入夜前 7 回合，建墙单继续。"""
    assert DAY_ROUNDS - 7 + 1 == 64
    payload = fresh(make_payload(roundNo=63, phaseTask=""))
    payload["teamOur"]["goldNum"] = 0
    _arm_pocket(payload)
    drop_walls(payload)
    place(payload, WORKER_1, 12, 20, backpack=["stone"])
    place(payload, WORKER_2, 16, 18, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_WALL, target=WALL_SITE, name="wall", started=40,
    )
    response = decide(payload)
    _validate(response, payload)
    job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert job is not None and job.kind == KIND_WALL
    assert move_pos(response, WORKER_1) != STAND


def test_dusk_interrupts_wall_and_mine_jobs(make_payload):
    """round_in_day >= 64：建墙/采矿单被打断；炮手走向站位；另一人去安全区。"""
    payload = fresh(make_payload(roundNo=64, phaseTask="请阅读task_1_alpha.md"))
    payload["teamOur"]["goldNum"] = 0
    _arm_pocket(payload)
    gunner_start = Pos(15, 20)
    other_start = Pos(18, 18)
    place(payload, WORKER_1, gunner_start.x, gunner_start.y, backpack=[])
    place(payload, WORKER_2, other_start.x, other_start.y, backpack=[])
    place(payload, PIONEER, 13, 14, backpack=["Medicine"])
    tasks_mod.MEMORY.jobs[WORKER_1] = Job(
        kind=KIND_WALL, target=WALL_SITE, name="wall", started=40,
    )
    tasks_mod.MEMORY.jobs[WORKER_2] = Job(
        kind=KIND_MINE, target=CENTER_COPPER, name="copper", started=40,
    )
    world = World.load(payload)
    response = decide(payload)
    _validate(response, payload)
    assert all(cmd["action"] != "attack" for cmd in response.values())

    gunner_step = move_pos(response, WORKER_1)
    assert gunner_step is not None
    assert distance(gunner_step, STAND) < distance(gunner_start, STAND)
    gunner_job = tasks_mod.MEMORY.jobs.get(WORKER_1)
    assert gunner_job is not None and gunner_job.kind == KIND_MAN_TOWER
    assert gunner_job.kind != KIND_WALL

    other = tasks_mod.MEMORY.jobs.get(WORKER_2)
    assert other is not None
    assert other.kind in {KIND_MINE, KIND_HOLD}
    assert other.target is not None
    assert other.target != CENTER_COPPER
    assert in_night_safe_zone(world, other.target)
    center = Pos(world.width // 2, world.height // 2)
    assert distance(other.target, center) > 8
    assert other.target != STAND
    other_step = move_pos(response, WORKER_2)
    assert other_step != STAND
    if other_step is not None:
        assert distance(other_step, other.target) < distance(other_start, other.target)
        assert other_step.x < other_start.x

    assert action_of(response, PIONEER) not in {"move", "collect", "build", "acceptTask"}
    pioneer_job = tasks_mod.MEMORY.jobs.get(PIONEER)
    assert pioneer_job is None or pioneer_job.kind != KIND_MAN_TOWER


def test_night_non_gunner_not_sent_to_center_mine(make_payload):
    """入夜后非炮手不继续白天那张中央矿，目标落在安全区。"""
    payload = fresh(make_payload(roundNo=85, phaseTask=""))
    _arm_pocket(payload)
    place(payload, WORKER_1, 9, 23, backpack=[], health=220)
    place(payload, WORKER_2, 16, 18, backpack=[], health=220)
    place(payload, PIONEER, 18, 16, backpack=["Medicine"], health=200)
    tasks_mod.MEMORY.jobs[WORKER_2] = Job(
        kind=KIND_MINE, target=CENTER_COPPER, name="copper", started=60,
    )
    tasks_mod.MEMORY.jobs[PIONEER] = Job(
        kind=KIND_WALL, target=WALL_SITE, name="wall", started=60,
    )
    payload["robot"] = {
        "roles": [
            {
                "id": 30001,
                "pos": {"x": 18, "y": 22},
                "roleType": "smallRobot",
                "health": 40,
                "abnormalState": "",
                "targetTeam": "challenger",
            }
        ]
    }
    response = decide(payload)
    _validate(response, payload)
    attacks = [cmd for cmd in response.values() if cmd["action"] == "attack"]
    assert len(attacks) == 1
    assert attacks[0]["controllerId"] == str(WORKER_1)
    assert action_of(response, WORKER_1) != "move"

    world = World.load(payload)
    center = Pos(world.width // 2, world.height // 2)
    for uid, start in ((WORKER_2, Pos(16, 18)), (PIONEER, Pos(18, 16))):
        assert action_of(response, uid) != "collect"
        job = tasks_mod.MEMORY.jobs.get(uid)
        assert job is not None and job.target is not None
        assert job.target != CENTER_COPPER
        assert job.target != WALL_SITE
        assert job.kind in {KIND_MINE, KIND_HOLD}
        assert in_night_safe_zone(world, job.target)
        assert distance(job.target, center) > 8
        step = move_pos(response, uid)
        assert step != STAND
        if step is not None:
            assert step != CENTER_COPPER
            assert distance(step, job.target) < distance(start, job.target)
            assert step.x < start.x
