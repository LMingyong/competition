from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .jobs import Job, update_fail_streaks
from .protocol import Pos, Unit
from .world import World, backpack_item, count_item

ITEM_ALIASES = {
    "古符石板": "AcientTablet",
    "星辰之沙": "StarSand",
    "烈焰之息": "FlameBreath",
    "寒霜药剂": "FrostPotion",
    "荆棘护符": "ThornAmulet",
    "回音铁哨": "IronWhistle",
    "acienttablet": "AcientTablet",
    "starsand": "StarSand",
    "flamebreath": "FlameBreath",
    "frostpotion": "FrostPotion",
    "thornamulet": "ThornAmulet",
    "ironwhistle": "IronWhistle",
}

LLM_PER_DAY = 3
TASK_ROOT = "/tmp/selfEvolutionTask"
TASK_FAIL_LIMIT = 3
# 只用 ASCII。\w 会把「请阅读task_1_alpha.md」整句当成文件名。
_TASK_FILE_NAME = re.compile(r"([A-Za-z0-9_./-]+\.(?:md|txt|json|py|csv))", re.I)
_ABS_TASK_PATH = re.compile(r"(/tmp/selfEvolutionTask/[^\s|:]+)")
_LOCAL_URL = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/[^\s'\"<>，。]*)?"
)
_BRACE_KEYS = re.compile(r"\{([^{}]{1,200})\}")


@dataclass
class Memory:
    last_round: int = 0
    day: int = 0
    llm_used: int = 0
    folk: list[str] = field(default_factory=list)
    official: list[str] = field(default_factory=list)
    treasure_done: bool = False
    treasure_pos: Pos | None = None
    treasure_items: tuple[str, ...] = ()
    treasure_day: int | None = None
    task_step: int = 0
    last_task: str = ""
    pending_answer: str = ""
    task_file: str = ""
    task_dir: str = ""
    task_body: str = ""
    task_fails: int = 0
    abandon_task: bool = False
    last_execute: str = ""
    api_fetched: bool = False
    saw_workspace: bool = False
    accepted_round: int = 0
    jobs: dict[int, Job] = field(default_factory=dict)
    roles: dict[int, str] = field(default_factory=dict)
    ticket_owner: dict[int, str] = field(default_factory=dict)
    ticket_penalty: dict[str, int] = field(default_factory=dict)
    ticket_hold: set[int] = field(default_factory=set)


MEMORY = Memory()


def observe(turn: World) -> Memory:
    memory = MEMORY
    if turn.day_index != memory.day:
        memory.day = turn.day_index
        memory.llm_used = 0
    if turn.round_no < memory.last_round:
        memory.last_task = ""
        _reset_task_progress(memory)
        memory.jobs.clear()
        memory.roles.clear()
        memory.ticket_owner.clear()
        memory.ticket_penalty.clear()
        memory.ticket_hold.clear()
    memory.last_round = turn.round_no
    update_fail_streaks(turn, memory)
    if turn.llm_limited():
        memory.llm_used = LLM_PER_DAY
    if turn.folk_legends and (
        not memory.folk or memory.folk[-1] != turn.folk_legends
    ):
        memory.folk.append(turn.folk_legends)
    if turn.official_news and (
        not memory.official or memory.official[-1] != turn.official_news
    ):
        memory.official.append(turn.official_news)
    if turn.last_summon in (1, 4):
        memory.treasure_done = True
    if turn.phase_task != memory.last_task:
        memory.last_task = turn.phase_task
        _reset_task_progress(memory)
    _absorb_llm(turn, memory)
    return memory


def _reset_task_progress(memory: Memory) -> None:
    memory.task_step = 0
    memory.pending_answer = ""
    memory.task_file = ""
    memory.task_dir = ""
    memory.task_body = ""
    memory.task_fails = 0
    memory.abandon_task = False
    memory.last_execute = ""
    memory.api_fetched = False
    memory.saw_workspace = False
    memory.accepted_round = 0


def can_prompt(turn: World, memory: Memory) -> bool:
    if turn.phase_task:
        return True
    return memory.llm_used < LLM_PER_DAY and not turn.llm_limited()


def mark_prompt(turn: World, memory: Memory) -> None:
    if not turn.phase_task:
        memory.llm_used += 1


