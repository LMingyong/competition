"""防御回归测试:基于真实比赛失败的朝向修复 + 人手分配修复。

背景:logs/teamB.log(defender 阵营)中,白天所有工人被派去采矿/交易,
从不建墙,导致第 71 回合机器人涌来时基地无墙可守而失败。
本文件针对两项修复做回归:
  1. brain._wall_order / brain._tower_sites 优先朝向地图中央建防御;
     第一天只按左右朝向中心砌约 50% 围墙,并沿外圈连续建造;
  2. brain._worker_day 在第 30 回合砌墙阶段、墙未建齐且无法升塔时优先建墙。
"""

from __future__ import annotations

from agent.brain import (
    _center_facing_east,
    _center_facing_sides,
    _gun_stand,
    _on_incoming_side,
    _tower_sites,
    _wall_order,
    _wall_ring,
    decide,
)
from agent.protocol import Pos, distance
from agent.world import World

VALID_ACTIONS = {
    "move", "collect", "build", "attack", "sell", "buy", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop",
}


def _strip_roles(payload, keep_wall=False):
    """把 roles 精简为:基地 + 3 塔 + 可选墙 + 2 工人,便于构造场景。"""
    station_extra = {"attackPower": 0, "attackRange": 0}
    kept = []
    for role in payload["teamOur"]["roles"]:
        rtype = role.get("roleType")
        if rtype == "station":
            kept.append({**role, **station_extra})
        elif rtype in ("gatling", "railgun", "rocket"):
            kept.append({**role, **station_extra})
        elif rtype == "wall" and keep_wall:
            kept.append({**role, **station_extra})
        elif rtype in ("worker", "pioneer"):
            kept.append(role)
    payload["teamOur"]["roles"] = kept
    return payload


def _set_station(payload, x, y):
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") == "station":
            role["pos"] = {"x": x, "y": y}
            return
    raise AssertionError("payload 中没有 station")


def _assert_day1_center_walls(world: World) -> tuple[Pos, ...]:
    """第一天墙位仍是朝向地图中心的半圈,但沿外圈连续砌,不再按距中心跳格。"""
    ring = _wall_ring(world)
    order = _wall_order(world)
    facing = _center_facing_sides(world)
    assert ring, "地图应有完整墙圈"
    assert order, "第一天应有朝向中心的可建墙位"
    assert facing in (frozenset({"east"}), frozenset({"west"})), (
        f"来敌方向只看左右: facing={facing}"
    )
    ratio = len(order) / len(ring)
    assert 0.4 <= ratio <= 0.6, (
        f"第一天墙位应约为整圈的 50%: got {len(order)}/{len(ring)}={ratio:.2f}"
    )
    assert all(_on_incoming_side(pos, world) for pos in order), (
        f"第一天墙位必须在朝向中心的左/右半圈: facing={sorted(facing)} order={order}"
    )
    for left, right in zip(order, order[1:]):
        assert distance(left, right) <= 2, (
            f"建墙应沿圈连续,相邻计划格不能跳开: {left} -> {right}"
        )
    back_cells = [pos for pos in ring if not _on_incoming_side(pos, world)]
    assert back_cells, "远离中心的半圈应留空作出入口"
    assert not (set(order) & set(back_cells)), "第一天不得把背向中心的墙排进计划"
    return order


def test_wall_order_faces_map_center(make_payload):
    """朝向修复:挑战者(左上)第一天只建朝东(地图中心)的半圈围墙。"""
    payload = make_payload(roundNo=1)
    payload = _strip_roles(payload)
    world = World.load(payload)
    order = _assert_day1_center_walls(world)
    assert _center_facing_east(world) is True
    assert _center_facing_sides(world) == frozenset({"east"})
    assert all(pos.x >= 11 for pos in order)


def test_wall_order_faces_map_center_for_defender(make_payload):
    """朝向修复:防守者(右下)第一天只建朝西(地图中心)的半圈围墙。"""
    payload = make_payload(roundNo=1)
    payload = _strip_roles(payload)
    payload["teamOur"]["type"] = "defender"
    _set_station(payload, 30, 10)
    world = World.load(payload)
    order = _assert_day1_center_walls(world)
    assert _center_facing_east(world) is False
    assert _center_facing_sides(world) == frozenset({"west"})
    assert all(pos.x <= 30 for pos in order)


def test_day1_wall_quota_is_about_half(make_payload):
    """数量:第一天计划墙位约为完整外圈的一半,两侧阵营都成立。"""
    payload = make_payload(roundNo=1)
    payload = _strip_roles(payload)
    challenger = World.load(payload)
    _assert_day1_center_walls(challenger)
    payload["teamOur"]["type"] = "defender"
    _set_station(payload, 30, 10)
    _assert_day1_center_walls(World.load(payload))


def test_tower_sites_form_one_pocket(make_payload):
    """三门火箭围住中间一格空地,站上去能同时挨到三门。"""
    payload = make_payload(roundNo=1)
    payload = _strip_roles(payload)
    world = World.load(payload)
    sites = _tower_sites(world)
    stand = _gun_stand(world)
    assert sites == (Pos(12, 24), Pos(12, 22), Pos(13, 22))
    assert stand == Pos(12, 23)
    assert stand is not None
    assert all(distance(stand, site) == 1 for site in sites)

    payload["teamOur"]["type"] = "defender"
    _set_station(payload, 30, 10)
    defender = World.load(payload)
    sites = _tower_sites(defender)
    stand = _gun_stand(defender)
    assert sites == (Pos(29, 9), Pos(29, 11), Pos(28, 11))
    assert stand == Pos(29, 10)
    assert stand is not None
    assert all(distance(stand, site) == 1 for site in sites)


