"""验证账号 profile 与静默 Cookie 提取。

用本地假站点冒充平台：
  /login  -> 种下会话 Cookie（模拟用户完成登录）
  /       -> 首页，不种 Cookie
断言三件事：
  1. 持久化 profile 能跨进程保住登录态
  2. silent_refresh 无头打开时能直接取到 Cookie，全程不需要再登录
  3. Cookie 新鲜时 get_cookie_header 走快路径，压根不启动浏览器
"""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: ⚠️ 必须是 ThreadingHTTPServer，不能用单线程的 HTTPServer。
#:
#: 这些假站点同时要服务**两种**请求：浏览器打开的页面（keep-alive，连接
#: 一直占着），和登录态自检那一发 `context.request.get`。单线程的
#: serve_forever 被前者占住时，后者卡在 accept 队列里等到超时，
#: Playwright 先放弃 → 服务端 write 撞上 BrokenPipe → `_run_probe`
#: 返回 None → 自检判定"问不出来"→ **退回 Cookie key 判定**。
#: 于是 test_stale_but_present_cookie_is_not_logged_in 这种"必须判未登录"
#: 的断言就会随机变绿/变红，而且只在整套跑、机器忙的时候才露头
#: （单独跑这个文件永远是绿的，最难查的那一类）。
HTTPServer = ThreadingHTTPServer
from pathlib import Path

import pytest

from app.browser import specs as spec_module
from app.browser.manager import BrowserSessionManager
from app.browser.specs import LoginSpec
from app.collectors.base import LoginRequired
from app.core.config import load_config
from app.core.db import Database
from app.repositories.account_repo import AccountRepository

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
pytestmark = pytest.mark.skipif(
    not Path(CHROMIUM).exists(), reason="沙箱 Chromium 不存在时跳过浏览器测试"
)


class _FakeSite(BaseHTTPRequestHandler):
    login_hits = 0

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/login"):
            type(self).login_hits += 1
            self.send_response(200)
            self.send_header("Set-Cookie", "fake_session=abc123; Path=/; Max-Age=86400")
            self.send_header("Set-Cookie", "fake_uid=u777; Path=/; Max-Age=86400")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>logged in</h1></body></html>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>home</h1></body></html>")

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_site():
    _FakeSite.login_hits = 0
    server = HTTPServer(("127.0.0.1", 0), _FakeSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture()
def fake_channel(fake_site, monkeypatch, tmp_path):
    """注册一个假平台的登录规则，指向本地站点。"""
    spec = LoginSpec(
        channel="faketest",
        home_url=f"{fake_site}/",
        login_url=f"{fake_site}/login",
        session_cookies=["fake_session"],
    )
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "faketest", spec)
    return spec


class _StubAccountRepo:
    """内存版账号仓储，避免这个测试依赖 MySQL。"""

    def __init__(self):
        self.rows = {}
        self.expired_calls = []

    def _key(self, channel, name):
        return f"{channel}:{name}"

    async def get(self, channel, account_name):
        return self.rows.get(self._key(channel, account_name))

    async def update(self, channel, account_name, fields):
        row = self.rows.setdefault(
            self._key(channel, account_name),
            {"channel": channel, "account_name": account_name, "enabled": 1},
        )
        row.update(fields)

    async def save_session(self, channel, account_name, *, cookies, profile_dir="", **kw):
        await self.update(channel, account_name, {
            "cookies": json.dumps(cookies),
            "cookie_updated_at": datetime.now(),
            "status": "active",
            "profile_dir": profile_dir,
        })

    async def mark_expired(self, channel, account_name, reason=""):
        self.expired_calls.append(reason)
        await self.update(channel, account_name, {"status": "expired", "last_error": reason})

    async def pick_active(self, channel, *, preferred="", group="", max_age_seconds=0):
        # ⚠️ 签名要跟着 AccountRepository.pick_active 走：真实实现加了 group
        # （按账号分组轮换）之后，这里不跟就会在调用点抛 TypeError，
        # 而错误信息指向的是 manager/page_session，很容易被当成生产代码的 bug。
        if preferred:
            return self.rows.get(self._key(channel, preferred))
        candidates = [
            row for row in self.rows.values()
            if row["channel"] == channel
            and (not group or row.get("account_group") == group)
        ]
        for row in candidates:
            if row.get("status") == "active":
                return row
        return candidates[0] if candidates else None

    async def active_names(self, channel, *, preferred="", group=""):
        """浏览器排队用的候选账号名单（见 browser/slots.py）。"""
        if preferred:
            return [preferred]
        return [
            row["account_name"] for row in self.rows.values()
            if row["channel"] == channel and row.get("status") == "active"
            and (not group or row.get("account_group") == group)
        ]


