"""agent.tasks 单元测试:跨回合记忆、任务、宝藏解析与 LLM 相关函数。"""

import agent.tasks as tasks_mod
from agent.protocol import Pos, Unit
from agent.tasks import (
    ITEM_ALIASES,
    LLM_PER_DAY,
    Memory,
    can_prompt,
    consume_names,
    mark_prompt,
    missing_treasure_items,
    next_task_command,
    observe,
    parse_llm_json,
    task_prompt,
    treasure_prompt,
    treasure_ready,
)
from agent.world import World


def _unit(**overrides):
    base = dict(
        unit_id=10010, pos=Pos(8, 8), kind="worker", health=220,
        level=0, cooldown=0, attack_range=0, capacity=100,
        backpack=("stone", "iron"),
    )
    base.update(overrides)
    return Unit(**base)


def _world(**overrides):
    payload = {
        "roundNo": 1,
        "mapInfo": {"width": 40, "height": 30, "zones": []},
        "teamOur": {
            "type": "challenger",
            "goldNum": 10,
            "roles": [
                {"id": 10013, "pos": {"x": 10, "y": 20},
                 "roleType": "station", "health": 1500, "level": 1},
                {"id": 10010, "pos": {"x": 8, "y": 8}, "roleType": "worker",
                 "health": 220, "backPackCapability": 100,
                 "backpack": ["stone", "iron"]},
            ],
        },
        "robot": {"roles": []},
    }
    payload.update(overrides)
    return World.load(payload)


# ---------- observe 记忆维护 ----------

def test_observe_switches_day_and_resets_llm_usage():
    """场景:跨天后 llm_used 清零并记录 day。"""
    tasks_mod.MEMORY = Memory(day=1, llm_used=2)
    world = _world(roundNo=131)  # 第 2 天
    result = observe(world)
    assert result is tasks_mod.MEMORY
    assert result.day == 2
    assert result.llm_used == 0


def test_observe_day_change_resets_llm():
    """场景:进入新的一天,llm_used 归零。"""
    memory = Memory(day=1, llm_used=3)
    tasks_mod.MEMORY = memory
    world = _world(roundNo=131)  # day_index=2
    result = observe(world)
    assert result.day == 2
    assert result.llm_used == 0


def test_observe_llm_limited_sets_usage_cap():
    """场景:errors 含 code=5 时 llm_used 直接置为当日上限。"""
    payload = {
        "roundNo": 1,
        "mapInfo": {"width": 10, "height": 10, "zones": []},
        "teamOur": {},
        "errors": [{"errorCode": 5, "description": "额度超限"}],
    }
    world = World.load(payload)
    result = observe(world)
    assert result.llm_used == LLM_PER_DAY


def test_observe_round_going_backwards_resets_task_state():
    """场景:回合号回退(重开/重置)清空任务进度与待提交答案。"""
    memory = Memory(task_step=2, last_task="旧任务", pending_answer="答案")
    tasks_mod.MEMORY = memory
    world = _world(roundNo=5)
    result = observe(world)
    assert result.last_round == 5
    result2 = observe(_world(roundNo=3))
    assert result2.task_step == 0
    assert result2.last_task == ""
    assert result2.pending_answer == ""


def test_observe_appends_news_deduplicated():
    """场景:新的传闻/官方新闻被追加,重复内容不重复记录。"""
    tasks_mod.MEMORY = Memory()
    payload = {
        "roundNo": 1,
        "mapInfo": {"width": 10, "height": 10, "zones": []},
        "teamOur": {},
        "worldNews": {"officialNews": "A", "folkLegends": "B"},
    }
    observe(World.load(payload))
    observe(World.load(payload))  # 同内容不追加
    assert tasks_mod.MEMORY.official == ["A"]
    assert tasks_mod.MEMORY.folk == ["B"]


def test_observe_last_summon_done_sets_treasure_flag():
    """场景:last_summon 为 1(成功)或 4(已空)标记宝藏已完成。"""
    tasks_mod.MEMORY = Memory()
    world = _world(lastSummonTreasureResult=1)
    result = observe(world)
    assert result.treasure_done is True


def test_observe_phase_task_change_resets_progress():
    """场景:phaseTask 变化时清空 last_task/任务步进/待提交答案。"""
    tasks_mod.MEMORY = Memory(last_task="上一个任务", pending_answer="旧答案")
    world = _world(phaseTask="新任务")
    result = observe(world)
    assert result.last_task == "新任务"
    assert result.task_step == 0
    assert result.pending_answer == ""