def treasure_prompt(turn: World, memory: Memory) -> str:
    shop = ", ".join(item.name for item in turn.weapon_shop)
    folk = "\n".join(f"DAY{index + 1}: {text}" for index, text in enumerate(memory.folk))
    return (
        "你在解析《未来战争》民间传闻以开启祭坛宝藏。"
        "只输出一行 JSON，不要解释："
        '{"x":数字,"y":数字,"items":["英文物品名"],"day":数字或null,"ready":是否现在可开}。'
        "物品名必须来自商店英文名，不能多也不能少。"
        f" 地图宽{turn.width}高{turn.height}，当前第{turn.day_index}天第{turn.round_no}回合。"
        f" 上回合召唤结果码={turn.last_summon}（0未探测1成功2位置或时间不对3物品错误4已空）。"
        f" 商店：{shop}。传闻：\n{folk}"
    )


def task_prompt(turn: World) -> str:
    names = _extract_task_files(turn.phase_task)
    hint = names[0] if names else "task_*.md"
    return (
        "你在沙盒中做自进化任务。沙盒无外网，可执行 shell / python，时限15秒。"
        "任务附件在 /tmp/selfEvolutionTask 下，必须用绝对路径读取。"
        "先 find /tmp/selfEvolutionTask -name '*.md' 列出全部 markdown，"
        f"再读取与 {hint} 同名的文件。禁止用中文整句做 -name 精确匹配。"
        "读到题面后调用其中的 localhost API，或按 spec.md 修到 ./check 通过，"
        "得到最终答案再填 taskAnswer。"
        "不要把目录列表、MISSING、文件路径或题面原文当成 taskAnswer。"
        "上一条命令若超时、MISSING 或 find 失败，换一种列 md / 读文件的方式，不要原样重试。"
        "只输出一行 JSON："
        '{"executeCmd":"下一条命令","taskAnswer":"若已得到最终答案则填写否则空字符串"}。'
        f"\n【任务】\n{turn.phase_task}\n【上次命令输出】\n{turn.last_cmd_result}"
    )


def parse_llm_json(text: str) -> dict:
    if not text:
        return {}
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def next_task_command(turn: World, memory: Memory) -> tuple[str, str]:
    _remember_task_path(memory, turn.last_cmd_result)
    raw = turn.last_cmd_result or ""
    failed = _cmd_failed(raw)
    body = _cmd_body(raw)
    if any(error.code == 1 for error in turn.errors):
        memory.abandon_task = True
    if _output_ok(raw) and _is_task_text(body):
        memory.task_body = body
        if not memory.api_fetched:
            memory.task_fails = 0
    elif failed and not _made_progress(raw, memory):
        memory.task_fails += 1
    parsed = parse_llm_json(turn.llm_resp)
    execute = str(parsed.get("executeCmd") or "").strip()
    answer = str(parsed.get("taskAnswer") or "").strip()
    if answer and _looks_like_answer(answer):
        memory.pending_answer = answer
    else:
        answer = ""
    if execute:
        execute = _rewrite_sandbox_cmd(execute, memory, turn)
        if failed and execute == memory.last_execute:
            execute = ""
    ready = "" if failed else _ready_answer(body, memory)
    if answer:
        memory.task_step += 1
        memory.last_execute = execute
        memory.task_fails = 0
        memory.abandon_task = False
        return execute, answer
    if execute and not memory.abandon_task and memory.task_fails < TASK_FAIL_LIMIT:
        memory.task_step += 1
        memory.last_execute = execute
        return execute, ""
    if ready:
        memory.pending_answer = ready
        memory.task_step += 1
        memory.last_execute = ""
        memory.task_fails = 0
        memory.abandon_task = False
        return "", ready
    if (
        memory.pending_answer
        and raw
        and not failed
        and _looks_like_answer(memory.pending_answer)
    ):
        memory.task_step += 1
        return "", memory.pending_answer
    if memory.abandon_task or memory.task_fails >= TASK_FAIL_LIMIT:
        memory.abandon_task = True
        memory.task_step += 1
        memory.last_execute = ""
        return "", ""
    command = _fallback_cmd(turn, memory)
    if failed and command and command == memory.last_execute:
        memory.task_fails += 1
        command = _switch_cmd(turn, memory)
    if memory.abandon_task or not command:
        memory.abandon_task = True
        memory.task_step += 1
        memory.last_execute = ""
        return "", ""
    memory.task_step += 1
    memory.last_execute = command
    return command, ""


