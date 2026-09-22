#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 Demo/CoreGeek，并在《未来战争》平台网页上传代码、选择对手发起挑战。

编写本脚本时，云端环境访问不了华为内网站点
``https://coregeek.rnd.huawei.com/GeneralIntro/1``（``curl -sI -m 15`` 的
http_code 为 000，无法解析主机名）。浏览器流程必须在能打开该站、并能手工完成
内网 SSO 的机器上运行。

仓库 ``docs/接口文档.md`` 描述的是判题器与选手程序之间的对局协议，不是平台
「上传代码 / 选择对手」的 HTTP 接口。这里没有可复现的上传或挑战 API，因此
脚本不提供 requests/httpx 模式，也不猜测路径、请求体或鉴权头。

接口路径未知。若只按「upload / challenge」等关键字过滤，会漏掉真正的请求。
脚本会把浏览器里全部 XHR/fetch 追加到 ``scripts/captured_api.jsonl``，并带上
「可能相关」标记。写入前会去掉 Cookie、Authorization、token（以及密码、
session 等同类字段）；不记录请求头原文。

示例：
  只打包，不打开浏览器（无需安装 Playwright）：
    python scripts/auto_challenge.py --pack-only

  首次使用：有头浏览器打开页面，手工登录后自动上传并挑战列表中的第一个对手：
    python scripts/auto_challenge.py

  按名称子串选择对手：
    python scripts/auto_challenge.py --opponent 某队名

  按序号选择对手（从 1 开始，与 --list-opponents 的编号一致）：
    python scripts/auto_challenge.py --index 3

  名称和序号一起用时，先按名称筛选，再取筛选结果中的该序号：
    python scripts/auto_challenge.py --opponent 队 --index 2

  只打印对手名称，不点击「挑战」：
    python scripts/auto_challenge.py --list-opponents

  只打开页面并记录接口，不自动上传、不自动挑战：
    python scripts/auto_challenge.py --discover

  结束时立刻关闭浏览器：
    python scripts/auto_challenge.py --close

依赖（仅浏览器模式需要）：
  pip install -r scripts/requirements.txt
  python -m playwright install chromium

登录态保存在仓库根目录 ``.browser-profile/``。默认有头模式，便于首次 SSO。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 平台页面。本环境（云端 VM）解析不了该主机，下面的浏览器步骤无法在这里实点。
PAGE_URL = "https://coregeek.rnd.huawei.com/GeneralIntro/1"
# 上传代码入口：页签 li[3]
UPLOAD_TAB_XPATH = "/html/body/div/div/div[2]/div/div[3]/div/div/div/div[1]/div/ul/li[3]/span"
# 选择对手进行挑战：页签 li[1]
CHALLENGE_TAB_XPATH = "/html/body/div/div/div[2]/div/div[3]/div/div/div/div[1]/div/ul/li[1]/span"
# 挑战弹窗中的地图列表，点击第一个可见项
MAP_XPATH = "/html/body/div[2]/div/div[2]/div/div[1]/div[2]/div/div/div/div/div/div"

# 这些键以及键名中包含 token/cookie/session 的字段，一律不写入日志。
_SENSITIVE_PARTS = ("cookie", "authorization", "token", "password", "secret", "session")
_INLINE_SECRET = re.compile(
    r"(?i)\b(cookie|authorization|access_token|refresh_token|id_token|token|password|session(?:id)?)"
    r"(\s*[:=]\s*)([^\s,&;\"']+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_RELATED = re.compile(r"upload|submit|challenge|opponent|battle|contest|archive|tar\.gz|上传|挑战|对手|地图|提交", re.I)
_CHALLENGE_TEXT = re.compile(r"^\s*挑战\s*$")
_SKIP_ROW_LABELS = {"挑战", "查看", "详情", "操作"}

_LOG_LOCK = threading.Lock()


def repo_root() -> Path:
    """脚本位于仓库的 scripts/ 下，仓库根是其上一级。"""
    return Path(__file__).resolve().parents[1]


def ms(seconds: float) -> int:
    return int(seconds * 1000)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)


