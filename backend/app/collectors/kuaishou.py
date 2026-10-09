"""快手采集器。

请求参数处理对齐 MediaCrawler 的 KuaiShouClient：

  快手网页端把「批量列表类」接口迁到了带签名的 REST v2 端点，
  签名参数是 __NS_hxfalcon，只能调用页面里已加载的签名环境生成
  （window.__ks_realm.$encode），没有可离线计算的纯算实现。
  所以采集全程必须持有一个活着的快手页面，并在导航前注入捕获脚本，
  否则页面初始化早就跑完了，钩子挂不上。

  未签名请求会直接返回 result:50；被限流返回 result:2，需要指数退避。

接口清单：
  关键字搜索  POST /rest/v/search/feed        需签名  {keyword, pcursor(页码), page:"search", searchSessionId}
  主页作品    POST /rest/v/profile/feed       需签名  {user_id, pcursor, page:"profile"}
  一级评论    POST /rest/v/photo/comment/list          {photoId, pcursor}
  二级评论    POST /rest/v/photo/comment/sublist       {photoId, pcursor, rootCommentId(int)}

响应字段（V2 用下划线命名，老 GraphQL 用驼峰，两种都兼容）：
  feed 项   {photo:{id, caption, timestamp, realLikeCount, viewCount, coverUrl, photoUrl}, author:{id, name, headerUrl}}
  评论项    {comment_id(int), content, timestamp, author_id, author_name, headurl,
             likedCount, commentCount, hasSubComments}
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.constants import CHANNEL_KUAISHOU
from ..core.logging import get_logger
from ..utils import normalize as nz
from .base import (
    AuthorItem,
    BaseCollector,
    CollectContext,
    CollectorRegistry,
    CollectTarget,
    CommentItem,
    LoginRequired,
    WorkItem,
)

logger = get_logger(__name__)

HOST = "https://www.kuaishou.com"

SEARCH_URI = "/rest/v/search/feed"
PROFILE_FEED_URI = "/rest/v/profile/feed"
COMMENT_URI = "/rest/v/photo/comment/list"
SUB_COMMENT_URI = "/rest/v/photo/comment/sublist"

SITE_HEADERS = {
    "Origin": HOST,
    "Referer": f"{HOST}/",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
}

# 在页面导航前注入，钩住签名环境的初始化时机
KS_SIGN_CAPTURE_SCRIPT = """
(() => {
  if (window.__ks_realm) return;
  let done = false;
  const setter = function (v) {
    if (!done && this && typeof this === "object" && this !== window &&
        typeof this.$encode === "function" &&
        typeof this.$getCatVersion === "function") {
      done = true;
      window.__ks_realm = this;
      try { delete Object.prototype.caver; } catch (e) {}
    }
    Object.defineProperty(this, "caver", {
      value: v, writable: true, enumerable: true, configurable: true,
    });
  };
  try {
    Object.defineProperty(Object.prototype, "caver", { set: setter, configurable: true });
  } catch (e) {}
})();
"""

SIGN_EVAL = """([u, q, b]) => new Promise((resolve, reject) => {
    window.__ks_realm.call('$encode', [
        { url: u, query: q, form: {}, requestBody: b },
        { suc: s => resolve(s), err: e => reject(new Error(String(e))) }
    ]);
})"""

SIGN_MAX_RETRY = 3


def _filter_notice(ctx) -> str:
    """快手的搜索接口没有排序和时间参数，实话实说。

    MediaCrawler 的 visionSearchPhoto GraphQL 只有
    keyword / pcursor / searchSessionId / page / webPageArea 五个变量，
    新版 REST /rest/v/search/feed 也一样。所以时间范围只能采回来再本地过滤，
    比其他平台多翻几页是正常的。
    """
    filters = ctx.search_filters
    if filters.window is None:
        return ""
    return (
        "[快手] 搜索接口不支持时间范围筛选，会先按默认顺序取回再本地过滤，"
        "可能要多翻几页才凑够数量"
    )


@CollectorRegistry.register
class KuaishouCollector(BaseCollector):
    channel = CHANNEL_KUAISHOU
    supports_works = True
    needs_login = True
    integrated = True
    supported_targets = ("keyword", "creator")

    host = HOST

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._client = None
        self._session = None
        self._live_view = None
        # 浏览器模拟采集器（API 被风控时降级使用）
        self._browser_collector = None
        self._use_browser_fallback = False

    # ---------------- 生命周期 ----------------
    def needs_browser_for_api(self, ctx: CollectContext) -> bool:
        """快手的接口采集**必须**有一个常驻页面。

        每个请求都要带 __NS_hxfalcon 签名，而那个签名只能调页面里的
        window.__ks_realm.$encode 算出来（见 _sign）——没有页面就没有签名，
        接口一律 403。所以快手即使选「API」模式，浏览器也得从头开到尾，
        账号在这段时间里不能给别的任务用。
        （抖音不一样：它的 a_bogus 在 Node 里算，页面只用来取一次 Cookie。）
        """
        return True

    async def prepare(self, ctx: CollectContext) -> None:
        from ..browser.page_session import PageSession

        # ⚠️ 拟人模式：接口这条路根本不走，**绝不能**在这里先开一个浏览器。
        #
        # 原来这里不管三七二十一先开一个 PageSession 取 Cookie、等签名，
        # 等到 collect_by_keyword 里发现"任务选的是拟人"，再让
        # KuaishouBrowserCollector.prepare() 开**第二个**——两个
        # launch_persistent_context 指向同一个 user-data-dir。
        # Chrome 不会开第二个实例：它把请求交给已经在跑的那个进程然后自己退出，
        # Playwright 看到进程没了，报的却是一句看不出因果的
        #
        #     BrowserType.launch_persistent_context:
        #     Target page, context or browser has been closed
        #
        # （Chrome 自己的那句 "在正在运行的浏览器会话中打开" 还被 GBK 编码
        #   糊成了乱码，更看不出来。）
        # 抖音那边一直是对的（见 DouyinCollector.prepare），快手漏了这一步。
        if self.human_only(ctx):
            ctx.log("[快手] 任务选的是「拟人」，跳过接口直接用浏览器模拟真人采集")
            await self._init_browser_fallback(ctx)
            self._use_browser_fallback = True
            return

        manager = ctx.params.get("browser_manager")
        if manager is None:
            raise LoginRequired("快手采集需要浏览器会话，但调度层没有传入 browser_manager")

        # 捕获脚本必须作为 init script 在导航前注入
        self._session = PageSession(
            manager, self.channel, ctx.account_name,
            init_scripts=[KS_SIGN_CAPTURE_SCRIPT],
            group=ctx.params.get("account_group", ""),
            slot_token=ctx.params.get("browser_slot_token", ""),
        )
        await self._session.start()

        # profile 里可能只有一堆游客 Cookie —— 那样接口会回"请先登录"，
        # 但那个错误指向采集器而不是账号，很误导。先在本地拦掉。
        self._session.ensure_logged_in()

        headers = dict(SITE_HEADERS)
        headers["Cookie"] = self._session.cookie_header
        user_agent = await self._session.user_agent()
        if user_agent:
            headers["User-Agent"] = user_agent

        self._client = self.make_client(base_headers=headers)
        await self._client.__aenter__()

        # 提前确认签名环境已就绪，早失败好过采到一半才报错
        await self._wait_sign_ready(ctx)

        # 快手接口模式的浏览器是**常驻**的（每次请求的签名都要调页面里的
        # __ks_realm），所以这里同样有"当前页面"可看。无头模式下这是唯一
        # 能看见风控弹窗/验证码的途径。
        from ..browser import live_view as lv
        self._live_view = lv.attach_session(
            self._session, ctx, self.channel,
            engine=getattr(ctx, "collect_engine", "api"))

        ctx.log(f"[快手] 采集客户端就绪，账号 {self._session.account_name}")

    async def _detach_live_view(self) -> None:
        """摘掉接口模式挂的实时画面。

        交接给拟人采集器之前必须摘：不然同一个会话上会挂两个推流，
        而且左边那个标的引擎还是 api，看着像跑错了模式。
        """
        from ..browser import live_view as lv
        await lv.detach_session(getattr(self, "_live_view", None))
        self._live_view = None

    async def cleanup(self) -> None:
        await self._detach_live_view()
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        await self._cleanup_browser_fallback()

    # ---------------- 浏览器模拟降级 ----------------
    async def _init_browser_fallback(self, ctx: CollectContext) -> None:
        """初始化拟人采集器。

        ⚠️ 这里必须先把浏览器租约拿到手。拟人采集会一直占着这个账号的页面
        直到任务结束，而混合模式是**跑到一半**才切过来的——那一刻账号很可能
        已经被别的任务占着。拿不到就排队等（见 browser/lease.py），
        而不是直接失败或者硬开第二个 Chromium 去抢同一个 profile 目录。
        """
        if self._browser_collector is not None:
            return

        lease = ctx.params.get("browser_lease")
        if lease is not None:
            account = await lease.acquire("拟人采集")
            if account:
                # 排队等到的可能不是原来那个账号，让后面开页面的用同一个
                ctx.account_name = account
        from .kuaishou_browser import KuaishouBrowserCollector
        self._browser_collector = KuaishouBrowserCollector(self.config, self.proxy_manager)
        # ⚠️ 混合模式是**跑到一半**被风控挡了才切过来的，这时候接口模式的
        # 浏览器还开着（快手的签名要常驻页面）。再开一个就撞同一个 profile
        # 目录，Chrome 直接把进程让给旧的然后退出 —— 就是上面那个
        # "Target page, context or browser has been closed"。
        # 所以把现成的会话**交接**过去：不重开、不换 Cookie，也没有
        # "先关再开"那一瞬间 profile 锁还没释放的竞争。
        # 交接之后这个会话归拟人采集器用，但**所有权还在我这儿**
        # （由 KuaishouCollector.cleanup 关闭），避免两边都去关。
        if self._session is not None:
            await self._detach_live_view()
            self._browser_collector.adopt_session(self._session)
        # ⚠️ prepare() 失败了就**不能把半成品留在手里**。
        # 实跑真实发生过：prepare 因为 profile 撞车失败，但 self._browser_collector
        # 已经被赋值了，上面那句 `if ... is not None: return` 于是让每一次重试
        # 都直接跳过 prepare。采集器带着 _page=None 一路跑，八个关键字全 0 条，
        # 任务还报成功。失败就清干净，让重试真的能重来一次。
        try:
            await self._browser_collector.prepare(ctx)
        except BaseException:
            broken = self._browser_collector
            self._browser_collector = None      # ← 关键：别把半成品留在手里
            try:
                await broken.cleanup()
            except Exception:  # noqa: BLE001
                pass          # 清理失败不能盖住原来那个错
            raise
        if self.human_only(ctx):
            ctx.log("[快手] 拟人采集器已就绪（任务选的是「拟人」）")
        else:
            ctx.log("[快手] API 采集被风控，降级为浏览器模拟采集", "warn")

    async def _cleanup_browser_fallback(self) -> None:
        """清理浏览器模拟采集器。"""
        if self._browser_collector is not None:
            await self._browser_collector.cleanup()
            self._browser_collector = None

    async def _wait_sign_ready(self, ctx: CollectContext) -> None:
        # 全部走 PageSession 的方法，不直接碰 page 对象——
        # 页面活在浏览器专用循环上，跨循环 await 会出问题（见 browser/loop.py）
        try:
            await self._session.wait_for_function("() => !!window.__ks_realm", 15_000)
        except Exception:  # noqa: BLE001
            # 未登录态下页面可能没触发过签名请求，重载一次让钩子在登录态下生效
            ctx.log("[快手] 首次未捕获到签名环境，重载页面重试")
            await self._session.reload()
            try:
                await self._session.wait_for_function("() => !!window.__ks_realm", 20_000)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "快手签名环境未就绪：页面里没找到 window.__ks_realm。"
                    "常见原因是登录态失效或页面被风控拦截，请在账号管理里重新登录该账号。"
                ) from exc

    # ---------------- 请求 ----------------
    async def _sign(self, uri: str, body: Dict[str, Any]) -> str:
        """签名绑定请求内容和时间窗口，每次重试都要重新生成。

        ⚠️ 每次都确认签名环境还在。`window.__ks_realm` 是靠劫持
        `Object.prototype.caver` 的 setter 抓下来的，**挂在 window 上**——
        页面一旦被风控跳转、或者自己刷新过，这个对象就没了。
        只在 prepare() 里检查一次的话，长任务跑到中途丢了就再也捞不回来，
        表现是后面每一页都签不出名。对齐 MediaCrawler 的
        get_ks_sign_from_playwright：它也是每次签名前都等一遍。
        """
        try:
            return await self._session.evaluate(SIGN_EVAL, [uri, {"caver": 2}, body])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[快手] 签名环境疑似丢失（%s），重新等待后重试一次", exc)
            await self._session.wait_for_function("() => !!window.__ks_realm", 15_000)
            return await self._session.evaluate(SIGN_EVAL, [uri, {"caver": 2}, body])

    async def _post_signed(self, uri: str, body: Dict[str, Any]) -> Dict[str, Any]:
        last: Dict[str, Any] = {}
        for attempt in range(SIGN_MAX_RETRY):
            sign = await self._sign(uri, body)
            url = f"{self.host}{uri}?__NS_hxfalcon={sign}&caver=2"
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            last = await self._client.request_json(
                "POST", url, data=payload.encode("utf-8")
            )
            result = last.get("result")
            if result == 1:
                return last
            if result == 2:
                # 服务端限流：先换 IP/指纹，再指数退避重试
                # 同一出口上死等只会越限越狠，换出口才有可能立刻恢复
                delay = 5 * (2 ** attempt) + random.uniform(0, 2)
                logger.warning(
                    "[快手] %s 被限流（result:2），换出口 IP 并 %.1fs 后重试",
                    uri, delay,
                )
                await self._client.rotate(f"快手接口 {uri} 限流（result:2）")
                await asyncio.sleep(delay)
                continue
            if result == 50:
                raise RuntimeError(
                    f"快手接口 {uri} 返回 result:50（签名未通过）。"
                    f"通常是页面签名环境失效，请重新登录该账号。"
                )
            raise RuntimeError(f"快手接口 {uri} 返回异常：{str(last)[:300]}")
        raise RuntimeError(f"快手接口 {uri} 多次限流后仍失败：{str(last)[:300]}")

    async def _post_plain(self, uri: str, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        data = await self._client.request_json(
            "POST", f"{self.host}{uri}", data=payload.encode("utf-8")
        )
        if data.get("result") != 1:
            raise RuntimeError(f"快手接口 {uri} 返回异常：{str(data)[:300]}")
        return data

    # ---------------- 关键字搜索 ----------------
    async def collect_by_keyword(
        self, ctx: CollectContext, keyword: str
    ) -> AsyncIterator[WorkItem]:
        # 建任务时选了「拟人」→ 一开始就走浏览器模拟，不浪费一轮必然被拦的接口请求
        if not self._use_browser_fallback and self.human_only(ctx):
            ctx.log("[快手] 任务选的是「拟人」，跳过接口直接用浏览器模拟真人采集")
            await self._init_browser_fallback(ctx)
            self._use_browser_fallback = True

        # 如果已经降级到浏览器模拟，直接用浏览器采集
        if self._use_browser_fallback:
            async for work in self._browser_collector.collect_by_keyword(ctx, keyword):
                yield work
            return

        page = 1
        session_id = ""
        emitted = 0
        seen: set[str] = set()

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            try:
                data = await self._post_signed(SEARCH_URI, {
                    "keyword": keyword,
                    "pcursor": str(page),
                    "page": "search",
                    "searchSessionId": session_id,
                })
            except RuntimeError as exc:
                # API 被限流/风控时，降级为浏览器模拟采集
                if "限流" in str(exc) or "风控" in str(exc):
                    # 「仅接口」的任务不许偷偷换路：接口坏了就要立刻暴露出来
                    if not self._use_browser_fallback and self.may_switch_to_human(ctx):
                        ctx.log("[快手] API 搜索被限流，尝试降级为浏览器模拟采集", "warn")
                        try:
                            await self._init_browser_fallback(ctx)
                            self._use_browser_fallback = True
                            async for work in self._browser_collector.collect_by_keyword(ctx, keyword):
                                yield work
                            return
                        except LoginRequired:
                            # 浏览器降级也不可用，回退到 API 错误
                            ctx.log("[快手] 浏览器模拟采集也不可用，报 API 错误", "warn")
                            raise
                raise
            session_id = data.get("searchSessionId", "") or session_id
            if page == 1:
                # 快手接口没有排序/时间参数，用户设了要说清楚是本地过滤的
                notice = _filter_notice(ctx)
                if notice:
                    ctx.log(notice, "warn")

            feeds = data.get("feeds") or []
            if not feeds:
                if page == 1 and emitted == 0:
                    raise self.empty_first_page(
                        keyword, data,
                        clues=self.cookie_clues(self._session.cookie_header),
                    )
                ctx.log(f"[快手] 关键字 [{keyword}] 第 {page} 页无数据，结束")
                break

            new_in_page = 0
            for feed in feeds:
                work = self.safe_map(ctx, self._to_work, ctx, feed,
                                     source_keyword=keyword)
                if work is None or work.work_id in seen:
                    continue
                seen.add(work.work_id)
                new_in_page += 1
                yield work
                emitted += 1
                if self.limit_reached(emitted, ctx.max_works):
                    break

            if new_in_page == 0:
                ctx.log(f"[快手] 关键字 [{keyword}] 本页无新增，结束")
                break

            pcursor = nz.to_text(data.get("pcursor"))
            if pcursor == "no_more":
                break
            page += 1
            await self._client.sleep_interval()

    # ---------------- 主页采集 ----------------
    async def collect_by_creator(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[WorkItem]:
        # 拟人采集器目前只做了关键字搜索，没有"打开某个作者主页往下翻"这条路。
        # 静默返回 0 条会让人以为这个作者没作品，所以明说。
        if self._use_browser_fallback:
            raise RuntimeError(
                "[快手] 拟人模式暂不支持「指定用户主页」采集，只支持关键字搜索。"
                "把任务的采集模式改成 API 或混合，或者改用关键字搜索。"
            )

        user_id = self._parse_user_id(target)
        pcursor = ""
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            data = await self._post_signed(PROFILE_FEED_URI, {
                "user_id": user_id,
                "pcursor": pcursor,
                "page": "profile",
            })
            feeds = data.get("feeds") or []
            if not feeds:
                break

            for feed in feeds:
                work = self.safe_map(ctx, self._to_work, ctx, feed)
                if work is None:
                    continue
                yield work
                emitted += 1
                if self.limit_reached(emitted, ctx.max_works):
                    break

            pcursor = nz.to_text(data.get("pcursor"))
            if not pcursor or pcursor == "no_more":
                break
            await self._client.sleep_interval()

    @staticmethod
    def _parse_user_id(target: CollectTarget) -> str:
        raw = (target.value or target.url or "").strip()
        if not raw.startswith("http") and "kuaishou.com" not in raw:
            return raw
        match = re.search(r"/profile/([a-zA-Z0-9_-]+)", raw)
        if match:
            return match.group(1)
        raise ValueError(f"无法从这个值里解析出快手 user_id：{raw}")

    # ---------------- 评论 ----------------
    async def collect_comments(
        self, ctx: CollectContext, work: WorkItem
    ) -> AsyncIterator[CommentItem]:
        # ⚠️ 拟人模式下评论也必须从页面上采。
        # 这个采集器的 _client 在拟人模式下**根本没建**（见 prepare），
        # 直接走接口会是 "'NoneType' object has no attribute 'get_json'"——
        # 而且每条作品都会犯，日志里一屏全是"评论采集失败，跳过这条"。
        if self._use_browser_fallback and self._browser_collector is not None:
            async for comment in self._browser_collector.collect_comments(ctx, work):
                yield comment
            return

        photo_id = work.work_id
        pcursor = ""
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_comments_per_work):
                break

            data = await self._post_plain(COMMENT_URI, {
                "photoId": photo_id, "pcursor": pcursor,
            })
            comments = data.get("rootCommentsV2") or data.get("rootComments") or []
            if not comments:
                break

            for raw in comments:
                item = self.safe_map(ctx, self._to_comment, ctx, raw, photo_id,
                                     depth=1, parent_id="", what="评论")
                if item is None:
                    continue
                yield item
                emitted += 1

                has_sub = raw.get("hasSubComments") or nz.to_int(
                    raw.get("subCommentCount") or raw.get("commentCount")
                ) > 0
                if ctx.enable_sub_comments and ctx.max_comment_level >= 2 and has_sub:
                    async for sub in self._collect_sub_comments(
                        ctx, photo_id, raw, item.comment_id
                    ):
                        yield sub
                        emitted += 1
                        if self.limit_reached(emitted, ctx.max_comments_per_work):
                            break

                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

            pcursor = nz.to_text(data.get("pcursorV2") or data.get("pcursor"))
            if not pcursor or pcursor == "no_more":
                break
            # 快手评论接口限流较严，固定间隔上再加抖动
            await asyncio.sleep(
                float(self.config.get("crawl.request_interval_seconds", 1.0))
                + random.uniform(1, 3)
            )

    async def _collect_sub_comments(
        self, ctx: CollectContext, photo_id: str, root_raw: Dict, root_comment_id: str
    ) -> AsyncIterator[CommentItem]:
        # V2 的 rootCommentId 必须是 int
        try:
            root_id_int = int(root_raw.get("comment_id") or root_raw.get("commentId") or 0)
        except (TypeError, ValueError):
            return
        if not root_id_int:
            return

        pcursor = ""
        while True:
            ctx.raise_if_cancelled()
            data = await self._post_plain(SUB_COMMENT_URI, {
                "photoId": photo_id,
                "pcursor": pcursor,
                "rootCommentId": root_id_int,
            })
            subs = data.get("subCommentsV2") or data.get("subComments") or []
            if not subs:
                return
            for raw in subs:
                item = self.safe_map(
                    ctx, self._to_comment, ctx, raw, photo_id, depth=2,
                    parent_id=root_comment_id, root_id=root_comment_id, what="评论",
                )
                if item is not None:
                    yield item
            pcursor = nz.to_text(data.get("pcursorV2") or data.get("pcursor"))
            if not pcursor or pcursor == "no_more":
                return
            await self._client.sleep_interval()

    # ---------------- 字段映射 ----------------
    def _to_work(self, ctx: CollectContext, feed: Dict,
                 source_keyword: str = "") -> Optional[WorkItem]:
        photo = feed.get("photo") or {}
        author = feed.get("author") or {}
        photo_id = nz.to_text(photo.get("id"))
        if not photo_id:
            return None

        caption = nz.to_text(photo.get("caption"))
        # 话题优先用 feed["tags"]（[{"name": "蓟州旅游", "type": 1}]）——
        # 那是平台给的结构化字段。拿不到再退回从正文里抠 #话题。
        tags = [nz.to_text(t.get("name")) for t in (feed.get("tags") or [])
                if isinstance(t, dict) and t.get("name")]
        if not tags:
            tags = re.findall(r"#([^#\s]{1,30})#?", caption)

        # ⚠️ 真实响应里的字段名是 photoUrls / photoH265Urls，元素是
        # {"cdn": ..., "url": ...}；老代码写的 photoUrl / mainMvUrls
        # **在搜索响应里根本不存在**，结果就是 video_list 一直是空的。
        videos = nz.collect_urls(
            photo.get("photoUrls"), photo.get("photoH265Urls"),
            photo.get("photoUrl"), photo.get("mainMvUrls"),
        )
        cover = nz.collect_urls(photo.get("coverUrl"), photo.get("coverUrls"))

        extra = {
            "cover_url": cover[0] if cover else "",
            "view_count": photo.get("viewCount"),
            "duration": photo.get("duration"),
            "type": feed.get("type"),
            "author_head": author.get("headerUrl"),
        }

        return self.new_work(
            ctx,
            work_id=photo_id,
            work_url=f"{self.host}/short-video/{photo_id}",
            author_id=nz.to_text(author.get("id")),
            author_name=nz.to_text(author.get("name")),
            title=caption[:500],
            description=caption,
            label=",".join(tags) if tags else None,
            image_list=nz.to_json_list(cover),
            video_list=nz.to_json_list(videos),
            likes=nz.to_int(photo.get("realLikeCount") or photo.get("likeCount")),
            # 搜索接口**给不出真实评论数**：photo 里没有 commentCount，
            # feed["comment"] 只有 {"us_c": 0}。真值来自评论接口的
            # commentCountV2，由拟人版在翻完评论后回填（_apply_comment_count）。
            comment_cnt=nz.to_int(photo.get("commentCount")
                                  or (feed.get("comment") or {}).get("us_c")),
            shares=nz.to_int(photo.get("shareCount")),
            location=nz.to_text(photo.get("location")),
            publish_time=nz.to_datetime(photo.get("timestamp")),
            crawl_time=datetime.now(),
            source_keyword=source_keyword,
            extra_content=nz.to_json({k: v for k, v in extra.items() if v not in (None, "")}),
        ).finalize()

    def _to_comment(self, ctx: CollectContext, raw: Dict, photo_id: str,
                    depth: int, parent_id: str,
                    root_id: str = "") -> Optional[CommentItem]:
        comment_id = nz.to_text(raw.get("comment_id") or raw.get("commentId"))
        if not comment_id:
            return None
        return self.new_comment(
            ctx,
            comment_id=comment_id,
            work_id=photo_id,
            depth=depth,
            comment_parent_id=parent_id,
            root_comment_id=root_id or comment_id,
            commenter_id=nz.to_text(raw.get("author_id") or raw.get("authorId")),
            commenter_name=nz.to_text(raw.get("author_name") or raw.get("authorName")),
            content=nz.to_text(raw.get("content")),
            # ⚠️ 真实字段是 likeCount；likedCount/realLikedCount 在
            # /rest/v/photo/comment/list 的响应里不存在，写它等于永远 0
            likes=nz.to_int(raw.get("likeCount") or raw.get("likedCount")
                            or raw.get("realLikedCount")),
            sub_comment_count=nz.to_int(
                raw.get("subCommentCount") or raw.get("commentCount")
            ),
            location=nz.to_text(raw.get("ipLocation") or raw.get("location")),
            publish_time=nz.to_datetime(raw.get("timestamp")),
            crawl_time=datetime.now(),
            extra_content=nz.to_json({
                "headurl": raw.get("headurl") or raw.get("headUrl"),
                "has_sub_comments": raw.get("hasSubComments"),
            }),
        ).finalize()

    async def fetch_author(
        self, ctx: CollectContext, author_id: str
    ) -> Optional[AuthorItem]:
        """快手的作者详情要走 GraphQL，本期不额外请求，
        作者基本信息已经随 feed 一起返回并写进了作品行。"""
        return None