def _make_manager(tmp_path, repo):
    import os
    os.environ["SMC_BROWSER_EXECUTABLE_PATH"] = CHROMIUM
    os.environ["SMC_BROWSER_PROFILES_DIR"] = str(tmp_path / "profiles")
    config = load_config(use_cache=False)
    return BrowserSessionManager(config, repo)


async def _simulate_user_login(manager, channel, account_name, login_url):
    """模拟用户在交互式浏览器里完成登录：打开 profile，访问 /login 种 Cookie。"""
    from playwright.async_api import async_playwright
    directory = manager.profile_dir(channel, account_name)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(directory), **manager._launch_kwargs(headless=True)
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(login_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(500)
        finally:
            await context.close()


@pytest.mark.asyncio
async def test_profile_persists_login_and_silent_refresh_works(fake_channel, tmp_path):
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    await repo.update("faketest", "acc1", {"status": "never_login"})

    # 1) 用户手动登录一次
    await _simulate_user_login(manager, "faketest", "acc1", fake_channel.login_url)
    assert _FakeSite.login_hits == 1

    # 2) 静默刷新：只访问首页，不碰 /login，仍应取到登录 Cookie
    cookies = await manager.silent_refresh("faketest", "acc1")
    names = {c["name"] for c in cookies}
    assert "fake_session" in names
    assert "fake_uid" in names
    # 关键断言：整个静默过程没有再走一次登录
    assert _FakeSite.login_hits == 1

    account = await repo.get("faketest", "acc1")
    assert account["status"] == "active"
    assert account["cookie_updated_at"] is not None


@pytest.mark.asyncio
async def test_fresh_cookie_skips_browser_entirely(fake_channel, tmp_path, monkeypatch):
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    await repo.update("faketest", "acc1", {
        "status": "active",
        "cookies": json.dumps([{"name": "fake_session", "value": "abc123"}]),
        "cookie_updated_at": datetime.now(),
    })

    # 只要走了浏览器就让测试失败
    async def _boom(*args, **kwargs):
        raise AssertionError("Cookie 还新鲜时不应该启动浏览器")
    monkeypatch.setattr(manager, "silent_refresh", _boom)

    account, header = await manager.get_cookie_header("faketest", "acc1")
    assert header == "fake_session=abc123"
    assert account["account_name"] == "acc1"


@pytest.mark.asyncio
async def test_stale_cookie_triggers_silent_refresh(fake_channel, tmp_path):
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    await _simulate_user_login(manager, "faketest", "acc1", fake_channel.login_url)
    # 造一个"很久以前"的 Cookie，强制走刷新
    await repo.update("faketest", "acc1", {
        "status": "active",
        "cookies": json.dumps([{"name": "stale", "value": "old"}]),
        "cookie_updated_at": datetime.now() - timedelta(hours=5),
    })

    account, header = await manager.get_cookie_header("faketest", "acc1")
    # 重点：陈旧的 Cookie 触发了一次静默刷新，页面里最新的那份被取回来了
    assert "fake_session=abc123" in header

    # ⚠️ 库里那条旧 Cookie 仍然在 header 里，这是**有意的**：
    # silent_refresh 会把库里已存的 Cookie 当种子塞进浏览器再取出来
    # （见 manager.silent_refresh 的说明）——平台会不定期清理/重发 Cookie，
    # 不带种子的话会出现"越采越少"，最后连登录凭证都掉了。
    # 代价就是这条 stale 会一直跟着；它不影响判定（登录态看的是 session_cookies）。
    assert "stale=old" in header


@pytest.mark.asyncio
async def test_dead_profile_raises_login_required(fake_channel, tmp_path):
    """profile 里没有登录态时必须明确抛 LoginRequired，而不是静默返回空 Cookie。"""
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    await repo.update("faketest", "acc_never", {"status": "never_login"})

    with pytest.raises(LoginRequired, match="登录态已失效"):
        await manager.silent_refresh("faketest", "acc_never")

    account = await repo.get("faketest", "acc_never")
    assert account["status"] == "expired"
    assert repo.expired_calls


@pytest.mark.asyncio
async def test_no_account_raises_login_required(fake_channel, tmp_path):
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    with pytest.raises(LoginRequired, match="没有可用账号"):
        await manager.get_cookie_header("faketest")


@pytest.mark.asyncio
async def test_no_login_channel_returns_empty(tmp_path):
    """携程/同程这类免登录平台，直接返回空 Cookie，不做任何账号检查。"""
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    account, header = await manager.get_cookie_header("ctrip")
    assert account == {} and header == ""


# ---------------------------------------------------------------------------
# 手动导入 Cookie（用户反馈：扫码登录之后 profile 里还是只有游客 Cookie）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_imported_cookies_land_in_the_browser_profile(fake_channel, tmp_path):
    """导入的 Cookie 必须真的写进 profile，不能只存数据库。

    这是最关键的一条：抖音/快手采集时会用这个 profile 开一个真实页面
    （msToken 在 localStorage、快手签名要调页面里的函数）。
    只写数据库的话，页面还是未登录，抖音照样回「请先登录再继续搜索吧」。
    """
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)

    result = await manager.import_cookies(
        "faketest", "acc_import",
        "fake_session=abc123; fake_uid=u777",
    )

    assert result["count"] == 2
    assert result["format"] == "header"
    assert not result.get("profile_warning"), result.get("profile_warning")
    # 写进去之后再读出来，数量对得上
    assert result["injected_to_profile"] >= 2

    # 数据库那边也写了
    row = await repo.get("faketest", "acc_import")
    assert row["status"] == "active"
    assert "fake_session" in row["cookies"]

    # 真正的验收：重新打开这个 profile（新进程、新 context），登录态还在。
    # 走 silent_refresh 这条生产路径，它会开无头浏览器、导航、判定登录态。
    cookies = await manager.silent_refresh("faketest", "acc_import")
    names = {c["name"] for c in cookies}
    assert "fake_session" in names, f"导入的 Cookie 没能留在 profile 里：{names}"