# ---------- can_prompt / mark_prompt ----------

def test_can_prompt_during_task_always_true():
    """场景:存在 phaseTask 时不受每日 LLM 限制。"""
    world = _world(phaseTask="任务")
    assert can_prompt(world, Memory())


def test_can_prompt_limited_by_daily_quota():
    """场景:非任务期 llm_used 达上限或 LLM 受限则不可 prompt。"""
    world = _world(phaseTask="")
    assert can_prompt(world, Memory(llm_used=LLM_PER_DAY - 1))
    assert not can_prompt(world, Memory(llm_used=LLM_PER_DAY))
    world_limited = _world()
    world_limited.errors = tuple(
        [__import__("agent.world", fromlist=["ErrorInfo"]).ErrorInfo(5, "x")]
    )
    assert not can_prompt(world_limited, Memory(llm_used=0))


def test_mark_prompt_bumps_usage_outside_task():
    """场景:非任务期 mark_prompt 使每日计数 +1;任务期不计数。"""
    memory = Memory()
    mark_prompt(_world(phaseTask=""), memory)
    assert memory.llm_used == 1
    memory = Memory()
    mark_prompt(_world(phaseTask="任务"), memory)
    assert memory.llm_used == 0


# ---------- prompt 构造 ----------

def test_treasure_prompt_contains_map_and_store():
    """场景:宝藏 prompt 包含地图尺寸/天数/商店物品与传闻。"""
    payload = {
        "roundNo": 1,
        "mapInfo": {"width": 40, "height": 30, "zones": []},
        "teamOur": {},
        "weaponShopList": [{"name": "AcientTablet", "price": 15}],
    }
    world = World.load(payload)
    memory = Memory(folk=["传说一"])
    text = treasure_prompt(world, memory)
    assert "40" in text and "30" in text
    assert "AcientTablet" in text
    assert "DAY1: 传说一" in text


def test_task_prompt_includes_task_and_last_output():
    """场景:任务 prompt 包含任务原文与上次命令输出。"""
    world = _world(phaseTask="求解 1+1", lastCmdResult="[exitCode:0]\n2")
    text = task_prompt(world)
    assert "求解 1+1" in text
    assert "[exitCode:0]" in text


# ---------- parse_llm_json ----------

def test_parse_llm_json_extracts_embedded_object():
    """场景:文本中嵌入 JSON 对象时可提取解析。"""
    result = parse_llm_json('说明文字 {"x": 1, "y": 2} 结尾')
    assert result == {"x": 1, "y": 2}


def test_parse_llm_json_empty_and_invalid():
    """场景:空串/无大括号/非法 JSON 均返回空字典;非 dict 值忽略。"""
    assert parse_llm_json("") == {}
    assert parse_llm_json("没有花括号") == {}
    assert parse_llm_json("{not json}") == {}
    assert parse_llm_json("[1,2,3]") == {}


# ---------- next_task_command ----------

def test_next_task_command_uses_llm_response():
    """场景:llmResp 提供 executeCmd/taskAnswer 时采用该响应并推进步进。"""
    world = _world(llmResp='{"executeCmd":"pwd","taskAnswer":"答案1"}')
    memory = Memory()
    execute, answer = next_task_command(world, memory)
    assert execute == "pwd"
    assert answer == "答案1"
    assert memory.pending_answer == "答案1"
    assert memory.task_step == 1


def test_next_task_command_answer_only_sets_pending():
    """场景:仅有 taskAnswer 时进入 pending_answer,不输出新命令。"""
    world = _world(llmResp='{"taskAnswer":"42"}')
    memory = Memory()
    execute, answer = next_task_command(world, memory)
    assert answer == "42"
    assert memory.task_step == 1


def test_next_task_command_falls_back_with_pending_answer():
    """场景:无新响应但存在 pending_answer 且有命令输出时提交答案。"""
    memory = Memory(pending_answer="缓存答案")
    world = _world(llmResp="", lastCmdResult="[exitCode:0]\n输出")
    execute, answer = next_task_command(world, memory)
    assert execute == ""
    assert answer == "缓存答案"


