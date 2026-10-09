"""抖音采集器。

请求参数处理严格对齐 MediaCrawler 的 DouYinClient 实现：

  1. 每个请求都要带一整套 web 端公共参数（device_platform / aid=6383 / …）
  2. webid：本地生成的 19 位随机串
  3. msToken：来自浏览器页面 localStorage 的 xmst 键 —— 所以采集全程要持有活页面
  4. a_bogus：把「查询串 + User-Agent」交给 libs/douyin.cjs 计算
       普通接口   -> sign_datail
       /reply 接口 -> sign_reply
     计算 a_bogus 用的 UA 必须和真正发出去的 UA 一致，否则签名对不上，
     所以这里把 ProxiedClient 的 UA 钉死成浏览器页面的 UA（换 IP 也不换 UA）。

接口清单：
  搜索      GET /aweme/v1/web/general/search/single/
  作品详情  GET /aweme/v1/web/aweme/detail/
  一级评论  GET /aweme/v1/web/comment/list/
  二级评论  GET /aweme/v1/web/comment/list/reply/
  主页作品  GET /aweme/v1/web/aweme/post/
  用户资料  GET /aweme/v1/web/user/profile/other/
"""
from __future__ import annotations

import asyncio
import json
import random
import urllib.parse
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.constants import CHANNEL_DOUYIN
from ..core.logging import get_logger
from ..utils import normalize as nz
from ..utils.jsvm import JsRuntimeError, get_js_runtime
from .base import (
    AuthorItem,
    BaseCollector,
    CollectContext,
    CollectorRegistry,
    CollectTarget,
    CommentItem,
    LoginRequired,
    WorkItem,
    browser_window,
)

logger = get_logger(__name__)

HOST = "https://www.douyin.com"
SIGN_JS = "douyin.cjs"

SEARCH_URI = "/aweme/v1/web/general/search/single/"
#: 命中这个片段的接口不参与 a_bogus 签名（对齐 MediaCrawler 的判断）
NO_SIGN_MARKER = "/v1/web/general/search"
DETAIL_URI = "/aweme/v1/web/aweme/detail/"
COMMENT_URI = "/aweme/v1/web/comment/list/"
SUB_COMMENT_URI = "/aweme/v1/web/comment/list/reply/"
USER_POST_URI = "/aweme/v1/web/aweme/post/"
USER_PROFILE_URI = "/aweme/v1/web/user/profile/other/"

SEARCH_PAGE_SIZE = 15
#: 我们的排序标识 -> 抖音的 sort_type
DY_SORT_TYPE = {"general": 0, "most_like": 1, "latest": 2}
#: 我们的时间窗标识 -> 抖音的 publish_time（天数）
DY_PUBLISH_TIME = {"unlimited": 0, "day": 1, "week": 7, "half_year": 180}
#: 搜索 Referer 里的 aid 与 from_group_id，取自 MediaCrawler 的实现
SEARCH_REFERER_AID = "f594bbd9-a0e2-4651-9319-ebe3cb6298c1"
SEARCH_FROM_GROUP_ID = "7378810571505847586"
COMMENT_PAGE_SIZE = 20
POST_PAGE_SIZE = 18

# web 端公共参数，缺一个都可能被判成非法请求
COMMON_PARAMS: Dict[str, str] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "version_code": "190600",
    "version_name": "19.6.0",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "cookie_enabled": "true",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "131.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "8",
    "device_memory": "8",
    "engine_version": "109.0",
    "platform": "PC",
    "screen_width": "1920",
    "screen_height": "1080",
    "effective_type": "4g",
    "round_trip_time": "50",
}

SITE_HEADERS = {
    "Origin": HOST,
    "Referer": f"{HOST}/",
    "Accept": "application/json, text/plain, */*",
}