@pytest.mark.asyncio
async def test_import_rejects_cookies_without_credentials(fake_channel, tmp_path):
    """只有游客 Cookie 的话直接拒绝，别让用户以为导入成功了。"""
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)

    with pytest.raises(ValueError, match="没有登录凭据"):
        await manager.import_cookies("faketest", "acc_bad", "visitor_id=1; theme=dark")

    # 拒绝掉的导入不能把账号标成可用
    assert await repo.get("faketest", "acc_bad") is None


@pytest.mark.asyncio
async def test_imported_cookies_are_usable_by_collectors(fake_channel, tmp_path):
    """导入后 get_cookie_header 能直接拿到可用的 Cookie 串。"""
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    await manager.import_cookies("faketest", "acc_use", "fake_session=abc123")

    account, header = await manager.get_cookie_header("faketest", "acc_use")
    assert account["account_name"] == "acc_use"
    assert "fake_session=abc123" in header


# ---------------------------------------------------------------------------
# 无头开关（用户反馈：系统设置里关了无头，跑起来还是无头）
# ---------------------------------------------------------------------------

def test_headless_follows_config(tmp_path, monkeypatch):
    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)

    manager.config.set("browser.headless", True)
    assert manager._launch_kwargs()["headless"] is True

    manager.config.set("browser.headless", False)
    assert manager._launch_kwargs()["headless"] is False, (
        "系统设置里把无头关掉了，采集时还是无头——用户报过这个"
    )

    # 显式传参仍然可以强制覆盖（登录窗口必须有头，写 Cookie 那次必须无头）
    assert manager._launch_kwargs(headless=True)["headless"] is True
    assert manager._launch_kwargs(headless=False)["headless"] is False


@pytest.mark.asyncio
async def test_page_session_seeds_cookies_from_db(fake_channel, fake_site, tmp_path):
    """采集页面要用库里存的 Cookie 当种子。

    这是"手动导入 Cookie"能对抖音/快手生效的关键：
    抖音的 msToken 在页面 localStorage 里、快手的签名要调页面里的函数，
    所以页面本身必须是登录态。光把 Cookie 串塞进请求头是不够的。

    这里用一个全新的空 profile：如果不种 Cookie，页面必然是未登录，
    PageSession 会抛 LoginRequired。
    """
    from app.browser.page_session import PageSession

    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)

    # 账号在库里是已登录的（比如刚手动导入过），但 profile 是空的
    await repo.save_session("faketest", "acc_seed", cookies=[{
        "name": "fake_session", "value": "abc123",
        "domain": "127.0.0.1", "path": "/",
        "expires": 4102444800,     # 2100 年，不会过期
    }])

    session = PageSession(manager, "faketest", "acc_seed")
    await session.start()
    try:
        assert "fake_session=abc123" in session.cookie_header
        session.ensure_logged_in()   # 不该抛
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_page_session_refuses_while_login_window_is_open(fake_channel, tmp_path):
    """登录窗口占着 profile 时，采集要直接说清楚，而不是开出一个空 profile。

    Chromium 不允许两个实例共用一个 user-data-dir。硬开的话拿到的常常是
    一个全新的空 profile —— 用户看到的就是"刚扫完码，任务却说只有 4 个 Cookie"。
    """
    from app.browser.page_session import PageSession

    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    await repo.save_session("faketest", "acc_busy", cookies=[
        {"name": "fake_session", "value": "abc", "domain": "127.0.0.1", "path": "/"}
    ])

    manager.mark_profile_busy("faketest", "acc_busy", "登录窗口已打开")
    session = PageSession(manager, "faketest", "acc_busy")
    with pytest.raises(LoginRequired, match="正被占用"):
        await session.start()

    manager.release_profile("faketest", "acc_busy")
    assert manager.profile_busy_reason("faketest", "acc_busy") == ""