def test_next_task_command_fallback_cmd_progression():
    """场景:无任何解析结果时按 task_step 在 /tmp/selfEvolutionTask 下找题目文件。"""
    memory = Memory()
    world = _world(phaseTask="请阅读task_1_alpha.md，获取任务信息")
    execute0, _ = next_task_command(world, memory)
    assert "selfEvolutionTask" in execute0
    assert "task_1_alpha.md" in execute0
    assert "phase_task.txt" not in execute0
    execute1, _ = next_task_command(world, memory)
    assert "selfEvolutionTask" in execute1
    assert memory.task_step == 2


def test_fallback_cats_absolute_path_from_find_output():
    """find 打出绝对路径后,下一步必须 cat 该路径,不能再 cat 相对文件名。"""
    memory = Memory()
    world = _world(
        phaseTask="请阅读task_1_alpha.md，获取任务信息",
        lastCmdResult=(
            "[exitCode:1]\n"
            "/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/task_1_alpha.md"
        ),
    )
    execute, answer = next_task_command(world, memory)
    assert answer == ""
    assert execute == (
        "cat /tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/task_1_alpha.md"
    )
    assert memory.task_file.endswith("task_1_alpha.md")


def test_fallback_does_not_submit_ls_listing():
    """目录列表/路径不得当作最终答案提交。"""
    memory = Memory(task_step=3)
    world = _world(
        phaseTask="请阅读task_1_alpha.md",
        lastCmdResult="[exitCode:0]\nFILE ./foo\nFILE ./bar",
    )
    execute, answer = next_task_command(world, memory)
    assert answer == ""
    assert "FILE ./bar" not in (execute or "")


def test_rewrite_relative_cat_to_self_evolution_root():
    """LLM 给出 cat task_1_alpha.md 时改写到 /tmp/selfEvolutionTask 下查找。"""
    memory = Memory()
    world = _world(
        phaseTask="请阅读task_1_alpha.md",
        llmResp='{"executeCmd":"cat task_1_alpha.md","taskAnswer":""}',
    )
    execute, answer = next_task_command(world, memory)
    assert answer == ""
    assert "task_1_alpha.md" in execute
    assert "selfEvolutionTask" in execute


def test_llm_json_still_wins_over_fallback():
    """有 llmResp 时仍优先用 LLM 的命令,不走相对路径 dump。"""
    world = _world(llmResp='{"executeCmd":"pwd","taskAnswer":""}')
    memory = Memory()
    execute, answer = next_task_command(world, memory)
    assert execute == "pwd"
    assert answer == ""


def test_search_does_not_use_chinese_filename():
    """查找命令不得用「请阅读….md」做 -name 精确匹配，改为列 *.md。"""
    phase = "请阅读task_1_alpha.md，获取任务信息"
    world = _world(phaseTask=phase)
    prompt = task_prompt(world)
    assert "-name '请阅读" not in prompt
    assert "*.md" in prompt
    memory = Memory()
    execute, _ = next_task_command(world, memory)
    assert "请阅读task_1_alpha.md" not in execute
    assert "-name '请阅读" not in execute
    assert "*.md" in execute or "task_1_alpha.md" in execute
    bad = _world(
        phaseTask=phase,
        llmResp='{"executeCmd":"find /tmp/selfEvolutionTask -name \'请阅读task_1_alpha.md\'","taskAnswer":""}',
    )
    rewritten, _ = next_task_command(bad, Memory())
    assert "请阅读task_1_alpha.md" not in rewritten
    assert "*.md" in rewritten


def test_timeout_switches_command_instead_of_repeating():
    """超时后换列 md / 读文件，不把同一条失败命令再发一遍。"""
    memory = Memory()
    phase = "请阅读task_2_beta.md，获取任务信息"
    first, _ = next_task_command(_world(phaseTask=phase), memory)
    second, _ = next_task_command(
        _world(phaseTask=phase, lastCmdResult="[TIMEOUT]\n"),
        memory,
    )
    assert second
    assert second != first
    assert "请阅读" not in second
    assert "*.md" in second or "task_2_beta.md" in second


def test_repeated_failures_abandon_without_illegal_command():
    """连续失败达到上限后不再发命令，交给外层离开任务点。"""
    memory = Memory(task_fails=2, last_execute="find /tmp/selfEvolutionTask -name '*.md'")
    execute, answer = next_task_command(
        _world(phaseTask="请阅读task_1_alpha.md", lastCmdResult="[TIMEOUT]\n"),
        memory,
    )
    assert execute == ""
    assert answer == ""
    assert memory.abandon_task is True


