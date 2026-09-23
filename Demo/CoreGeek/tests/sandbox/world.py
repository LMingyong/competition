"""私有单回合沙箱:固定 defender 地图,供 decide() 单测。

约定(与用户确认过):
- 先做单回合 payload;叠加回合状态留到后续。
- 第一夜 15 只 smallRobot;后续每一夜数量 *1.5(四舍五入)。
- 刷兵区域=靠近基地的地图边缘三分之一(横向三分之一 ∩ 纵向三分之一的角落)。
- 不放敌方英雄,只求自己第一夜能开火活下来。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from agent.protocol import Pos, distance, station_footprint
from agent.world import World

MAP_WIDTH = 41
MAP_HEIGHT = 32
NIGHT1_SMALL = 15
WAVE_GROWTH = 1.5
FIRST_NIGHT_ROUND = 71

DEFENDER_STATION = Pos(30, 10)
WORKER_1 = 20010
PIONEER = 20011
WORKER_2 = 20012
STATION_ID = 20013
GATLING = 20020
RAILGUN = 20030
ROCKET = 20040

# 沙箱地图上的三座 1 级塔（加特林/电磁/火箭，与当前三火箭开局不同）
TOWER_SITES = {
    GATLING: ("gatling", Pos(29, 8), 3, 10),
    RAILGUN: ("railgun", Pos(29, 9), 6, 10),
    ROCKET: ("rocket", Pos(29, 10), 10, 20),
}

# 日志里出现过的矿 + 基地旁一格石矿,方便测 P3
LOG_MINES = (
    (Pos(34, 11), "copper"),
    (Pos(38, 25), "copper"),
    (Pos(32, 6), "stone"),
)

VENDOR = Pos(20, 16)
WEAPON_SHOP = Pos(25, 20)

VENDOR_SHOP = (
    {"name": "stone", "price": 1},
    {"name": "iron", "price": 3},
    {"name": "copper", "price": 5},
)

WEAPON_SHOP_LIST = (
    {"name": "WeaponUpgradeVoucher1", "price": 100},
    {"name": "WeaponUpgradeVoucher2", "price": 150},
    {"name": "WallUpgradeVoucher1", "price": 20},
    {"name": "WallUpgradeVoucher2", "price": 30},
    {"name": "StationUpgradeVoucher1", "price": 100},
    {"name": "StationUpgradeVoucher2", "price": 150},
    {"name": "WallFixer", "price": 10},
    {"name": "Medicine", "price": 10},
    {"name": "DizzyWeapon", "price": 100},
    {"name": "Bomb", "price": 100},
    {"name": "SmallRobotSummonOrder", "price": 20},
    {"name": "MiddleRobotSummonOrder", "price": 30},
    {"name": "LargeRobotSummonOrder", "price": 100},
    {"name": "BossRobotSummonOrder", "price": 200},
    {"name": "AcientTablet", "price": 15},
    {"name": "StarSand", "price": 15},
    {"name": "FlameBreath", "price": 15},
    {"name": "FrostPotion", "price": 15},
    {"name": "ThornAmulet", "price": 15},
    {"name": "IronWhistle", "price": 15},
)


def night_wave_count(day_index: int) -> int:
    """第1夜 15 只;之后每一夜数量 ×1.5,四舍五入(2.5→3)。"""
    day = max(1, int(day_index))
    return max(1, int(NIGHT1_SMALL * (WAVE_GROWTH ** (day - 1)) + 0.5))


def spawn_band(station: Pos, width: int = MAP_WIDTH, height: int = MAP_HEIGHT) -> tuple[Pos, ...]:
    """靠近基地的地图角落:横向边缘 1/3 与纵向边缘 1/3 的交集。"""
    right = station.x >= (width - 1) / 2
    top = station.y >= (height - 1) / 2
    x_cut = (width * 2) // 3 if right else width // 3
    y_cut = (height * 2) // 3 if top else height // 3
    cells = []
    for x in range(width):
        for y in range(height):
            x_ok = x >= x_cut if right else x < x_cut
            y_ok = y >= y_cut if top else y < y_cut
            if x_ok and y_ok:
                cells.append(Pos(x, y))
    return tuple(cells)


def _zone(kind: str, pos: Pos) -> dict[str, Any]:
    return {"neutralType": kind, "pos": pos.dump()}


def _role(
    unit_id: int,
    pos: Pos,
    kind: str,
    health: int,
    *,
    level: int = 0,
    attack_power: int = 0,
    attack_range: int = 0,
    cooldown: int = 0,
    capacity: int = 0,
    backpack: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": unit_id,
        "pos": pos.dump(),
        "roleType": kind,
        "health": health,
        "attackPower": attack_power,
        "attackRange": attack_range,
        "level": level,
        "cooldown": cooldown,
        "backPackCapability": capacity,
        "backpack": list(backpack),
    }


def _base_zones() -> list[dict[str, Any]]:
    zones = [
        _zone("challengerTaskPoint1", Pos(14, 14)),
        _zone("challengerTaskPoint2", Pos(17, 17)),
        _zone("challengerTaskPoint2", Pos(16, 17)),
        _zone("defenderTaskPoint1", Pos(23, 14)),
        _zone("defenderTaskPoint2", Pos(26, 17)),
        _zone("defenderTaskPoint2", Pos(27, 17)),
        _zone("vendor", VENDOR),
        _zone("weaponShop", WEAPON_SHOP),
        _zone("stone", Pos(4, 24)),
        _zone("stone", Pos(14, 3)),
        _zone("iron", Pos(25, 10)),
        _zone("iron", Pos(8, 28)),
        _zone("copper", Pos(22, 26)),
        _zone("copper", Pos(7, 2)),
    ]
    for pos, kind in LOG_MINES:
        zones.append(_zone(kind, pos))
    return zones


def _empty_payload(round_no: int, gold: int, roles: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "roundNo": round_no,
        "mapInfo": {
            "width": MAP_WIDTH,
            "height": MAP_HEIGHT,
            "zones": _base_zones(),
        },
        "teamOur": {
            "type": "defender",
            "teamId": "sandbox",
            "teamName": "SandboxDefender",
            "goldNum": gold,
            "totalScore": 0,
            "playerTasks": [
                {
                    "taskType": "自进化类1",
                    "taskPosition": {"x": 23, "y": 14},
                    "coldDownRounds": 0,
                    "scoreReward": 50,
                    "goldReward": 30,
                    "isValid": False,
                    "timeoutRounds": 40,
                },
                {
                    "taskType": "自进化类2",
                    "taskPosition": {"x": 26, "y": 17},
                    "coldDownRounds": 0,
                    "scoreReward": 50,
                    "goldReward": 30,
                    "isValid": False,
                    "timeoutRounds": 40,
                },
            ],
            "roles": roles,
        },
        "teamEnemy": {"roles": []},
        "robot": {"roles": []},
        "phaseTask": "",
        "lastRoundRoleActionResults": {},
        "lastSummonTreasureResult": 0,
        "llmResp": "",
        "worldNews": {"officialNews": "", "folkLegends": ""},
        "lastCmdResult": "",
        "vendorShopList": [dict(item) for item in VENDOR_SHOP],
        "weaponShopList": [dict(item) for item in WEAPON_SHOP_LIST],
        "errors": [],
    }


def new_game() -> dict[str, Any]:
    """第1回合开局:75 金币,无塔无墙,英雄站在基地旁。"""
    roles = [
        _role(STATION_ID, DEFENDER_STATION, "station", 1500, level=1),
        _role(WORKER_1, Pos(28, 8), "worker", 220, capacity=100),
        _role(WORKER_2, Pos(29, 7), "worker", 220, capacity=100),
        _role(PIONEER, Pos(32, 9), "pioneer", 200, capacity=40),
    ]
    return _empty_payload(1, 75, roles)


def with_day1_towers(payload: dict[str, Any]) -> dict[str, Any]:
    """把沙箱里的三座 1 级塔拍在基地西侧。"""
    payload = copy.deepcopy(payload)
    roles = [
        role for role in payload["teamOur"]["roles"]
        if role["roleType"] not in ("gatling", "railgun", "rocket")
    ]
    for unit_id, (kind, pos, attack_range, attack_power) in TOWER_SITES.items():
        roles.append(
            _role(
                unit_id, pos, kind, 1000,
                level=1, attack_power=attack_power, attack_range=attack_range,
            )
        )
    payload["teamOur"]["roles"] = roles
    payload["teamOur"]["goldNum"] = 0
    return payload


def place_heroes_on_guns(payload: dict[str, Any]) -> dict[str, Any]:
    """三人全部贴塔,保证第一夜三座炮都有人操控。"""
    payload = copy.deepcopy(payload)
    spots = {
        WORKER_1: Pos(28, 8),
        PIONEER: Pos(28, 10),
        WORKER_2: Pos(28, 9),
    }
    for role in payload["teamOur"]["roles"]:
        if role["id"] in spots:
            role["pos"] = spots[role["id"]].dump()
            role["health"] = 220 if role["roleType"] == "worker" else 200
            role["backpack"] = []
    return payload


def _blocked_cells(payload: dict[str, Any]) -> set[Pos]:
    world = World.load(payload)
    blocked = set(world.occupied_for_build())
    blocked.update(world.zones.keys())
    return blocked


def pick_spawn_cells(payload: dict[str, Any], count: int) -> list[Pos]:
    station = DEFENDER_STATION
    blocked = _blocked_cells(payload)
    footprint = set(station_footprint(station))
    candidates = [
        cell for cell in spawn_band(station)
        if cell not in blocked
        and cell not in footprint
        and distance(cell, station) >= 2
    ]
    candidates.sort(key=lambda cell: (distance(cell, station), cell.x, cell.y))
    if len(candidates) < count:
        raise RuntimeError(f"刷兵格不足: 只要 {count} 有 {len(candidates)}")
    return candidates[:count]


def spawn_small_robots(payload: dict[str, Any], count: int | None = None) -> dict[str, Any]:
    payload = copy.deepcopy(payload)
    n = NIGHT1_SMALL if count is None else count
    cells = pick_spawn_cells(payload, n)
    payload["robot"] = {
        "roles": [
            {
                "id": 30001 + index,
                "pos": cell.dump(),
                "roleType": "smallRobot",
                "health": 40,
                "abnormalState": "",
                "targetTeam": "defender",
            }
            for index, cell in enumerate(cells)
        ]
    }
    return payload


def night1() -> dict[str, Any]:
    """第一夜第1回合:三塔已建、三人贴塔、15 只小兵刷在基地角落。"""
    payload = new_game()
    payload = with_day1_towers(payload)
    payload = place_heroes_on_guns(payload)
    payload["roundNo"] = FIRST_NIGHT_ROUND
    payload["teamOur"]["goldNum"] = 10
    return spawn_small_robots(payload, night_wave_count(1))


def day_mining(round_no: int = 20) -> dict[str, Any]:
    """白天、三塔已建、无墙,工人靠近日志里的铜矿,用来测采矿/砌墙优先级。"""
    payload = new_game()
    payload = with_day1_towers(payload)
    payload["roundNo"] = round_no
    payload["teamOur"]["goldNum"] = 10
    payload["teamOur"]["roles"] = [
        role for role in payload["teamOur"]["roles"] if role["roleType"] != "wall"
    ]
    for role in payload["teamOur"]["roles"]:
        if role["id"] == WORKER_1:
            role["pos"] = {"x": 33, "y": 11}
            role["backpack"] = []
        elif role["id"] == WORKER_2:
            role["pos"] = {"x": 28,  "y": 8}
            role["backpack"] = ["stone", "stone", "stone", "stone"]
        elif role["id"] == PIONEER:
            role["pos"] = {"x": 28, "y": 10}
            role["backpack"] = []
    return payload


@dataclass
class SandboxTurn:
    """单回合状态。后续叠加昼夜时对 payload 做 deepcopy 再改 roundNo/robot。"""

    payload: dict[str, Any]

    def copy(self) -> "SandboxTurn":
        return SandboxTurn(copy.deepcopy(self.payload))

    def decide(self) -> dict[str, dict[str, Any]]:
        from agent.brain import decide
        return decide(self.payload)