@pytest.mark.asyncio
async def test_ensure_logged_in_rejects_visitor_only_cookies(fake_channel, tmp_path):
    """Cookie 里没有登录凭据时，采集器要在本地就拦下来。

    用户遇到的原状：profile 里 4 个游客 Cookie -> 判定"已登录" -> 任务跑起来
    -> 抖音回 status_code=2483。那个报错指向采集器而不是账号，很误导。
    """
    from app.browser.page_session import PageSession
    from app.browser import specs as spec_module

    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)

    session = PageSession(manager, "douyin", "acc_visitor")
    session.cookie_header = (
        "ttwid=1%7CnSMW; passport_csrf_token=0ee9; "
        "passport_csrf_token_default=0ee9; s_v_web_id=verify_mt71"
    )
    with pytest.raises(LoginRequired) as excinfo:
        session.ensure_logged_in()

    message = str(excinfo.value)
    assert "只有 4 个 Cookie" in message
    assert "sessionid" in message
    assert "导入 Cookie" in message, "报错里要告诉用户下一步怎么办"


# ---------------------------------------------------------------------------
# 跨域登录：登录站点和采集站点不是同一个域（微博就是这样）
# ---------------------------------------------------------------------------

class _CrossDomainSite(BaseHTTPRequestHandler):
    """模拟微博那套跨域登录。

    真实场景：
        登录在 passport.weibo.com  -> Cookie 落在 .weibo.com
        采集用 m.weibo.cn          -> 属于 .weibo.cn，**另一个注册域**
        两边都有一个叫 SUB 的 Cookie，但采集域那份要走完登录再访问一次才拿得到。

    这里用同一个端口的两个主机名来复现：127.0.0.1 当登录域，localhost 当采集域。
    浏览器按主机名隔离 Cookie，所以这两个是实打实的不同域。
    """

    logged_in = False

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/sso/signin"):
            # 登录域也种一个叫 SUB 的 Cookie —— 微博的 passport 就是这么干的。
            # 判定登录态时如果不限定域，就会被这一份骗过去。
            type(self).logged_in = True
            self._html("SUB=passport_domain_only; Path=/; Max-Age=86400")
            return
        if self.path.startswith("/collect/handoff"):
            # SSO 的最后一跳落在采集域上，这一步才种采集域的会话 Cookie
            self._html("SUB=collect_domain_session; Path=/; Max-Age=86400")
            return
        # 采集域首页自己不种 Cookie —— 跨域拿不到登录域那份，
        # 所以没走过 handoff 的话，这里就是未登录状态
        self._html()

    def _html(self, cookie: str = ""):
        self.send_response(200)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>ok</body></html>")

    def log_message(self, *args):
        pass