def _fallback_cmd(turn: World, memory: Memory) -> str:
    _remember_task_path(memory, turn.last_cmd_result)
    names = _extract_task_files(turn.phase_task)
    name = names[0] if names else ""
    raw = turn.last_cmd_result or ""
    body = _cmd_body(raw)
    if _should_cat(memory, raw, body):
        return f"cat {memory.task_file}"
    if _output_ok(raw) and body.strip() and not _looks_like_listing(body):
        source = memory.task_body or body
        if _is_task_text(body) or memory.task_body:
            follow = _follow_task_text(source, memory)
            if follow:
                return follow
    if memory.task_fails >= 2 and name:
        return _read_named_md_cmd(name)
    if memory.task_fails >= 1:
        return "find /tmp/selfEvolutionTask -name '*.md'"
    return _list_md_cmd(name)


def _switch_cmd(turn: World, memory: Memory) -> str:
    if memory.task_fails >= TASK_FAIL_LIMIT:
        memory.abandon_task = True
        return ""
    names = _extract_task_files(turn.phase_task)
    name = names[0] if names else ""
    if memory.task_fails >= 2 and name:
        return _read_named_md_cmd(name)
    return "find /tmp/selfEvolutionTask -name '*.md'"


def _should_cat(memory: Memory, raw: str, body: str) -> bool:
    if not memory.task_file or memory.task_body:
        return False
    if _is_task_text(body):
        return False
    if _looks_like_listing(body) or "No such file" in body or "MISSING" in body:
        return True
    if not _output_ok(raw):
        return True
    if memory.task_file in body and len(body.strip().splitlines()) <= 8:
        return True
    return False


def _extract_task_files(text: str) -> list[str]:
    names: list[str] = []
    for match in _TASK_FILE_NAME.finditer(text or ""):
        base = match.group(1).split("/")[-1]
        if base.lower() in {"spec.md", "readme.md", "phase_task.txt"}:
            continue
        if base not in names:
            names.append(base)
    preferred = [name for name in names if name.lower().startswith("task_")]
    return preferred or names


def _remember_task_path(memory: Memory, raw: str | None) -> None:
    if not raw:
        return
    for match in _ABS_TASK_PATH.finditer(raw):
        path = match.group(1).rstrip("。,;|\"'")
        if "/tmp/selfEvolutionTask/" not in path:
            continue
        if path.endswith((".md", ".txt", ".json", ".py", ".csv")):
            memory.task_file = path
            slash = path.rfind("/")
            memory.task_dir = path[:slash] if slash > 0 else TASK_ROOT
            return


def _ascii_task_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:md|txt|json|py|csv)", name or "", re.I):
        return name
    return ""


def _list_md_cmd(name: str) -> str:
    """列出 /tmp/selfEvolutionTask 下的 *.md，有 ASCII 文件名时优先该文件。"""
    safe = _ascii_task_name(name)
    script = (
        "from pathlib import Path\n"
        "root=Path('/tmp/selfEvolutionTask')\n"
        f"want={safe!r}\n"
        "hits=[]\n"
        "if root.exists():\n"
        "    mds=list(root.rglob('*.md'))\n"
        "    named=[p for p in mds if p.name==want] if want else []\n"
        "    hits=[str(p) for p in (named or mds)][:40]\n"
        "print('\\n'.join(hits) if hits else 'MISSING')\n"
    )
    return "python3 -c " + json.dumps(script)


def _read_named_md_cmd(name: str) -> str:
    safe = _ascii_task_name(name)
    if not safe:
        return "find /tmp/selfEvolutionTask -name '*.md'"
    script = (
        "from pathlib import Path\n"
        f"name={safe!r}\n"
        "root=Path('/tmp/selfEvolutionTask')\n"
        "hits=list(root.rglob('*.md')) if root.exists() else []\n"
        "picked=[p for p in hits if p.name==name]\n"
        "target=picked or hits[:1]\n"
        "print(target[0].read_text(encoding='utf-8') if target else 'MISSING')\n"
    )
    return "python3 -c " + json.dumps(script)


