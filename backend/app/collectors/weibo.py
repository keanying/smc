"""微博采集器（**PC 网页端**）。

## 为什么只走 PC 端

用户明确要求「走 web 端就行，不用考虑 m.weibo.cn」，而且这也是他那套跑通的
`opinion-hub-all` 采集链路的做法——`config.yaml` 里微博的
`base_url: https://weibo.com`、`search_url: https://s.weibo.com/weibo?q={keyword}`，
Cookie 从 weibo.com 采，采集也全走 PC 接口。

曾经走过 m.weibo.cn，代价是：

  1. 登录在 weibo.com（`.weibo.com`），采集在 m.weibo.cn（`.weibo.cn`），
     **两个不同的注册域、两套独立会话**。移动站的会话靠 SSO 换过来，
     有自己的有效期，换不过来就被判成"登录态已失效"——
     用户的原话是"我其实已经登录的呢，老出现重新登录"。
  2. 移动端容器接口挑剔得多：往 containerid 里多塞一个 timescope，
     「宝珠洞索道」这类小众词就退化成一张 card_type=4 的空提示卡。

PC 端没有这两个问题：登录、Cookie、采集在同一个域上。

## 接口清单（全部照 weibo_crawler.py 的 pc 分支）

  关键字搜索  GET https://s.weibo.com/weibo      q/page/rd/tw/Refer（热门加 xsort=hot）
              GET https://s.weibo.com/realtime   实时（按时间倒序）
              → 返回 **HTML**，从里面抠出 mid，再逐条取详情
  作品详情    GET https://weibo.com/ajax/statuses/show?id={mid}
  主页作品    GET https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0
  作者资料    GET https://weibo.com/ajax/profile/info?uid={uid}
  评论        GET https://weibo.com/ajax/statuses/buildComments
              一级 flow=1 fetch_level=0 id=作品ID；二级 flow=0 fetch_level=1 id=评论ID
              游标是 max_id，返回 0/空表示到底了

## 两个容易踩的地方

  1. 搜索结果是 HTML，不是 JSON。三种 mid 的写法都要认（见 `_extract_mids`）。
  2. 正文优先 `longTextContent` → `text_raw` → `text`。只读 `text` 的话，
     长微博会被截断成「...全文」。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from ..browser.specs import missing_login_cookies
from ..core.constants import CHANNEL_WEIBO
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

HOST = "https://weibo.com"
SEARCH_HOST = "https://s.weibo.com"

SHOW_URI = "/ajax/statuses/show"
COMMENTS_URI = "/ajax/statuses/buildComments"
MYMBLOG_URI = "/ajax/statuses/mymblog"
PROFILE_URI = "/ajax/profile/info"

COMMENT_PAGE_SIZE = 20
CREATOR_PAGE_SIZE = 20

#: 排序 -> s.weibo.com 的路径（热门是 weibo + xsort=hot）
WEIBO_SORT_PATH = {
    "general": "weibo",
    "latest": "realtime",
    "most_like": "weibo",
}

SITE_HEADERS = {
    "Referer": f"{SEARCH_HOST}/",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    # ⚠️ 对齐参考项目 weibo_opinion 的 PC 模式请求头：
    # 桌面 Chrome UA + 完整的 sec-ch-ua 头，移动端指纹会被 s.weibo.com 风控。
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

#: 微博图片 CDN，只有 pic_ids 没有 pic_infos 时用来兜底拼地址
PIC_HOST = "https://wx1.sinaimg.cn"

TAG_RE = re.compile(r"<[^>]+>")
#: 搜索结果页里 mid 的三种写法，按优先级试
MID_PATTERNS = (
    re.compile(r'mid="(\d+)"'),
    re.compile(r"/detail/(\d+)"),
    re.compile(r'action-data="[^"]*mid=(\d+)'),
)


def strip_html(text: Any) -> str:
    """去掉微博正文里的 HTML 标签，保留纯文本。"""
    raw = nz.to_text(text)
    if not raw:
        return ""
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    return TAG_RE.sub("", raw).strip()


def _pic_urls(mblog: Dict[str, Any]) -> List[str]:
    """挑出图片地址，四种来源按优先级降级。

    ⚠️ PC 端和移动端给的形状完全不同，而且元素**不一定是字典**：

        pic_ids + pic_infos   PC 端标准形状，pic_infos 是以 pid 为键的字典
        pic_ids               只有 id，自己拼 large 地址
        pics                  移动端形状，元素可能是字典、也可能直接是字符串
        pic_urls              老接口，只给缩略图，把 /thumbnail/ 换成 /large/

    只处理第一种的代价是很多微博一张图都没有；而写死 `p.get(...)`
    碰到字符串元素会抛 AttributeError，那个异常曾经把**整个平台的采集**带走
    （日志里只有一句 "weibo 采集失败：'str' object has no attribute 'get'"）。
    """
    urls: List[str] = []

    pic_ids = [nz.to_text(p) for p in (mblog.get("pic_ids") or [])]
    pic_infos = mblog.get("pic_infos")
    if pic_ids and isinstance(pic_infos, dict):
        for pid in pic_ids:
            info = pic_infos.get(pid)
            if not isinstance(info, dict):
                continue
            for key in ("original", "largest", "large", "mw2000", "bmiddle"):
                node = info.get(key)
                url = node.get("url") if isinstance(node, dict) else None
                if url:
                    urls.append(nz.to_text(url))
                    break
    if not urls and pic_ids:
        urls = [f"{PIC_HOST}/large/{pid}.jpg" for pid in pic_ids if pid]

    if not urls:
        for pic in mblog.get("pics") or []:
            if isinstance(pic, dict):
                large = pic.get("large")
                url = (large or {}).get("url") if isinstance(large, dict) else None
                url = url or pic.get("url")
                if url:
                    urls.append(nz.to_text(url))
            elif isinstance(pic, str) and pic.strip():
                text = pic.strip()
                urls.append(text if text.startswith(("http", "//"))
                            else f"{PIC_HOST}/large/{text}")

    if not urls:
        for pic in mblog.get("pic_urls") or []:
            thumb = pic.get("thumbnail_pic") if isinstance(pic, dict) else None
            if thumb:
                urls.append(nz.to_text(thumb).replace("/thumbnail/", "/large/"))

    return urls


def _video_urls(mblog: Dict[str, Any]) -> List[str]:
    """视频地址，按清晰度优先级取一个。"""
    page_info = mblog.get("page_info") or {}
    if page_info.get("type") == "video":
        media = page_info.get("media_info") or {}
        for key in ("stream_url_hd", "stream_url", "mp4_720p_mp4", "mp4_hd_url"):
            if media.get(key):
                return [nz.to_text(media[key])]
        for item in media.get("playback_list") or []:
            url = (item.get("play_info") or {}).get("url")
            if url:
                return [nz.to_text(url)]
    # 图文混排里的视频
    for item in (mblog.get("mix_media_info") or {}).get("items") or []:
        if item.get("type") != "video":
            continue
        media = (item.get("data") or {}).get("media_info") or {}
        for key in ("stream_url_hd", "stream_url"):
            if media.get(key):
                return [nz.to_text(media[key])]
    return []


@CollectorRegistry.register
class WeiboCollector(BaseCollector):
    channel = CHANNEL_WEIBO
    supports_works = True
    needs_login = True
    integrated = True
    supported_targets = ("keyword", "creator")

    host = HOST
    search_host = SEARCH_HOST

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        self._client = None
        self._cookie = ""

    # ---------------- 生命周期 ----------------
    async def prepare(self, ctx: CollectContext) -> None:
        manager = ctx.params.get("browser_manager")
        cookie = ctx.params.get("cookie_header", "")
        if not cookie and manager is not None:
            # 只要 weibo.com 这个域的 Cookie
            _, cookie = await manager.get_cookie_header(
                self.channel, ctx.account_name, for_host="weibo.com",
                group=ctx.params.get("account_group", ""),
                slot_token=ctx.params.get("browser_slot_token", ""),
            )
        if not cookie:
            raise LoginRequired("微博采集需要登录态，请先在账号管理里登录一个微博账号")
        missing = missing_login_cookies(self.channel, cookie)
        if missing:
            raise LoginRequired(
                f"账号 [{self.channel}/{ctx.account_name or '默认'}] 的 Cookie 里没有登录凭据"
                f"（需要其中之一：{'、'.join(missing)}）。"
                f"请在账号管理里重新登录，或用「导入 Cookie」把浏览器导出的 Cookie 贴进来。"
            )

        self._cookie = cookie
        headers = dict(SITE_HEADERS)
        headers["Cookie"] = cookie
        # PC 端的 ajax 接口要带 XSRF token，值就在 Cookie 里
        token = _cookie_value(cookie, "XSRF-TOKEN")
        if token:
            headers["X-XSRF-TOKEN"] = token
        self._client = self.make_client(base_headers=headers)
        await self._client.__aenter__()
        await self._assert_logged_in(ctx)
        ctx.log("[微博] 采集客户端就绪（PC 端，无需签名）")

    async def cleanup(self) -> None:
        self.login_verified = False
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _assert_logged_in(self, ctx: CollectContext) -> None:
        """开采前不再预判登录态，由真实业务请求确认。

        ⚠️ 用户参考项目 weibo_opinion 就是这么做的：
        启动时只用 Cookie 里的 SUB 判定"有没有登录凭据"，
        然后直接发真实搜索请求，由响应里的 ok=-100 / errno=100006 / 重定向
        来确认登录态是否失效。这样不会因为预判接口抽风而误判。
        """
        self.login_verified = True
        ctx.log("[微博] 登录态判定：由真实业务请求确认（对齐 weibo_opinion 策略）")

    # ---------------- 请求 ----------------
    async def _get(self, url: str, params: Optional[Dict[str, Any]] = None,
                   ctx: Optional[CollectContext] = None) -> Dict[str, Any]:
        """请求一个 ajax 接口，**原样返回整个响应体**。

        ⚠️ 不要在这里"顺手把 data 剥出来"。buildComments 的返回是
        `{"ok":1, "data":[...], "max_id":777666}` —— `data` 和 `max_id`
        是**平级**的。剥掉外层就把游标丢了，表现是评论永远只采第一页
        （而且不报错，看着一切正常）。各接口的外壳形状本来就不一样：

            /ajax/statuses/show          微博本身在顶层，没有 ok/data 外壳
            /ajax/profile/info           {"ok":1,"data":{"user":…}}
            /ajax/statuses/buildComments {"ok":1,"data":[…],"max_id":…}
            /ajax/statuses/mymblog       {"ok":1,"data":{"list":[…]}}

        由调用方各取所需，这里只负责错误判定。
        """
        full = self._client.build_url(url, params)
        data = await self._client.get_json(url, params=params)
        if not isinstance(data, dict):
            raise RuntimeError(f"微博接口 {full} 返回非预期结构：{str(data)[:200]}")

        ok = data.get("ok")
        message = nz.to_text(data.get("msg") or data.get("message"))
        # ok=-100 / errno=100006 是微博的"未登录"，取自用户脚本的 _is_cookie_expired
        if ok == -100 or data.get("errno") in ("100006", 100006):
            if ctx is not None:
                await self._mark_expired_and_raise(ctx, "微博返回「未登录」")
            raise LoginRequired("微博返回「未登录」，登录态已失效")
        if ok == 0:
            # 「请先登录」这类文案同样是登录问题，不能当成普通业务错误——
            # 当成业务错误的话，登录态自检会以为"只是接口抽风"而放行。
            if "登录" in message:
                if ctx is not None:
                    await self._mark_expired_and_raise(ctx, f"微博接口回了「{message}」")
                raise LoginRequired(f"微博接口回了「{message}」，登录态已失效")
            raise RuntimeError(f"微博接口 {full} 报错：{message or data}")
        return data

    async def _mark_expired_and_raise(self, ctx: CollectContext, detail: str) -> None:
        """标记当前账号登录态失效，并抛出 LoginRequired 让上层轮换账号。

        对齐用户参考项目 weibo_opinion 的 _mark_dead + _rotate：
        真实业务请求确认失效后，标记当前账号，由上层决定是否换下一个。
        """
        manager = ctx.params.get("browser_manager")
        if manager is not None and ctx.account_name:
            await manager.accounts.mark_expired(
                self.channel, ctx.account_name, detail
            )
        raise LoginRequired(
            f"账号 [{self.channel}/{ctx.account_name or '默认'}] {detail}，"
            f"登录态已失效。请在账号管理里重新登录，或用「Cookie」导入一份新的。"
        )

    async def _get_text(self, url: str, params: Optional[Dict[str, Any]] = None,
                        ctx: Optional[CollectContext] = None) -> str:
        """搜索页返回的是 HTML，单独走一条路。"""
        response = await self._client.request("GET", url, params=params, expect_json=False)
        text = response.text or ""
        # 被重定向到登录页：标记账号失效并抛出
        if ctx is not None and ("passport.weibo.com" in text or "login.sina" in text):
            await self._mark_expired_and_raise(ctx, "搜索页被重定向到登录页")
        return text

    # ---------------- 关键字搜索 ----------------
    async def collect_by_keyword(
        self, ctx: CollectContext, keyword: str
    ) -> AsyncIterator[WorkItem]:
        filters = ctx.search_filters
        path = WEIBO_SORT_PATH.get(filters.sort, "weibo")
        page = 1
        emitted = 0
        seen: set[str] = set()

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            params: Dict[str, Any] = {
                "q": keyword, "page": page,
                "rd": path, "tw": path, "Refer": f"weibo_{path}",
            }
            if filters.sort == "most_like":
                params["xsort"] = "hot"

            html = await self._get_text(f"{self.search_host}/{path}", params, ctx=ctx)
            # 搜索页被重定向到登录页：标记账号失效并抛出
            if "passport.weibo.com" in html or "login.sina" in html:
                await self._mark_expired_and_raise(ctx, "搜索页被重定向到登录页")
            mids = [m for m in self._extract_mids(html) if m not in seen]
            if not mids:
                if page == 1 and emitted == 0:
                    self.handle_empty_first_page(
                        ctx, keyword, None, clues=self._html_clues(html),
                    )
                    return
                ctx.log(f"[微博] 关键字 [{keyword}] 第 {page} 页没有新内容，结束")
                break

            for mid in mids:
                ctx.raise_if_cancelled()
                seen.add(mid)
                mblog = await self._fetch_detail(ctx, mid)
                if not mblog:
                    continue
                work = self.safe_map(ctx, self._to_work, ctx, mblog,
                                     source_keyword=keyword)
                if work is None:
                    continue
                yield work
                emitted += 1
                if self.limit_reached(emitted, ctx.max_works):
                    break
                await self._client.sleep_interval()

            page += 1
            await self._client.sleep_interval()

    @staticmethod
    def _extract_mids(html: str) -> List[str]:
        """从搜索结果页里抠出微博 ID，保持页面顺序、去重。

        三种写法都要认：不同的卡片模板用的不一样，只认第一种会漏掉一部分。
        """
        for pattern in MID_PATTERNS:
            found = pattern.findall(html or "")
            if found:
                return list(dict.fromkeys(found))
        return []

    @staticmethod
    def _html_clues(html: str) -> List[str]:
        text = html or ""
        if not text.strip():
            return ["搜索页返回了空内容，多半是这个出口 IP 被限流了"]
        if "抱歉，未找到" in text or "没有找到相关结果" in text:
            return ["搜索页明确显示「未找到相关结果」，这个词确实没有内容"]
        if "登录" in text and "passport" in text:
            return ["搜索页被重定向到了登录页，登录态已失效"]
        if "passport.weibo.com" in text or "login.sina" in text:
            return ["搜索页被重定向到了登录页，登录态已失效"]
        return [f"搜索页拿到了 {len(text)} 字符，但没解析出任何微博 ID"]

    async def _fetch_detail(self, ctx: CollectContext, mid: str) -> Optional[Dict]:
        """取一条微博的详情。取不到就跳过这一条，不影响整批。"""
        try:
            data = await self._get(f"{self.host}{SHOW_URI}", {"id": mid}, ctx=ctx)
        except LoginRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[微博] 作品 {mid} 详情取不到，跳过（{exc}）", "warn")
            return None
        if not isinstance(data, dict) or not (data.get("id") or data.get("mid")):
            return None
        return data

    # ---------------- 主页采集 ----------------
    async def collect_by_creator(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[WorkItem]:
        uid = self._parse_uid(target)
        page = 1
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_works):
                break

            payload = await self._get(f"{self.host}{MYMBLOG_URI}", {
                "uid": uid, "page": page, "feature": 0,
            }, ctx=ctx)
            items = (payload.get("data") or {}).get("list") or []
            if not items:
                break

            for mblog in items:
                work = self.safe_map(ctx, self._to_work, ctx, mblog)
                if work is None:
                    continue
                yield work
                emitted += 1
                if self.limit_reached(emitted, ctx.max_works):
                    break

            page += 1
            await self._client.sleep_interval()

    @staticmethod
    def _parse_uid(target: CollectTarget) -> str:
        value = nz.to_text(target.value)
        match = re.search(r"/u/(\d+)", value) or re.search(r"(\d{6,})", value)
        return match.group(1) if match else value

    # ---------------- 评论 ----------------
    async def collect_comments(
        self, ctx: CollectContext, work: WorkItem
    ) -> AsyncIterator[CommentItem]:
        note_id = work.work_id
        max_id: Any = None
        emitted = 0

        while True:
            ctx.raise_if_cancelled()
            if self.limit_reached(emitted, ctx.max_comments_per_work):
                break

            batch, max_id = await self._fetch_comments(
                ctx, note_id, {"id": note_id, "flow": 1, "fetch_level": 0}, max_id,
            )
            if not batch:
                break

            for raw in batch:
                item = self.safe_map(ctx, self._to_comment, ctx, raw, note_id,
                                     depth=1, parent_id="", what="评论")
                if item is None:
                    continue
                yield item
                emitted += 1

                if (ctx.enable_sub_comments and ctx.max_comment_level >= 2
                        and nz.to_int(raw.get("total_number")) > 0):
                    async for sub in self._collect_sub_comments(
                        ctx, note_id, item.comment_id
                    ):
                        yield sub
                        emitted += 1
                        if self.limit_reached(emitted, ctx.max_comments_per_work):
                            break

                if self.limit_reached(emitted, ctx.max_comments_per_work):
                    break

            if not max_id:
                break
            await self._client.sleep_interval()

    async def _collect_sub_comments(
        self, ctx: CollectContext, note_id: str, root_comment_id: str
    ) -> AsyncIterator[CommentItem]:
        max_id: Any = None
        while True:
            ctx.raise_if_cancelled()
            batch, max_id = await self._fetch_comments(
                ctx, note_id,
                {"id": root_comment_id, "flow": 0, "fetch_level": 1},
                max_id,
            )
            if not batch:
                return
            for raw in batch:
                item = self.safe_map(
                    ctx, self._to_comment, ctx, raw, note_id, depth=2,
                    parent_id=root_comment_id, root_id=root_comment_id, what="评论",
                )
                if item is not None:
                    yield item
            if not max_id:
                return
            await self._client.sleep_interval()

    async def _fetch_comments(
        self, ctx: CollectContext, note_id: str,
        base_params: Dict[str, Any], max_id: Any,
    ) -> tuple[List[Dict], Any]:
        """取一页评论，返回 (列表, 下一页游标)。

        游标是 max_id，返回 0 或空就是到底了。
        """
        params = {
            **base_params,
            "is_reload": 1, "is_show_bulletin": 2, "is_mix": 0,
            "count": COMMENT_PAGE_SIZE,
        }
        if max_id:
            params["max_id"] = max_id
        try:
            data = await self._get(f"{self.host}{COMMENTS_URI}", params, ctx=ctx)
        except LoginRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            # 评论关了、作品删了都会走到这里，跳过这条不影响整体
            ctx.log(f"[微博] 作品 {note_id} 评论不可用：{exc}", "warn")
            return [], None

        # data 有时是列表，有时是 {"data": [...]}，两种都见过
        comments = data.get("data")
        if isinstance(comments, dict):
            comments = comments.get("data")
        if not isinstance(comments, list):
            comments = []
        # ⚠️ max_id 和 data 是平级的，在**顶层**
        next_max_id = data.get("max_id")
        if not next_max_id or next_max_id == 0:
            next_max_id = None
        return comments, next_max_id

    # ---------------- 作者 ----------------
    async def fetch_author(
        self, ctx: CollectContext, author_id: str
    ) -> Optional[AuthorItem]:
        try:
            data = await self._get(f"{self.host}{PROFILE_URI}", {"uid": author_id}, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[微博] 作者 %s 资料取不到：%s", author_id, exc)
            return None
        user = ((data or {}).get("data") or {}).get("user") or {}
        if not user:
            return None
        return AuthorItem(
            channel=self.channel,
            author_id=nz.to_text(user.get("id") or author_id),
            author_name=nz.to_text(user.get("screen_name")),
            avatar=nz.to_text(user.get("avatar_hd") or user.get("profile_image_url")),
            signature=nz.to_text(user.get("description")),
            gender=_gender(user),
            location=nz.to_text(user.get("location")),
            home_url=f"{self.host}/u/{user.get('id') or author_id}",
            fans_count=nz.to_int(user.get("followers_count")),
            follow_count=nz.to_int(user.get("friends_count")),
            works_count=nz.to_int(user.get("statuses_count")),
            crawl_time=datetime.now(),
        )

    # ---------------- 字段映射 ----------------
    def _to_work(self, ctx: CollectContext, mblog: Dict,
                 source_keyword: str = "") -> Optional[WorkItem]:
        note_id = nz.to_text(mblog.get("mid") or mblog.get("id"))
        if not note_id:
            return None
        user = mblog.get("user") or {}
        # ⚠️ 顺序不能改：只读 text 的话长微博会被截成「…全文」
        raw_text = (mblog.get("longTextContent") or mblog.get("text_raw")
                    or mblog.get("text") or "")
        text = strip_html(raw_text)
        tags = re.findall(r"#([^#\s]{1,30})#", nz.to_text(raw_text))

        extra = {
            "mid": mblog.get("mid"),
            "source": mblog.get("source"),
            "is_long_text": mblog.get("isLongText"),
            "page_info_type": (mblog.get("page_info") or {}).get("type"),
            "uid": user.get("id"),
        }

        return self.new_work(
            ctx,
            work_id=note_id,
            work_url=f"{self.host}/detail/{note_id}",
            author_id=nz.to_text(user.get("id")),
            author_name=nz.to_text(user.get("screen_name") or user.get("name")),
            title=text[:500],
            description=text,
            label=",".join(tags) if tags else None,
            image_list=nz.to_json_list(nz.collect_urls(_pic_urls(mblog))),
            video_list=nz.to_json_list(nz.collect_urls(_video_urls(mblog))),
            likes=nz.to_int(mblog.get("attitudes_count")),
            comment_cnt=nz.to_int(mblog.get("comments_count")),
            shares=nz.to_int(mblog.get("reposts_count")),
            location=nz.to_text(mblog.get("region_name")).replace("发布于 ", ""),
            publish_time=nz.to_datetime(mblog.get("created_at")),
            crawl_time=datetime.now(),
            source_keyword=source_keyword,
            extra_content=nz.to_json({k: v for k, v in extra.items() if v not in (None, "")}),
        )

    def _to_comment(self, ctx: CollectContext, raw: Dict, note_id: str,
                    depth: int = 1, parent_id: str = "",
                    root_id: str = "") -> Optional[CommentItem]:
        comment_id = nz.to_text(raw.get("id"))
        if not comment_id:
            return None
        user = raw.get("user") or {}
        content = strip_html(raw.get("text_raw") or raw.get("text"))
        # IP 属地藏在 source 里，形如「来自北京」
        source = nz.to_text(raw.get("source"))
        location = source.replace("来自", "").strip() if source else ""

        if not parent_id:
            reply = raw.get("reply_comment")
            if isinstance(reply, dict) and reply.get("id"):
                parent_id = nz.to_text(reply["id"])

        return self.new_comment(
            ctx,
            comment_id=comment_id,
            work_id=note_id,
            depth=depth,
            comment_parent_id=parent_id,
            root_comment_id=root_id or parent_id or comment_id,
            commenter_id=nz.to_text(user.get("id")),
            commenter_name=nz.to_text(user.get("screen_name") or user.get("name")),
            content=content,
            likes=nz.to_int(raw.get("like_counts") or raw.get("like_count")),
            sub_comment_count=nz.to_int(raw.get("total_number")),
            location=location,
            publish_time=nz.to_datetime(raw.get("created_at")),
            crawl_time=datetime.now(),
        )


def _cookie_value(cookie_header: str, name: str) -> str:
    for part in (cookie_header or "").split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip() == name:
            return value.strip()
    return ""


def _gender(user: Dict[str, Any]) -> str:
    return {"m": "男", "f": "女"}.get(nz.to_text(user.get("gender")).lower(), "")