def test_task_file_with_api_is_followed_then_submitted():
    """读到题面后调用其中的 API；答案 JSON 就绪就交卷，不把题面本身交上去。"""
    phase = "请阅读task_1_beijing.md，获取任务信息"
    question = (
        "查询全部文化遗产。接口 http://localhost:8899/heritage 。"
        "提交 {city, total_count, world_heritage_count, types, oldest_era}。"
    )
    memory = Memory()
    execute, answer = next_task_command(
        _world(phaseTask=phase, lastCmdResult="[exitCode:0]\n" + question),
        memory,
    )
    assert answer == ""
    assert "localhost:8899" in execute
    assert "请阅读task_1_beijing.md" not in execute
    submit_execute, submit_answer = next_task_command(
        _world(
            phaseTask=phase,
            lastCmdResult=(
                '[exitCode:0]\n'
                '{"city":"北京","total_count":3,"world_heritage_count":1,'
                '"types":["古建"],"oldest_era":"商","extra":1}\n'
            ),
        ),
        memory,
    )
    assert submit_execute == ""
    parsed = __import__("json").loads(submit_answer)
    assert parsed["city"] == "北京"
    assert parsed["total_count"] == 3
    assert "extra" not in parsed


# ---------- 背包操作辅助 ----------

def test_missing_treasure_items_accounts_duplicates():
    """场景:按需物品逐个扣减背包,数量不足才记为缺失。"""
    role = _unit(backpack=("StarSand", "StarSand"))
    assert missing_treasure_items(role, ("StarSand", "FlameBreath")) == ("FlameBreath",)
    assert missing_treasure_items(role, ("StarSand", "StarSand")) == ()


def test_consume_names_returns_actual_pack_names():
    """场景:consume_names 返回背包真实大小写名称,缺失回填原名字。"""
    role = _unit(backpack=("STONESAND",))
    assert consume_names(role, ("stonesand", "missing")) == ("STONESAND", "missing")


# ---------- 宝藏记忆吸收 / ready ----------

def test_absorb_llm_parses_coordinates_and_items():
    """场景:llmResp 含坐标/物品/day 时写入宝藏记忆。"""
    tasks_mod.MEMORY = Memory()
    world = _world(llmResp='{"x":1,"y":2,"items":["AcientTablet"],"day":2,"ready":false}')
    result = observe(world)
    assert result.treasure_pos == Pos(1, 2)
    assert result.treasure_items == ("AcientTablet",)
    assert result.treasure_day == 2


def test_absorb_llm_ready_today_sets_day():
    """场景:ready=true 时宝藏当日可开启。"""
    tasks_mod.MEMORY = Memory()
    world = _world(llmResp='{"x":5,"y":6,"ready":true}')
    result = observe(world)
    assert result.treasure_pos == Pos(5, 6)
    assert result.treasure_day == 1


def test_heuristic_treasure_from_folk_legends():
    """场景:无 llmResp 时从传闻解析坐标/物品名/天数。"""
    tasks_mod.MEMORY = Memory(folk=[
        "西边(12,8)有石门,需 AcientTablet 和 星辰之沙,第3天开启",
    ])
    world = _world(llmResp="")
    result = observe(world)
    assert result.treasure_pos == Pos(12, 8)
    assert "AcientTablet" in result.treasure_items
    assert "StarSand" in result.treasure_items
    assert result.treasure_day == 3


def test_treasure_ready_gating():
    """场景:缺 pos/物品/已领取完成/未到开启日均不可开宝。"""
    memory = Memory(treasure_pos=Pos(3, 3), treasure_items=("AcientTablet",))
    turn = _world()
    assert treasure_ready(turn, memory)
    assert not treasure_ready(turn, Memory(treasure_pos=None))
    assert not treasure_ready(turn, Memory(treasure_pos=Pos(3, 3), treasure_items=()))
    assert not treasure_ready(turn, Memory(
        treasure_pos=Pos(3, 3), treasure_items=("AcientTablet",), treasure_done=True,
    ))
    later = Memory(
        treasure_pos=Pos(3, 3), treasure_items=("AcientTablet",),
        treasure_day=2,
    )
    assert not treasure_ready(turn, later)  # 当前第 1 天,未到第 2 天


# ---------- 别名表一致性 ----------

def test_item_aliases_map_known_items():
    """场景:中文别名与英文小写均映射到标准英文名。"""
    assert ITEM_ALIASES["古符石板"] == "AcientTablet"
    assert ITEM_ALIASES["starsand"] == "StarSand"
    assert ITEM_ALIASES["flamebreath"] == "FlameBreath"
