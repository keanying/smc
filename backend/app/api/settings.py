"""系统设置 API：代理、采集参数、浏览器、调度等在页面上可改。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core.constants import ALL_CHANNELS, CHANNEL_LABELS
from ..proxy.pool import KdlProxyPool
from ..repositories.setting_repo import OVERRIDABLE_SECTIONS
from .common import bad_request, ok
from .deps import AppState, get_state
from .schemas import ProxyTestIn, SettingIn

router = APIRouter(prefix="/api/settings", tags=["系统设置"])


@router.get("")
async def get_settings(state: AppState = Depends(get_state)):
    """返回当前生效的配置（密钥已打码）与可修改的段。"""
    return ok({
        "config": state.config.redacted(),
        "editable_sections": list(OVERRIDABLE_SECTIONS),
        "config_file": str(state.config.source_path or ""),
        "channels": [
            {"value": c, "label": CHANNEL_LABELS[c]} for c in ALL_CHANNELS
        ],
    })


@router.put("")
async def save_settings(payload: SettingIn, state: AppState = Depends(get_state)):
    try:
        await state.settings.save_section(payload.section, payload.values, state.config)
    except ValueError as exc:
        raise bad_request(str(exc))

    # 代理配置变了要让已建的池失效，下次采集按新配置重建
    if payload.section == "proxy":
        state.proxy_manager.reset()
    # 标注引擎启动时就把模型名/Key 定死在客户端里了，不重启改了等于没改
    if payload.section == "labeling":
        from ..labeling import get_manager
        await get_manager(state.config).reset()
    return ok(state.config.redacted().get(payload.section), "设置已保存并立即生效")


@router.post("/notify/test")
async def test_notify(state: AppState = Depends(get_state)):
    """发一条测试通知，并把渲染出来的正文一起返回。

    返回 preview 是有意的：用户在页面上就能看到消息长什么样，
    不用切到飞书群里找。发不出去时也返回 preview——
    至少能确认格式和远程账号密码填对了。
    """
    from ..notify import get_notifier

    return ok(await get_notifier(state.config).test())


@router.get("/proxy/status")
async def proxy_status(state: AppState = Depends(get_state)):
    """当前各平台持有的出口 IP 与剩余有效期。"""
    return ok({
        "enabled": bool(state.config.get("proxy.enabled")),
        "pool_scope": state.config.get("proxy.pool_scope", "channel"),
        "pools": await state.proxy_manager.status(),
        "per_channel": {
            channel: state.proxy_manager.enabled_for(channel) for channel in ALL_CHANNELS
        },
    })


@router.post("/proxy/test")
async def test_proxy(payload: ProxyTestIn, state: AppState = Depends(get_state)):
    """用页面上填的密钥试提一个 IP，验证配置是否可用。

    不落库、不影响正在跑的任务，纯粹是"点一下看看通不通"。
    """
    proxy_config = state.config.get("proxy", {})
    pool = KdlProxyPool(
        state.redis,
        scope="probe",
        secret_id=payload.secret_id or proxy_config.get("secret_id", ""),
        secret_key=payload.secret_key or proxy_config.get("secret_key", ""),
        username=payload.username if payload.username is not None else proxy_config.get("username", ""),
        password=payload.password if payload.password is not None else proxy_config.get("password", ""),
        auth_mode=payload.auth_mode or proxy_config.get("auth_mode", "token"),
        validate_on_fetch=True,
        fetch_retries=1,
    )
    if not pool.secret_id or not pool.secret_key:
        raise bad_request("请先填写快代理的 secret_id 和 secret_key")

    try:
        proxy, ttl = await pool._fetch_valid_proxy()
    except Exception as exc:  # noqa: BLE001
        return ok({"ok": False, "message": str(exc)}, "代理测试失败")
    finally:
        state.redis.delete(pool.current_key, pool.lock_key)

    return ok(
        {"ok": True, "proxy": proxy, "ttl_seconds": ttl},
        f"提取成功：{proxy}（有效期约 {ttl} 秒），连通性校验通过",
    )


@router.get("/redis/status")
async def redis_status(state: AppState = Depends(get_state)):
    return ok({
        "enabled": bool(state.config.get("redis.enabled")),
        "connected": state.redis.ping(),
        "is_real_redis": state.redis.is_real_redis,
        "key_prefix": state.redis.prefix,
    })


@router.get("/scheduler/status")
async def scheduler_status(state: AppState = Depends(get_state)):
    return ok(state.scheduler.status())