def _api_cmd(url: str) -> str:
    script = (
        "import urllib.request\n"
        f"url={url!r}\n"
        "try:\n"
        "    with urllib.request.urlopen(url, timeout=8) as resp:\n"
        "        print(resp.read().decode('utf-8','replace'))\n"
        "except Exception as exc:\n"
        "    print('API_FAIL', type(exc).__name__)\n"
    )
    return "python3 -c " + json.dumps(script)


def _follow_task_text(body: str, memory: Memory) -> str:
    """题面已经读到：接着调 API 或跑 ./check，不要停在文件路径上。"""
    match = _LOCAL_URL.search(body or "")
    if match and not memory.api_fetched:
        memory.api_fetched = True
        return _api_cmd(match.group(0).rstrip(").,;，。"))
    if any(token in (body or "") for token in ("./check", "spec.md", "ws_")):
        root = memory.task_dir if memory.task_dir.startswith(TASK_ROOT) else TASK_ROOT
        if not memory.saw_workspace:
            memory.saw_workspace = True
            return _explore_workspace_cmd(memory.task_dir or root)
        return f"cd {root} && ./check"
    return ""


def _bad_find(command: str) -> bool:
    if re.search(r"-name\s+(['\"]).*请阅读", command):
        return True
    if re.search(r"want\s*=\s*(['\"]).*请阅读", command):
        return True
    if re.search(r"rglob\(\s*(['\"]).*请阅读", command):
        return True
    match = re.search(r"-name\s+(['\"])(.+?)\1", command)
    return bool(match and re.search(r"[^\x00-\x7f]", match.group(2)))


def _explore_workspace_cmd(task_dir: str) -> str:
    root = task_dir if task_dir.startswith(TASK_ROOT) else TASK_ROOT
    return (
        f"ls -la {root}; ls -la {root}/ws_* 2>/dev/null; "
        f"find {root} -name spec.md -o -name README.md 2>/dev/null | head -20"
    )


def _rewrite_sandbox_cmd(command: str, memory: Memory, turn: World) -> str:
    if _bad_find(command):
        names = _extract_task_files(turn.phase_task)
        return _list_md_cmd(names[0] if names else "")
    stripped = command.strip()
    match = re.match(
        r"cat\s+(['\"]?)([A-Za-z0-9_./-]+\.(?:md|txt|json|py|csv))\1\s*$",
        stripped,
        re.I,
    )
    if not match:
        return command
    target = match.group(2)
    if target.startswith(TASK_ROOT):
        return f"cat {target}"
    base = target.split("/")[-1]
    if memory.task_file and memory.task_file.endswith("/" + base):
        return f"cat {memory.task_file}"
    return _list_md_cmd(base)


def _cmd_failed(raw: str) -> bool:
    if not raw:
        return False
    if "API_FAIL" in raw:
        return True
    lines = raw.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("[TIMEOUT]") or first.startswith("[JUDGER_ERROR]"):
        return True
    body = _cmd_body(raw).strip()
    if body == "MISSING" or body.startswith("MISSING"):
        return True
    if "No such file" in raw:
        return True
    if first.startswith("[exitCode:") and not first.startswith("[exitCode:0]"):
        return True
    return False


def _made_progress(raw: str, memory: Memory) -> bool:
    if memory.task_file and memory.task_file in (raw or ""):
        return True
    if _output_ok(raw):
        body = _cmd_body(raw).strip()
        if body and not _looks_like_listing(body) and "API_FAIL" not in body:
            return True
    return False


def _is_task_text(body: str) -> bool:
    text = (body or "").strip()
    if not text:
        return False
    if text.startswith("#") or "请阅读" in text or "自进化任务" in text:
        return True
    if "localhost" in text or "127.0.0.1" in text:
        return True
    return False


def _ready_answer(body: str, memory: Memory) -> str:
    text = (body or "").strip()
    if not text or _looks_like_listing(text) or _is_task_text(text):
        return ""
    if text.startswith("API_FAIL") or text.startswith("MISSING"):
        return ""
    parsed, candidate = _parse_json_answer(text)
    if parsed is None:
        return ""
    return _shape_answer(memory.task_body, parsed, candidate)


def _parse_json_answer(text: str) -> tuple[object | None, str]:
    for candidate in (text, *[line.strip() for line in text.splitlines() if line.strip()][-1:]):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed, candidate
    return None, ""


