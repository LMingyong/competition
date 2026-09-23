"""沙箱世界工厂:defender 出生点、第一夜 15 小兵、刷兵带。"""

from agent.protocol import Pos, distance, station_footprint
from agent.world import World

from tests.sandbox.world import (
    DEFENDER_STATION,
    FIRST_NIGHT_ROUND,
    GATLING,
    MAP_HEIGHT,
    MAP_WIDTH,
    NIGHT1_SMALL,
    RAILGUN,
    ROCKET,
    day_mining,
    new_game,
    night1,
    night_wave_count,
    spawn_band,
    with_day1_towers,
)


def test_new_game_is_defender_without_enemy():
    payload = new_game()
    assert payload["roundNo"] == 1
    assert payload["teamOur"]["type"] == "defender"
    assert payload["teamOur"]["goldNum"] == 75
    assert payload["teamEnemy"]["roles"] == []
    assert payload["robot"]["roles"] == []
    station = next(r for r in payload["teamOur"]["roles"] if r["roleType"] == "station")
    assert station["pos"] == {"x": 30, "y": 10}
    kinds = {r["roleType"] for r in payload["teamOur"]["roles"]}
    assert kinds == {"station", "worker", "pioneer"}


def test_wave_count_grows_fifty_percent_each_night():
    assert night_wave_count(1) == NIGHT1_SMALL
    assert night_wave_count(2) == 23
    assert night_wave_count(3) == 34


def test_spawn_band_is_corner_third_near_base():
    band = spawn_band(DEFENDER_STATION, MAP_WIDTH, MAP_HEIGHT)
    assert band
    # 右下角: x>=27 且 y<10
    assert all(cell.x >= 27 and cell.y < 10 for cell in band)
    assert Pos(29, 8) in band
    assert Pos(0, 31) not in band
    assert Pos(40, 0) in band


def test_night1_has_fifteen_small_robots_in_band():
    payload = night1()
    assert payload["roundNo"] == FIRST_NIGHT_ROUND
    robots = payload["robot"]["roles"]
    assert len(robots) == 15
    band = set(spawn_band(DEFENDER_STATION))
    footprint = set(station_footprint(DEFENDER_STATION))
    for robot in robots:
        assert robot["roleType"] == "smallRobot"
        assert robot["health"] == 40
        assert robot["targetTeam"] == "defender"
        pos = Pos(robot["pos"]["x"], robot["pos"]["y"])
        assert pos in band
        assert pos not in footprint


def test_night1_robots_not_on_buildings():
    payload = night1()
    world = World.load(payload)
    buildings = set()
    heroes = set()
    for unit in world.ours:
        if unit.kind in ("worker", "pioneer"):
            heroes.add(unit.pos)
        else:
            buildings.update(world.footprint(unit))
    for robot in payload["robot"]["roles"]:
        pos = Pos(robot["pos"]["x"], robot["pos"]["y"])
        assert pos not in buildings
        assert pos not in heroes


def test_night1_towers_sit_west_of_defender_base():
    payload = with_day1_towers(new_game())
    by_id = {role["id"]: role for role in payload["teamOur"]["roles"]}
    assert by_id[GATLING]["pos"] == {"x": 29, "y": 8}
    assert by_id[RAILGUN]["pos"] == {"x": 29, "y": 9}
    assert by_id[ROCKET]["pos"] == {"x": 29, "y": 10}


def test_day_mining_worker_stands_by_log_copper():
    payload = day_mining(20)
    worker = next(r for r in payload["teamOur"]["roles"] if r["id"] == 20010)
    assert worker["pos"] == {"x": 33, "y": 11}
    assert distance(Pos(33, 11), Pos(34, 11)) == 1
