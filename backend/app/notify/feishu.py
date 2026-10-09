"""飞书机器人通知。

什么时候发
----------
采集卡在**需要人**的地方时：账号失效、二次验证、要重新登录、以及采集报错。
这类问题机器解决不了，越早叫人越好——拟人采集一卡就是几十分钟白跑。

⚠️ 通知失败**绝不能影响采集**。发不出去最多是没人知道，
   而因为发通知把采集带崩，丢的是数据。所以这里吞掉一切异常。

⚠️ 同一个问题不要反复轰炸。一个平台在冷却期内只发一次
   （默认 10 分钟），否则一轮采集能刷出上百条一模一样的消息，
   真出事的时候反而被淹掉。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from ..core.constants import CHANNEL_LABELS
from ..core.logging import get_logger

logger = get_logger(__name__)

#: 事件类型 → 通知里「验证项」那一行怎么写
EVENT_LABELS = {
    "login_required": "需要登录",
    "captcha": "验证码",
    "account_invalid": "账号失效",
    "two_factor": "二次验证",
    "error": "采集错误",
}


@dataclass
class NotifyEvent:
    """一次要通知的事件。"""
    channel: str                    # 平台
    kind: str                       # EVENT_LABELS 里的键
    detail: str = ""                # 错误信息 / 补充说明
    account: str = ""               # 出问题的账号
    task_id: str = ""
    scenic_name: str = ""

    @property
    def item(self) -> str:
        """「验证项」那一行。错误要把错误信息带上，否则人得去翻日志。"""
        label = EVENT_LABELS.get(self.kind, self.kind)
        if self.kind == "error" and self.detail:
            return f"{label}（{self.detail[:200]}）"
        return f"{label}{f'（{self.detail[:120]}）' if self.detail else ''}"

    @property
    def dedup_key(self) -> str:
        """冷却用的键：同一个平台的同一类问题算同一件事。"""
        return f"{self.channel}:{self.kind}"


class FeishuNotifier:
    """往飞书群机器人发消息。"""

    def __init__(self, config):
        self.config = config
        self._last_sent: Dict[str, float] = {}

    # ------------------------------------------------------------------ 配置
    @property
    def _cfg(self) -> Dict[str, Any]:
        return (self.config.get("notify") or {})

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("enabled", False)) and bool(self.webhook)

    @property
    def webhook(self) -> str:
        return str(self._cfg.get("feishu_webhook") or "").strip()

    @property
    def cooldown_seconds(self) -> float:
        return float(self._cfg.get("cooldown_seconds", 600))

    # ------------------------------------------------------------------ 正文
    def render(self, event: NotifyEvent) -> str:
        """按用户给定的格式拼消息。**格式是用户定的，别自己加戏。**

            舆情采集需人工验证：
            平台：抖音
            验证项：验证码或登录/采集错误（错误信息）
            请使用向日葵远程登录操作：
            远程账号：11111
            远程密码：1111
        """
        cfg = self._cfg
        channel = CHANNEL_LABELS.get(event.channel, event.channel)
        lines = [
            "舆情采集需人工验证：",
            f"平台：{channel}",
            f"验证项：{event.item}",
        ]
        # 账号/景区/任务是附加信息，有就带上——出事时能少问一轮
        extra = []
        if event.account:
            extra.append(f"账号：{event.account}")
        if event.scenic_name:
            extra.append(f"景区：{event.scenic_name}")
        if extra:
            lines.append("　".join(extra))
        lines.append("请使用向日葵远程登录操作：")
        lines.append(f"远程账号：{cfg.get('remote_account') or '（未配置）'}")
        lines.append(f"远程密码：{cfg.get('remote_password') or '（未配置）'}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 发送
    def _cooling(self, key: str) -> bool:
        last = self._last_sent.get(key, 0.0)
        return (time.time() - last) < self.cooldown_seconds

    async def send(self, event: NotifyEvent, *, force: bool = False) -> bool:
        """发一条。返回是否真的发出去了。

        ⚠️ 这个方法**不抛异常**。调用方在采集的关键路径上，
        不该因为通知失败而中断。
        """
        if not self.enabled:
            return False
        if not force and self._cooling(event.dedup_key):
            logger.debug("[通知] %s 在冷却期内，跳过", event.dedup_key)
            return False
        text = self.render(event)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.webhook,
                    json={"msg_type": "text", "content": {"text": text}},
                )
            body = {}
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                pass
            # 飞书成功时返回 {"code":0,...} 或 {"StatusCode":0,...}
            code = body.get("code", body.get("StatusCode", 0))
            if resp.status_code != 200 or code not in (0, None):
                logger.warning("[通知] 飞书返回异常：HTTP %s %s",
                               resp.status_code, str(body)[:200])
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("[通知] 发送失败（不影响采集）：%s", exc)
            return False
        self._last_sent[event.dedup_key] = time.time()
        logger.info("[通知] 已发送：%s / %s", event.channel, event.item)
        return True

    async def test(self) -> Dict[str, Any]:
        """设置页面上的「发送测试」。绕过冷却，并把渲染结果一起返回，
        让用户在页面上就能看到消息长什么样。"""
        event = NotifyEvent(channel="douyin", kind="captcha",
                            detail="这是一条测试通知", account="测试账号")
        if not self.webhook:
            return {"sent": False, "preview": self.render(event),
                    "error": "没配飞书机器人地址"}
        # 测试要绕过 enabled：用户可能想先测通再打开开关
        saved, self._last_sent = self._last_sent, {}
        try:
            text = self.render(event)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.webhook,
                    json={"msg_type": "text", "content": {"text": text}})
            body = {}
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                pass
            code = body.get("code", body.get("StatusCode", 0))
            good = resp.status_code == 200 and code in (0, None)
            return {"sent": good, "preview": text,
                    "error": "" if good else f"HTTP {resp.status_code} {str(body)[:200]}"}
        except Exception as exc:  # noqa: BLE001
            return {"sent": False, "preview": self.render(event), "error": str(exc)}
        finally:
            self._last_sent = saved


_notifier: Optional[FeishuNotifier] = None


def get_notifier(config=None) -> FeishuNotifier:
    """全局单例——冷却状态要跨调用保持，每次新建就等于没有冷却。"""
    global _notifier
    if _notifier is None:
        if config is None:
            return _DISABLED
        _notifier = FeishuNotifier(config)
    return _notifier


class _DisabledNotifier:
    """还没初始化时的占位。采集侧拿到它就是"不通知"，而不是崩。"""
    enabled = False
    webhook = ""

    async def send(self, event, *, force: bool = False) -> bool:
        return False

    async def test(self):
        return {"sent": False, "preview": "", "error": "通知模块尚未初始化"}

    def render(self, event) -> str:
        return ""


_DISABLED = _DisabledNotifier()
