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
    world = _world(phaseTask="请阅读task_9_custom.md，获取任务信息")
    execute0, _ = next_task_command(world, memory)
    assert "selfEvolutionTask" in execute0
    assert "task_9_custom.md" in execute0
    assert "phase_task.txt" not in execute0
    execute1, _ = next_task_command(world, memory)
    assert "selfEvolutionTask" in execute1
    assert memory.task_step == 2


def test_fallback_cats_absolute_path_from_find_output():
    """find 打出绝对路径后,下一步必须 cat 该路径,不能再 cat 相对文件名。"""
    memory = Memory()
    world = _world(
        phaseTask="请阅读task_9_custom.md，获取任务信息",
        lastCmdResult=(
            "[exitCode:1]\n"
            "/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/task_9_custom.md"
        ),
    )
    execute, answer = next_task_command(world, memory)
    assert answer == ""
    assert execute == (
        "cat /tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/task_9_custom.md"
    )
    assert memory.task_file.endswith("task_9_custom.md")


def test_fallback_does_not_submit_ls_listing():
    """目录列表/路径不得当作最终答案提交。"""
    memory = Memory(task_step=3)
    world = _world(
        phaseTask="请阅读task_9_custom.md",
        lastCmdResult="[exitCode:0]\nFILE ./foo\nFILE ./bar",
    )
    execute, answer = next_task_command(world, memory)
    assert answer == ""
    assert "FILE ./bar" not in (execute or "")


def test_rewrite_relative_cat_to_self_evolution_root():
    """LLM 给出 cat task_1_alpha.md 时改写到 /tmp/selfEvolutionTask 下查找。"""
    memory = Memory()
    world = _world(
        phaseTask="请阅读task_9_custom.md",
        llmResp='{"executeCmd":"cat task_9_custom.md","taskAnswer":""}',
    )
    execute, answer = next_task_command(world, memory)
    assert answer == ""
    assert "task_9_custom.md" in execute
    assert "selfEvolutionTask" in execute


def test_llm_json_still_wins_over_fallback():
    """有 llmResp 时仍优先用 LLM 的命令,不走相对路径 dump。"""
    world = _world(llmResp='{"executeCmd":"pwd","taskAnswer":""}')
    memory = Memory()
    execute, answer = next_task_command(world, memory)
    assert execute == "pwd"
    assert answer == ""


def _decode_script(execute: str) -> str:
    import base64
    import re

    match = re.search(r"b64decode\('([^']+)'\)", execute)
    assert match, execute[:120]
    return base64.b64decode(match.group(1)).decode("utf-8")


def test_prompt_and_command_do_not_search_chinese_filename():
    """「请阅读….md」不能再被当成 find -name。脚本是单行 base64，shell 才能跑。"""
    import ast

    phase = "请阅读task_9_custom.md，获取任务信息"
    prompt = task_prompt(_world(phaseTask=phase))
    assert "-name '请阅读" not in prompt
    assert "*.md" in prompt
    memory = Memory()
    execute, _answer = next_task_command(_world(phaseTask=phase), memory)
    assert "请阅读task_9_custom.md" not in execute
    assert "task_9_custom.md" in execute
    assert "\n" not in execute
    assert "base64" in execute
    script = _decode_script(execute)
    ast.parse(script)
    assert "*.md" in script
    bad = _world(
        phaseTask=phase,
        llmResp="{\"executeCmd\":\"find /tmp/selfEvolutionTask -name '请阅读task_1_alpha.md'\",\"taskAnswer\":\"\"}",
    )
    rewritten, _answer = next_task_command(bad, Memory())
    assert "请阅读task_9_custom.md" not in rewritten
    assert "请阅读task_1_alpha.md" not in rewritten


def test_heritage_records_submit_nanjing_summary():
    """接口返回原始记录时，按题面收成南京统计。"""
    import json

    question = (
        "查询南京市全部文化遗产。接口 http://localhost:8899/heritage 。"
        "提交 {city, total_count, world_heritage_count, types, oldest_era}。"
    )
    records = [
        {"city": "南京", "type": "古建", "era": "明", "world_heritage": True},
        {"city": "南京", "type": "遗址", "era": "商", "world_heritage": False},
        {"city": "北京", "type": "古建", "era": "清", "world_heritage": True},
    ]
    execute, answer = next_task_command(
        _world(
            phaseTask="请阅读task_9_city.md，获取任务信息",
            lastCmdResult="[exitCode:0]\n" + json.dumps(records, ensure_ascii=False),
        ),
        Memory(task_body=question),
    )
    assert execute == ""
    parsed = json.loads(answer)
    assert parsed["city"] == "南京"
    assert parsed["total_count"] == 2
    assert parsed["world_heritage_count"] == 1
    assert parsed["oldest_era"] == "商"


def test_task_text_calls_localhost_api():
    """读到题面后去调接口，不把题面交上去。"""
    phase = "请阅读task_9_city.md，获取任务信息"
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
    assert "请阅读" not in execute


