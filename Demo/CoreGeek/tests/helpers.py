"""P1–P3 场景构造帮手。不改生产代码。"""

from __future__ import annotations

from typing import Any

from agent.brain import _wall_order
from agent.protocol import Pos, WEAPON_BUILD_COST, distance
from agent.world import World

TOWER_KINDS = ("gatling", "railgun", "rocket")
WORKER_1 = 10010
WORKER_2 = 10012
PIONEER = 10011
GATLING = 10020
RAILGUN = 10030
ROCKET = 10040


def role_by_id(payload: dict[str, Any], unit_id: int) -> dict[str, Any]:
    for role in payload["teamOur"]["roles"]:
        if role["id"] == unit_id:
            return role
    raise KeyError(unit_id)


def fresh(payload: dict[str, Any]) -> dict[str, Any]:
    """清掉样例里的上回合失败标记,避免 collect 被 last_ok=false 跳过。"""
    payload["lastRoundRoleActionResults"] = {}
    payload["errors"] = []
    return payload


def place(
    payload: dict[str, Any],
    unit_id: int,
    x: int,
    y: int,
    backpack: list[str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    role = role_by_id(payload, unit_id)
    role["pos"] = {"x": x, "y": y}
    if backpack is not None:
        role["backpack"] = list(backpack)
    for key, value in fields.items():
        role[key] = value
    return role


def drop_walls(payload: dict[str, Any]) -> None:
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"] if role.get("roleType") != "wall"
    ]


def fill_walls(payload: dict[str, Any]) -> None:
    drop_walls(payload)
    order = _wall_order(World.load(payload))
    for index, cell in enumerate(order):
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


def tower_cells(payload: dict[str, Any]) -> list[Pos]:
    return [
        Pos(role["pos"]["x"], role["pos"]["y"])
        for role in payload["teamOur"]["roles"]
        if role.get("roleType") in TOWER_KINDS
    ]


def nearest_tower(pos: Pos, payload: dict[str, Any]) -> Pos:
    cells = tower_cells(payload)
    return min(
        cells,
        key=lambda cell: (max(abs(pos.x - cell.x), abs(pos.y - cell.y)), cell.x, cell.y),
    )


def action_of(response: dict[str, Any], unit_id: int) -> str | None:
    command = response.get(str(unit_id))
    if not command:
        return None
    return command.get("action")


def move_pos(response: dict[str, Any], unit_id: int) -> Pos | None:
    command = response.get(str(unit_id))
    if not command or command.get("action") != "move":
        return None
    raw = command["targetPos"][0]
    return Pos(int(raw["x"]), int(raw["y"]))


def park_other_worker_building(payload: dict[str, Any]) -> None:
    """让 10012 带着石头站在墙位旁,避免和第二名工人抢路。"""
    place(payload, WORKER_2, 12, 21, backpack=["stone", "stone", "stone", "stone"])


def _zone_kind(payload: dict[str, Any], x: int, y: int) -> str | None:
    for zone in payload.get("mapInfo", {}).get("zones") or ():
        pos = zone.get("pos") or {}
        if int(pos.get("x") or 0) == x and int(pos.get("y") or 0) == y:
            return str(zone.get("neutralType") or "") or None
    return None


def apply_turn(
    payload: dict[str, Any],
    response: dict[str, Any],
    *,
    next_round: bool = True,
) -> dict[str, Any]:
    """把本回合指令写回下一帧,禁止用 place 瞬移冒充多回合。

    move 改坐标;非墙 build 扣 25 金并 append 武器;collect 进背包;
    lastRoundRoleActionResults 记下本回合有指令的角色。
    """
    results: dict[str, bool] = {}
    for key, command in response.items():
        unit_id = int(key)
        action = command.get("action")
        results[str(unit_id)] = True
        if action == "move":
            raw = command["targetPos"][0]
            role = role_by_id(payload, unit_id)
            role["pos"] = {"x": int(raw["x"]), "y": int(raw["y"])}
        elif action == "build":
            name = str(command.get("name") or "")
            if name == "wall":
                continue
            raw = command["targetPos"][0]
            gold = int(payload["teamOur"].get("goldNum") or 0)
            payload["teamOur"]["goldNum"] = gold - 25
            payload["teamOur"]["roles"].append(
                {
                    "id": 50000 + len(payload["teamOur"]["roles"]),
                    "pos": {"x": int(raw["x"]), "y": int(raw["y"])},
                    "roleType": name,
                    "health": 1000,
                    "level": 1,
                    "attackPower": 20 if name == "rocket" else 10,
                    "attackRange": 10,
                    "backpack": [],
                }
            )
        elif action == "collect":
            raw = command["targetPos"][0]
            kind = _zone_kind(payload, int(raw["x"]), int(raw["y"]))
            if kind:
                role_by_id(payload, unit_id)["backpack"].append(kind)
    payload["lastRoundRoleActionResults"] = results
    if next_round:
        payload["roundNo"] = int(payload["roundNo"]) + 1
    return payload