@pytest.fixture()
def cross_domain_site():
    _CrossDomainSite.logged_in = False
    server = HTTPServer(("127.0.0.1", 0), _CrossDomainSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    yield {
        # 两个不同的主机名 = 两个不同的 Cookie 域
        "login": f"http://127.0.0.1:{port}/sso/signin",
        "collect": f"http://localhost:{port}/collect",
        "handoff": f"http://localhost:{port}/collect/handoff",
    }
    server.shutdown()


@pytest.mark.asyncio
async def test_login_state_is_judged_on_the_collect_domain(
    cross_domain_site, monkeypatch, tmp_path
):
    """判定登录态只能看**采集域**收得到的 Cookie。

    用户遇到的原状：微博登录成功、系统也提示"登录态已保存"，
    但下次采集又说失效，要反复重新登录，直到手动导入 Cookie 才好。

    原因是判定时把 context 里所有 Cookie 都算上了。微博的 passport 域也有一个
    叫 SUB 的 Cookie，判定就这么通过了——可采集用的 m.weibo.cn 根本收不到它。
    """
    from app.browser.specs import LoginSpec

    spec = LoginSpec(
        channel="crossdomain",
        home_url=cross_domain_site["collect"],
        login_url=cross_domain_site["login"],
        session_cookies=["SUB"],
        cookie_urls=[cross_domain_site["collect"]],
        post_login_url=cross_domain_site["collect"],
    )
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "crossdomain", spec)

    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)

    # 只在登录域走了一圈：拿到的是登录域那份 SUB，采集域还是空的。
    # 这时候必须判为「未登录」——不限定域的话这里会误判成已登录。
    await _visit(manager, "crossdomain", "acc_cross", cross_domain_site["login"])
    with pytest.raises(LoginRequired):
        await manager.silent_refresh("crossdomain", "acc_cross")

    # 走完 SSO 的最后一跳（登录成功后我们会自动跳一次采集域），
    # 采集域这才拿到真正的会话 Cookie
    await _visit(manager, "crossdomain", "acc_cross", cross_domain_site["handoff"])

    cookies = await manager.silent_refresh("crossdomain", "acc_cross")
    by_name = {c["name"]: c["value"] for c in cookies}
    assert by_name.get("SUB") == "collect_domain_session", (
        f"存下来的不是采集域那份 Cookie：{by_name}"
    )


# ---------------------------------------------------------------------------
# 过期但还在的 Cookie：只看 key 判定不出来，得问平台
# ---------------------------------------------------------------------------

class _StaleCookieSite(BaseHTTPRequestHandler):
    """profile 里留着上次的会话 Cookie，但服务端早就不认了。

    这正是用户遇到的微博：日志里「交互式登录成功，保存 11 个 Cookie」只用了 7 秒
    ——根本来不及扫码。因为 profile 里上次那份 .weibo.cn Cookie 还在，
    key 一个不少，登录窗口一开就判定"已登录"，存下这份废 Cookie 就关窗，
    然后采集回来一张 card_type=4 的提示卡，下次又让你登录。

    /api/config 是微博自己的「我登录了吗」接口，问它才知道 Cookie 还有没有效。
    """

    accepts = False   # 服务端认不认这份 Cookie

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/config"):
            payload = json.dumps({"ok": 1, "data": {"login": type(self).accepts}})
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 首页照发会话 Cookie —— key 判定这一关它永远能过
        self.send_response(200)
        self.send_header("Set-Cookie", "SESSION_TOKEN=left_over_from_last_time; Path=/; Max-Age=86400")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>ok</body></html>")

    def log_message(self, *args):
        pass


@pytest.fixture()
def stale_cookie_site():
    _StaleCookieSite.accepts = False
    server = HTTPServer(("127.0.0.1", 0), _StaleCookieSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.mark.asyncio
async def test_stale_but_present_cookie_is_not_logged_in(
    stale_cookie_site, monkeypatch, tmp_path
):
    """Cookie 还在但已失效时，必须判为未登录。

    只看 key 的话这里会通过——这就是微博"登录完又让我登录"的根因。
    """
    from app.browser.specs import LoginSpec

    spec = LoginSpec(
        channel="stalecookie",
        home_url=stale_cookie_site,
        session_cookies=["SESSION_TOKEN"],
        verify_url=f"{stale_cookie_site}/api/config",
        verify_truthy_path=["data", "login"],
    )
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "stalecookie", spec)

    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)

    # 访问一次首页：profile 里现在有 SESSION_TOKEN 了，key 判定会说"已登录"
    await _visit(manager, "stalecookie", "acc_stale", stale_cookie_site)

    with pytest.raises(LoginRequired):
        await manager.silent_refresh("stalecookie", "acc_stale")

    # 服务端开始认这份 Cookie（= 用户真的登录了），同样的 Cookie 就该通过
    _StaleCookieSite.accepts = True
    cookies = await manager.silent_refresh("stalecookie", "acc_stale")
    assert any(c["name"] == "SESSION_TOKEN" for c in cookies), cookies