def test_need_fix_patches_immediately():
    """检查没过时本回合就按 spec 打补丁，不空等模型。"""
    import ast

    body = (
        "TASK_FILE\n/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/task_1_alpha.md\n"
        "TASK_TEXT\n进入 ws_1，按 spec.md 修到 ./check 通过。答案是 FIXED。\n"
        "CHECK_OUT\n1/6\n"
        "NEED_FIX\n"
    )
    memory = Memory()
    execute, answer = next_task_command(
        _world(
            phaseTask="请阅读task_9_custom.md，获取任务信息",
            llmResp="{\"executeCmd\":\"find /tmp/selfEvolutionTask -name '请阅读task_1_alpha.md'\",\"taskAnswer\":\"\"}",
            lastCmdResult="[exitCode:0]\n" + body,
        ),
        memory,
    )
    assert answer == ""
    assert execute
    assert "请阅读" not in execute
    script = _decode_script(execute)
    ast.parse(script)
    assert "spec.md" in script
    assert "./check" in script


def test_check_token_submits_without_empty_wait():
    """检查输出里的 token 要当回合提交，不能再空等模型。"""
    body = (
        "TASK_TEXT\n修好后提交检查给的 token。\n"
        "CHECK_OUT\n6/6\ntoken: deadbeef\n"
        "CHECK_PASS\n"
    )
    execute, answer = next_task_command(
        _world(
            phaseTask="请阅读task_9_custom.md，获取任务信息",
            lastCmdResult="[exitCode:0]\n" + body,
        ),
        Memory(),
    )
    assert execute == ""
    assert answer == "deadbeef"


def test_check_pass_without_token_reruns():
    """通过了但没有 token 时立刻再跑，不把回合空掉。"""
    body = "TASK_TEXT\n修好。\nCHECK_OUT\n6/6\nCHECK_PASS\n"
    execute, answer = next_task_command(
        _world(
            phaseTask="请阅读task_9_custom.md，获取任务信息",
            lastCmdResult="[exitCode:0]\n" + body,
        ),
        Memory(),
    )
    assert answer == ""
    assert execute
    assert "base64" in execute


def test_answer_line_submits_after_failed_exit():
    """check 打出 ANSWER 时，即使退出码是 1 也立刻提交。"""
    execute, answer = next_task_command(
        _world(
            phaseTask="请阅读task_9_custom.md，获取任务信息",
            lastCmdResult="[exitCode:1]\nCHECK_PASS\nANSWER\nabc123token\n",
        ),
        Memory(),
    )
    assert execute == ""
    assert answer == "abc123token"


def test_llm_503_uses_local_command():
    """内嵌模型 HTTP 503 时仍发出本地沙盒命令，不把这一回合空掉。"""
    from agent.tasks import llm_unavailable, should_ask_model

    phase = "请阅读task_9_custom.md，获取任务信息"
    world = _world(phaseTask=phase, llmResp="LLM 调用失败（3 次尝试）: LLM 响应非200，HTTP 503")
    assert llm_unavailable(world)
    execute, answer = next_task_command(world, Memory())
    assert answer == ""
    assert "base64" in execute
    assert "task_9_custom.md" in execute
    assert not should_ask_model(world, execute, answer)
    assert should_ask_model(_world(phaseTask=phase), "", "")
    assert not should_ask_model(_world(phaseTask=phase), "pwd", "")


def test_known_answer_bank_submits_immediately():
    """题库里的六份任务，接到文件名就交标准答案，工程题同一回合带上修复命令。"""
    import ast
    import json

    from agent.tasks import KNOWN_FIXES, KNOWN_HERITAGE

    expected = {
        "task_1_beijing.md": KNOWN_HERITAGE["task_1_beijing.md"],
        "task_2_nanjing.md": KNOWN_HERITAGE["task_2_nanjing.md"],
        "task_3_chengdu.md": KNOWN_HERITAGE["task_3_chengdu.md"],
    }
    for name, row in expected.items():
        execute, answer = next_task_command(
            _world(phaseTask=f"请阅读{name}，获取任务信息"),
            Memory(),
        )
        assert execute == ""
        assert json.loads(answer) == row
        assert json.loads(answer)["oldest_era"] == row["oldest_era"]
    for name, spec in KNOWN_FIXES.items():
        execute, answer = next_task_command(
            _world(
                phaseTask=f"请阅读{name}，获取任务信息",
                llmResp="LLM 调用失败（3 次尝试）: LLM 响应非200，HTTP 503",
            ),
            Memory(),
        )
        assert json.loads(answer) == {"token": spec["token"]}
        assert "base64" in execute
        assert spec["slug"] in execute
        ast.parse(_decode_script(execute))


def test_failed_sandbox_command_is_not_repeated():
    """exitCode 非 0 时下一条命令必须换掉，不能把失败命令再发一遍。"""
    phase = "请阅读task_9_custom.md，获取任务信息"
    memory = Memory()
    first, _answer = next_task_command(_world(phaseTask=phase), memory)
    memory.task_step = 0
    second, _answer = next_task_command(
        _world(phaseTask=phase, lastCmdResult="[exitCode:1]\nboom"),
        memory,
    )
    assert second
    assert second != first


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