def _shape_answer(task_body: str, parsed: object, raw: str) -> str:
    if isinstance(parsed, dict):
        keys: list[str] = []
        for match in _BRACE_KEYS.finditer(task_body or ""):
            parts = [part.strip() for part in match.group(1).split(",")]
            if parts and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
                keys = parts
                break
        if keys and all(key in parsed for key in keys):
            return json.dumps({key: parsed[key] for key in keys}, ensure_ascii=False)
    return json.dumps(parsed, ensure_ascii=False) if not raw.strip().startswith("{") else raw.strip()


def _output_ok(raw: str) -> bool:
    if not raw:
        return False
    first = raw.splitlines()[0].strip()
    return first.startswith("[exitCode:0]") or (
        not first.startswith("[exitCode:") and "No such file" not in raw
    )


def _looks_like_listing(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return True
    if "FILE " in body or "No such file" in body or body.startswith("MISSING"):
        return True
    if body.startswith("saved "):
        return True
    if body.startswith(TASK_ROOT) and len(body.splitlines()) <= 8:
        return True
    return False


def _looks_like_answer(text: str) -> bool:
    body = _cmd_body(text).strip() if "\n" in (text or "") else (text or "").strip()
    if not body or _looks_like_listing(body):
        return False
    if body.startswith("#") or "自进化任务" in body or "请阅读" in body:
        return False
    if body.startswith(TASK_ROOT) or body.startswith("/tmp/"):
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1]
    if len(last) > 400:
        return False
    return True


def _cmd_body(raw: str) -> str:
    lines = raw.splitlines()
    if lines and lines[0].startswith("[") and lines[0].endswith("]"):
        return "\n".join(lines[1:])
    return raw


def missing_treasure_items(role: Unit, items: tuple[str, ...]) -> tuple[str, ...]:
    missing = []
    used: dict[str, int] = {}
    for name in items:
        have = count_item(role, name) - used.get(name.lower(), 0)
        if have <= 0:
            missing.append(name)
        else:
            used[name.lower()] = used.get(name.lower(), 0) + 1
    return tuple(missing)


def consume_names(role: Unit, items: tuple[str, ...]) -> tuple[str, ...]:
    names = []
    for name in items:
        actual = backpack_item(role, name)
        names.append(actual or name)
    return tuple(names)


def _absorb_llm(turn: World, memory: Memory) -> None:
    data = parse_llm_json(turn.llm_resp)
    if not data:
        _heuristic_treasure(turn, memory)
        return
    if "x" in data and "y" in data:
        try:
            memory.treasure_pos = Pos(int(data["x"]), int(data["y"]))
        except (TypeError, ValueError):
            pass
    raw_items = data.get("items") or []
    if isinstance(raw_items, list):
        mapped = tuple(
            _canonical_item(str(name), turn) for name in raw_items if str(name)
        )
        mapped = tuple(name for name in mapped if name)
        if mapped:
            memory.treasure_items = mapped
    day = data.get("day")
    if isinstance(day, int):
        memory.treasure_day = day
    if data.get("ready") is True:
        memory.treasure_day = turn.day_index
    if data.get("taskAnswer"):
        memory.pending_answer = str(data["taskAnswer"])


def _heuristic_treasure(turn: World, memory: Memory) -> None:
    text = "\n".join(memory.folk)
    if memory.treasure_pos is None:
        match = re.search(r"\((\d{1,2})\s*[,，]\s*(\d{1,2})\)", text)
        if match:
            memory.treasure_pos = Pos(int(match.group(1)), int(match.group(2)))
    found: list[str] = []
    for alias, name in ITEM_ALIASES.items():
        if alias in text or name.lower() in text.lower():
            if name not in found:
                found.append(name)
    if found and not memory.treasure_items:
        memory.treasure_items = tuple(found)
    match = re.search(r"第\s*(\d{1,2})\s*天", text)
    if match and memory.treasure_day is None:
        memory.treasure_day = int(match.group(1))


def _canonical_item(name: str, turn: World) -> str:
    mapped = ITEM_ALIASES.get(name) or ITEM_ALIASES.get(name.lower())
    if mapped:
        return mapped
    item = turn.shop_item(name)
    return item.name if item else name


def treasure_ready(turn: World, memory: Memory) -> bool:
    if memory.treasure_done or memory.treasure_pos is None or not memory.treasure_items:
        return False
    if memory.treasure_day is not None and turn.day_index < memory.treasure_day:
        return False
    return True