@pytest.mark.asyncio
async def test_verify_url_unreachable_falls_back_to_cookie_keys(
    monkeypatch, tmp_path, cross_domain_site
):
    """自检接口连不上时退回 key 判定，不能把用户锁在门外。

    诊断手段变成故障点是最糟的一种设计：平台接口改个字段，
    所有账号一夜之间全变"未登录"。
    """
    from app.browser.specs import LoginSpec

    spec = LoginSpec(
        channel="unreachable",
        home_url=cross_domain_site["collect"],
        session_cookies=["SUB"],
        cookie_urls=[cross_domain_site["collect"]],
        # 指向一个不存在的端口
        verify_url="http://127.0.0.1:9/api/config",
        verify_truthy_path=["data", "login"],
    )
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "unreachable", spec)

    repo = _StubAccountRepo()
    manager = _make_manager(tmp_path, repo)
    await _visit(manager, "unreachable", "acc_fb", cross_domain_site["handoff"])

    cookies = await manager.silent_refresh("unreachable", "acc_fb")
    assert any(c["name"] == "SUB" for c in cookies), cookies


async def _visit(manager, channel, account_name, url):
    """在该账号的 profile 里访问一个 URL，模拟用户手动操作。"""
    from playwright.async_api import async_playwright
    from app.core.subprocess_loop import SUBPROCESS_LOOP

    directory = manager.profile_dir(channel, account_name)

    async def _run():
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(directory), **manager._launch_kwargs(headless=True)
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(600)
            finally:
                await context.close()

    await SUBPROCESS_LOOP.run(_run())


def test_cookie_scope_defaults_to_home_url():
    from app.browser.specs import LoginSpec, get_spec

    plain = LoginSpec(channel="x", home_url="https://example.com")
    assert plain.cookie_scope == ["https://example.com"]
    assert plain.settle_url == "https://example.com"

    # 微博全走 PC 端：登录、Cookie、采集都在 weibo.com 这一个域上。
    # 登录过程会经过 login.sina.com.cn，所以那个域也收
    weibo = get_spec("weibo")
    assert weibo.home_url == "https://weibo.com"
    assert "https://weibo.com" in weibo.cookie_urls
    assert not any("m.weibo.cn" in u for u in weibo.cookie_urls), (
        "已经不走移动端了，别再把 m.weibo.cn 放回来"
    )
    assert weibo.settle_url == "https://weibo.com"
    assert "weibo.com" in weibo.login_entry
    # ⚠️ 微博**故意没有** verify_probes（见 specs.py 里的说明）：
    # context.request.get 不跟随重定向，未登录时微博 302 到 passport，
    # 而已登录的 profile 也可能因为 SSO 未同步拿到 302，
    # 结果是"明明登录着却被判失效"。它只靠 session_cookies（SUB）判定。
    # 所以这里断言的是"没有探测点"，有探测点反而是回退。
    assert weibo.probes == [], "微博改回用探测点判定登录态了？那个方案会误判"


# ---------------------------------------------------------------------------
# profile 单例锁：残留会让 Chromium 另开一个空 profile
# ---------------------------------------------------------------------------

def test_release_profile_locks_removes_stale_singletons(tmp_path):
    """进程被强杀后留下的 SingletonLock 必须清掉。

    不清的话 Chromium 认为目录被占用，**默默开一个全新的空 profile**——
    不报错、不告警，用户看到的是"昨天还登录着，今天又要重新登录"。
    这是持久化 profile 方案迟早都会踩到的一个坑。
    """
    profile = tmp_path / "douyin" / "acc1"
    profile.mkdir(parents=True)
    (profile / "SingletonLock").write_text("stale")
    (profile / "SingletonSocket").write_text("stale")
    (profile / "Cookies").write_text("真正的数据，不许动")

    removed = BrowserSessionManager.release_profile_locks(profile)

    assert set(removed) == {"SingletonLock", "SingletonSocket"}
    assert not (profile / "SingletonLock").exists()
    assert (profile / "Cookies").read_text() == "真正的数据，不许动", (
        "只能清那几个已知的锁文件，不能碰 profile 里别的东西"
    )


def test_release_profile_locks_handles_dangling_symlink(tmp_path):
    """Linux/Mac 上这几个是符号链接，断链时 exists() 返回 False。

    只判 exists() 的话断链清不掉，而 Chromium 照样认为目录被占用。
    """
    profile = tmp_path / "p"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to(tmp_path / "不存在的目标")
    assert not (profile / "SingletonLock").exists()      # 断链

    assert BrowserSessionManager.release_profile_locks(profile) == ["SingletonLock"]
    assert not (profile / "SingletonLock").is_symlink()


def test_release_profile_locks_is_quiet_when_clean(tmp_path):
    profile = tmp_path / "p"
    profile.mkdir()
    assert BrowserSessionManager.release_profile_locks(profile) == []


