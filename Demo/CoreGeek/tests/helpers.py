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


def _hero_roles(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        role["id"]: role
        for role in payload["teamOur"]["roles"]
        if role.get("roleType") in ("worker", "pioneer")
    }


def _weapon_roles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        role for role in payload["teamOur"]["roles"]
        if role.get("roleType") in TOWER_KINDS
    ]


def apply_turn(payload: dict[str, Any], commands: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把上回合合法指令写回下一帧：坐标、金币、武器、背包、lastRoundRoleActionResults。

    只改 roundNo 或瞬移不算连续回合。move 必须邻格且可走才写回坐标；
    非墙 build 扣 25 金并把武器 append 进 roles；collect 把矿名放进背包。
    """
    world = World.load(payload)
    occupied = {unit.pos for unit in world.ours if unit.health > 0}
    heroes = _hero_roles(payload)
    mines = dict(world.all_mines())
    results: dict[str, bool] = {}
    next_id = 70000 + int(payload.get("roundNo") or 0) * 10
    for key, command in sorted(commands.items(), key=lambda item: int(item[0])):
        uid = int(key)
        role = heroes.get(uid)
        action = command.get("action")
        if role is None:
            results[key] = action in {"attack", "remove"}
            continue
        cur = Pos(int(role["pos"]["x"]), int(role["pos"]["y"]))
        unit = next((item for item in world.ours if item.unit_id == uid), None)
        ok = False
        if action == "move":
            raw = (command.get("targetPos") or [None])[0]
            dest = Pos(int(raw["x"]), int(raw["y"])) if isinstance(raw, dict) else None
            blocked = set(world.blocked(unit)) if unit is not None else set()
            others = set(occupied) - {cur}
            if (
                dest is not None
                and unit is not None
                and dest not in blocked
                and dest not in others
                and world.land(dest)
                and distance(cur, dest) == 1
                and dest != cur
            ):
                role["pos"] = dest.dump()
                occupied.discard(cur)
                occupied.add(dest)
                ok = True
        elif action == "build" and command.get("name") != "wall":
            raw = (command.get("targetPos") or [None])[0]
            target = Pos(int(raw["x"]), int(raw["y"])) if isinstance(raw, dict) else None
            occ = World.load(payload).occupied_for_build()
            if (
                target is not None
                and role.get("roleType") == "worker"
                and cur != target
                and distance(cur, target) <= 1
                and target not in occ
                and payload["teamOur"]["goldNum"] >= WEAPON_BUILD_COST
                and len(_weapon_roles(payload)) < 3
            ):
                payload["teamOur"]["goldNum"] -= WEAPON_BUILD_COST
                payload["teamOur"]["roles"].append(
                    {
                        "id": next_id + target.x * 40 + target.y,
                        "pos": target.dump(),
                        "roleType": command.get("name") or "rocket",
                        "health": 1000,
                        "level": 1,
                        "attackPower": 20,
                        "attackRange": 10,
                        "backpack": [],
                    }
                )
                ok = True
        elif action == "collect":
            raw = (command.get("targetPos") or [None])[0]
            target = Pos(int(raw["x"]), int(raw["y"])) if isinstance(raw, dict) else None
            kind = mines.get(target) if target is not None else None
            if (
                target is not None
                and kind
                and cur != target
                and distance(cur, target) <= 1
            ):
                role.setdefault("backpack", []).append(kind)
                ok = True
        elif action in {
            "sell", "buy", "acceptTask", "submitAnswer", "summonTreasure",
            "use", "drop", "attack", "remove",
        }:
            ok = True
        results[key] = ok
    payload["lastRoundRoleActionResults"] = results
    payload["roundNo"] = int(payload.get("roundNo") or 1) + 1
    return payload
