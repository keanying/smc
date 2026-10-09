"""小红书采集器。

签名走纯 Python 算法（xhshow），不需要浏览器参与，是四个社媒平台里最省资源的一个：
    X-S / X-T / x-S-Common / X-B3-Traceid  由 uri + 请求体 + Cookie 算出

有两个容易踩的坑，这里都处理了：

  1. GET 请求的查询串必须用「逗号不编码」的方式拼接。
     签名是对这个字符串算的，交给 httpx 自动编码会把 , 变成 %2C，
     签名立刻对不上，接口返回 406/300012。所以 URL 手工拼好再发。

  2. 笔记详情、评论接口都要带 xsec_token，而这个 token 只能从
     搜索结果或主页列表里拿到，且和 note_id 一一对应、有时效。
     所以搜索时就把 token 存进作品的 extra_content，采评论时再取出来用。

接口清单：
  搜索      POST /api/sns/web/v1/search/notes   {keyword, page, page_size, search_id, sort, note_type}
  笔记详情  POST /api/sns/web/v1/feed           {source_note_id, xsec_source, xsec_token, ...}
  一级评论  GET  /api/sns/web/v2/comment/page   {note_id, cursor, top_comment_id, image_formats, xsec_token}
  二级评论  GET  /api/sns/web/v2/comment/sub/page
  主页笔记  GET  /api/sns/web/v1/user_posted    {num, cursor, user_id, image_formats, xsec_token, xsec_source}
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote

import httpx

from ..core.constants import CHANNEL_XHS
from ..browser.specs import missing_login_cookies
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

API_HOST = "https://edith.xiaohongshu.com"
WEB_HOST = "https://www.xiaohongshu.com"

SEARCH_URI = "/api/sns/web/v1/search/notes"
FEED_URI = "/api/sns/web/v1/feed"
COMMENT_URI = "/api/sns/web/v2/comment/page"
SUB_COMMENT_URI = "/api/sns/web/v2/comment/sub/page"
USER_POSTED_URI = "/api/sns/web/v1/user_posted"

SEARCH_PAGE_SIZE = 20
#: 我们的排序标识 -> 小红书 sort 取值
#: ⚠️ 只有这三档。MediaCrawler 的 SearchSortType 枚举、RedCrack 的调用、
#: xhshow 里都只有这三个值；曾经写过的 comment_descending / collect_descending
#: 在任何一份参考实现里都不存在，是凭"应该有吧"加上去的——
#: 服务端不认的排序值会被静默忽略，用户以为按评论排了，其实是综合排序。
XHS_SORT = {
    "general": "general",
    "latest": "time_descending",
    "most_like": "popularity_descending",
}

#: 需要带 x-rap-param 的接口。RedCrack 的 XRAP_ENCRYPT_URL 名单和
#: xhshow 的 sign_headers(x_rap=...) 说明互相印证：搜索、feed、主页作品都要。
#: 不带的话搜索会直接失败，而且报错里看不出是签名少了东西。
XRAP_URIS = {
    "/api/sns/web/v1/search/notes",
    "/api/sns/web/v1/feed",
    "/api/sns/web/v1/user_posted",
    "/api/sns/web/v1/homefeed",
}

#: 这些接口从 2026-03 起拒绝 XYS_ 格式的签名，回 406，必须用 XYW_
XYW_URIS = {"/api/sns/web/v1/user_posted"}
#: 时间窗 -> filters 里 filter_note_time 的标签（服务端只认这几个中文词）
XHS_TIME_TAG = {
    "unlimited": "不限",
    "day": "一天内",
    "week": "一周内",
    "half_year": "半年内",
}
SUB_COMMENT_PAGE_SIZE = 10

#: 连续多少条笔记取详情失败，就把整个关键字切到浏览器模拟采集。
#: 单条失败是常态（笔记被删、xsec_token 过期、偶发限流），为它开浏览器不划算；
#: 但**连续**失败说明是接口级问题（签名被拒 / 账号被风控 / 出口 IP 被拉黑），
#: 这时候再用 API 打下去只是白发请求 + 继续拉高风控计数。
#: 只统计"连续"：中间成功一条就清零，避免零散失败慢慢累积到阈值。
DETAIL_FAIL_STREAK_LIMIT = 5

SITE_HEADERS = {
    "Origin": WEB_HOST,
    "Referer": f"{WEB_HOST}/",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
}

# 接口返回的业务码
IP_ERROR_CODE = 300012
SECURITY_LIMIT_CODE = 300011
NOTE_NOT_FOUND_CODE = -510000

BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def base36encode(number: int, alphabet: str = BASE36_ALPHABET) -> str:
    if number == 0:
        return alphabet[0]
    sign = "-" if number < 0 else ""
    number = abs(number)
    result = ""
    while number:
        number, remainder = divmod(number, len(alphabet))
        result = alphabet[remainder] + result
    return sign + result


def generate_search_id() -> str:
    """搜索会话 ID，算法对齐 MediaCrawler 的 get_search_id。"""
    e = int(time.time() * 1000) << 64
    t = int(random.uniform(0, 2147483646))
    return base36encode(e + t)


def build_query_string(params: Dict[str, Any]) -> str:
    """按浏览器习惯拼查询串：逗号保持原样不编码。

    签名是对这个串算的，编码方式必须和签名时完全一致。
    """
    parts = []
    for key, value in params.items():
        text = "" if value is None else str(value)
        parts.append(f"{key}={quote(text, safe=',')}")
    return "&".join(parts)


@CollectorRegistry.register
class XhsCollector(BaseCollector):
    channel = CHANNEL_XHS
    supports_works = True
    needs_login = True
    integrated = True
    supported_targets = ("keyword", "creator", "detail")

    api_host = API_HOST
    web_host = WEB_HOST

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._client = None
        self._cookie = ""
        self._signer = None
        #: note_id -> xsec_token，采评论时要用
        self._tokens: Dict[str, str] = {}
        # 浏览器模拟采集器（API 被风控时降级使用）
        self._browser_collector = None
        self._use_browser_fallback = False
        #: 连续取详情失败的条数，成功一条就清零（见 DETAIL_FAIL_STREAK_LIMIT）
        self._detail_fail_streak = 0
        #: 浏览器降级试过但起不来（没登录态等），别每条笔记都再试一遍
        self._fallback_unavailable = False

    # ---------------- 生命周期 ----------------
    async def prepare(self, ctx: CollectContext) -> None:
        manager = ctx.params.get("browser_manager")
        cookie = ctx.params.get("cookie_header", "")
        if not cookie and manager is not None:
            _, cookie = await manager.get_cookie_header(
                self.channel, ctx.account_name,
                group=ctx.params.get("account_group", ""),
                slot_token=ctx.params.get("browser_slot_token", ""),
            )
        if not cookie:
            raise LoginRequired("小红书采集需要登录态，请先在账号管理里登录一个小红书账号")

        missing = missing_login_cookies(self.channel, cookie)
        if missing:
            raise LoginRequired(
                f"账号 [{self.channel}/{ctx.account_name or '默认'}] 的 Cookie 里没有登录凭据"
                f"（需要其中之一：{'、'.join(missing)}）。"
                f"请在账号管理里重新登录，或用「导入 Cookie」把浏览器导出的 Cookie 贴进来。"
            )
        self._cookie = cookie

        try:
            from xhshow import Xhshow
        except ImportError as exc:
            raise RuntimeError(
                "缺少 xhshow 依赖（小红书 X-S 签名的纯算实现）。"
                "执行 pip install xhshow 后重试。"
            ) from exc
        self._signer = Xhshow()

        headers = dict(SITE_HEADERS)
        headers["Cookie"] = cookie
        self._client = self.make_client(base_headers=headers)
        await self._client.__aenter__()
        ctx.log("[小红书] 采集客户端就绪（纯算签名，无需浏览器）")

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._tokens.clear()
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
        from .xhs_browser import XhsBrowserCollector
        self._browser_collector = XhsBrowserCollector(self.config, self.proxy_manager)
        await self._browser_collector.prepare(ctx)
        if self.human_only(ctx):
            ctx.log("[小红书] 拟人采集器已就绪（任务选的是「拟人」）")
        else:
            ctx.log("[小红书] API 采集被风控，降级为浏览器模拟采集", "warn")

    async def _cleanup_browser_fallback(self) -> None:
        """清理浏览器模拟采集器。"""
        if self._browser_collector is not None:
            await self._browser_collector.cleanup()
            self._browser_collector = None

    # ---------------- 请求 ----------------
    # xhshow 返回的键名 -> 实际发出去的请求头名（大小写按浏览器实际发送的来）
    _HEADER_NAMES = {
        "x-s": "X-S",
        "x-t": "X-T",
        "x-s-common": "X-S-Common",
        "x-b3-traceid": "X-B3-Traceid",
        "x-mns": "X-Mns",
        "x-xray-traceid": "X-Xray-Traceid",
        "xy-direction": "xy-direction",
        "x-rap-param": "x-rap-param",
    }

    def _sign_headers(self, uri: str, data: Any, method: str) -> Dict[str, str]:
        """算签名头。

        xhshow 除了 x-s / x-t / x-s-common / x-b3-traceid，还会给出
        x-mns、x-xray-traceid、xy-direction —— 真实浏览器这几个也会发，
        所以全部带上，少发反而更容易被识别成非浏览器请求。
        """
        path = uri.split("?", 1)[0]
        extra = {
            "x_rap": path in XRAP_URIS,
            "sign_format": "xyw" if path in XYW_URIS else "xys",
        }
        if method.upper() == "POST":
            signs = self._signer.sign_headers_post(
                uri=uri, cookies=self._cookie,
                payload=data if isinstance(data, dict) else {},
                **extra,
            )
        else:
            signs = self._signer.sign_headers_get(
                uri=uri, cookies=self._cookie,
                params=data if isinstance(data, dict) else {},
                **extra,
            )
        headers: Dict[str, str] = {}
        for key, value in (signs or {}).items():
            if value in (None, ""):
                continue
            headers[self._HEADER_NAMES.get(key, key)] = str(value)
        return headers

    def _check(self, data: Dict[str, Any], uri: str) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise RuntimeError(f"小红书接口 {uri} 返回非预期结构：{str(data)[:200]}")
        # ⚠️ 不能写成 `data.get("code") in (0, None)`：返回体里没有 code 字段时
        # None in (0, None) 为真，失败响应会被当成成功，然后在解析阶段
        # 报一个和真实原因毫不相干的错。
        if data.get("success") is True or data.get("code") == 0:
            return data.get("data") if "data" in data else data

        code = data.get("code")
        message = data.get("msg") or data.get("message") or ""
        if code == IP_ERROR_CODE:
            raise RuntimeError(
                f"小红书判定当前出口 IP 异常（code={code}）。"
                f"开启代理或更换出口 IP 后重试。原始信息：{message}"
            )
        if code == SECURITY_LIMIT_CODE:
            raise RuntimeError(
                f"小红书触发风控限流（code={code}）。"
                f"建议降低采集频率（crawl.request_interval_seconds）或更换账号。"
            )
        if code == NOTE_NOT_FOUND_CODE:
            raise RuntimeError(f"笔记不存在或已删除：{message}")
        raise RuntimeError(f"小红书接口 {uri} 报错：code={code}，msg={message}")

    #: 这三个状态码是小红书**仅有的**明确信号，翻译过来排查成本差一个数量级
    _STATUS_HINTS = {
        406: (
            "小红书拒绝了签名格式（HTTP 406）。这个接口大概率只认 XYW_ 格式，"
            "或者缺了 x-rap-param。检查 XYW_URIS / XRAP_URIS 里有没有把它列进去。"
        ),
        461: (
            "小红书要求验证（HTTP 461）。一般是扫码验证，"
            "请在账号管理里重新登录这个账号。"
        ),
        471: (
            "小红书要求验证（HTTP 471）。一般是滑块验证，"
            "换一个出口 IP、降低采集频率，然后在账号管理里重新登录。"
        ),
    }

    #: 这两个码表示**平台要求人来验证**，不是普通的采集错误。
    #: 抛 LoginRequired 才能走 runner 的「发通知 + 暂停等人处理」那条路；
    #: 抛 RuntimeError 的话只会被记成一条"采集错误"，而且会被下面
    #: 取详情的 `except Exception` 吞掉，连错误通知都发不出来。
    _NEED_HUMAN_STATUS = (461, 471)

    async def _request(
        self, method: str, uri: str, url: str, headers: Dict[str, str], **kwargs
    ) -> Dict[str, Any]:
        try:
            data = await self._client.request_json(method, url, headers=headers, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            hint = self._STATUS_HINTS.get(status)
            if hint and status in self._NEED_HUMAN_STATUS:
                raise LoginRequired(f"{hint}（接口 {uri}）") from exc
            if hint:
                # 不要让这三个码落进通用的 raise_for_status 报错里。
                # 「HTTP 471」四个字对用户毫无意义，而它恰恰是最需要动作的信号。
                raise RuntimeError(f"{hint}（接口 {uri}）") from exc
            raise
        return self._check(data, uri)

    async def _post(self, uri: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = self._sign_headers(uri, payload, "POST")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return await self._request(
            "POST", uri, f"{self.api_host}{uri}", headers, data=body.encode("utf-8"),
        )

    async def _get(self, uri: str, params: Dict[str, Any]) -> Dict[str, Any]:
        headers = self._sign_headers(uri, params, "GET")
        # 手工拼 URL：不能让 httpx 再编码一遍，否则和签名对不上
        url = f"{self.api_host}{uri}?{build_query_string(params)}"
        return await self._request("GET", uri, url, headers)

    @staticmethod
    def _filter_payload(ctx: CollectContext) -> Dict[str, Any]:
        """排序与时间筛选。

        小红书有两处要写：顶层的 `sort`，以及 `filters` 数组里的
        sort_type / filter_note_time 两项。只写一处服务端不认，
        实测两处都得给，且时间只认「一天内 / 一周内 / 半年内」这几个中文词。
        """
        filters = ctx.search_filters
        sort = XHS_SORT.get(filters.sort, "general")
        time_tag = XHS_TIME_TAG.get(filters.publish_within, "不限")
        return {
            "sort": sort,
            "filters": [
                {"tags": [sort], "type": "sort_type"},
                {"tags": ["不限"], "type": "filter_note_type"},
                {"tags": [time_tag], "type": "filter_note_time"},
                {"tags": ["不限"], "type": "filter_note_range"},
                {"tags": ["不限"], "type": "filter_pos_distance"},
            ],
        }

    # ---------------- 关键字搜索 ----------------
    async def collect_by_keyword(
        self, ctx: CollectContext, keyword: str
    ) -> AsyncIterator[WorkItem]:
        # 建任务时选了「拟人」→ 一开始就走浏览器模拟，不浪费一轮必然被拦的接口请求
        if not self._use_browser_fallback and self.human_only(ctx):
            ctx.log("[小红书] 任务选的是「拟人」，跳过接口直接用浏览器模拟真人采集")
            await self._init_browser_fallback(ctx)
            self._use_browser_fallback = True

        # 如果已经降级到浏览器模拟，直接用浏览器采集
        if self._use_browser_fallback:
            async for work in self._browser_collector.collect_by_keyword(ctx, keyword):
                yield work
            return

        search_id = generate_search_id()
        page = 1
        emitted = 0
        seen: set[str] = set()

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            try:
                data = await self._post(SEARCH_URI, {
                    "keyword": keyword,
                    "page": page,
                    "page_size": SEARCH_PAGE_SIZE,
                    "search_id": search_id,
                    "note_type": 0,
                    **self._filter_payload(ctx),
                })
            except RuntimeError as exc:
                # API 被风控/限流时，降级为浏览器模拟采集
                if "IP 异常" in str(exc) or "风控" in str(exc) or "限流" in str(exc):
                    # 「仅接口」的任务不许偷偷换路：接口坏了就要立刻暴露出来
                    if not self._use_browser_fallback and self.may_switch_to_human(ctx):
                        ctx.log("[小红书] API 搜索被风控，尝试降级为浏览器模拟采集", "warn")
                        try:
                            await self._init_browser_fallback(ctx)
                            self._use_browser_fallback = True
                            async for work in self._browser_collector.collect_by_keyword(ctx, keyword):
                                yield work
                            return
                        except LoginRequired:
                            # 浏览器降级也不可用，回退到 API 错误
                            ctx.log("[小红书] 浏览器模拟采集也不可用，报 API 错误", "warn")
                            raise RuntimeError(
                                f"小红书 API 被风控且浏览器模拟采集不可用：{exc}"
                            ) from exc
                raise

            items = data.get("items") or []
            if not items:
                if page == 1 and emitted == 0:
                    raise self.empty_first_page(
                        keyword, data, clues=self.cookie_clues(self._cookie),
                    )
                ctx.log(f"[小红书] 关键字 [{keyword}] 第 {page} 页无数据，结束")
                break

            new_in_page = 0
            for item in items:
                # 搜索结果里混着广告/用户卡片，只要笔记
                if item.get("model_type") not in (None, "note"):
                    continue
                note = item.get("note_card") or {}
                note_id = nz.to_text(note.get("note_id") or item.get("id"))
                if not note_id or note_id in seen:
                    continue
                seen.add(note_id)
                new_in_page += 1

                xsec_token = nz.to_text(item.get("xsec_token") or note.get("xsec_token"))
                if xsec_token:
                    self._tokens[note_id] = xsec_token

                note = await self._with_detail(ctx, note, note_id, xsec_token)
                work = self.safe_map(ctx, self._to_work, ctx, note, note_id,
                                     xsec_token, source_keyword=keyword)
                if work is not None:
                    yield work
                    emitted += 1
                    if self.limit_reached(emitted, ctx.max_works):
                        break

                # 连续 N 条详情失败 = 接口级故障，整体切到浏览器模拟。
                # 已经 yield 出去的作品不会丢也不会重：runner.add_work 按
                # (scenic_id, channel, work_id) 去重，浏览器重采到的同一条
                # 会被记成 dup_works，不会重复入库。
                if self._detail_degraded() and self.may_switch_to_human(ctx):
                    ctx.log(
                        f"[小红书] 连续 {self._detail_fail_streak} 条笔记取详情失败，"
                        f"判定为接口级故障，切换到浏览器模拟采集", "warn",
                    )
                    try:
                        await self._init_browser_fallback(ctx)
                    except LoginRequired as exc:
                        # 起不来就继续用 API：字段少一些，总比整个关键字断掉强。
                        # 置位后不再重试，否则每条笔记都要再尝试开一次浏览器。
                        self._fallback_unavailable = True
                        ctx.log(
                            f"[小红书] 浏览器模拟采集不可用（{exc}），"
                            f"继续用 API，只是详情字段会缺失", "warn",
                        )
                    else:
                        self._use_browser_fallback = True
                        async for browser_work in (
                            self._browser_collector.collect_by_keyword(ctx, keyword)
                        ):
                            yield browser_work
                        return

            if new_in_page == 0:
                ctx.log(f"[小红书] 关键字 [{keyword}] 本页无新增，结束")
                break
            if not data.get("has_more", True):
                break

            page += 1
            await self._client.sleep_interval()

    def _detail_degraded(self) -> bool:
        """连续失败是否已经到阈值。阈值可在 platforms.xiaohongshu 下覆盖。"""
        if self._use_browser_fallback or self._fallback_unavailable:
            return False
        limit = int(self.platform_config.get(
            "detail_fail_streak_limit", DETAIL_FAIL_STREAK_LIMIT,
        ) or DETAIL_FAIL_STREAK_LIMIT)
        return limit > 0 and self._detail_fail_streak >= limit

    async def _with_detail(
        self, ctx: CollectContext, note: Dict, note_id: str, xsec_token: str
    ) -> Dict:
        """用 /feed 把搜索卡片补成完整笔记。

        ⚠️ 这一步不是"锦上添花"，是**时间筛选能不能生效的前提**。
        搜索结果的 note_card 里**没有 time 字段**，只有标题、封面、互动数。
        少了它，work.publish_time 恒为 None，于是：
          - runner 的时间窗兜底 `in_window(None)` 恒真（设计上就是"解析不出来就放行"）
          - 「按最新排序翻过下界就提前停」的 `older_than_window(None)` 恒假
        两条一起失效 = 小红书的时间范围筛选**完全没生效**，
        而且不报错、不告警，用户只会觉得"怎么采回来一堆很老的笔记"。
        顺带补齐的还有 desc / 评论数 / IP 属地 / 话题标签。

        取不到详情就退回用卡片本身：宁可少几个字段，也不能因为详情接口
        抽风把整条作品丢掉。
        """
        # ⚠️ /api/sns/web/v1/feed 只认 POST，而且 image_formats 必须是数组、
        # extra 必须是对象——不是 JSON 字符串。这三点在 MediaCrawler
        # (media_platform/xhs/client.py get_note_by_id) 和 RedCrack
        # (request/web/apis/note.py note_detail) 里完全一致。
        # 这里曾经写成 GET + 逗号串 + JSON 字符串，服务端一律拒绝，
        # 表现就是每条笔记都「取详情失败」，而搜索和评论（本来就是 GET）全都正常。
        payload = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": "pc_search",
            "xsec_token": xsec_token,
        }

        # 没有 token 就别发了：feed 接口缺 xsec_token 必失败，
        # 白白多打一次接口还会拉高风控计数。
        # ⚠️ 这种失败**不计入**连续失败计数：它是这条笔记自己的数据问题，
        # 不是接口坏了，拿它去触发整体降级会误判。
        if not xsec_token:
            ctx.log(
                f"[小红书] 笔记 {note_id} 搜索结果里没有 xsec_token，跳过详情，"
                f"只用搜索卡片的字段（时间筛选对这条不生效）", "warn",
            )
            return note

        try:
            data = await self._post(FEED_URI, payload)
        except LoginRequired:
            # ⚠️ 不能被下面那个兜底 except 吞掉。
            #    平台要求验证 / 登录态失效是**需要人处理**的事，
            #    吞成 warn 的话表现就是"每条笔记都少字段"，
            #    而真正的原因（账号掉了）一条通知都不会发。
            raise
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 这里原来是 logger.debug + 一句不带原因的 ctx.log，
            # 结果就是"每条都失败但看不出为什么"。原因必须带出来。
            logger.warning("[小红书] 笔记 %s 取详情失败：%s", note_id, exc)
            # 详情接口被风控/限流时，换出口 IP 再试一次，不要直接放弃
            if "IP 异常" in str(exc) or "风控" in str(exc) or "限流" in str(exc):
                logger.warning("[小红书] 笔记 %s 详情接口被风控，换出口 IP 重试", note_id)
                await self._client.rotate(f"小红书详情接口风控：{exc}")
                try:
                    data = await self._post(FEED_URI, payload)
                except Exception as retry_exc:  # noqa: BLE001
                    logger.warning(
                        "[小红书] 笔记 %s 换 IP 后仍失败：%s", note_id, retry_exc
                    )
                    ctx.log(
                        f"[小红书] 笔记 {note_id} 取详情失败（换 IP 后仍失败）："
                        f"{retry_exc}，只用搜索卡片的字段", "warn",
                    )
                    self._detail_fail_streak += 1
                    return note
            else:
                ctx.log(
                    f"[小红书] 笔记 {note_id} 取详情失败：{exc}，"
                    f"只用搜索卡片的字段", "warn",
                )
                self._detail_fail_streak += 1
                return note

        # 走到这里说明详情接口是通的，连续失败计数清零
        self._detail_fail_streak = 0

        items = (data or {}).get("items") or []
        detail = (items[0] or {}).get("note_card") if items else None
        if not isinstance(detail, dict) or not detail:
            return note
        # 详情为主、卡片兜底：卡片里的 xsec_token 之类详情不一定有
        merged = dict(note)
        merged.update({k: v for k, v in detail.items() if v not in (None, "", [], {})})
        return merged

    # ---------------- 主页采集 ----------------
    async def collect_by_creator(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[WorkItem]:
        # 拟人采集器目前只做了关键字搜索，没有"打开某个作者主页往下翻"这条路。
        # 静默返回 0 条会让人以为这个作者没作品，所以明说。
        if self._use_browser_fallback:
            raise RuntimeError(
                "[小红书] 拟人模式暂不支持「指定用户主页」采集，只支持关键字搜索。"
                "把任务的采集模式改成 API 或混合，或者改用关键字搜索。"
            )

        user_id, url_token = self._parse_user(target)
        cursor = ""
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            data = await self._get(USER_POSTED_URI, {
                "num": 30,
                "cursor": cursor,
                "user_id": user_id,
                "image_formats": "jpg,webp,avif",
                "xsec_token": url_token,
                "xsec_source": "pc_feed",
            })
            notes = data.get("notes") or []
            if not notes:
                break

            for note in notes:
                note_id = nz.to_text(note.get("note_id") or note.get("id"))
                if not note_id:
                    continue
                token = nz.to_text(note.get("xsec_token")) or url_token
                if token:
                    self._tokens[note_id] = token
                work = self.safe_map(ctx, self._to_work, ctx, note, note_id, token)
                if work is None:
                    continue
                yield work
                emitted += 1
                if self.limit_reached(emitted, ctx.max_works):
                    break

            if not data.get("has_more"):
                break
            cursor = nz.to_text(data.get("cursor"))
            if not cursor:
                break
            await self._client.sleep_interval()

    @staticmethod
    def _parse_user(target: CollectTarget) -> tuple[str, str]:
        """返回 (user_id, xsec_token)。主页链接里常带着 token，一并取出来。"""
        raw = (target.value or target.url or "").strip()
        token = nz.to_text(target.extra.get("xsec_token"))
        if raw.startswith("http"):
            match = re.search(r"/user/profile/([0-9a-zA-Z]+)", raw)
            if not match:
                raise ValueError(f"无法从这个链接里解析出小红书 user_id：{raw}")
            user_id = match.group(1)
            token_match = re.search(r"xsec_token=([^&#]+)", raw)
            if token_match:
                token = token_match.group(1)
            return user_id, token
        return raw, token

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

        note_id = work.work_id
        xsec_token = self._token_for(work)
        if not xsec_token:
            ctx.log(f"[小红书] 笔记 {note_id} 没有 xsec_token，跳过评论采集", "warn")
            return

        cursor = ""
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_comments_per_work):
                break

            data = await self._get(COMMENT_URI, {
                "note_id": note_id,
                "cursor": cursor,
                "top_comment_id": "",
                "image_formats": "jpg,webp,avif",
                "xsec_token": xsec_token,
            })
            comments = data.get("comments") or []
            if not comments:
                break

            for raw in comments:
                item = self.safe_map(ctx, self._to_comment, ctx, raw, note_id,
                                     depth=1, parent_id="", what="评论")
                if item is None:
                    continue
                yield item
                emitted += 1

                # 一级评论里常常已经内联了前几条子评论，先用掉不用额外请求
                inline_subs = raw.get("sub_comments") or []
                for sub_raw in inline_subs:
                    if not (ctx.enable_sub_comments and ctx.max_comment_level >= 2):
                        break
                    sub = self.safe_map(
                        ctx, self._to_comment, ctx, sub_raw, note_id, depth=2,
                        parent_id=item.comment_id, root_id=item.comment_id, what="评论",
                    )
                    if sub is not None:
                        yield sub
                        emitted += 1

                sub_total = nz.to_int(raw.get("sub_comment_count"))
                if (ctx.enable_sub_comments and ctx.max_comment_level >= 2
                        and sub_total > len(inline_subs)):
                    async for sub in self._collect_sub_comments(
                        ctx, note_id, item.comment_id, xsec_token,
                        start_cursor=nz.to_text(raw.get("sub_comment_cursor")),
                    ):
                        yield sub
                        emitted += 1
                        if self.limit_reached(emitted, ctx.max_comments_per_work):
                            break

                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

            if not data.get("has_more"):
                break
            cursor = nz.to_text(data.get("cursor"))
            if not cursor:
                break
            await self._client.sleep_interval()

    async def _collect_sub_comments(
        self, ctx: CollectContext, note_id: str, root_comment_id: str,
        xsec_token: str, start_cursor: str = "",
    ) -> AsyncIterator[CommentItem]:
        cursor = start_cursor
        while True:
            ctx.raise_if_cancelled()
            data = await self._get(SUB_COMMENT_URI, {
                "note_id": note_id,
                "root_comment_id": root_comment_id,
                "num": str(SUB_COMMENT_PAGE_SIZE),
                "cursor": cursor,
                "image_formats": "jpg,webp,avif",
                "top_comment_id": "",
                "xsec_token": xsec_token,
            })
            comments = data.get("comments") or []
            if not comments:
                return
            for raw in comments:
                item = self.safe_map(
                    ctx, self._to_comment, ctx, raw, note_id, depth=2,
                    parent_id=root_comment_id, root_id=root_comment_id, what="评论",
                )
                if item is not None:
                    yield item
            if not data.get("has_more"):
                return
            cursor = nz.to_text(data.get("cursor"))
            if not cursor:
                return
            await self._client.sleep_interval()

    def _token_for(self, work: WorkItem) -> str:
        if work.work_id in self._tokens:
            return self._tokens[work.work_id]
        # 作品可能来自上一轮任务，token 存在 extra_content 里
        try:
            extra = json.loads(work.extra_content or "{}")
            return nz.to_text(extra.get("xsec_token"))
        except (ValueError, TypeError):
            return ""

    # ---------------- 字段映射 ----------------
    def _to_work(self, ctx: CollectContext, note: Dict, note_id: str,
                 xsec_token: str, source_keyword: str = "") -> WorkItem:
        user = note.get("user") or {}
        interact = note.get("interact_info") or {}

        image_list = note.get("image_list") or []
        images = nz.collect_urls([
            {"url": img.get("url_default") or img.get("url")} for img in image_list
        ])
        videos = self._video_urls(note)
        tags = [
            t.get("name") for t in (note.get("tag_list") or [])
            if t.get("name") and t.get("type") == "topic"
        ]
        desc = nz.to_text(note.get("desc"))
        # 搜索卡片里标题叫 display_title，详情里才叫 title——只读 title 会得到空标题
        title = (
            nz.to_text(note.get("title"))
            or nz.to_text(note.get("display_title"))
            or desc[:255]
        )

        extra = {
            "note_type": note.get("type"),
            "xsec_token": xsec_token,     # 采评论要用，必须留着
            "last_update_time": note.get("last_update_time"),
            "user_id": user.get("user_id"),
        }

        return self.new_work(
            ctx,
            work_id=note_id,
            work_url=(
                f"{self.web_host}/explore/{note_id}"
                f"?xsec_token={xsec_token}&xsec_source=pc_search"
            ),
            author_id=nz.to_text(user.get("user_id")),
            author_name=nz.to_text(user.get("nickname") or user.get("nick_name")),
            title=title,
            description=desc,
            label=",".join(tags) if tags else None,
            image_list=nz.to_json_list(images),
            video_list=nz.to_json_list(videos),
            likes=nz.to_int(interact.get("liked_count")),
            collection_cnt=nz.to_int(interact.get("collected_count")),
            comment_cnt=nz.to_int(interact.get("comment_count")),
            shares=nz.to_int(interact.get("share_count")),
            location=nz.to_text(note.get("ip_location")),
            publish_time=(nz.to_datetime(note.get("time"))
                          or self._corner_publish_time(note)),
            crawl_time=datetime.now(),
            source_keyword=source_keyword,
            extra_content=nz.to_json({k: v for k, v in extra.items() if v not in (None, "")}),
        ).finalize()

    @staticmethod
    def _corner_publish_time(note: Dict):
        """搜索卡片的发布时间藏在 corner_tag_info 里，是**人话**不是时间戳。

            "corner_tag_info": [{"type": "publish_time", "text": "2天前"}]

        实测见过四种写法：「1小时前」「2天前」「昨天」「08-28」。
        一周以内是相对时间，更早的就只给月日、**不给年份**。

        为什么必须用它：搜索响应的 note_card 里**没有** time 字段，
        完整的发布时间只有 /v1/feed 才给。拿不到 feed 的时候
        （接口没触发、超时、被限流），publish_time 就是 None，
        然后被 runner 的时间窗过滤整片丢掉——现象是"搜得到却不入库"。
        有这个兜底至少精确到天，足够时间窗用了。
        """
        for tag in note.get("corner_tag_info") or []:
            if not isinstance(tag, dict):
                continue
            if tag.get("type") != "publish_time":
                continue
            when = nz.to_datetime(nz.to_text(tag.get("text")))
            if when is not None:
                return when
        return None

    @staticmethod
    def _video_urls(note: Dict) -> List[str]:
        """视频地址藏在 video.media.stream 下按编码分的几个数组里。"""
        video = note.get("video") or {}
        stream = ((video.get("media") or {}).get("stream")) or {}
        candidates: List[Any] = []
        for codec in ("h264", "h265", "av1", "h266"):
            candidates.extend(stream.get(codec) or [])
        urls = nz.collect_urls([
            {"url": c.get("master_url") or c.get("backup_urls")} for c in candidates
        ])
        if not urls:
            urls = nz.collect_urls(video.get("url"), (video.get("consumer") or {}).get("origin_video_key"))
        return urls

    def _to_comment(self, ctx: CollectContext, raw: Dict, note_id: str,
                    depth: int, parent_id: str,
                    root_id: str = "") -> Optional[CommentItem]:
        comment_id = nz.to_text(raw.get("id"))
        if not comment_id:
            return None
        user = raw.get("user_info") or {}
        pictures = nz.collect_urls([
            {"url": p.get("url_default") or p.get("url")}
            for p in (raw.get("pictures") or [])
        ])
        target = raw.get("target_comment") or {}

        return self.new_comment(
            ctx,
            comment_id=comment_id,
            work_id=note_id,
            depth=depth,
            comment_parent_id=parent_id or nz.to_text(target.get("id")),
            root_comment_id=root_id or comment_id,
            commenter_id=nz.to_text(user.get("user_id")),
            commenter_name=nz.to_text(user.get("nickname")),
            content=nz.to_text(raw.get("content")),
            likes=nz.to_int(raw.get("like_count")),
            sub_comment_count=nz.to_int(raw.get("sub_comment_count")),
            location=nz.to_text(raw.get("ip_location")),
            image_list=nz.to_json_list(pictures),
            publish_time=nz.to_datetime(raw.get("create_time")),
            crawl_time=datetime.now(),
            extra_content=nz.to_json({
                "avatar": user.get("image"),
                "at_users": raw.get("at_users"),
                "target_comment_id": target.get("id"),
            }),
        ).finalize()

    async def fetch_author(
        self, ctx: CollectContext, author_id: str
    ) -> Optional[AuthorItem]:
        """小红书的创作者资料要解析主页 HTML 里的 __INITIAL_STATE__，
        本期先不额外请求；作者基本信息已随笔记一起入库。"""
        return None