# ---------------------------------------------------------------------------
# Cookie 请求头去重
# ---------------------------------------------------------------------------

def test_cookie_header_keeps_one_value_per_name():
    """同名 Cookie 只能发一份。

    一个 name 会在多个域上各存一份（`.douyin.com` 和 `www.douyin.com`
    都能匹配 https://www.douyin.com），直接 join 会发成
    `ttwid=A; ...; ttwid=B`。真实浏览器绝不会这么发，
    而服务端取第一个还是最后一个各家不一样——于是就有了
    "同一份 Cookie，有时候能采有时候不能"这种最难查的现象。
    """
    from app.browser import cookie_import

    header = cookie_import.to_header([
        {"name": "ttwid", "value": "旧的", "domain": ".douyin.com"},
        {"name": "sessionid", "value": "S1", "domain": ".douyin.com"},
        {"name": "ttwid", "value": "新的", "domain": "www.douyin.com"},
    ])
    assert header.count("ttwid=") == 1, header
    # 取后面那个：Playwright 返回时域更具体的通常在后，也是浏览器优先发的那份
    assert "ttwid=新的" in header
    assert "sessionid=S1" in header


def test_cookie_header_skips_empty_names_and_none_values():
    from app.browser import cookie_import

    header = cookie_import.to_header([
        {"name": "", "value": "x"},
        {"name": "a", "value": None},
        {"name": "b", "value": ""},
        {"name": "c", "value": "1"},
    ])
    assert header == "b=; c=1"


# ---------------------------------------------------------------------------
# 微博式两站登录：PC 站登录，采集站的会话靠 SSO 换过来
# ---------------------------------------------------------------------------

class _TwoSiteWeibo(BaseHTTPRequestHandler):
    """复刻微博的结构。

        weibo.com（这里用 127.0.0.1）   —— 登录态的源头，用户在这里登录
        m.weibo.cn（这里用 localhost）  —— 采集接口认的那一份，靠 SSO 换过来

    两个域**各有一个叫 SUB 的 Cookie，值不一样**。
    采集站的会话只有在"PC 站已登录 + 访问过采集站"之后才存在。
    """

    pc_logged_in = False
    mobile_handoff_done = False
    #: 移动站要访问几次才换过来（模拟 SSO 那串重定向不是一次就落地）
    handoff_needs = 1
    handoff_visits = 0

    def do_GET(self):  # noqa: N802
        cls = type(self)
        # ---- PC 站 ----
        if self.path.startswith("/pc/login"):
            cls.pc_logged_in = True
            self._html("SUB=pc_domain_session; Path=/; Max-Age=86400")
            return
        if self.path.startswith("/ajax/profile/info"):
            if not cls.pc_logged_in:
                # 没登录就把你踢回登录页——探测靠这个判定
                self.send_response(302)
                self.send_header("Location", "/passport/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json({"ok": 1, "data": {"user": {"id": 123}}})
            return
        if self.path.startswith("/passport/login"):
            self._html()
            return

        # ---- 移动站 ----
        if self.path.startswith("/api/config"):
            self._json({"ok": 1, "data": {"login": cls.mobile_handoff_done}})
            return
        if self.path.startswith("/mobile"):
            cls.handoff_visits += 1
            if cls.pc_logged_in and cls.handoff_visits >= cls.handoff_needs:
                cls.mobile_handoff_done = True
                self._html("SUB=mobile_domain_session; Path=/; Max-Age=86400")
                return
            self._html()
            return
        self._html()

    def _json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, cookie: str = ""):
        self.send_response(200)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>ok</body></html>")

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        # Playwright 的探测请求经常读到响应就断开，写回去会 BrokenPipe。
        # 那是客户端行为，不是被测代码的问题，别让它污染测试输出。
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


