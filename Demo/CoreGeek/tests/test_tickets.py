"""工人任务单:计划函数、两名工人分领、石头一次采够再砌。"""

import agent.tasks as tasks_mod
from agent.brain import _wall_order, decide
from agent.protocol import Pos, distance
from agent.tickets import (
    BUILD_TOWER,
    BUILD_WALL,
    EVOLVE,
    UPGRADE_TOWER,
    UPGRADE_WALL,
    claim_next_ticket,
    plan_big_task,
    plan_pioneer_task,
    worker_ticket_order,
)
from agent.world import World

from tests.helpers import (
    PIONEER,
    WORKER_1,
    WORKER_2,
    action_of,
    drop_walls,
    fresh,
    move_pos,
    place,
)


def _strip_weapons(payload):
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") not in ("gatling", "railgun", "rocket", "wall")
    ]


def test_plan_three_rockets_with_gold_skips_mining(make_payload):
    """缺 3 门且金币 >= 75:建造数 3、金币 75,小任务里没有先采矿。"""
    payload = fresh(make_payload(roundNo=1))
    _strip_weapons(payload)
    payload["teamOur"]["goldNum"] = 75
    turn = World.load(payload)
    plan = plan_big_task(turn, BUILD_TOWER)
    assert plan.name == BUILD_TOWER
    assert plan.claimable
    assert plan.count == 3
    assert plan.material == "gold"
    assert plan.material_count == 75
    assert plan.steps
    assert all(step.kind != "collect" for step in plan.steps)
    assert any(step.kind == "build" and step.count == 3 for step in plan.steps)


def test_plan_walls_collect_batch_then_build(make_payload):
    """缺 N 块墙时石头数是 N,小任务先整批采集再建造,不是一块一块交替。"""
    payload = fresh(make_payload(roundNo=1))
    drop_walls(payload)
    turn = World.load(payload)
    missing = len(_wall_order(turn))
    assert missing > 1
    plan = plan_big_task(turn, BUILD_WALL)
    assert plan.name == BUILD_WALL
    assert plan.claimable
    assert plan.count == missing
    assert plan.material == "stone"
    assert plan.material_count == missing
    kinds = [step.kind for step in plan.steps]
    assert kinds == ["collect", "build"]
    assert plan.steps[0].count == missing
    assert plan.steps[1].count == missing
    assert kinds != ["collect", "build"] * missing


def test_upgrade_tickets_need_standing_targets(make_payload):
    payload = fresh(make_payload(roundNo=1))
    _strip_weapons(payload)
    turn = World.load(payload)
    towers = plan_big_task(turn, UPGRADE_TOWER)
    walls = plan_big_task(turn, UPGRADE_WALL)
    assert towers.claimable is False
    assert towers.count == 0
    assert walls.claimable is False
    assert walls.count == 0


def test_two_idle_workers_split_tower_and_wall(make_payload):
    """开局两名空闲工人:一人领建炮,另一人领建墙。领走建炮后它低于建墙。"""
    payload = fresh(make_payload(roundNo=1))
    _strip_weapons(payload)
    payload["teamOur"]["goldNum"] = 75
    turn = World.load(payload)
    memory = tasks_mod.Memory()
    first = claim_next_ticket(turn, memory, WORKER_1)
    assert first == BUILD_TOWER
    order = worker_ticket_order(memory.ticket_penalty)
    assert order.index(BUILD_WALL) < order.index(BUILD_TOWER)
    second = claim_next_ticket(turn, memory, WORKER_2)
    assert second == BUILD_WALL
    assert first != second

    place(payload, WORKER_1, 12, 16, backpack=[])
    place(payload, WORKER_2, 16, 16, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    response = decide(payload)
    owners = tasks_mod.MEMORY.ticket_owner
    assert owners[WORKER_1] == BUILD_TOWER
    assert owners[WORKER_2] == BUILD_WALL
    assert owners.get(PIONEER) != BUILD_TOWER
    assert owners.get(PIONEER) != BUILD_WALL
    assert action_of(response, WORKER_2) != "build"


def test_wall_ticket_mines_until_stones_then_builds(make_payload):
    """已经领了建墙:石头不够就去采石,够了才砌。"""
    payload = fresh(make_payload(roundNo=35))
    drop_walls(payload)
    turn = World.load(payload)
    missing = len(_wall_order(turn))
    assert missing > 1
    place(payload, WORKER_1, 12, 21, backpack=["stone"])
    place(payload, WORKER_2, 4, 4, backpack=[])
    place(payload, PIONEER, 18, 18, backpack=["Medicine"])
    tasks_mod.MEMORY.ticket_owner[WORKER_1] = BUILD_WALL
    tasks_mod.MEMORY.ticket_penalty[BUILD_WALL] = 2
    short = decide(payload)
    assert action_of(short, WORKER_1) != "build"
    assert action_of(short, WORKER_1) in {"move", "collect"}
    if action_of(short, WORKER_1) == "move":
        step = move_pos(short, WORKER_1)
        assert step is not None
        mines = [
            pos for pos, kind in World.load(payload).all_mines()
            if kind == "stone"
        ]
        start = Pos(12, 21)
        nearest = min(mines, key=lambda pos: (distance(start, pos), pos.x, pos.y))
        assert distance(step, nearest) < distance(start, nearest)

    tasks_mod.MEMORY = tasks_mod.Memory()
    place(payload, WORKER_1, 12, 21, backpack=["stone"] * missing)
    tasks_mod.MEMORY.ticket_owner[WORKER_1] = BUILD_WALL
    ready = decide(payload)
    command = ready[str(WORKER_1)]
    assert command["action"] == "build"
    assert command.get("name") == "wall"


def test_pioneer_plan_is_only_self_evolve(make_payload):
    payload = fresh(make_payload(roundNo=10))
    turn = World.load(payload)
    plan = plan_pioneer_task(turn)
    assert plan.name == EVOLVE
    assert [step.kind for step in plan.steps] == ["answer", "walk"]
    assert plan_big_task(turn, EVOLVE).claimable is False