def scrub_text(text: str) -> str:
    """抹掉字符串里的凭据样式，避免日志留下 Cookie、Authorization 或 token。"""
    text = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)
    text = _BEARER.sub("Bearer ***", text)
    text = _JWT.sub("***", text)
    return text


def redact_obj(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
                cleaned[key] = "***"
            else:
                cleaned[key] = redact_obj(item)
        return cleaned
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if is_sensitive_key(key):
            pairs.append((key, "***"))
        else:
            pairs.append((key, scrub_text(value)))
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))
    return scrub_text(cleaned)


def summarize_body(request) -> object:
    """生成请求体摘要。multipart 不读取文件字节；JSON 会按字段名脱敏。"""
    try:
        content_type = (request.header_value("content-type") or "").lower()
    except Exception:
        content_type = ""
    if "multipart/form-data" in content_type:
        return {"类型": "multipart/form-data", "说明": "已省略文件二进制与表单原文"}
    try:
        raw = request.post_data
    except Exception:
        return None
    if not raw:
        return None
    if len(raw) > 5000:
        raw = raw[:5000] + "...(已截断)"
    try:
        return redact_obj(json.loads(raw))
    except Exception:
        return scrub_text(raw)


def looks_related(url: str, body_text: str) -> bool:
    return _RELATED.search(f"{url}\n{body_text}") is not None


def append_capture(log_path: Path, record: dict) -> None:
    line = scrub_text(json.dumps(record, ensure_ascii=False))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def on_response(response, log_path: Path) -> None:
    """只记 XHR/fetch。不写请求头，避免把 Cookie 和 Authorization 原样落盘。"""
    try:
        request = response.request
        if request.resource_type not in ("xhr", "fetch"):
            return
        url = redact_url(request.url)
        body = summarize_body(request)
        body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        record = {
            "时间": datetime.now().isoformat(timespec="seconds"),
            "方法": request.method,
            "url": url,
            "状态码": response.status,
            "资源类型": request.resource_type,
            "请求体摘要": body,
            "可能相关": looks_related(url, body_text),
        }
        append_capture(log_path, record)
    except Exception as exc:
        print(f"记录接口时出错，已跳过这一条：{exc}")


def attach_capture(context, log_path: Path) -> None:
    def bind(page) -> None:
        page.on("response", lambda response: on_response(response, log_path))

    for page in context.pages:
        bind(page)
    context.on("page", bind)


def save_error_screenshot(page) -> Path | None:
    shot = repo_root() / "scripts" / "last_error.png"
    if page is None:
        return None
    shot.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(shot), full_page=True)
        return shot
    except Exception:
        try:
            page.screenshot(path=str(shot))
            return shot
        except Exception as exc:
            print(f"截图失败：{exc}", file=sys.stderr)
            return None


def fail(page, message: str) -> None:
    shot = save_error_screenshot(page)
    current = ""
    if page is not None:
        try:
            current = page.url
        except Exception:
            current = ""
    print(f"错误：{message}", file=sys.stderr)
    if current:
        print(f"当前页面：{current}", file=sys.stderr)
    if shot is not None:
        print(f"已保存截图：{shot}", file=sys.stderr)
    raise SystemExit(1)


