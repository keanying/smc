"""账号管理 API：多平台账号增删改、静默检测、交互式登录会话。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..browser.live_session import LiveBrowserSession, close_all, get_session, register
from ..browser.cookie_import import CookieParseError
from ..collectors.diagnostics import probe_all_channels, probe_keyword
from ..browser.specs import get_spec, needs_login
from ..core.constants import ALL_CHANNELS, CHANNEL_LABELS, CHANNELS_NEED_LOGIN
from .common import bad_request, not_found, ok
from .deps import AppState, get_state
from .schemas import AccountIn, CookieImportIn

router = APIRouter(prefix="/api/accounts", tags=["账号"])


@router.get("/channels")
async def channels():
    """账号管理页的平台列表，标出哪些平台压根不需要登录。"""
    return ok([
        {
            "value": channel,
            "label": CHANNEL_LABELS[channel],
            "need_login": channel in CHANNELS_NEED_LOGIN,
        }
        for channel in ALL_CHANNELS
    ])


@router.get("")
async def list_accounts(
    channel: str = "", status: str = "", group: str = "",
    page: int = 1, page_size: int = 50,
    state: AppState = Depends(get_state),
):
    return ok(await state.accounts.list(
        channel=channel, status=status, group=group, page=page, page_size=page_size
    ))


@router.get("/quota")
async def quota_usage(channel: str = "", account_name: str = "", days: int = 1,
                      state: AppState = Depends(get_state)):
    """账号今日用量 + 各渠道生效的配额上限。账号管理页用。"""
    from ..core.account_quota import AccountQuota
    from ..core.redis_client import get_redis

    from ..core.account_rotation import AccountRotation

    quota = AccountQuota(state.config, get_redis(), state.db)
    rotation = AccountRotation(state.config, get_redis())

    # 轮换锁的剩余时间。锁在 Redis 里、按 (渠道, 账号) 一把，所以要逐个问；
    # 账号数量是几个到几十个这个量级，一趟循环没什么成本。
    locked: dict = {}
    if rotation.enabled:
        page = await state.accounts.list(channel=channel, page=1, page_size=500)
        for row in page.get("items", []):
            left = rotation.locked_seconds(row["channel"], row["account_name"])
            if left > 0:
                locked[f"{row['channel']}/{row['account_name']}"] = left

    return ok({
        "enabled": quota.enabled,
        "limits": {ch: quota.limits_for(ch) for ch in CHANNELS_NEED_LOGIN},
        # 不算单平台配置时的值：页面上单平台留空 = 用这个
        "defaults": {ch: quota.base_limits_for(ch) for ch in CHANNELS_NEED_LOGIN},
        "usage": await quota.usage(channel=channel, account=account_name, days=days),
        "rotation": {
            "enabled": rotation.enabled,
            "default_hours": rotation.global_hours(),
            "locked": locked,
        },
    })


@router.post("/{channel}/{account_name}/unlock-rotation")
async def unlock_rotation(channel: str, account_name: str,
                          state: AppState = Depends(get_state)):
    """人工解掉轮换锁，让这个号立刻能被挑中。

    跟解除冷却不一样，这个**没什么风险**：轮换锁只是"刚干过活，让别人
    先上"的软偏好，解掉它顶多是这个号被连着用两轮，配额和冷却那两道闸
    还在。
    """
    from ..core.account_rotation import AccountRotation
    from ..core.redis_client import get_redis

    account = await state.accounts.get(channel, account_name)
    if not account:
        raise not_found(f"账号 {channel}/{account_name} 不存在")
    AccountRotation(state.config, get_redis()).release(channel, account_name)
    return ok(message="已解除轮换锁，这个账号下一轮就能被挑到")


@router.post("/{channel}/{account_name}/clear-cooldown")
async def clear_cooldown(channel: str, account_name: str,
                         state: AppState = Depends(get_state)):
    """人工解除冷却。急着用的时候能把账号放出来。

    ⚠️ 这是个**明知故犯**的口子：配额闸门本来就是为了别把账号用到被封，
    解除之后接着采是有风险的。所以只给一个按钮，不做自动解除。
    """
    from ..core.account_quota import AccountQuota
    from ..core.redis_client import get_redis

    account = await state.accounts.get(channel, account_name)
    if not account:
        raise not_found(f"账号 {channel}/{account_name} 不存在")
    quota = AccountQuota(state.config, get_redis(), state.db)
    await quota.clear_cooldown(channel, account_name)
    return ok(message="已解除冷却，这个账号可以接着用了")


@router.get("/browser-usage")
async def browser_usage(state: AppState = Depends(get_state)):
    """当前哪些账号的浏览器正被占用、被谁占着、占了多久。

    拟人模式下一个账号同时只能服务一个景区，账号全被占用时后来的任务会
    排队等。排队的时候页面上除了任务日志看不到别的，这个接口就是那扇窗——
    "为什么我的任务卡着不动"一眼就能答上来。
    """
    return ok(state.browser_manager.busy_snapshot())


@router.get("/groups")
async def list_groups(state: AppState = Depends(get_state)):
    """所有账号分组及组内账号数（账号管理页的分组筛选与任务页的选组下拉）。"""
    return ok(await state.accounts.list_groups())


@router.post("")
async def create_account(payload: AccountIn, state: AppState = Depends(get_state)):
    data = payload.model_dump()
    if not needs_login(data["channel"]):
        data["login_type"] = "none"
        data["status"] = "active"   # 免登录平台的账号只是个占位，直接算可用
    data["profile_dir"] = str(
        state.browser_manager.profile_dir(data["channel"], data["account_name"])
    )
    await state.accounts.create(data)
    message = (
        "账号已添加，请点击登录按钮完成授权"
        if needs_login(data["channel"])
        else "该平台无需登录，账号已就绪"
    )
    return ok(message=message)


@router.delete("/{channel}/{account_name}")
async def delete_account(
    channel: str, account_name: str, state: AppState = Depends(get_state)
):
    await state.browser_manager.clear_profile(channel, account_name)
    await state.accounts.delete(channel, account_name)
    return ok(message="账号及其浏览器 profile 已删除")


@router.put("/{channel}/{account_name}/enabled")
async def toggle_account(
    channel: str, account_name: str, enabled: bool = Query(...),
    state: AppState = Depends(get_state),
):
    await state.accounts.update(channel, account_name, {"enabled": int(enabled)})
    return ok(message="已启用" if enabled else "已停用")


@router.post("/{channel}/{account_name}/check")
async def check_account(
    channel: str, account_name: str, state: AppState = Depends(get_state)
):
    """静默检测登录态：后台无头打开该账号的 profile 看一眼，用户无感知。"""
    if not needs_login(channel):
        return ok({"ok": True, "message": "该平台无需登录"})
    result = await state.browser_manager.check_account(channel, account_name)
    return ok(result, result["message"])


@router.post("/{channel}/{account_name}/cookies")
async def import_cookies(
    channel: str, account_name: str, payload: CookieImportIn,
    state: AppState = Depends(get_state),
):
    """手动导入 Cookie。

    扫码登录不总是走得通（会过期、会被风控拦、服务器上没人扫），
    所以留一条最直接的路：用户在自己的浏览器里登录好，
    用 Cookie-Editor 导出，贴进来。

    导入的 Cookie 同时会写进该账号的浏览器 profile —— 抖音、快手
    采集时要开真实页面算签名，光存数据库那边还是未登录。
    """
    if not needs_login(channel):
        raise bad_request(f"{CHANNEL_LABELS.get(channel, channel)} 无需登录，不用导入 Cookie")
    if not await state.accounts.get(channel, account_name):
        raise not_found(f"账号不存在：{channel}/{account_name}，请先添加账号")

    try:
        result = await state.browser_manager.import_cookies(
            channel, account_name, payload.raw
        )
    except CookieParseError as exc:
        raise bad_request(f"Cookie 解析失败：{exc}")
    except ValueError as exc:
        raise bad_request(str(exc))

    message = f"已导入 {result['count']} 个 Cookie，账号已置为可用"
    if result.get("profile_warning"):
        message = result["profile_warning"]
    return ok(result, message)


@router.get("/{channel}/{account_name}/cookies")
async def inspect_cookies(
    channel: str, account_name: str, state: AppState = Depends(get_state)
):
    """看一眼这个账号当前存了哪些 Cookie（只回 key，不回值）。

    排查"到底是没登录还是采集器坏了"时，这一屏最省事。
    """
    account = await state.accounts.get(channel, account_name)
    if not account:
        raise not_found(f"账号不存在：{channel}/{account_name}")

    from ..browser.specs import missing_login_cookies
    from ..repositories.account_repo import AccountRepository

    cookies = AccountRepository.cookies_of(account)
    header = AccountRepository.cookie_header(account)
    missing = missing_login_cookies(channel, header)
    spec = get_spec(channel) if needs_login(channel) else None

    return ok({
        "count": len(cookies),
        "names": sorted(c.get("name", "") for c in cookies if c.get("name")),
        "cookie_updated_at": account.get("cookie_updated_at"),
        "status": account.get("status"),
        "logged_in": not missing,
        "missing": missing,
        "expected_any_of": list(spec.must_have) if spec else [],
        "home_url": spec.home_url if spec else "",
    })


@router.post("/{channel}/{account_name}/harvest")
async def harvest_cookies(
    channel: str, account_name: str, state: AppState = Depends(get_state)
):
    """立即从该账号的浏览器 profile 里抓一次 Cookie 存库。

    和「检测」是同一条链路，但语义是"我要拿最新的 Cookie"，
    所以单独给一个入口，返回里带上抓到多少个、有没有登录凭据。
    """
    if not needs_login(channel):
        return ok({"ok": True, "count": 0, "message": "该平台无需登录"})

    result = await state.browser_manager.check_account(channel, account_name)
    account = await state.accounts.get(channel, account_name)

    from ..browser.specs import missing_login_cookies
    from ..repositories.account_repo import AccountRepository

    header = AccountRepository.cookie_header(account or {})
    missing = missing_login_cookies(channel, header)
    payload = {
        **result,
        "logged_in": bool(result.get("ok")) and not missing,
        "missing": missing,
    }
    if result.get("ok") and missing:
        payload["message"] = (
            f"抓到 {result.get('cookie_count', 0)} 个 Cookie，但里面没有登录凭据"
            f"（需要其中之一：{'、'.join(missing)}）。"
            f"请点「打开浏览器」重新登录，或直接导入 Cookie。"
        )
    return ok(payload, payload["message"])


@router.post("/{channel}/{account_name}/probe")
async def probe(
    channel: str, account_name: str,
    keyword: str = Query(..., description="用来试搜的关键字"),
    target_id: str = Query("", description="携程 POI_ID / 同程 sid"),
    state: AppState = Depends(get_state),
):
    """「试搜一下」：用真实链路跑一个关键字，把每一步的结果摊开。

    平台的失败大多是静默的（200 + 空 data），任务日志里只剩"采到 0 条"。
    这个接口跑的是生产代码本身，但把中间状态都记下来：
    Cookie 有几个、走的哪个代理 IP、HTTP 状态码、响应体前几百字符。
    只取一条就停，不写库。
    """
    if channel not in ALL_CHANNELS:
        raise bad_request(f"未知平台：{channel}")

    report = await probe_keyword(
        config=state.config,
        proxy_manager=state.proxy_manager,
        browser_manager=state.browser_manager,
        channel=channel,
        keyword=keyword,
        account_name="" if account_name == "-" else account_name,
        target_id=target_id,
    )
    message = (
        f"通了，拿到 {report['found']} 条（用时 {report['elapsed_seconds']}s）"
        if report["ok"] else (report["error"] or "没拿到数据")
    )
    return ok(report, message)


@router.post("/check-all")
async def check_all(channel: str = "", state: AppState = Depends(get_state)):
    return ok(await state.browser_manager.check_all(channel))


@router.post("/probe-all")
async def probe_all(
    keyword: str = Query(..., description="试搜关键字"),
    ctrip_target: str = Query("", description="携程 POI_ID"),
    tongcheng_target: str = Query("", description="同程 sid"),
    state: AppState = Depends(get_state),
):
    """一键体检：六个平台挨个试搜一遍，每个平台出一份诊断报告。

    串行执行，抖音/快手要开浏览器页面，整体可能要一两分钟。
    报告结构和单平台「试搜」一致，前端逐个平台展示。
    """
    reports = await probe_all_channels(
        config=state.config,
        proxy_manager=state.proxy_manager,
        browser_manager=state.browser_manager,
        keyword=keyword,
        targets={"ctrip": ctrip_target, "tongcheng": tongcheng_target},
    )
    passed = sum(1 for r in reports if r.get("ok"))
    return ok(reports, f"体检完成：{passed}/{len(reports)} 个平台能采到数据")


@router.post("/{channel}/{account_name}/logout")
async def logout(channel: str, account_name: str, state: AppState = Depends(get_state)):
    await state.browser_manager.clear_profile(channel, account_name)
    return ok(message="已退出登录并清空 profile，下次需要重新扫码")


# ---------------- 交互式登录 ----------------

@router.post("/{channel}/{account_name}/login-session")
async def create_login_session(
    channel: str, account_name: str,
    mode: str = Query("login", description="login=登录；browse=已登录的号直接打开浏览器，不跳登录页"),
    state: AppState = Depends(get_state),
):
    """创建一个实时流登录会话，返回 session_id。

    前端拿到 session_id 后连 WebSocket /ws/browser/{session_id}，
    就能看到服务端浏览器的实时画面并直接在上面操作（扫码、输账号密码）。
    """
    if not needs_login(channel):
        raise bad_request(f"{CHANNEL_LABELS.get(channel, channel)} 无需登录")
    account = await state.accounts.get(channel, account_name)
    if not account:
        raise not_found(f"账号不存在：{channel}/{account_name}")

    session = LiveBrowserSession(state.browser_manager, channel, account_name, mode=mode)
    register(session)
    return ok(
        {"session_id": session.session_id, "ws_path": f"/ws/browser/{session.session_id}"},
        "登录会话已创建，请连接 WebSocket 查看浏览器画面",
    )


@router.post("/login-session/{session_id}/save")
async def save_login_session(session_id: str):
    """用户说「我已经登录好了」——直接把浏览器里的 Cookie 存下来。

    ⚠️ 为什么要有这个入口：自动判定再准也有失手的时候，
    而"到底登没登上"这件事人看一眼画面就知道，比任何探测都可靠。
    没有它，一旦判定失手，用户明明扫码成功了也只能干瞪眼，下次还得重扫。
    """
    session = get_session(session_id)
    if not session:
        raise not_found("登录会话不存在或已关闭")
    result = await session.save_now()
    if not result.get("saved"):
        raise bad_request(result.get("message") or "还没有检测到登录凭据")
    return ok(result, result.get("message", "登录态已保存"))


@router.delete("/login-session/{session_id}")
async def close_login_session(session_id: str):
    session = get_session(session_id)
    if not session:
        raise not_found("登录会话不存在或已关闭")
    result = await session.stop()
    return ok(result, "登录成功，登录态已保存" if result["logged_in"] else "会话已关闭")
