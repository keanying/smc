"""子进程专用事件循环的回归测试。

背景：Windows 上 uvicorn 带 --reload 或 --workers>1 时会把事件循环策略切成
SelectorEventLoop，而它不支持创建子进程。系统里有两处要 fork：
Playwright（浏览器驱动）和 jsvm（抖音 a_bogus 签名的 Node 进程），
两处都会直接抛 NotImplementedError。

这组测试锁住修复方式：**所有开子进程的调用都必须在专用循环
（subprocess-loop 线程）上执行，与服务器循环无关**。

最后一条是结构性检查：任何新增的 create_subprocess / Playwright 用法
如果忘了走这个循环，会被直接揪出来——第一次修复就是漏了 jsvm 才复发的。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

from app.core.subprocess_loop import (
    SUBPROCESS_LOOP, SubprocessLoop, current_loop_supports_subprocess,
)

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


@pytest.mark.asyncio
async def test_runs_on_dedicated_thread():
    """提交的协程必须落在 subprocess-loop 线程上，而不是调用方线程。"""
    caller_thread = threading.current_thread().name
    caller_loop = asyncio.get_running_loop()

    async def probe():
        return threading.current_thread().name, asyncio.get_running_loop()

    worker_thread, worker_loop = await SUBPROCESS_LOOP.run(probe())

    assert worker_thread == "subprocess-loop", f"跑在了 {worker_thread}"
    assert worker_thread != caller_thread
    assert worker_loop is not caller_loop, "复用了调用方的循环，等于没修"


@pytest.mark.asyncio
async def test_returns_values_and_propagates_exceptions():
    async def ok():
        return 42

    async def boom():
        raise ValueError("来自专用循环的错误")

    assert await SUBPROCESS_LOOP.run(ok()) == 42
    with pytest.raises(ValueError, match="来自专用循环的错误"):
        await SUBPROCESS_LOOP.run(boom())


@pytest.mark.asyncio
async def test_to_caller_loop_delivers_back():
    """专用循环里产生的回调要能安全地送回调用方循环执行。"""
    caller_loop = asyncio.get_running_loop()
    seen: list[tuple[str, object]] = []

    async def callback(value: str) -> None:
        seen.append((threading.current_thread().name, asyncio.get_running_loop()))

    forward = SUBPROCESS_LOOP.to_caller_loop(callback)

    async def inside_subprocess_loop():
        # 确认自己确实在专用循环里，然后往回投递
        assert threading.current_thread().name == "subprocess-loop"
        await forward("hello")

    await SUBPROCESS_LOOP.run(inside_subprocess_loop())

    assert len(seen) == 1
    thread_name, loop = seen[0]
    assert loop is caller_loop, "回调没有回到调用方循环"
    assert thread_name != "subprocess-loop"


def test_windows_uses_proactor_loop():
    """Windows 上必须显式用 ProactorEventLoop —— 只有它能开子进程。"""
    loop = SubprocessLoop._new_loop()
    try:
        if sys.platform == "win32":
            assert type(loop).__name__ == "ProactorEventLoop"
        else:
            assert isinstance(loop, asyncio.AbstractEventLoop)
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_survives_caller_loop_that_cannot_spawn_subprocess(monkeypatch):
    """模拟 Windows Selector 循环：调用方循环开子进程会抛 NotImplementedError，
    但通过 SUBPROCESS_LOOP 提交的 Playwright 任务照样能跑起来。"""
    caller_loop = asyncio.get_running_loop()

    async def _refuse(*args, **kwargs):
        raise NotImplementedError

    # 把调用方循环打残：它现在开不了子进程
    monkeypatch.setattr(caller_loop, "_make_subprocess_transport", _refuse, raising=False)
    monkeypatch.setattr(caller_loop, "subprocess_exec", _refuse, raising=False)

    with pytest.raises(NotImplementedError):
        await caller_loop.subprocess_exec(asyncio.SubprocessProtocol, "echo", "hi")

    # 专用循环不受影响
    async def spawn():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('ok')",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        return stdout.decode().strip()

    assert await SUBPROCESS_LOOP.run(spawn()) == "ok"


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="沙箱缺 Chromium")
@pytest.mark.asyncio
async def test_playwright_launches_through_subprocess_loop():
    """真起一次 Chromium，确认整条链路在专用循环上可用。"""
    async def launch():
        from playwright.async_api import async_playwright

        assert threading.current_thread().name == "subprocess-loop"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, executable_path=CHROMIUM,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            version = browser.version
            await browser.close()
            return version

    version = await SUBPROCESS_LOOP.run(launch())
    assert version, "没拿到浏览器版本"


@pytest.mark.asyncio
async def test_silent_refresh_uses_subprocess_loop(monkeypatch, tmp_path):
    """BrowserSessionManager 的 Playwright 部分必须走专用循环，
    数据库部分必须留在调用方循环——两者绝不能互换。"""
    import os

    from app.browser import specs as spec_module
    from app.browser.manager import BrowserSessionManager
    from app.browser.specs import LoginSpec
    from app.core.config import load_config

    os.environ["SMC_BROWSER_PROFILES_DIR"] = str(tmp_path / "profiles")
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "looptest", LoginSpec(
        channel="looptest", home_url="http://127.0.0.1:1/", session_cookies=["x"],
    ))

    caller_loop = asyncio.get_running_loop()
    where = {}

    class _Repo:
        async def get(self, channel, account_name):
            return None

        async def save_session(self, channel, account_name, *, cookies, profile_dir="", **kw):
            where["db"] = (threading.current_thread().name, asyncio.get_running_loop())

        async def mark_expired(self, *a, **kw):
            where["db"] = (threading.current_thread().name, asyncio.get_running_loop())

    manager = BrowserSessionManager(load_config(use_cache=False), _Repo())

    async def fake_grab(spec, directory, seed_cookies=None):
        where["browser"] = (threading.current_thread().name, asyncio.get_running_loop())
        return [{"name": "x", "value": "1"}]

    monkeypatch.setattr(manager, "_grab_cookies", fake_grab)
    await manager.silent_refresh("looptest", "acc")

    assert where["browser"][0] == "subprocess-loop", "浏览器动作没走专用循环"
    assert where["db"][1] is caller_loop, "数据库操作跑到别的循环上了，连接池会坏掉"


# ===================================================================
# 抖音签名的 Node 子进程 —— 第一次修复漏掉的那处
# ===================================================================

@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "libs" / "douyin.cjs").exists(),
    reason="缺少 libs/douyin.cjs",
)
@pytest.mark.asyncio
async def test_js_runtime_survives_crippled_caller_loop(monkeypatch):
    """调用方循环开不了子进程时，抖音 a_bogus 签名仍然要能算出来。

    这就是 2026-08-26 现场的复现：Playwright 已经改好了，但 jsvm 还留在
    服务器循环上，任务跑到「搜索关键字」那一步直接 NotImplementedError。
    """
    from app.utils.jsvm import get_js_runtime, shutdown_js_runtime

    # 先确保运行时是干净的，避免复用上一条测试留下的进程
    await shutdown_js_runtime()

    caller_loop = asyncio.get_running_loop()

    async def _refuse(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(caller_loop, "_make_subprocess_transport", _refuse, raising=False)
    monkeypatch.setattr(caller_loop, "subprocess_exec", _refuse, raising=False)

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    signature = await get_js_runtime().call(
        "douyin.cjs", "sign_datail",
        ["device_platform=webapp&aid=6383&keyword=test", user_agent],
    )
    assert isinstance(signature, str) and len(signature) > 50, f"签名不对：{signature!r}"

    await shutdown_js_runtime()


@pytest.mark.asyncio
async def test_js_runtime_runs_on_subprocess_loop():
    """Node 进程必须建在专用循环上——管道和进程绑定同一个循环，不能分家。"""
    from app.utils.jsvm import NodeJsRuntime

    runtime = NodeJsRuntime()
    where = {}

    original = runtime._ensure_started

    async def spy():
        where["thread"] = threading.current_thread().name
        await original()

    runtime._ensure_started = spy
    try:
        await runtime.call(
            "douyin.cjs", "sign_datail", ["aid=6383", "UA"],
        )
    except Exception:
        # 就算 douyin.cjs 不在，也已经进到 _ensure_started 了，线程名照样能断言
        pass
    finally:
        await runtime.shutdown()

    assert where.get("thread") == "subprocess-loop", (
        f"Node 进程建在了 {where.get('thread')}，不是专用循环"
    )


# ===================================================================
# 结构性检查：别再漏掉第三处
# ===================================================================

def test_all_subprocess_users_go_through_the_loop():
    """凡是会 fork 子进程的模块，都必须引用 SUBPROCESS_LOOP。

    这条测试的意义：第一次修复只处理了 Playwright，jsvm 里的
    asyncio.create_subprocess_exec 被漏掉，结果同一个 bug 换个地方又炸。
    以后任何人新增一处子进程调用而忘了走专用循环，这里会立刻失败。
    """
    import re

    app_dir = Path(__file__).resolve().parents[1] / "app"
    # 这些写法都会 fork 子进程
    spawn_pattern = re.compile(
        r"create_subprocess_exec|create_subprocess_shell|async_playwright"
    )

    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        if not spawn_pattern.search(text):
            continue
        # 专用循环模块自己是例外——它就是那个循环
        if path.name == "subprocess_loop.py":
            continue
        if "SUBPROCESS_LOOP" not in text:
            offenders.append(str(path.relative_to(app_dir.parent)))

    assert not offenders, (
        "以下模块会创建子进程但没有走 SUBPROCESS_LOOP，"
        f"在 Windows + uvicorn --reload 下会抛 NotImplementedError：{offenders}"
    )