def pack_demo(demo_dir: Path) -> Path:
    """在 Demo 目录执行用户指定的打包命令。已有同名包先删掉，避免打进旧包。"""
    demo_dir = demo_dir.resolve()
    source = demo_dir / "CoreGeek"
    if not source.is_dir():
        print(f"错误：找不到待打包目录 {source}", file=sys.stderr)
        raise SystemExit(1)
    archive = demo_dir / "CoreGeek.tar.gz"
    if archive.exists():
        archive.unlink()
        print(f"已删除旧压缩包：{archive}")
    command = ["tar", "-czvf", "CoreGeek.tar.gz", "CoreGeek"]
    print(f"在 {demo_dir} 执行：{' '.join(command)}")
    result = subprocess.run(
        command,
        cwd=demo_dir,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        print("错误：打包失败。", file=sys.stderr)
        raise SystemExit(result.returncode or 1)
    if not archive.is_file():
        print("错误：tar 已结束，但没有生成 CoreGeek.tar.gz。", file=sys.stderr)
        raise SystemExit(1)
    print(f"打包完成：{archive}（{archive.stat().st_size} 字节）")
    return archive


def click_xpath(page, xpath: str, what: str, timeout_ms: int) -> None:
    locator = page.locator(f"xpath={xpath}").first
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.click()
    except Exception as exc:
        fail(page, f"无法点击{what}。绝对 xpath 可能已失效：{xpath}。{exc}")


def wait_until_logged_in(page, timeout_ms: int) -> None:
    print(
        f"等待上传代码入口出现（最多 {timeout_ms / 1000:.0f} 秒）。"
        "首次使用请在打开的浏览器里完成内网 SSO。"
    )
    locator = page.locator(f"xpath={UPLOAD_TAB_XPATH}").first
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        fail(
            page,
            "在登录等待时间内没有看到上传代码入口。请确认已经登录，且页面结构没有变化。"
            f" xpath：{UPLOAD_TAB_XPATH}。{exc}",
        )
    print("已检测到上传代码入口。")


def click_button_named(scope, names: tuple[str, ...]) -> str | None:
    for name in names:
        buttons = scope.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$"))
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if button.is_visible() and button.is_enabled():
                    button.click()
                    return name
            except Exception:
                continue
    return None


def visible_dialog(page):
    dialogs = page.locator("[role=dialog], .ant-modal-content, .el-dialog")
    for index in range(dialogs.count()):
        item = dialogs.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def pick_file_input(page):
    inputs = page.locator("input[type=file]")
    count = inputs.count()
    for index in range(count):
        item = inputs.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    if count:
        return inputs.first
    return None


def choose_file(page, archive: Path, timeout_ms: int, playwright_timeout) -> None:
    """先点上传页签。优先 set_input_files；页签若直接弹出文件框，则用 file chooser。"""
    tab = page.locator(f"xpath={UPLOAD_TAB_XPATH}").first
    try:
        tab.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        fail(page, f"无法点击上传代码入口。绝对 xpath 可能已失效：{UPLOAD_TAB_XPATH}。{exc}")

    try:
        with page.expect_file_chooser(timeout=2000) as chooser_info:
            tab.click()
        chooser_info.value.set_files(str(archive))
        print(f"已通过文件选择框选中：{archive.name}")
        return
    except playwright_timeout:
        # 点击已发生，但没有弹出系统文件框，继续找页面上的 file input。
        pass
    except Exception as exc:
        fail(page, f"点击上传代码入口失败。绝对 xpath 可能已失效：{UPLOAD_TAB_XPATH}。{exc}")

    page.wait_for_timeout(500)
    file_input = pick_file_input(page)
    if file_input is not None:
        file_input.set_input_files(str(archive))
        print(f"已通过 input[type=file] 选中：{archive.name}")
        return

    trigger = page.locator("button, a, span, div").filter(
        has_text=re.compile(r"选择文件|浏览|上传文件|点击上传")
    ).first
    try:
        with page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
            trigger.click()
        chooser_info.value.set_files(str(archive))
        print(f"已通过文件选择框选中：{archive.name}")
    except Exception as exc:
        fail(
            page,
            f"页面上没有 input[type=file]，也没有弹出文件选择框，无法提交 {archive.name}。{exc}",
        )


def confirm_upload(page, timeout_ms: int) -> None:
    primary = ("确认上传", "提交代码", "上传代码", "提交")
    deadline = time.monotonic() + timeout_ms / 1000
    fallback_after = time.monotonic() + min(8.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        name = click_button_named(page, primary)
        if name:
            print(f"已点击「{name}」。")
            return
        if time.monotonic() >= fallback_after:
            dialog = visible_dialog(page)
            if dialog is not None:
                name = click_button_named(dialog, ("确认", "确定"))
                if name:
                    print(f"已点击弹窗「{name}」。")
                    return
        page.wait_for_timeout(300)
    print("没有找到单独的提交/确认上传按钮，改为等待成功提示或文件名。")


def wait_upload_success(page, filename: str, timeout_ms: int) -> None:
    pattern = re.compile(re.escape(filename) + r"|上传成功|提交成功|上传完成")
    hint = page.get_by_text(pattern)
    try:
        hint.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        fail(page, f"已选择文件，但超时内没有看到「{filename}」或上传成功提示。{exc}")
    try:
        text = " ".join(hint.first.inner_text().split())
    except Exception:
        text = filename
    print(f"上传步骤完成，页面出现：{text[:120]}")


def upload_archive(page, archive: Path, timeout_ms: int, playwright_timeout) -> None:
    print("打开上传代码页签。")
    choose_file(page, archive, timeout_ms, playwright_timeout)
    confirm_upload(page, timeout_ms)
    wait_upload_success(page, archive.name, timeout_ms)


def opponent_name(text: str) -> str:
    """从表格行文本抽出对手名称，去掉单独的「挑战」等操作字样。"""
    parts: list[str] = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line or line in _SKIP_ROW_LABELS:
            continue
        parts.append(line)
    return " ".join(parts).strip()


def scroll_to_top(page) -> None:
    scroller = page.locator(".ant-table-body, .el-table__body-wrapper").first
    try:
        if scroller.count() and scroller.is_visible():
            scroller.evaluate("element => { element.scrollTop = 0; }")
            return
    except Exception:
        pass
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass


def scroll_list(page) -> bool:
    """向下翻对手列表。返回值表示滚动位置是否变化。"""
    scroller = page.locator(".ant-table-body, .el-table__body-wrapper").first
    try:
        if scroller.count() and scroller.is_visible():
            return bool(
                scroller.evaluate(
                    """element => {
                        const before = element.scrollTop;
                        const step = Math.max(element.clientHeight * 0.8, 120);
                        element.scrollTop = Math.min(element.scrollTop + step, element.scrollHeight);
                        return element.scrollTop !== before;
                    }"""
                )
            )
    except Exception:
        pass
    try:
        page.mouse.wheel(0, 700)
        return True
    except Exception:
        return False


def visible_opponent_rows(page) -> list:
    rows = page.locator("tbody tr")
    found = []
    for index in range(rows.count()):
        row = rows.nth(index)
        try:
            if not row.is_visible():
                continue
            text = row.inner_text(timeout=1000)
        except Exception:
            continue
        if "挑战" not in text:
            continue
        found.append(row)
    if found:
        return found

    buttons = page.locator("button, a").filter(has_text=_CHALLENGE_TEXT)
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            if not button.is_visible():
                continue
        except Exception:
            continue
        container = button.locator("xpath=ancestor::tr[1]")
        if container.count() == 0:
            container = button.locator("xpath=ancestor::li[1]")
        found.append(container.first if container.count() else button)
    return found


def click_challenge_button(row) -> None:
    button = row.locator("button, a, span").filter(has_text=_CHALLENGE_TEXT)
    if button.count():
        button.first.click()
        return
    row.get_by_text("挑战", exact=True).first.click()


def open_challenge_list(page, timeout_ms: int) -> None:
    print("打开选择对手页签。")
    click_xpath(page, CHALLENGE_TAB_XPATH, "「选择对手进行挑战」页签", timeout_ms)
    try:
        page.get_by_text("挑战", exact=True).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        fail(page, f"已点击挑战页签，但没有看到「挑战」按钮。{exc}")
    page.wait_for_timeout(400)
    scroll_to_top(page)


def iter_opponents(page):
    """从列表顶部往下收集对手。同文案的行只保留第一次，避免虚拟滚动重复计数。"""
    scroll_to_top(page)
    seen: set[str] = set()
    stagnant = 0
    for _ in range(300):
        grew = False
        for row in visible_opponent_rows(page):
            try:
                name = opponent_name(row.inner_text(timeout=1000))
            except Exception:
                continue
            if not name:
                name = "（未能从该行解析出名称）"
            if name in seen:
                continue
            seen.add(name)
            grew = True
            yield len(seen), name, row
        if grew:
            stagnant = 0
        else:
            stagnant += 1
        if stagnant >= 2 or not scroll_list(page):
            break
        page.wait_for_timeout(250)


def list_opponents(page, timeout_ms: int) -> None:
    open_challenge_list(page, timeout_ms)
    count = 0
    for index, name, _row in iter_opponents(page):
        print(f"{index}. {name}")
        count = index
    if count == 0:
        fail(page, "挑战列表里没有解析到对手。请确认表格行中能看到「挑战」。")
    print(f"共 {count} 个对手。此模式不点击「挑战」。")


def is_challenge_target(index: int, matches_seen: int, opponent: str | None, wanted_index: int | None) -> bool:
    """判断当前行是不是要挑战的对手。

    index、matches_seen 都从 1 开始。未指定名称时按完整列表序号；
    指定了名称时，matches_seen 只计名称命中的行。
    """
    if opponent is None:
        return index == (1 if wanted_index is None else wanted_index)
    return matches_seen == (1 if wanted_index is None else wanted_index)


def challenge_once(page, args, timeout_ms: int) -> None:
    open_challenge_list(page, timeout_ms)
    matches_seen = 0
    preview: list[str] = []
    for index, name, row in iter_opponents(page):
        if len(preview) < 80:
            preview.append(f"{index}. {name}")
        if args.opponent is not None and args.opponent not in name:
            continue
        matches_seen += 1
        if not is_challenge_target(index, matches_seen, args.opponent, args.index):
            continue
        print(f"挑战对手：[{index}] {name}")
        try:
            click_challenge_button(row)
        except Exception as exc:
            fail(page, f"找到对手「{name}」，但点击「挑战」失败。{exc}")
        select_first_map(page, timeout_ms)
        return

    shown = "\n".join(preview) if preview else "（空）"
    if args.opponent:
        fail(page, f"没有名称包含「{args.opponent}」的可挑战对手。已看到：\n{shown}")
    fail(page, f"没有找到要挑战的对手。已看到：\n{shown}")


def select_first_map(page, timeout_ms: int) -> None:
    maps = page.locator(f"xpath={MAP_XPATH}")
    try:
        maps.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        fail(page, f"找不到地图选项。绝对 xpath 可能已失效：{MAP_XPATH}。{exc}")

    clicked = False
    for index in range(maps.count()):
        item = maps.nth(index)
        try:
            if item.is_visible():
                item.click()
                print(f"已点击地图列表中第 {index + 1} 个可见项。")
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        fail(page, f"地图 xpath 匹配到了元素，但没有可点击的可见项：{MAP_XPATH}")

    page.wait_for_timeout(400)
    scopes = []
    popup = page.locator("xpath=/html/body/div[2]")
    if popup.count():
        scopes.append(popup.first)
    dialog = visible_dialog(page)
    if dialog is not None:
        scopes.append(dialog)
    for scope in scopes:
        name = click_button_named(scope, ("确定", "确认", "开始"))
        if name:
            print(f"已点击「{name}」。")
            return
    print("弹窗里没有「确定/确认/开始」按钮，选图后即结束这一步。")


def hold_for_user(args) -> None:
    if args.close:
        print("已指定 --close，即将关闭浏览器。")
        return
    if not sys.stdin.isatty():
        print("当前不是交互终端，不等待回车，直接关闭浏览器。")
        return
    try:
        input("流程已结束。浏览器保持打开，按回车后关闭...")
    except EOFError:
        print("标准输入已结束，关闭浏览器。")


def run_browser(args, archive: Path | None) -> None:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "错误：未安装 Playwright。请执行："
            "pip install -r scripts/requirements.txt && python -m playwright install chromium",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # 这里再次说明：编写脚本的环境访问不了内网，真正点页面要换到能登录该站的机器。
    print("浏览器流程需在能访问 https://coregeek.rnd.huawei.com 的机器上运行。")
    profile = repo_root() / ".browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    log_path = repo_root() / "scripts" / "captured_api.jsonl"
    print(f"浏览器配置目录：{profile}")
    print(f"接口日志（已去掉 Cookie / Authorization / token）：{log_path}")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=args.headless,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        context.set_default_timeout(ms(args.step_timeout))
        page = context.pages[0] if context.pages else context.new_page()
        attach_capture(context, log_path)
        try:
            try:
                page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=ms(max(args.step_timeout, 60)))
            except Exception as exc:
                fail(
                    page,
                    f"无法打开 {PAGE_URL}。该地址在华为内网，外网或当前云端环境通常不可达。{exc}",
                )
            wait_until_logged_in(page, ms(args.login_timeout))
            if args.discover:
                print("发现模式：不自动上传，也不点击挑战。可手工操作页面，请求会写入日志。")
            elif args.list_opponents:
                list_opponents(page, ms(args.step_timeout))
            else:
                if archive is None:
                    fail(page, "内部错误：正常模式缺少压缩包路径。")
                upload_archive(page, archive, ms(args.step_timeout), PlaywrightTimeout)
                for round_index in range(1, args.times + 1):
                    if args.times > 1:
                        print(f"开始第 {round_index}/{args.times} 次挑战。")
                    if round_index > 1:
                        try:
                            page.keyboard.press("Escape")
                        except Exception:
                            pass
                        page.wait_for_timeout(300)
                    challenge_once(page, args, ms(args.step_timeout))
            hold_for_user(args)
        finally:
            try:
                context.close()
            except Exception:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="打包 CoreGeek，并在平台网页上传代码、选择对手发起挑战。",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出。")
    parser._optionals.title = "参数"
    parser.add_argument(
        "--demo-dir",
        default=None,
        help="Demo 目录。默认是仓库根下的 Demo。在该目录执行 tar -czvf CoreGeek.tar.gz CoreGeek。",
    )
    parser.add_argument(
        "--pack-only",
        action="store_true",
        help="只打包，不打开浏览器。没有 Playwright 时也可以用这个参数检查压缩包。",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="打开页面并记录 XHR/fetch。不自动上传，也不点击挑战。",
    )
    parser.add_argument(
        "--list-opponents",
        action="store_true",
        help="进入挑战列表并打印对手名称，不点击「挑战」。",
    )
    parser.add_argument(
        "--opponent",
        default=None,
        help="对手名称子串。在包含这段文字的行里点击「挑战」。不传则挑战列表中的第一个。",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=None,
        help="对手序号，从 1 开始。与 --opponent 同时使用时，先按名称筛选，再取筛选结果里的该序号。",
    )
    parser.add_argument(
        "--times",
        type=int,
        default=1,
        help="连续挑战次数，默认 1。未指定时不会无限循环。",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="使用无头浏览器。默认有头，方便内网 SSO 手工登录。",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=300,
        help="等待上传页签出现的秒数，默认 300，便于首次手工登录。",
    )
    parser.add_argument(
        "--step-timeout",
        type=float,
        default=30,
        help="单步点击、选文件和选地图的超时秒数，默认 30。",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="流程结束后立即关闭浏览器。默认在交互终端等待回车；非交互终端不会卡住。",
    )
    args = parser.parse_args(argv)
    if args.times < 1:
        parser.error("--times 必须是大于等于 1 的整数")
    if args.index is not None and args.index < 1:
        parser.error("--index 从 1 开始")
    if args.login_timeout <= 0 or args.step_timeout <= 0:
        parser.error("超时时间必须大于 0")
    if args.pack_only and (args.discover or args.list_opponents or args.opponent or args.index is not None):
        parser.error("--pack-only 只打包，不能同时指定发现模式、列对手或挑战对象")
    if args.discover and (args.list_opponents or args.opponent or args.index is not None or args.times != 1):
        parser.error("--discover 只记录接口，请不要同时指定列对手、对手或挑战次数")
    if args.list_opponents and (args.opponent or args.index is not None or args.times != 1):
        parser.error("--list-opponents 只打印名单，请不要同时指定 --opponent、--index 或 --times")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    demo_dir = Path(args.demo_dir) if args.demo_dir else repo_root() / "Demo"
    if args.pack_only:
        pack_demo(demo_dir)
        return
    archive = None
    if not args.discover and not args.list_opponents:
        archive = pack_demo(demo_dir)
    run_browser(args, archive)


if __name__ == "__main__":
    main()