@pytest.fixture()
def two_site_weibo():
    _TwoSiteWeibo.pc_logged_in = False
    _TwoSiteWeibo.mobile_handoff_done = False
    _TwoSiteWeibo.handoff_needs = 1
    _TwoSiteWeibo.handoff_visits = 0
    server = HTTPServer(("127.0.0.1", 0), _TwoSiteWeibo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    yield {
        # 两个主机名 = 两个真正不同的 Cookie 域
        "pc": f"http://127.0.0.1:{port}",
        "pc_login": f"http://127.0.0.1:{port}/pc/login",
        "pc_probe": f"http://127.0.0.1:{port}/ajax/profile/info?uid=1",
        "mobile": f"http://localhost:{port}/mobile",
        "mobile_probe": f"http://localhost:{port}/api/config",
    }
    server.shutdown()


def _two_site_spec(site) -> "LoginSpec":
    from app.browser.specs import LoginProbe, LoginSpec

    return LoginSpec(
        channel="twosite",
        home_url=site["pc"],
        login_url=site["pc_login"],
        session_cookies=["SUB"],
        cookie_urls=[site["pc"], site["mobile"]],
        handoff_urls=[site["mobile"]],
        verify_probes=[
            LoginProbe(url=site["mobile_probe"], truthy_path=["data", "login"]),
            LoginProbe(url=site["pc_probe"]),
        ],
    )


@pytest.mark.asyncio
async def test_handoff_runs_on_every_silent_refresh(
    two_site_weibo, monkeypatch, tmp_path
):
    """用户的原话：「我其实已经登录的呢，老出现重新登录」。

    根因：采集站（m.weibo.cn）的会话是从 PC 站 SSO 换过来的，
    它有自己的有效期。过期后 PC 站通常还好好的，这时候**访问一次采集站**
    微博就会把会话重新种上。少了这一步，系统就会一口咬定"登录态已失效"。
    """
    spec = _two_site_spec(two_site_weibo)
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "twosite", spec)

    manager = _make_manager(tmp_path, _StubAccountRepo())

    # 只在 PC 站登录，**没有**访问过采集站
    await _visit(manager, "twosite", "acc", two_site_weibo["pc_login"])
    assert _TwoSiteWeibo.pc_logged_in is True
    assert _TwoSiteWeibo.mobile_handoff_done is False

    # 静默刷新时会自动做一次会话交接，所以这里应该成功而不是抛"登录失效"
    cookies = await manager.silent_refresh("twosite", "acc")

    assert _TwoSiteWeibo.mobile_handoff_done is True, "静默刷新必须触发一次会话交接"
    by_domain = {(c["domain"].lstrip("."), c["name"]): c["value"] for c in cookies}
    # 两个域的 SUB 都收到了，而且各是各的值
    assert by_domain.get(("127.0.0.1", "SUB")) == "pc_domain_session"
    assert by_domain.get(("localhost", "SUB")) == "mobile_domain_session"


@pytest.mark.asyncio
async def test_handoff_is_retried_when_sso_needs_two_hops(
    two_site_weibo, monkeypatch, tmp_path
):
    """SSO 是一串重定向，不一定一次就落地，所以要给第二次机会。"""
    _TwoSiteWeibo.handoff_needs = 2
    spec = _two_site_spec(two_site_weibo)
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "twosite", spec)

    manager = _make_manager(tmp_path, _StubAccountRepo())
    await _visit(manager, "twosite", "acc", two_site_weibo["pc_login"])

    cookies = await manager.silent_refresh("twosite", "acc")
    assert _TwoSiteWeibo.mobile_handoff_done is True
    assert any(c["name"] == "SUB" and c["value"] == "mobile_domain_session"
               for c in cookies)


@pytest.mark.asyncio
async def test_logged_out_everywhere_still_fails(two_site_weibo, monkeypatch, tmp_path):
    """放宽成"任意一个域通过"，不能放宽到"哪个都没通过也算登录"。"""
    spec = _two_site_spec(two_site_weibo)
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "twosite", spec)

    manager = _make_manager(tmp_path, _StubAccountRepo())
    # 谁都没登录，只是访问过首页
    await _visit(manager, "twosite", "acc", two_site_weibo["pc"])

    with pytest.raises(LoginRequired):
        await manager.silent_refresh("twosite", "acc")


@pytest.mark.asyncio
async def test_pc_login_alone_is_enough_to_not_be_expired(
    two_site_weibo, monkeypatch, tmp_path
):
    """采集站怎么都换不过来时，也不能把账号判成"登录已失效"。

    PC 站还登录着就说明用户没退出，这时候该报的是"会话没换过来"，
    而不是把人赶去重新扫码——那正是用户抱怨的"老出现重新登录"。
    """
    _TwoSiteWeibo.handoff_needs = 99      # 采集站永远换不过来
    spec = _two_site_spec(two_site_weibo)
    monkeypatch.setitem(spec_module.LOGIN_SPECS, "twosite", spec)

    manager = _make_manager(tmp_path, _StubAccountRepo())
    await _visit(manager, "twosite", "acc", two_site_weibo["pc_login"])

    # PC 站那个探测点会通过，所以不该抛 LoginRequired
    cookies = await manager.silent_refresh("twosite", "acc")
    assert any(c["value"] == "pc_domain_session" for c in cookies)