def generate_web_id() -> str:
    """生成 19 位 webid，算法对齐 MediaCrawler 的 get_web_id。"""

    def _e(t: Optional[int]) -> str:
        if t is not None:
            return str(t ^ (int(16 * random.random()) >> (t // 4)))
        return "".join([
            str(int(1e7)), "-", str(int(1e3)), "-", str(int(4e3)),
            "-", str(int(8e3)), "-", str(int(1e11)),
        ])

    web_id = "".join(_e(int(x)) if x in "018" else x for x in _e(None))
    return web_id.replace("-", "")[:19]


@CollectorRegistry.register
class DouyinCollector(BaseCollector):
    channel = CHANNEL_DOUYIN
    supports_works = True
    needs_login = True
    integrated = True
    supported_targets = ("keyword", "creator", "detail")

    host = HOST

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._client = None
        self._session = None          # PageSession，提供 msToken 与 UA
        self._web_id = generate_web_id()
        self._user_agent = ""
        self._ms_token = ""
        self._verify_fp = ""
        #: 从页面取出来的 Cookie 串。取完浏览器就关了，所以要自己留一份，
        #: 不能再去问 self._session（那时候它已经是 None 了）。
        self._cookie_header = ""
        self._account_name = ""
        # 浏览器模拟采集器（API 被风控时降级使用）
        self._browser_collector = None
        self._use_browser_fallback = False

    # ---------------- 生命周期 ----------------
    def needs_browser_for_api(self, ctx: CollectContext) -> bool:
        """抖音的接口采集**不需要**常驻浏览器。

        a_bogus 是在 Node 里算的（libs/douyin.cjs），页面只用来做两件事：
        读一次 msToken（localStorage 的 xmst）和把 profile 里的 Cookie 取出来。
        两件事都是一次性的，取完就可以把浏览器关掉——
        剩下几十分钟的接口采集一个浏览器进程都不占，
        同一个账号立刻可以给别的任务用。
        （快手不一样：它每次请求的签名都要调页面里的 __ks_realm，必须常驻。）
        """
        return False

    async def prepare(self, ctx: CollectContext) -> None:
        # 拟人模式：接口这条路根本不走，就别白开一次浏览器取 Cookie 了。
        if self.human_only(ctx):
            ctx.log("[抖音] 任务选的是「拟人」，跳过接口直接用浏览器模拟真人采集")
            await self._init_browser_fallback(ctx)
            self._use_browser_fallback = True
            return

        await self._load_login_state(ctx, first_time=True)

        headers = dict(SITE_HEADERS)
        headers["Cookie"] = self._cookie_header
        # UA 必须和算 a_bogus 时用的一致，钉死它：换 IP 不换 UA
        if self._user_agent:
            headers["User-Agent"] = self._user_agent

        self._client = self.make_client(base_headers=headers)
        await self._client.__aenter__()
        ctx.log(
            f"[抖音] 采集客户端就绪，账号 {self._account_name}，"
            f"msToken {'已获取' if self._ms_token else '缺失'}，"
            f"页面指纹 {'正常' if self._verify_fp else '缺失'}，"
            f"Cookie {len([p for p in self._cookie_header.split(';') if p.strip()])} 个，"
            f"浏览器已关闭（接口采集不需要它一直开着）"
        )

    async def _load_login_state(self, ctx: CollectContext, *, first_time: bool) -> None:
        """开一次浏览器把登录态取出来，然后**立刻关掉**。

        取的东西：UA、msToken（xmst）、页面指纹、Cookie 串。
        这几样在整轮采集里都是固定的，没有理由为它们把浏览器挂几十分钟。

        中途 Cookie 失效时会再调一次（first_time=False），
        这也是"API 采集过程中发现失效就再启动浏览器更新 cookie"的落点。
        """
        from ..browser.page_session import PageSession

        manager = ctx.params.get("browser_manager")
        if manager is None:
            raise LoginRequired("抖音采集需要浏览器会话，但调度层没有传入 browser_manager")

        lease = ctx.params.get("browser_lease")
        note = "读取登录态" if first_time else "登录态失效，重新读取"
        async with browser_window(lease, note):
            session = PageSession(
                manager, self.channel, ctx.account_name,
                group=ctx.params.get("account_group", ""),
                slot_token=ctx.params.get("browser_slot_token", ""),
            )
            await session.start()
            try:
                self._user_agent = await session.user_agent()
                self._ms_token = await session.local_storage("xmst") or ""
                if not self._ms_token:
                    ctx.log("[抖音] 页面里没读到 msToken（xmst），继续尝试采集但成功率会下降", "warn")

                # s_v_web_id 不作为请求参数发出去（MediaCrawler 也没发），
                # 但它在不在能反映页面初始化是否完整，出问题时是条有用的线索
                self._verify_fp = (
                    await session.local_storage("s_v_web_id")
                    or session.cookie_value("s_v_web_id")
                    or ""
                )
                # profile 里可能只有一堆游客 Cookie —— 那样接口会回"请先登录"，
                # 但那个错误指向采集器而不是账号，很误导。先在本地拦掉。
                session.ensure_logged_in()
                self._cookie_header = session.cookie_header
                self._account_name = session.account_name
                # 后面要用同一个账号刷新，别每次重新挑一个
                if not ctx.account_name:
                    ctx.account_name = session.account_name
            finally:
                # ⚠️ 关键的一行：取完就关。
                # 改造前这个页面会一直开到任务结束，一个账号因此被独占几十分钟，
                # 别的任务撞上就被"跳过"。
                await session.close()

    async def _refresh_login_state(self, ctx: CollectContext) -> bool:
        """接口说登录态不行了：重开一次浏览器刷 Cookie，然后接着用接口采。

        返回是否刷到了新的 Cookie（没刷到就别重试了，重试也是一样的结果）。
        """
        before = self._cookie_header
        try:
            await self._load_login_state(ctx, first_time=False)
        except LoginRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[抖音] 重新读取登录态失败：{exc}", "warn")
            return False

        if self._cookie_header and self._cookie_header != before:
            if self._client is not None:
                self._client.set_header("Cookie", self._cookie_header)
                if self._user_agent:
                    self._client.set_header("User-Agent", self._user_agent)
            ctx.log("[抖音] 已重新取到登录态，继续用接口采集")
            return True
        ctx.log("[抖音] 重新取到的 Cookie 和之前一样，登录态可能真的失效了", "warn")
        return False

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        await self._cleanup_browser_fallback()

    # ---------------- 请求 ----------------
    async def _build_params(self, uri: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """补公共参数并算 a_bogus。顺序很重要：a_bogus 必须最后加。"""
        merged: Dict[str, Any] = dict(params)
        merged.update(COMMON_PARAMS)
        merged["webid"] = self._web_id
        if self._ms_token:
            merged["msToken"] = self._ms_token

        # ⚠️ 综合搜索接口**不能带 a_bogus**。
        # MediaCrawler 里这一行是显式排除的：
        #     if "/v1/web/general/search" not in uri:
        #         params["a_bogus"] = a_bogus
        # 带上之后抖音不会报错，而是回 200 + 空 data —— 表现就是
        # "搜索第 1 页无数据"，最难查的那种失败。
        if NO_SIGN_MARKER in uri:
            return merged

        query_string = urllib.parse.urlencode(merged)
        function = "sign_reply" if "/reply" in uri else "sign_datail"
        try:
            a_bogus = await get_js_runtime().call(
                SIGN_JS, function, [query_string, self._user_agent]
            )
        except JsRuntimeError as exc:
            raise RuntimeError(
                f"计算抖音 a_bogus 失败：{exc}。"
                f"确认 backend/libs/douyin.cjs 存在且本机装了 Node.js 18+。"
            ) from exc

        merged["a_bogus"] = a_bogus
        return merged

    #: 这些状态码基本可以确定是"登录态不行了"，而不是参数写错。
    #: 撞上它们值得重开一次浏览器把 Cookie 刷新掉再试一次。
    LOGIN_STATUS_CODES = {8, 2154}

    def _looks_like_login_issue(self, data: Dict[str, Any]) -> bool:
        if data.get("status_code") in self.LOGIN_STATUS_CODES:
            return True
        message = f"{data.get('status_msg') or ''}{data.get('message') or ''}"
        return any(word in message for word in ("登录", "login", "身份"))

    async def _get(self, uri: str, params: Dict[str, Any],
                   extra_headers: Optional[Dict[str, str]] = None,
                   *, ctx: Optional[CollectContext] = None,
                   _allow_refresh: bool = True) -> Dict[str, Any]:
        full_params = await self._build_params(uri, params)
        data = await self._client.get_json(
            f"{self.host}{uri}", params=full_params, headers=extra_headers
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"抖音接口返回了非预期结构：{str(data)[:200]}")
        # status_code 非 0 通常是风控或参数失效
        status = data.get("status_code")
        if status not in (None, 0):
            # 登录态过期是可以自己修的：重开一次浏览器刷 Cookie，然后重试一次。
            # 这就是「API 采集过程中发现失效就再次启动浏览器更新 cookie」。
            # 只给一次机会——刷完还不行说明账号真的废了，再刷也是白开浏览器。
            if _allow_refresh and ctx is not None and self._looks_like_login_issue(data):
                ctx.log(
                    f"[抖音] 接口返回 status_code={status}，判断是登录态失效，"
                    f"重新开一次浏览器刷新 Cookie", "warn",
                )
                if await self._refresh_login_state(ctx):
                    return await self._get(uri, params, extra_headers,
                                           ctx=ctx, _allow_refresh=False)
            raise RuntimeError(
                f"抖音接口报错：status_code={status}，"
                f"msg={data.get('status_msg') or data.get('message', '')}"
            )
        if not data:
            # 抖音被风控时经常回 200 + 空 JSON，不给任何错误码。
            # 不识别出来的话，上层只会看到"没数据"，完全查不下去。
            raise RuntimeError(self._blocked_hint(uri))
        return data

    async def _switch_to_human(self, ctx: CollectContext, keyword: str):
        """从接口切到拟人，接着把这个关键字采完。

        已经 yield 出去的作品不会丢也不会重：runner.add_work 按
        (scenic_id, channel, work_id) 去重，拟人重采到的同一条会记成 dup_works。
        """
        await self._init_browser_fallback(ctx)
        self._use_browser_fallback = True
        async for work in self._browser_collector.collect_by_keyword(ctx, keyword):
            yield work

    def _blocked_hint(self, uri: str) -> str:
        return (
            f"抖音接口 {uri} 返回了空响应（HTTP 200 但没有内容）。"
            f"这通常是风控：当前出口 IP 或账号被限流。可以试试："
            f"换一个代理 IP、降低采集频率、或在账号管理里重新导入一份新鲜的 Cookie。"
        )

    def _empty_result_hint(self, keyword: str, data: Dict[str, Any]) -> str:
        """搜索第一页就没结果时，把能帮上忙的线索都摆出来。

        接口没报错、只是 data 为空，这种"静默失败"最难查——
        用户看到的只有"采集到 0 条作品"，无从判断是关键字真的没内容，
        还是被风控了、还是参数少了。
        """
        clues = []
        if not self._ms_token:
            clues.append("msToken 缺失")
        if not self._verify_fp:
            clues.append("页面指纹 s_v_web_id 缺失，页面可能没初始化完")
        cookie_count = len([p for p in self._cookie_header.split(";") if p.strip()])
        if cookie_count < 20:
            clues.append(f"Cookie 只有 {cookie_count} 个，登录态可能不完整")

        detail = (
            f"接口 status_code={data.get('status_code')}，"
            f"顶层字段={sorted(data.keys())[:12]}，"
            f"has_more={data.get('has_more')}"
        )
        advice = (
            "；".join(clues) if clues
            else "参数看起来是齐的，多半是当前出口 IP 被抖音限流了"
        )
        return (
            f"[抖音] 关键字 [{keyword}] 第 1 页就没有结果。{detail}。"
            f"可能原因：{advice}。"
            f"换个代理 IP 或重新导入 Cookie 后再试；"
            f"也可以先用浏览器搜一下这个词确认确实有内容。"
        )

    @staticmethod
    def _filter_params(ctx: CollectContext) -> Dict[str, Any]:
        """把排序/时间窗翻译成抖音的 filter_selected。

        抖音把这两项打包成一个 JSON 串放在 filter_selected 里，
        并且**只要用了筛选，is_filter_search 就得置 1**，否则服务端不认。
        时间只认 0/1/7/180 这几档；用户填了自定义日期区间的话，
        这里不传，交给 runner 的本地时间窗过滤。
        """
        filters = ctx.search_filters
        sort_type = DY_SORT_TYPE.get(filters.sort, 0)
        publish_time = DY_PUBLISH_TIME.get(filters.publish_within, 0)
        if not sort_type and not publish_time:
            return {}
        return {
            "filter_selected": json.dumps(
                {"sort_type": str(sort_type), "publish_time": str(publish_time)},
                separators=(",", ":"),
            ),
            "is_filter_search": 1,
            "search_source": "tab_search",
        }

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
        from .douyin_browser import DouyinBrowserCollector
        self._browser_collector = DouyinBrowserCollector(self.config, self.proxy_manager)
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
            ctx.log("[抖音] 拟人采集器已就绪（任务选的是「拟人」）")
        else:
            ctx.log("[抖音] 接口采集拿不到数据，已切换到拟人模式", "warn")

    async def _cleanup_browser_fallback(self) -> None:
        """清理浏览器模拟采集器。"""
        if self._browser_collector is not None:
            await self._browser_collector.cleanup()
            self._browser_collector = None

    # ---------------- 关键字搜索 ----------------
    async def collect_by_keyword(
        self, ctx: CollectContext, keyword: str
    ) -> AsyncIterator[WorkItem]:
        # 「拟人」在 prepare 里就切好了（那样连取 Cookie 的浏览器都不用白开一次）。
        # 这里再兜一次是给"直接调采集器、没走 prepare"的路径用的。
        if not self._use_browser_fallback and self.human_only(ctx):
            ctx.log("[抖音] 任务选的是「拟人」，跳过接口直接用浏览器模拟真人采集")
            await self._init_browser_fallback(ctx)
            self._use_browser_fallback = True

        # 已经切到拟人了，直接用它
        if self._use_browser_fallback:
            async for work in self._browser_collector.collect_by_keyword(ctx, keyword):
                yield work
            return

        search_id = ""
        offset = 0
        emitted = 0
        seen: set[str] = set()

        # Referer 要带 aid，和 MediaCrawler 一致：
        # https://www.douyin.com/search/{kw}?aid=...&type=general
        referer = urllib.parse.quote(
            f"{self.host}/search/{keyword}"
            f"?aid={SEARCH_REFERER_AID}&type=general",
            safe=":/",
        )

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            params = {
                "search_channel": "aweme_general",
                "enable_history": "1",
                "keyword": keyword,
                "search_source": "tab_search",
                "query_correct_type": "1",
                "is_filter_search": "0",
                # MediaCrawler 固定带这个值；抖音只是用它标记搜索来源分组
                "from_group_id": SEARCH_FROM_GROUP_ID,
                "offset": offset,
                "count": SEARCH_PAGE_SIZE,
                "need_filter_settings": "1",
                "list_type": "multi",
                "search_id": search_id,
            }
            params.update(self._filter_params(ctx))
            try:
                data = await self._get(SEARCH_URI, params, {"Referer": referer}, ctx=ctx)
            except RuntimeError as exc:
                # 接口整个不通（风控回空、状态码报错）。混合模式下这正是
                # "没有数据了就换拟人"该发生的时刻——不换的话这一轮就白跑了。
                if self._use_browser_fallback or not self.may_switch_to_human(ctx):
                    raise
                ctx.log(f"[抖音] 接口采集失败（{exc}），改用拟人模式继续", "warn")
                try:
                    async for work in self._switch_to_human(ctx, keyword):
                        yield work
                except (LoginRequired, RuntimeError) as switch_exc:
                    # 拟人也起不来（没账号、profile 被占、浏览器起不来……）。
                    # 这时候报拟人的错会误导：真正的起因是接口那条路先断了，
                    # 用户看到"没有可用账号"会跑去加账号，其实加了也没用。
                    ctx.log(f"[抖音] 拟人模式也没能起来：{switch_exc}", "warn")
                    raise RuntimeError(f"{exc}；改用拟人模式也失败了：{switch_exc}") from switch_exc
                return

            entries = data.get("data") or []
            if not entries:
                page_no = offset // SEARCH_PAGE_SIZE + 1
                if page_no == 1 and emitted == 0:
                    # 第一页就空 = 出问题了，不能当成"采完了"悄悄结束。
                    # 「仅接口」的任务不许偷偷换路：接口坏了就要立刻暴露出来。
                    if not self._use_browser_fallback and self.may_switch_to_human(ctx):
                        ctx.log("[抖音] 接口搜索返回空，改用拟人模式继续", "warn")
                        try:
                            async for work in self._switch_to_human(ctx, keyword):
                                yield work
                            return
                        except LoginRequired:
                            # 拟人也起不来，回到接口那条错误上——那条更具体
                            ctx.log("[抖音] 拟人采集也不可用，报接口错误", "warn")
                            raise RuntimeError(self._empty_result_hint(keyword, data))
                    raise RuntimeError(self._empty_result_hint(keyword, data))
                ctx.log(f"[抖音] 关键字 [{keyword}] 第 {page_no} 页无数据，结束")
                break

            # 下一页要带上本页返回的 logid 作为 search_id
            search_id = (data.get("extra") or {}).get("logid", "") or search_id

            new_in_page = 0
            for entry in entries:
                aweme = entry.get("aweme_info")
                if not aweme:
                    # 合集类结果包在 aweme_mix_info.mix_items 里
                    mix_items = (entry.get("aweme_mix_info") or {}).get("mix_items") or []
                    aweme = mix_items[0] if mix_items else None
                if not aweme:
                    continue
                aweme_id = nz.to_text(aweme.get("aweme_id"))
                if not aweme_id or aweme_id in seen:
                    continue
                seen.add(aweme_id)
                new_in_page += 1

                work = self.safe_map(ctx, self._to_work, ctx, aweme,
                                     source_keyword=keyword)
                if work is None:
                    continue
                yield work
                emitted += 1
                if self.limit_reached(emitted, ctx.max_works):
                    break

            if new_in_page == 0:
                ctx.log(f"[抖音] 关键字 [{keyword}] 本页无新增，结束")
                break

            offset += SEARCH_PAGE_SIZE
            if not data.get("has_more", 1):
                ctx.log(f"[抖音] 关键字 [{keyword}] 接口报告没有更多了")
                break
            await self._client.sleep_interval()

    # ---------------- 主页采集 ----------------
    async def collect_by_creator(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[WorkItem]:
        # 拟人采集器目前只做了关键字搜索，没有"打开某个作者主页往下翻"这条路。
        # 静默返回 0 条会让人以为这个作者没作品，所以明说。
        if self._use_browser_fallback:
            raise RuntimeError(
                "[抖音] 拟人模式暂不支持「指定用户主页」采集，只支持关键字搜索。"
                "把任务的采集模式改成 API 或混合，或者改用关键字搜索。"
            )

        sec_user_id = self._parse_sec_user_id(target)
        max_cursor = ""
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            params = {
                "sec_user_id": sec_user_id,
                "count": POST_PAGE_SIZE,
                "max_cursor": max_cursor,
                "locate_query": "false",
                "publish_video_strategy_type": 2,
            }
            data = await self._get(USER_POST_URI, params)
            aweme_list = data.get("aweme_list") or []
            if not aweme_list:
                break

            for aweme in aweme_list:
                work = self.safe_map(ctx, self._to_work, ctx, aweme)
                if work is None:
                    continue
                yield work
                emitted += 1
                if self.limit_reached(emitted, ctx.max_works):
                    break

            if not data.get("has_more"):
                break
            max_cursor = data.get("max_cursor") or ""
            if not max_cursor:
                break
            await self._client.sleep_interval()

    @staticmethod
    def _parse_sec_user_id(target: CollectTarget) -> str:
        """从主页链接或纯 ID 里取 sec_user_id。"""
        import re

        raw = (target.value or target.url or "").strip()
        if raw.startswith("MS4wLjABAAAA") or ("douyin.com" not in raw and not raw.startswith("http")):
            return raw
        match = re.search(r"/user/([^/?#]+)", raw)
        if match:
            return match.group(1)
        raise ValueError(f"无法从这个值里解析出抖音 sec_user_id：{raw}")

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

        aweme_id = work.work_id
        cursor = 0
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_comments_per_work):
                break

            data = await self._get(COMMENT_URI, {
                "aweme_id": aweme_id,
                "cursor": cursor,
                "count": COMMENT_PAGE_SIZE,
                "item_type": 0,
            })
            comments = data.get("comments") or []
            if not comments:
                break

            for raw in comments:
                item = self.safe_map(ctx, self._to_comment, ctx, raw, aweme_id,
                                     depth=1, parent_id="", what="评论")
                if item is None:
                    continue
                yield item
                emitted += 1

                sub_total = nz.to_int(raw.get("reply_comment_total"))
                if (ctx.enable_sub_comments and ctx.max_comment_level >= 2
                        and sub_total > 0):
                    async for sub in self._collect_sub_comments(
                        ctx, aweme_id, item.comment_id
                    ):
                        yield sub
                        emitted += 1
                        if self.limit_reached(emitted, ctx.max_comments_per_work):
                            break

                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

            if not data.get("has_more"):
                break
            cursor = data.get("cursor") or 0
            await self._client.sleep_interval()

    async def _collect_sub_comments(
        self, ctx: CollectContext, aweme_id: str, root_comment_id: str
    ) -> AsyncIterator[CommentItem]:
        cursor = 0
        while True:
            ctx.raise_if_cancelled()
            data = await self._get(SUB_COMMENT_URI, {
                "comment_id": root_comment_id,
                "cursor": cursor,
                "count": COMMENT_PAGE_SIZE,
                "item_type": 0,
                "item_id": aweme_id,
            })
            comments = data.get("comments") or []
            if not comments:
                return
            for raw in comments:
                item = self.safe_map(
                    ctx, self._to_comment, ctx, raw, aweme_id, depth=2,
                    parent_id=nz.to_text(raw.get("reply_id")) or root_comment_id,
                    root_id=root_comment_id, what="评论",
                )
                if item is not None:
                    yield item
            if not data.get("has_more"):
                return
            cursor = data.get("cursor") or 0
            await self._client.sleep_interval()

    # ---------------- 创作者 ----------------
    async def fetch_author(
        self, ctx: CollectContext, author_id: str
    ) -> Optional[AuthorItem]:
        try:
            data = await self._get(USER_PROFILE_URI, {
                "sec_user_id": author_id,
                "publish_video_strategy_type": 2,
                "personal_center_strategy": 1,
            })
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[抖音] 取创作者 {author_id} 资料失败：{exc}", "warn")
            return None

        user = data.get("user") or {}
        if not user:
            return None
        return AuthorItem(
            channel=self.channel,
            author_id=nz.to_text(user.get("sec_uid") or user.get("uid")),
            author_name=nz.to_text(user.get("nickname")),
            avatar=self._first_url(user.get("avatar_larger") or user.get("avatar_thumb")),
            signature=nz.to_text(user.get("signature")),
            gender={1: "男", 2: "女"}.get(user.get("gender"), ""),
            location=nz.to_text(user.get("ip_location")),
            home_url=f"{self.host}/user/{user.get('sec_uid', '')}",
            fans_count=nz.to_int(user.get("follower_count")),
            follow_count=nz.to_int(user.get("following_count")),
            works_count=nz.to_int(user.get("aweme_count")),
            liked_count=nz.to_int(user.get("total_favorited")),
            extra_content=nz.to_json({"uid": user.get("uid"), "unique_id": user.get("unique_id")}),
            crawl_time=datetime.now(),
        ).finalize()

    # ---------------- 字段映射 ----------------
    def _to_work(self, ctx: CollectContext, aweme: Dict,
                 source_keyword: str = "") -> WorkItem:
        aweme_id = nz.to_text(aweme.get("aweme_id"))
        author = aweme.get("author") or {}
        stats = aweme.get("statistics") or {}
        desc = nz.to_text(aweme.get("desc"))

        # 话题标签在 text_extra 里，只取 hashtag 类型
        tags = [
            t.get("hashtag_name") for t in (aweme.get("text_extra") or [])
            if t.get("hashtag_name")
        ]

        images = nz.collect_urls(aweme.get("images"))
        videos = nz.collect_urls((aweme.get("video") or {}).get("play_addr"))
        cover = self._first_url((aweme.get("video") or {}).get("cover"))

        extra = {
            "aweme_type": aweme.get("aweme_type"),
            "cover_url": cover,
            "music": ((aweme.get("music") or {}).get("play_url") or {}).get("uri"),
            "duration": (aweme.get("video") or {}).get("duration"),
            "uid": author.get("uid"),
            "sec_uid": author.get("sec_uid"),
        }

        return self.new_work(
            ctx,
            work_id=aweme_id,
            work_url=f"{self.host}/video/{aweme_id}",
            author_id=nz.to_text(author.get("sec_uid") or author.get("uid")),
            author_name=nz.to_text(author.get("nickname")),
            title=desc,
            description=desc,
            label=",".join(tags) if tags else None,
            image_list=nz.to_json_list(images),
            video_list=nz.to_json_list(videos),
            likes=nz.to_int(stats.get("digg_count")),
            collection_cnt=nz.to_int(stats.get("collect_count")),
            comment_cnt=nz.to_int(stats.get("comment_count")),
            shares=nz.to_int(stats.get("share_count")),
            location=nz.to_text(aweme.get("ip_attribution") or aweme.get("region")),
            publish_time=nz.to_datetime(aweme.get("create_time")),
            crawl_time=datetime.now(),
            source_keyword=source_keyword,
            extra_content=nz.to_json({k: v for k, v in extra.items() if v not in (None, "")}),
        ).finalize()

    def _to_comment(self, ctx: CollectContext, raw: Dict, aweme_id: str,
                    depth: int, parent_id: str,
                    root_id: str = "") -> Optional[CommentItem]:
        comment_id = nz.to_text(raw.get("cid"))
        if not comment_id:
            return None
        user = raw.get("user") or {}
        images = nz.collect_urls(raw.get("image_list"))

        return self.new_comment(
            ctx,
            comment_id=comment_id,
            work_id=aweme_id,
            depth=depth,
            comment_parent_id=parent_id if parent_id not in ("0", "") else "",
            root_comment_id=root_id or comment_id,
            commenter_id=nz.to_text(user.get("sec_uid") or user.get("uid")),
            commenter_name=nz.to_text(user.get("nickname")),
            content=nz.to_text(raw.get("text")),
            likes=nz.to_int(raw.get("digg_count")),
            sub_comment_count=nz.to_int(raw.get("reply_comment_total")),
            location=nz.to_text(raw.get("ip_label")),
            image_list=nz.to_json_list(images),
            publish_time=nz.to_datetime(raw.get("create_time")),
            crawl_time=datetime.now(),
            extra_content=nz.to_json({
                "uid": user.get("uid"),
                "reply_id": raw.get("reply_id"),
                "reply_to_reply_id": raw.get("reply_to_reply_id"),
            }),
        ).finalize()

    @staticmethod
    def _first_url(node: Any) -> str:
        urls = nz.collect_urls(node)
        return urls[0] if urls else ""
