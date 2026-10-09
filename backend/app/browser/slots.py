"""浏览器占用登记 + 排队。

要解决的是三件真实发生过的事：

1. **同一个 profile 目录被两个 Chromium 同时打开。**
   user-data-dir 是独占的。第二个实例要么拿不到登录态（退化成一个空 profile，
   表现是"明明登录了，采集却说没登录"），要么两边一起卡住不动——
   日志里安静得像什么都没发生。

2. **拟人模式下同一个账号同时跑两个景区。**
   拟人采集全程都在同一个页面上操作：输关键字、点筛选、点开作品、翻评论。
   两个任务共用一个账号就等于两只手抢同一个鼠标，采到的数据会串景区。
   所以一个账号在拟人模式下同时只能服务一个景区。

3. **平台的浏览器全被占用时任务被"跳过"。**
   改造前的做法是 raise_if_profile_busy 直接抛 LoginRequired，被上层吞掉
   变成"跳过该平台"——用户建的任务安安静静什么都没采。
   正确的行为是**排队**：等前面那个跑完，拿到浏览器再继续。

实现上刻意保持简单：
  · 占用表就是一个 dict，key 是 "平台:账号"；
  · 等待用 asyncio.Event 列表，释放时把所有等待者叫醒，各自重新抢一次。
    抢的人数量级是个位数，不值得为公平性引入更复杂的结构；
  · release() 是**同步**方法——现有的 close()/finally 都在同步路径上调它，
    改成 async 会让每个调用点都要 await，很容易漏掉一处就永久占着。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class SlotInfo:
    """谁占着这个浏览器、占了多久。给日志和接口看。"""
    channel: str
    account_name: str
    reason: str
    #: 持有者标识。同一个持有者（同一条任务）可以在自己占着的账号上
    #: 反复开关浏览器——刷 Cookie、开拟人页面——而不会被自己的占用挡住。
    #: 没有它的话，"任务先占位、采集器再开页面"这个顺序会自己把自己拦下来。
    token: str = ""
    since: float = field(default_factory=time.time)

    @property
    def held_seconds(self) -> float:
        return max(0.0, time.time() - self.since)

    def describe(self) -> str:
        minutes = self.held_seconds / 60
        return f"{self.reason}（已占用 {minutes:.1f} 分钟）"


class SlotBusy(Exception):
    """想立刻拿但被占着（不排队的调用方用这个）。"""


class SlotWaitTimeout(Exception):
    """排队等超时了。"""


class BrowserSlot:
    """一次占用。用完必须 release —— 放在 finally 里。"""

    def __init__(self, slots: "BrowserSlots", channel: str, account_name: str):
        self._slots = slots
        self.channel = channel
        self.account_name = account_name
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._slots.release(self.channel, self.account_name)

    def __enter__(self) -> "BrowserSlot":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class BrowserSlots:
    """按「平台:账号」登记浏览器占用，并支持排队等待。"""

    def __init__(self) -> None:
        self._held: Dict[str, SlotInfo] = {}
        #: 每个平台上正在等待的人。释放时只叫醒同平台的，别惊动无关的等待者。
        self._waiters: Dict[str, List[asyncio.Event]] = {}

    # ---------------- 基础读写 ----------------
    @staticmethod
    def _key(channel: str, account_name: str) -> str:
        return f"{channel}:{account_name or 'default'}"

    def mark(self, channel: str, account_name: str, reason: str, token: str = "") -> None:
        """直接占上，不排队。登录窗口这类"用户已经点开了"的场景用它。"""
        self._held[self._key(channel, account_name)] = SlotInfo(
            channel=channel, account_name=account_name or "default",
            reason=reason, token=token,
        )

    def release(self, channel: str, account_name: str) -> None:
        self._held.pop(self._key(channel, account_name), None)
        self._wake(channel)

    def info(self, channel: str, account_name: str) -> Optional[SlotInfo]:
        return self._held.get(self._key(channel, account_name))

    def reason(self, channel: str, account_name: str) -> str:
        found = self.info(channel, account_name)
        return found.describe() if found else ""

    def busy(self, channel: str, account_name: str) -> bool:
        return self._key(channel, account_name) in self._held

    def held_by(self, channel: str, account_name: str, token: str) -> bool:
        """这个账号是不是正被 token 指定的持有者占着（也就是"我自己"）。"""
        if not token:
            return False
        found = self.info(channel, account_name)
        return bool(found and found.token and found.token == token)

    def snapshot(self) -> List[Dict[str, object]]:
        """当前谁占着什么。给 /api 和排障日志用。"""
        return [
            {
                "channel": item.channel,
                "account_name": item.account_name,
                "reason": item.reason,
                "held_seconds": round(item.held_seconds, 1),
            }
            for item in self._held.values()
        ]

    # ---------------- 排队 ----------------
    def _wake(self, channel: str) -> None:
        for event in self._waiters.get(channel, []):
            event.set()

    def try_acquire(
        self, channel: str, accounts: Sequence[str], *, reason: str, token: str = ""
    ) -> Optional[BrowserSlot]:
        """按顺序找第一个空闲账号占上。都被占着就返回 None。"""
        for account in accounts:
            if not self.busy(channel, account) or self.held_by(channel, account, token):
                self.mark(channel, account, reason, token)
                return BrowserSlot(self, channel, account)
        return None

    async def acquire(
        self,
        channel: str,
        accounts: Sequence[str],
        *,
        reason: str,
        token: str = "",
        timeout: Optional[float] = None,
        on_wait=None,
        should_cancel=None,
    ) -> BrowserSlot:
        """占一个浏览器；都忙就**排队等**，而不是跳过。

        accounts   候选账号，按优先级排。只给一个就是"只能用这个账号"。
        timeout    最多等多久（秒）。None = 一直等。超时抛 SlotWaitTimeout。
        on_wait    每轮等待回调 on_wait(waited_seconds, blocking_text)，用来打日志，
                   让用户在页面上看得到"在排队，前面是谁"。
        should_cancel  返回 True 就放弃等待（任务被取消时用），抛 asyncio.CancelledError。
        """
        if not accounts:
            raise SlotBusy(f"平台 {channel} 没有可用账号")

        started = time.monotonic()
        event: Optional[asyncio.Event] = None
        try:
            while True:
                # ⚠️ 先看取消再抢：反过来的话，任务被取消的同时恰好有人释放，
                # 这个已经不打算跑的任务会把浏览器抢走再扔掉，
                # 后面真正在等的任务白等一轮。
                if should_cancel is not None and should_cancel():
                    raise asyncio.CancelledError()

                slot = self.try_acquire(channel, accounts, reason=reason, token=token)
                if slot is not None:
                    return slot

                waited = time.monotonic() - started
                if timeout is not None and waited >= timeout:
                    raise SlotWaitTimeout(
                        f"等了 {waited / 60:.1f} 分钟，平台 {channel} 的浏览器仍然全部被占用："
                        f"{self._blocking_text(channel, accounts)}"
                    )

                if event is None:
                    event = asyncio.Event()
                    self._waiters.setdefault(channel, []).append(event)
                event.clear()

                if on_wait is not None:
                    on_wait(waited, self._blocking_text(channel, accounts))

                # 定期醒来重试：一是要打"还在排队"的日志，二是防止极端情况下
                # 释放事件恰好错过（release 在我们注册 event 之前发生）。
                slice_seconds = 15.0
                if timeout is not None:
                    slice_seconds = min(slice_seconds, max(0.5, timeout - waited))
                try:
                    await asyncio.wait_for(event.wait(), timeout=slice_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            if event is not None:
                queue = self._waiters.get(channel)
                if queue and event in queue:
                    queue.remove(event)
                if queue is not None and not queue:
                    self._waiters.pop(channel, None)

    def _blocking_text(self, channel: str, accounts: Sequence[str]) -> str:
        parts = []
        for account in accounts:
            found = self.info(channel, account)
            if found:
                parts.append(f"{account}={found.describe()}")
        return "；".join(parts) or "（占用信息已失效，稍后重试）"
