"""一条任务对「某平台一个账号的浏览器」的租约。

为什么要有这层，而不是让采集器各自去 mark/release：

采集方式改成三挡之后，"什么时候需要浏览器"不再是按平台一刀切的：

    模式      抖音                    快手                  小红书
    api      只在取/刷 Cookie 时开    全程要（签名调页面JS）   完全不开
    hybrid   同上；换拟人后全程要      全程要                同上
    human    全程要                   全程要                全程要

也就是说：**有的场景要提前占住，有的场景要等到真的开浏览器那一刻才占**。
提前占住的代价是别的任务干等着（API 模式明明只用一秒钟浏览器）；
不提前占的风险是换到拟人那一刻发现账号被别人拿走了，任务半路失败。

租约把这两种都统一成一个对象：
  · 需要全程持有的（拟人 / 快手），调度层在开始前 acquire()；
  · 只是偶尔开一下的（抖音 API 模式刷 Cookie），采集器临时 acquire()，
    因为 acquire 是幂等的，已经持有就直接返回，不会自己把自己挡住；
  · 无论哪种，都由调度层在 finally 里 release() —— 出口只有一个。

排队而不是跳过：账号全被占用时 acquire 会**等**，并定期把"在排队、前面是谁"
打进任务日志，用户在页面上看得见，而不是任务悄悄什么都没采就结束了。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional

from ..core.logging import get_logger
from .slots import BrowserSlot, SlotWaitTimeout

logger = get_logger(__name__)

#: 排队最多等多久。超过就让这一轮失败，而不是无限期挂着——
#: 真正卡死的场景通常是有人开着登录窗口忘了关，那需要人来处理。
DEFAULT_WAIT_TIMEOUT_SECONDS = 30 * 60


class BrowserLease:
    """某条任务在某个平台上占用的浏览器账号。"""

    def __init__(
        self,
        manager,
        channel: str,
        *,
        token: str,
        reason: str,
        preferred_account: str = "",
        group: str = "",
        log=None,
        cancel_event: Optional[asyncio.Event] = None,
        wait_timeout: Optional[float] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self.manager = manager
        self.channel = channel
        self.token = token
        self.reason = reason
        self.preferred_account = preferred_account
        self.group = group
        self.log = log
        self.cancel_event = cancel_event
        self.wait_timeout = wait_timeout
        self._slot: Optional[BrowserSlot] = None

    # ---------------- 状态 ----------------
    @property
    def held(self) -> bool:
        return self._slot is not None

    @property
    def account_name(self) -> str:
        return self._slot.account_name if self._slot else ""

    # ---------------- 占用 / 释放 ----------------
    async def acquire(self, note: str = "") -> str:
        """拿到一个账号的浏览器；已经拿着就直接返回（幂等）。

        返回账号名。全被占用时会排队等，等不到（超时）抛 SlotWaitTimeout。
        """
        if self._slot is not None:
            return self._slot.account_name

        accounts = await self._candidates()
        if not accounts:
            from ..collectors.base import LoginRequired
            raise LoginRequired(
                f"平台 {self.channel} 没有可用账号，请先在账号管理里添加并登录一个账号"
            )

        reason = f"{self.reason}{('：' + note) if note else ''}"
        # 先试一把不排队的，能立刻拿到就别打"排队中"的日志吓人
        slot = self.manager.slots.try_acquire(
            self.channel, accounts, reason=reason, token=self.token
        )
        if slot is None:
            self._log(
                f"{self.channel} 的浏览器都在忙，本任务进入排队等待"
                f"（候选账号 {len(accounts)} 个）", "warn",
            )
            slot = await self.manager.acquire_browser(
                self.channel, accounts,
                reason=reason,
                timeout=self.wait_timeout,
                on_wait=self._on_wait,
                should_cancel=self._should_cancel,
            )
            # acquire 走的是 slots，没带 token；补一次登记，
            # 否则后面采集器自己开页面时会被自己的占用挡住。
            self.manager.slots.mark(self.channel, slot.account_name, reason, self.token)
        self._slot = slot
        self._log(f"已占用 {self.channel} 账号 [{slot.account_name}] 的浏览器（{reason}）")
        return slot.account_name

    @asynccontextmanager
    async def temporary(self, note: str = ""):
        """临时用一下浏览器，用完立刻还。

        API 模式的抖音就是这个形态：开一次页面把 msToken/Cookie 取出来就关掉，
        剩下几十分钟的接口采集完全不占浏览器，别的任务可以马上用这个账号。

        如果本来就长租着（拟人模式、快手），这里什么都不做也不还——
        还掉就把正在用的页面从别人手里抽走了。
        """
        already_held = self.held
        await self.acquire(note)
        try:
            yield self.account_name
        finally:
            if not already_held:
                self.release()

    def release(self) -> None:
        if self._slot is None:
            return
        account = self._slot.account_name
        self._slot.release()
        self._slot = None
        self._log(f"已释放 {self.channel} 账号 [{account}] 的浏览器")

    # ---------------- 内部 ----------------
    async def _candidates(self) -> List[str]:
        return await self.manager.accounts.active_names(
            self.channel, preferred=self.preferred_account, group=self.group
        )

    def _should_cancel(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _on_wait(self, waited: float, blocking: str) -> None:
        # 15 秒醒一次，但日志按分钟打，别把任务日志刷满
        if waited > 0 and int(waited) % 60 < 16:
            self._log(f"排队中：已等 {waited / 60:.0f} 分钟。占用方：{blocking}", "warn")

    def _log(self, message: str, level: str = "info") -> None:
        if self.log is not None:
            getattr(self.log, level, self.log.info)(message)
        else:
            getattr(logger, "warning" if level == "warn" else level, logger.info)(message)


__all__ = ["BrowserLease", "SlotWaitTimeout", "DEFAULT_WAIT_TIMEOUT_SECONDS"]