def test_worker_builds_wall_when_towers_done(make_payload):
    """人手分配:砌墙阶段 3 塔已建完、墙未建齐、工人紧邻墙位且带足 stone 时,直接建墙。"""
    payload = make_payload(roundNo=35)
    payload = _strip_roles(payload)
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"] if role.get("roleType") != "wall"
    ]
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") == "worker":
            # 紧邻东侧墙位(13,21),带足石头应直接 build wall
            role["pos"] = {"x": 12, "y": 21}
            role["backpack"] = ["stone", "stone", "stone", "stone", "stone"]
            break
    response = decide(payload)
    _validate_decision(response, payload)
    builds = [
        cmd for cmd in response.values()
        if cmd["action"] == "build" and cmd.get("name") == "wall"
    ]
    assert builds, "墙未建齐且工人紧邻墙位时应直接建墙(不应去卖矿/采矿)"

    sells = [cmd for cmd in response.values() if cmd["action"] == "sell"]
    assert not sells, "墙未建齐时不应出现 sell(不被卖矿抢占)"
    for cmd in builds:
        target = cmd["targetPos"][0]
        assert target["x"] >= 11, (
            f"第一天应建朝东半圈,不应建西侧背面: {target}"
        )


def test_day1_worker_does_not_build_back_wall(make_payload):
    """第一天工人即使站在背面墙位旁,也不得把墙建在背向地图中心的一侧。"""
    payload = make_payload(roundNo=1)
    payload = _strip_roles(payload)
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"] if role.get("roleType") != "wall"
    ]
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") == "worker":
            # (9,23) 紧邻西侧背面墙位 (8,23),且够不着朝向中心的墙
            role["pos"] = {"x": 9, "y": 23}
            role["backpack"] = ["stone", "stone", "stone", "stone", "stone"]
            break
    response = decide(payload)
    _validate_decision(response, payload)
    for cmd in response.values():
        if cmd["action"] != "build" or cmd.get("name") != "wall":
            continue
        target = cmd["targetPos"][0]
        assert target["x"] >= 11, (
            f"第一天不得在西侧背面建墙: {target}"
        )


def test_worker_builds_wall_over_selling(make_payload):
    """人手分配:砌墙阶段墙未建齐、工人紧邻墙位且背包携 iron/copper 时,优先建墙而非卖矿。"""
    payload = make_payload(roundNo=35)
    payload = _strip_roles(payload)
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"] if role.get("roleType") != "wall"
    ]
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") == "worker":
            # 紧邻墙位、带足石头,同时携铁/铜(可卖)验证不被 sell 抢占
            role["pos"] = {"x": 12, "y": 21}
            role["backpack"] = ["stone", "stone", "stone", "stone", "stone"]
            break
    response = decide(payload)
    _validate_decision(response, payload)
    sells = [
        cmd for cmd in response.values() if cmd["action"] == "sell"
    ]
    assert not sells, "墙未建齐时应优先建墙而非卖矿(不应出现 sell)"
    assert any(
        cmd.get("name") == "wall"
        for cmd in response.values() if cmd["action"] == "build"
    ), "优先建墙而非卖矿"


def test_worker_returns_to_selling_when_walls_done(make_payload):
    """人手分配:朝向中心的约一半墙已建齐时,工人回到卖矿路线(不再强制建墙)。"""
    payload = make_payload(roundNo=1)
    payload = _strip_roles(payload)
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"] if role.get("roleType") != "wall"
    ]
    world = World.load(payload)
    for index, cell in enumerate(_wall_order(world)):
        payload["teamOur"]["roles"].append(
            {
                "id": 40000 + index,
                "pos": {"x": cell.x, "y": cell.y},
                "roleType": "wall",
                "health": 1000,
                "level": 1,
                "attackPower": 0,
                "attackRange": 0,
                "backpack": [],
            }
        )
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") == "worker":
            role["pos"] = {"x": 5, "y": 23}
            role["backpack"] = ["stone", "iron", "copper"]
            break
    response = decide(payload)
    _validate_decision(response, payload)
    builds = [
        cmd for cmd in response.values()
        if cmd["action"] == "build"
    ]
    for cmd in builds:
        assert cmd.get("name") != "wall", \
            "第一天朝向中心的墙已建齐不应再强制建墙(回到经营路线)"


def test_decide_contract_holds_with_wall_priority(make_payload):
    """整链校验:开启建墙优先级后,decide 输出仍符合接口契约。"""
    payload = make_payload(roundNo=1)
    payload = _strip_roles(payload)
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"] if role.get("roleType") != "wall"
    ]
    for role in payload["teamOur"]["roles"]:
        if role.get("roleType") == "worker":
            role["backpack"] = ["stone"]
            break
    response = decide(payload)
    _validate_decision(response, payload)


def _validate_decision(response, payload):
    assert isinstance(response, dict)
    unique = {str(role["id"]) for role in payload["teamOur"].get("roles", [])}
    assert set(response.keys()) <= unique, "指令 key 必须属于我方角色"
    for key, command in response.items():
        assert command["action"] in VALID_ACTIONS, f"非法动作: {command['action']}"
