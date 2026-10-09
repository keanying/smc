"""浏览器模拟采集器：通过 CDP 拦截 network 响应获取数据。

参考用户提供的 browser_engine.py 实现，核心机制：
  1. is_capturing 门控：只有采集状态才处理响应
  2. 平台专用流程：搜索 → 点击作品 → 打开评论区 → 滚动采集
  3. 评论到底检测：连续空评论停止
  4. 水印去重：避免重复采集
  5. request body 解析：快手评论的 photoId/rootCommentId

当 API 采集被风控时，降级为浏览器模拟人点击页面，
从 network F12 拦截 XHR/Fetch 响应中提取作品和评论。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from ..browser.specs import missing_login_cookies
from ..core.logging import get_logger
from ..core.subprocess_loop import SUBPROCESS_LOOP
from .base import (
    AuthorItem,
    BaseCollector,
    CollectContext,
    CollectTarget,
    CommentItem,
    LoginRequired,
    WorkItem,
)

logger = get_logger(__name__)

#: 被踢回登录页的 URL 特征。和 specs.VerifyProbe.reject_url_contains 同源——
#: 四个平台的登录页都落在这几个词上。
LOGIN_URL_HINTS = ("login", "passport", "signin", "/user/login")


class BrowserCaptureCollector(BaseCollector):
    """浏览器模拟采集器基类。

    子类需要实现：
      - _target_url_patterns: 要拦截的 API URL 模式列表（顺序敏感）
      - _search_url_template: 搜索页 URL 模板
      - _work_url_template: 作品页 URL 模板
      - _parse_work_from_response: 从响应体解析作品
      - _parse_comment_from_response: 从响应体解析评论
      - _open_comments: 打开评论区
      - _scroll_comments: 滚动评论区
      - _switch_next_work: 切换下一个作品
    """

    channel = "browser_capture"
    supports_works = True
    needs_login = True
    integrated = False
    supported_targets = ("keyword", "creator")

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._session = None
        self._page = None
        #: 这个会话是不是自己开的。借来的（adopt_session）不由这边关
        self._owns_session = True
        #: 上一次操作是不是撞上了"浏览器已经没了"
        self._browser_is_gone = False
        #: 采集节奏。所有"停一下"都走它，别再写裸的 sleep——
        #: 那种停顿用户既看不见也改不掉，正是"有时候等很久"说不清的原因。
        from ..core.pacing import make_pacer
        self.pacer = make_pacer(config, self.channel)
        self._live_view = None
        self._ctx = None
        self._is_capturing = False
        self._captured_works: List[Dict] = []
        self._captured_comments: List[Dict] = []
        self._seen_work_ids: Set[str] = set()
        self._seen_comment_ids: Set[str] = set()
        self._comment_api_empty = False
        self._comment_api_empty_count = 0
        self._current_keyword = ""
        self._current_work_id = ""
        self._comments_exhausted = False

    # ---------------- 事件循环 ----------------
    async def _run(self, coro):
        """把一次 Playwright 调用提交到浏览器循环执行。

        ⚠️ 这不是可选的优化，是必须的。app/core/subprocess_loop.py 的
        「注意事项」第 1 条：Playwright 的连接对象绑定在创建它的循环上，
        从建立到关闭的**每一次**调用都必须在同一个循环里。

        PageSession 在 SUBPROCESS_LOOP 上创建浏览器，我们这里拿到的
        self._page 就属于那个循环。之前的代码直接 `await self._page.goto(...)`，
        协程在服务器循环上等一个永远不会在本循环上完成的结果——
        不报错、不超时，就是**永久挂起**。症状：日志停在某一行不动。

        用法：把 `await self._page.X(...)` 写成 `await self._run(self._page.X(...))`。
        协程对象在创建时并不绑定循环，await 时才绑定，所以这样是安全的。
        """
        return await SUBPROCESS_LOOP.run(coro)

    async def _wait_visible(self, locator, timeout_s: float = 8.0) -> bool:
        """等某个元素**真的**可见，最多等 timeout_s 秒。

        ⚠️ 不要用 `await locator.is_visible(timeout=N)` 代替。
        Playwright 的 `is_visible()` 是**即时查询**，它的 timeout 参数已废弃且
        被完全忽略（实测：元素 1.5 秒后才出现时，`is_visible(timeout=3000)`
        在 91 毫秒后就返回了 False）。写成那样等于"看一眼，没有就算了"。

        这正是「没找到「多列」按钮」在无头模式下高发的原因：
        无头没有窗口管理和真实绘制，整条流程跑得更快，探测的时刻比
        有头时更早，工具栏还没挂上去就被判了"不存在"。
        （已实测对比过无头/有头的渲染环境：screen、innerWidth、devicePixelRatio、
        hover/pointer 媒体特性、maxTouchPoints、UA、webdriver、WebGL renderer、
        visibilityState、rAF、IntersectionObserver 全部一致——
        页面并没有被渲染成另一个样子，差的只是时机。）
        """
        try:
            await self._run(locator.wait_for(state="visible",
                                             timeout=int(timeout_s * 1000)))
            return True
        except Exception:  # noqa: BLE001  超时/元素不存在都算"没等到"
            return False

    # ---------------- 子类配置 ----------------
    @property
    def _target_url_patterns(self) -> List[Dict[str, str]]:
        """要拦截的 API URL 模式，子类覆盖。顺序敏感：reply 必须在 comment 之前。"""
        raise NotImplementedError

    @property
    def _search_url_template(self) -> str:
        """搜索页 URL 模板，子类覆盖。"""
        raise NotImplementedError

    @property
    def _work_url_template(self) -> str:
        """作品页 URL 模板，子类覆盖。"""
        raise NotImplementedError

    # ---------------- 生命周期 ----------------
    def adopt_session(self, session) -> None:
        """接管一个**已经开着**的 PageSession，不再自己开。

        ⚠️ 混合模式跑到一半被风控挡了才切到拟人，这时候接口模式的浏览器
        还开着（快手的签名要常驻页面）。这边再 launch_persistent_context
        一次就撞同一个 user-data-dir——Chrome 不开第二个实例，它把请求交给
        已经在跑的那个进程然后自己退出，Playwright 报的却是

            Target page, context or browser has been closed

        看不出因果。所以由原持有者调这个方法把会话交过来。
        **所有权不转移**：谁开的谁关，这边 cleanup 不去动它，
        免得两边都关、或者关完对方还在用。
        """
        self._session = session
        self._owns_session = False

    async def prepare(self, ctx: CollectContext) -> None:
        from ..browser.page_session import PageSession

        if self._session is not None and not getattr(self, "_owns_session", True):
            # 别人把开好的会话交过来了，直接用
            ctx.log(f"[{self.channel}] 沿用已经开着的浏览器会话（不重开，"
                    f"避免撞同一个 profile 目录）")
        else:
            manager = ctx.params.get("browser_manager")
            if manager is None:
                raise LoginRequired(
                    "浏览器模拟采集需要浏览器会话，但调度层没有传入 browser_manager。"
                    "请确保任务参数里包含 browser_manager，或关闭浏览器模拟降级。"
                )

            # 拟人模式下这个账号的浏览器由调度层的租约占着（见 browser/lease.py），
            # 把租约标识带下去，PageSession 才不会被"自己占着"这件事拦下来。
            self._owns_session = True
            self._session = PageSession(
                manager, self.channel, ctx.account_name,
                group=ctx.params.get("account_group", ""),
                slot_token=ctx.params.get("browser_slot_token", ""),
            )
            await self._session.start()
        self._session.ensure_logged_in()
        self._page = self._session._page

        # 设置 network 拦截
        await self._setup_network_capture(ctx)

        # 挂上「实时画面」：无头模式下这是唯一能看到页面当前长什么样的途径。
        # 注册本身几乎零成本——只有真的有人在看的时候才会开始推流。
        self._register_live_view(ctx)

        ctx.log(f"[{self.channel}] 浏览器模拟采集就绪，账号 {self._session.account_name}")
        # 把节奏打进日志：用户改了配置能立刻确认生效，
        # 也解释了"为什么这一条等了这么久"
        ctx.log(f"[{self.channel}] {self.pacer.describe()}")

    async def recheck_login(self, ctx: CollectContext, *, reason: str = "") -> None:
        """采集途中复查登录态：掉了就抛 LoginRequired。

        ⚠️ **这是「跳登录页」唯一会被发现的地方。**

        在这之前，三个浏览器采集器里 `LoginRequired` 出现 **0 次**——
        平台把页面跳回登录页之后，采集器只是"找不到卡片"，于是走
        「列表里没有新卡片了，结束」那条路静默收尾。日志上看像是
        "这个关键字没搜到内容"，一条通知都不会发，人完全不知道
        账号已经掉了。用户复现小红书掉登录时没收到通知，就是这个原因。

        抛出去之后，runner 那条现成的链路会接手：发飞书通知 →
        暂停等人处理 → 检测到账号恢复自动继续（见 `_pause_for_human`）。

        两个判据，从便宜到贵：
          1. 当前 URL 跳到了登录页——最直接，一次 evaluate 就够
          2. Cookie 里的登录凭据没了——URL 没跳但会话已失效时靠它
        """
        if self._session is None or self._session.spec is None:
            return

        who = f"{self.channel}/{self._session.account_name}"
        tail = f"（{reason}）" if reason else ""

        # 1) URL 被跳到登录页
        try:
            url = await self._run(self._page.evaluate("location.href")) or ""
        except Exception:  # noqa: BLE001
            url = ""
        low = str(url).lower()
        if low and any(hint in low for hint in LOGIN_URL_HINTS):
            raise LoginRequired(
                f"账号 [{who}] 采集途中被跳回登录页{tail}：{url}。"
                f"请在账号管理里重新登录")

        # 2) URL 没跳，但 Cookie 里的登录凭据已经没了
        try:
            header = await self._session.refresh_cookies()
        except Exception as exc:  # noqa: BLE001
            # 复查本身失败不能把采集带崩——它只是个探针
            logger.debug("[%s] 复查登录态时读 Cookie 失败（忽略）：%s", self.channel, exc)
            return
        missing = missing_login_cookies(self.channel, header)
        if missing:
            raise LoginRequired(
                f"账号 [{who}] 的登录态在采集途中失效了{tail}："
                f"Cookie 里已经没有登录凭据（需要其中之一：{'、'.join(missing)}）。"
                f"请在账号管理里重新登录")

        ctx.log(f"[{self.channel}] 复查登录态：正常{tail}")

    def _register_live_view(self, ctx: CollectContext) -> None:
        from ..browser import live_view as lv

        self._live_view = lv.attach_session(
            self._session, ctx, self.channel,
            engine=getattr(ctx, "collect_engine", "human"))

    def note_step(self, step: str) -> None:
        """把当前步骤写到实时画面旁边。日志说"点了筛选"，画面说"现在长这样"，
        两个凑一起才知道这一帧对应的是哪一步。"""
        view = getattr(self, "_live_view", None)
        if view is not None:
            view.note(step)

    async def cleanup(self) -> None:
        self._is_capturing = False
        from ..browser import live_view as lv
        await lv.detach_session(getattr(self, "_live_view", None))
        self._live_view = None
        if self._session is not None:
            # 借来的会话不能由这边关——所有权还在把它交过来的那个采集器手里
            if getattr(self, "_owns_session", True):
                await self._session.close()
            self._session = None
            self._page = None

    # ---------------- Network 拦截 ----------------
    async def _setup_network_capture(self, ctx: CollectContext) -> None:
        """设置 network 响应拦截。"""
        if self._page is None:
            raise RuntimeError("页面未初始化")

        async def on_response(response):
            if not self._is_capturing:
                return
            url = response.url
            matched_type = None
            for pattern in self._target_url_patterns:
                if pattern["url_contains"] in url:
                    matched_type = pattern["type"]
                    break
            if not matched_type:
                return
            try:
                if response.status != 200:
                    return
                # ⚠️ 不能直接 response.json()：有的接口回的**不是**一个完整 JSON。
                # 抖音的 general/search/stream 就是分块流式返回——正文形如
                #     af7c{...}{"ack":...}{...}
                # 前面是 chunk 长度的十六进制，后面是多个 JSON 对象首尾相接。
                # 用 .json() 解必抛异常，然后被这里的 except 吞掉，
                # 表现就是"接口明明拦到了，却一条数据都没有"。
                text = await response.text()
                try:
                    body = json.loads(text)
                except ValueError:
                    # 不是单个 JSON，交给子类按原始文本处理
                    await self._handle_captured_raw(ctx, matched_type, url, text, response)
                    return
                await self._handle_captured_response(ctx, matched_type, url, body, response)
            except Exception as exc:
                logger.debug("[%s] 拦截响应解析失败 %s: %s", self.channel, url, exc)

        self._page.on("response", on_response)
        logger.info("[%s] Network 拦截已启动，监听 %d 个 API 模式",
                    self.channel, len(self._target_url_patterns))

    async def _handle_captured_raw(
        self, ctx: CollectContext, matched_type: str, url: str, text: str, response
    ) -> None:
        """响应不是单个 JSON 时的兜底（流式/分块）。默认只记一条日志。"""
        logger.debug("[%s] %s 不是单个 JSON（%d 字节），子类没实现 _handle_captured_raw",
                     self.channel, matched_type, len(text or ""))

    async def _handle_captured_response(
        self, ctx: CollectContext, matched_type: str, url: str, body: Dict, response
    ) -> None:
        """处理拦截到的响应，子类覆盖。"""
        raise NotImplementedError

    # ---------------- 模拟人操作 ----------------
    async def _simulate_scroll(self, times: int = 3) -> None:
        """模拟人滚动页面，触发懒加载。"""
        if self._page is None:
            return
        for _ in range(times):
            distance = random.randint(300, 800)
            await self._run(self._page.evaluate(f"window.scrollBy(0, {distance})"))
            # 走 pacer 的 scroll 档：滚动是**在循环里**的，
            # 一条作品最多滚几十屏，这里每次多 0.5 秒就是几十秒的差别
            await self.pacer.wait("scroll_seconds", self._ctx)

    async def _simulate_click_load_more(self) -> bool:
        """模拟点击"加载更多"按钮。"""
        if self._page is None:
            return False
        selectors = [
            "text=加载更多",
            "text=查看更多",
            "text=更多",
            "[class*='load-more']",
            "[class*='more']",
            "button:has-text('更多')",
        ]
        for selector in selectors:
            try:
                element = await self._run(self._page.query_selector(selector))
                if element:
                    await self._run(element.click())
                    await asyncio.sleep(random.uniform(1, 2))
                    return True
            except Exception:
                continue
        return False

    async def _open_comments(self) -> bool:
        """打开评论区，子类覆盖。"""
        raise NotImplementedError

    async def _scroll_comments(self, max_scrolls: int = 80) -> None:
        """滚动评论区，子类覆盖。"""
        raise NotImplementedError

    async def _switch_next_work(self) -> bool:
        """切换下一个作品，子类覆盖。"""
        raise NotImplementedError

    # ---------------- 关键字搜索 ----------------
    async def _goto_search(self, ctx: CollectContext, keyword: str) -> None:
        """默认实现：直接打开搜索结果 URL。子类可改成模拟真人输入。"""
        search_url = self._search_url_template.format(keyword=keyword)
        self.note_step(f"搜索「{keyword}」")
        ctx.log(f"[{self.channel}] 打开搜索页：{search_url}")
        # ⚠️ 不能用 networkidle：抖音/快手/小红书都有常驻的轮询和推流请求，
        # "网络空闲 500ms" 这个条件永远不成立，goto 会一直阻塞到超时。
        # 表现就是脚本停在「打开搜索页」那一行不动，看起来像死了。
        await self._run(self._page.goto(
            search_url, wait_until="domcontentloaded", timeout=60_000))

    async def _prepare_search(self, ctx: CollectContext, keyword: str) -> None:
        """搜索结果出来之后的准备动作，默认什么都不做。"""
        return None

    async def _goto_work(self, ctx: CollectContext, work: WorkItem) -> bool:
        """到达这条作品。返回 False 表示没到达，调用方应跳过它。

        默认实现：直接打开作品页 URL。子类可改成在列表页点卡片。
        """
        work_url = self._work_url_template.format(work_id=work.work_id)
        ctx.log(f"[{self.channel}] 打开作品页：{work_url}")
        await self._run(self._page.goto(
            work_url, wait_until="domcontentloaded", timeout=60_000))
        return True

    #: 浏览器/页面已经没了的特征字样。Playwright 把这些都包成普通 Exception，
    #: 光看类型分不出"这一条打不开"和"整个浏览器没了"。
    BROWSER_GONE_MARKS = (
        "has been closed",           # Target page, context or browser has been closed
        "Connection closed",         # Connection closed while reading from the driver
        "Target closed",
        "browser has been closed",
    )

    @classmethod
    def browser_gone(cls, exc: BaseException) -> bool:
        """这个错是不是"浏览器整个没了"。

        ⚠️ 区分它很重要。实跑日志里，浏览器一关（用户 Ctrl+C），
        采集器把它当成"这一条打不开"，于是**又往下跑了九条**：
        每条都开方框、每条都报 0 条评论、每条都写库，
        还一路把「连续 N 条都打不开」的计数喊到 9。
        噪音掩盖了真正的原因，而且往库里写了九条"没有评论"的假结论。
        """
        text = str(exc)
        return any(mark in text for mark in cls.BROWSER_GONE_MARKS)

    def _require_page(self, what: str) -> None:
        """没有页面就**立刻炸**，绝不能安静地往下走。

        ⚠️ 这条是拿血换来的。实跑时 prepare() 因为 profile 撞车失败了，
        但上层把这个半成品采集器缓存了下来，重试时直接跳过 prepare。
        于是 self._page 一直是 None，而下面每个动作都写着
        `if self._page is None: return` —— 一路静悄悄地返回，
        最后报「连续 3 次滚动无新数据，结束」「采集到 0 条作品」，
        八个关键字全是 0，**任务还报成功**。
        日志里看不出任何异常，只看到"搜不到东西"。

        少采一条是小事；采了个寂寞还说自己成功，是必须炸出来的事。
        """
        if self._page is None:
            raise RuntimeError(
                f"[{self.channel}] {what}时没有可用页面：采集器没 prepare 成功就被拿来用了。"
                f"多半是上一次 prepare() 失败（比如同一个账号的 profile 被另一个"
                f"浏览器占着），但失败的采集器被缓存了下来。"
            )

    async def collect_by_keyword(
        self, ctx: CollectContext, keyword: str
    ) -> AsyncIterator[WorkItem]:
        """通过浏览器模拟搜索采集作品。"""
        self._require_page("搜索关键字")
        self._current_keyword = keyword
        self._is_capturing = True
        # _open_comments 这类钩子的签名里没有 ctx，存一份给它们用
        self._ctx = ctx

        # 导航与「搜索前的准备」拆成两个钩子，子类可以各自覆盖：
        #   _goto_search   —— 怎么到达搜索结果页（直开 URL / 模拟真人输入）
        #   _prepare_search —— 到了之后还要做什么（切列表模式、点筛选面板…）
        await self._goto_search(ctx, keyword)
        # 搜完等页面稳下来。走 pacer，用户能在配置里调（crawl.pace）
        await self.pacer.wait("work_seconds", ctx)
        await self._prepare_search(ctx, keyword)
        self.note_step(f"「{keyword}」搜索结果已就绪，开始逐条采集")

        emitted = 0
        no_new_count = 0
        max_no_new = 3

        try:
            while True:
                ctx.raise_if_cancelled()
                if self.limit_reached(emitted, ctx.max_works):
                    break

                # 从已捕获的数据中提取新作品
                new_works = await self._extract_new_works(ctx, keyword)
                for work in new_works:
                    yield work
                    emitted += 1
                    if self.limit_reached(emitted, ctx.max_works):
                        break

                if not new_works:
                    no_new_count += 1
                    if no_new_count >= max_no_new:
                        ctx.log(f"[{self.channel}] 连续 {max_no_new} 次滚动无新数据，结束")
                        break
                else:
                    no_new_count = 0

                # 模拟人滚动加载更多
                await self._simulate_scroll(times=2)
                await self._simulate_click_load_more()
                await asyncio.sleep(random.uniform(1, 2))
        finally:
            self._is_capturing = False

    async def _extract_new_works(
        self, ctx: CollectContext, keyword: str
    ) -> List[WorkItem]:
        """从捕获的响应中提取新作品，子类覆盖。"""
        raise NotImplementedError

    # ---------------- 评论采集 ----------------
    #: 连续多少条作品打不开就判定为页面改版/被风控，停止拟人采集
    GOTO_FAIL_STREAK_LIMIT = 5
    #: 点开评论区之后，最多等多久等第一批评论到货（秒）。
    #: 等不到就当这条作品没有评论，直接采下一条。
    FIRST_COMMENT_TIMEOUT_SECONDS = 8.0

    async def collect_comments(
        self, ctx: CollectContext, work: WorkItem
    ) -> AsyncIterator[CommentItem]:
        """通过浏览器模拟打开作品页采集评论。"""
        self._require_page("采集评论")
        self._current_work_id = work.work_id
        self._is_capturing = True
        self._ctx = ctx
        # 怎么"到达"这条作品，交给子类决定：
        # 有的平台是跳 URL，有的平台必须在搜索结果页上点卡片
        # （抖音就是后者：跳 URL 会重新加载页面，把筛选状态冲掉）
        # 只是给实时画面加个标题——用 getattr 取，绝不能因为一个展示用的
        # 字段缺失就把整条采集带崩
        title = (getattr(work, "title", "") or work.work_id or "").strip().replace("\n", " ")
        self.note_step(f"打开作品：{title[:30]}" if title else "打开作品")
        started_at = time.monotonic()
        opened = await self._goto_work(ctx, work)
        if not opened and self._browser_is_gone:
            # 浏览器没了不是"这一条打不开"，别再往下跑了
            raise RuntimeError(
                f"[{self.channel}] 浏览器/页面已经关闭，停止采集。"
                f"（服务被停掉、或者浏览器崩了。这不是单条作品的问题，"
                f"继续跑只会一条条报假的「没有评论」）"
            )
        if not opened:
            # 没到达就别往下走：不然会在**上一条**作品的页面上开评论区，
            # 把别人的评论记到这条作品名下——比少采一条严重得多
            self._goto_fail_streak = getattr(self, "_goto_fail_streak", 0) + 1
            ctx.log(
                f"[{self.channel}] 作品 {work.work_id} 没能打开"
                f"（等了 {time.monotonic() - started_at:.1f}s），跳过它的评论"
                f"（连续第 {self._goto_fail_streak} 条）", "warn",
            )
            self._is_capturing = False
            # ⚠️ 连续打不开要停下来。
            # 这个分支只是 return，不抛异常——上层看到的是"这条作品 0 条评论"，
            # 于是一百条作品可以一条不落地全"跳过"，任务最后还报成功。
            # 用户看到的现象是"浏览器停在搜索页不动，日志却哗哗往下走"。
            # 单条打不开是常态（作品被删、卡片文案对不上）；连续打不开
            # 说明页面结构变了或者被风控挡了，继续点下去没有意义。
            if self._goto_fail_streak >= self.GOTO_FAIL_STREAK_LIMIT:
                raise RuntimeError(
                    f"[{self.channel}] 连续 {self._goto_fail_streak} 条作品都打不开，"
                    f"停止拟人采集。多半是页面改版（选择器失效）或者被风控拦了，"
                    f"重跑一次 probe_page.py 看看卡片结构还对不对"
                )
            return
        self._goto_fail_streak = 0
        # 打开作品之后停一下再翻评论——真人不会秒开秒滑。
        # 这是拟人采集里**最主要**的那段等待，所以走 pacer：
        # 嫌慢就把 crawl.pace.<平台>.work_seconds 调小。
        await self.pacer.wait("work_seconds", ctx, note="再开始翻这条的评论")

        # ⚠️ 必须在打开评论区**之前**清掉这个标记：
        # 它是上一条作品留下的，不清的话下面那个"等第一批评论"会
        # 立刻认为"这条也到底了"，直接跳过——整轮采集一条评论都拿不到。
        self._comments_exhausted = False

        # 打开评论区
        await self._open_comments()

        # ⚠️ 打开之后**最多等这么久**等第一批评论到货，没有就走人。
        # 为什么要有这个：有的作品评论区是空的、有的作者关了评论、
        # 有的就是加载不出来。原来的做法是"取不到 → 滚一轮 → 再取"，
        # 空转三轮，每轮还要滚 5 次、睡 1~2 秒——一条没有评论的作品
        # 能白耗小半分钟，几百条作品就是一两个小时。
        first_batch = await self._wait_for_first_comments(ctx, work)
        if not first_batch:
            ctx.log(
                f"[{self.channel}] 作品 {work.work_id} 打开评论区后 "
                f"{self.FIRST_COMMENT_TIMEOUT_SECONDS:.0f} 秒没有评论到货，"
                f"按原步骤走下一条", "warn",
            )
            self._is_capturing = False
            return

        emitted = 0
        no_new_count = 0
        max_no_new = 3
        # （_comments_exhausted 在打开评论区之前就清过了——
        #   子类确认"这条作品的评论已经到底"后会把它置位，
        #   避免白白再滚几轮。实测日志里一条作品出现过 6 次「评论采集完成」。）

        try:
            while True:
                ctx.raise_if_cancelled()
                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

                # ⚠️ 顺序至关重要：**先取数据，再判断要不要结束**。
                # 上一版把 _comments_exhausted 的判断放在了取数据之前，
                # 结果是：滚动过程中拦到的评论进了缓冲区、同时滚动函数
                # 发现"到底了"置了位，下一轮循环开头直接 break——
                # 缓冲区里那一批评论**一条都没被取出来就丢了**。
                # 症状：日志显示评论区打开正常、滚动正常，最后 0 条。
                # 等待阶段已经取到的那一批要先产出，不然它们就白等了
                if first_batch:
                    new_comments, first_batch = first_batch, []
                else:
                    new_comments = await self._extract_new_comments(ctx, work.work_id)
                for comment in new_comments:
                    yield comment
                    emitted += 1
                    if self.limit_reached(emitted, ctx.max_comments_per_work):
                        break

                # 到底了就收工——但一定是在上面把数据取干净之后
                if self._comments_exhausted:
                    break

                if not new_comments:
                    no_new_count += 1
                    if no_new_count >= max_no_new:
                        break
                else:
                    no_new_count = 0

                await self._scroll_comments(max_scrolls=5)
                await asyncio.sleep(random.uniform(1, 2))
        finally:
            self._is_capturing = False

    async def _wait_for_first_comments(
        self, ctx: CollectContext, work: WorkItem
    ) -> List[CommentItem]:
        """等第一批评论到货，最多等 FIRST_COMMENT_TIMEOUT_SECONDS 秒。

        评论是靠拦截页面请求拿到的，点开评论区到响应落地之间有网络延迟，
        所以要轮询而不是睡一个固定时长：来得快就早点走，来得慢也等得住。

        取到的那一批**原样返回**，由调用方产出——在这里丢掉的话，
        就成了"等到了却没采"，比不等还糟。
        """
        deadline = time.monotonic() + self.FIRST_COMMENT_TIMEOUT_SECONDS
        while True:
            ctx.raise_if_cancelled()
            found = await self._extract_new_comments(ctx, work.work_id)
            if found:
                return found
            # 子类已经确认"这条作品就是没有评论"，别再等满 8 秒
            if self._comments_exhausted:
                return []
            if time.monotonic() >= deadline:
                return []
            await asyncio.sleep(0.5)

    async def _extract_new_comments(
        self, ctx: CollectContext, work_id: str
    ) -> List[CommentItem]:
        """从捕获的响应中提取新评论，子类覆盖。"""
        raise NotImplementedError
