"""快手浏览器模拟采集器：从 network 拦截获取作品和评论。

用户描述的真人动线（2026-09-05）——比小红书简单，**没有筛选**这一步：

    进首页 → 输入关键字 → 点搜索 → 点作品 → **关掉联播** → 点评论
    → 滚评论 → 关掉回到列表 → 滚作品列表 → 下一条

拦截的 API（顺序敏感）：
  - /rest/v/photo/comment/sublist  子评论（必须在 comment/list 之前，
                                   因为 sublist 的 URL 里含 "comment/list" 子串）
  - /rest/v/photo/comment/list     一级评论
  - /rest/v/search/feed            搜索
  - /rest/v/profile/feed           主页作品

────────────────────────────────────────────────────────────────
DOM 结构：**已确认的**部分（2026-09-05 record_page.py 实测）
────────────────────────────────────────────────────────────────
搜索结果页 URL
    https://www.kuaishou.com/search/{关键字}?source=NewReco

搜索框（首页和结果页顶栏是同一个）
    header.navbar div.search-container div.search input.input
    placeholder="搜索你感兴趣的内容"
搜索按钮
    header.navbar div.search-container div.search div.search-text   文字「搜索」

结果页的 Tab
    span.tab-item（「视频」/「用户」），选中的带 .tab-item-active

作品卡片（一屏 18 张）
    div.video-list > div.cards > div.card-container > div.photo-card
      ├ div.cover > div.aspect-div > img.cover-img   （alt 就是文案）
      ├ div.caption                                  文案
      └ div.info > span.user.desktop-user > span.name  作者
    卡片上还有 data-like-count="⭐ 262"

⚠️ **卡片上没有作品 id**——和小红书的 data-note-id 不一样。
   所以 photo_id 只能从拦到的 /rest/v/search/feed 响应里取，
   卡片按**位置**点（和抖音那边同一套做法）。

────────────────────────────────────────────────────────────────
接口响应的真实形状（2026-09-05 用户抓包，**不是推测**）
────────────────────────────────────────────────────────────────
搜索 /rest/v/search/feed
    {"result":1, "pcursor":"1", "feeds":[
        {"type":1,
         "tags":[{"name":"蓟州旅游","type":1}, ...],      ← 话题在这儿
         "photo":{"id":"3xhan8fvtsxax8s", "caption":"...",
                  "timestamp":1779718149823,             ← 毫秒
                  "likeCount":427, "viewCount":34046,
                  "coverUrl":"...", "photoUrls":[{"cdn":..,"url":..}],
                  "photoH265Urls":[...]},                ← 视频地址（不是 photoUrl）
         "author":{"id":"...","name":"..."},
         "comment":{"us_c":0}}]}                         ← **评论数在这儿永远是 0**

一级评论 /rest/v/photo/comment/list
    {"result":1, "commentCountV2":305, "pcursorV2":"1170632227652",
     "rootCommentsV2":[{"comment_id":1174060219763, "author_id":"...",
                        "author_name":"...", "content":"...",
                        "timestamp":1788542450634, "likeCount":0,
                        "commentCount":0, "hasSubComments":false,
                        "reply_to":"0"}]}

子评论 /rest/v/photo/comment/sublist
    请求 {"photoId":"...","pcursor":"","rootCommentId":1164081691000}
    {"result":1, "subCommentsV2":[...], "pcursorV2":"no_more"}

⚠️ 三个响应都是**平铺**的，评论**不在** body["data"] 里。
   照 data 去找 = 接口拦到了也解析成 0 条，日志显示
   「打开评论区后 8 秒没有评论到货」，看着像页面没点开。
⚠️ 作品的评论总数**只有评论接口给得出**（commentCountV2）；
   搜索接口的 comment.us_c 恒为 0。翻完评论要回填到作品行。

────────────────────────────────────────────────────────────────
两个实跑才暴露的坑
────────────────────────────────────────────────────────────────
1. 「联播」开关刚打开视频时是**锁着**的：
       <span role="switch" aria-disabled="true"
             class="toggle-switch is-active is-disabled size-small">
   Playwright 认 aria-disabled，会等到超时报 element is not enabled。
   它所在那块叫 hover-tip——**鼠标悬停上去才解锁**。
2. 搜索必须**在搜索框里打字再点「搜索」**，不能拼 /search/{关键字} 跳过去。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Dict, List

from ..core.constants import CHANNEL_KUAISHOU
from ..core.logging import get_logger
from ..utils import normalize as nz
from .base import CollectContext, CommentItem, WorkItem
from .browser_capture import BrowserCaptureCollector

logger = get_logger(__name__)


class KuaishouBrowserCollector(BrowserCaptureCollector):
    """快手浏览器模拟采集器。"""

    channel = CHANNEL_KUAISHOU
    #: ✅ 已接入。三个接口路径都由**真实抓包确认**（2026-09-05，用户提供）：
    #:   /rest/v/search/feed           搜索结果（POST {"keyword","page","pcursor"}）
    #:   /rest/v/photo/comment/list    一级评论（POST {"photoId","pcursor"}）
    #:   /rest/v/photo/comment/sublist 子评论  （POST {"photoId","rootCommentId"}）
    #: 而且实跑已经从搜索接口拿到过真实作品（作者、发布时间、点赞数都对）。
    integrated = True

    #: 借用 KuaishouCollector._to_work 时它会用 self.host 拼 work_url
    #: （f"{self.host}/short-video/{photo_id}"）。拟人版不继承接口版，
    #: 不定义的话搜索/点击/评论全干完了才在字段映射那一步炸 AttributeError。
    #: 和小红书 web_host、抖音 host 是同一类坑。
    host = "https://www.kuaishou.com"

    #: ↓ 以下选择器**已经实测确认**（2026-09-05 录制）
    SEARCH_URL = "https://www.kuaishou.com/search/{keyword}?source=NewReco"
    SEARCH_INPUT_SELECTOR = "header.navbar div.search-container input.input"
    SEARCH_BUTTON_SELECTOR = "header.navbar div.search-container div.search-text"
    #: 结果页的「视频」「用户」Tab，选中的带 .tab-item-active
    TAB_SELECTOR = "span.tab-item"
    #: 作品卡片。⚠️ 卡片上**没有**作品 id，只能按位置点，
    #: id 从拦到的 /rest/v/search/feed 响应里取
    CARD_SELECTOR = "div.video-list div.card-container div.photo-card"
    CARD_CAPTION_SELECTOR = "div.caption"
    CARD_AUTHOR_SELECTOR = "div.info span.user span.name"

    @property
    def _target_url_patterns(self) -> List[Dict[str, str]]:
        return [
            # 注意顺序：sublist 必须在 list 之前，因为 sublist URL 包含 "comment/list" 子串
            {"url_contains": "/rest/v/photo/comment/sublist", "type": "kuaishou_sub_comment"},
            {"url_contains": "/rest/v/photo/comment/list", "type": "kuaishou_comment"},
            {"url_contains": "/rest/v/search/feed", "type": "kuaishou_search"},
            {"url_contains": "/rest/v/profile/feed", "type": "kuaishou_profile"},
        ]

    @property
    def _search_url_template(self) -> str:
        # 录制实测的真实地址（旧的 /search/video?searchKey= 已经不是这个形状了）
        return "https://www.kuaishou.com/search/{keyword}?source=NewReco"

    @property
    def _work_url_template(self) -> str:
        return "https://www.kuaishou.com/short-video/{work_id}"

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        #: 联播开关只需要关一次，关过就别再点了（再点等于又打开）
        self._autoplay_off = False
        #: 搜索接口返回的作品顺序，和 DOM 里卡片的顺序一致，用来按位置点卡片
        self._order: List[str] = []
        #: 当前打开的是哪条作品
        self._open_work_id = ""
        #: photo_id -> 评论总数。搜索接口里**没有**这个数
        #: （feed["comment"] 只有 {"us_c": 0}），只有评论接口的
        #: commentCountV2 才是真的。实测：不取它的话作品行的"评论数"
        #: 永远写 0，和评论表里几百条对不上。
        self._comment_counts: Dict[str, int] = {}
        #: 联播开关连续几次是「锁着」的。锁着时点不动（Playwright 会报
        #: "element is not enabled"），试几次还不行就别每条都白等 5 秒了。
        self._autoplay_locked_streak = 0
        #: 「连续 N 屏没有新评论」这句话，每条作品只说一次
        self._idle_notified = False
        #: 评论接口请求里的 photoId —— 页面上**实际**打开的那条作品。
        #: 用来核对"点开的是不是我以为的那条"，点错了才能发现。
        self._comment_photo_id = ""

    #: 首页。真人是从这儿开始打字搜的
    HOME_URL = "https://www.kuaishou.com/"
    #: 判断"是不是已经在站内"的标志串。离线自检会覆盖它（见上面 _goto_search 的注释）
    SITE_HOST_MARK = "kuaishou.com"
    #: 联播开关连续锁着几次就不再试了（每次要白等好几秒）
    AUTOPLAY_LOCK_GIVE_UP = 3

    async def _goto_search(self, ctx: CollectContext, keyword: str) -> None:
        """真人动线：进首页 →（在搜索框里）**一个字一个字打** → 点「搜索」。

        ⚠️ 不再直接拼 `/search/{关键字}` 跳过去。
        用户原话："应该是要手动输入关键字，然后点击搜索"。
        直接跳 URL 和打字搜出来的页面状态不一样（`webPageArea`、
        播放器初始化时机都不同），而且拼 URL 这个动作本身就不像真人。

        找不到搜索框时**退回拼 URL**——宁可动线不像真人，也别让
        这个关键字整个废掉。
        """
        if self._page is None:
            return
        self.note_step(f"搜索「{keyword}」")
        # ⚠️ 换关键字 = 页面上的卡片**整批换掉**，顺序表必须跟着清空。
        #
        # 不清的话它会一路累加：第一个关键字留下 15 条，第二个关键字的
        # 第一条作品在表里的下标就是 15，可它在新页面上是**第 1 张卡片**。
        # 下标全线错位，点开的作品和日志里那条对不上。
        # 症状很有欺骗性：**单个关键字跑完全正确**（表是空的、下标天然对齐），
        # 一旦跑第二个关键字就开始错——用户就是这么发现的
        # （verify_kuaishou_browser.py 只跑一个关键字，所以一直是绿的）。
        self._order.clear()
        self._open_work_id = ""
        self._comment_photo_id = ""
        # ⚠️ 先把上一个关键字最后那条视频关掉再去搜。
        # 不关的话 player-pop-mask 会把顶栏整个盖住，
        # 搜索框点不着、「搜索」按钮也点不着——实跑就是这么卡住的。
        await self._close_video(ctx)

        try:
            current = await self._run(self._page.evaluate("location.href")) or ""
        except Exception:  # noqa: BLE001
            current = ""
        # ⚠️ 这个判断用类属性而不是写死字符串，是为了让离线自检能覆盖
        #    「已经在站内、**不重新加载**」这条路径。写死 "kuaishou.com" 的话，
        #    自检里的地址是 127.0.0.1，每次都会走整页重载——
        #    而整页重载会把遮罩一起冲掉，于是"视频没关"这个 bug
        #    在自检里**永远不会出现**。实跑才炸。
        if self.SITE_HOST_MARK not in current:
            ctx.log(f"[快手] 打开首页：{self.HOME_URL}")
            await self._run(self._page.goto(
                self.HOME_URL, wait_until="domcontentloaded", timeout=60_000))
            await asyncio.sleep(random.uniform(1.2, 2.4))

        box = self._page.locator(self.SEARCH_INPUT_SELECTOR).first
        if not await self._wait_visible(box, timeout_s=10.0):
            url = self._search_url_template.format(keyword=keyword)
            ctx.log(f"[快手] 没找到搜索框 {self.SEARCH_INPUT_SELECTOR}，"
                    f"退回直接打开搜索页：{url}", "warn")
            await self._run(self._page.goto(
                url, wait_until="domcontentloaded", timeout=60_000))
            return

        try:
            await self._click_through_mask(ctx, box, "搜索框")
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await self._run(box.fill(""))
            await asyncio.sleep(random.uniform(0.2, 0.5))
            # 一个字一个字敲，带随机停顿——整串 fill 进去没有 keydown 序列
            await self._run(box.type(keyword, delay=random.uniform(90, 220)))
            ctx.log(f"[快手] 已在搜索框输入「{keyword}」")
            await asyncio.sleep(random.uniform(0.5, 1.2))
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[快手] 往搜索框打字出错：{exc}", "warn")

        clicked = False
        try:
            button = self._page.locator(self.SEARCH_BUTTON_SELECTOR).first
            if await self._wait_visible(button, timeout_s=3.0):
                await self._click_through_mask(ctx, button, "「搜索」按钮")
                clicked = True
                ctx.log("[快手] 已点「搜索」")
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[快手] 点搜索按钮出错：{exc}", "warn")
        if not clicked:
            # 按钮点不着就敲回车——效果一样，也还是"输入后触发搜索"
            try:
                await self._run(box.press("Enter"))
                ctx.log("[快手] 搜索按钮点不着，改用回车")
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"[快手] 回车也没成功：{exc}", "warn")

        # 等结果真的出来：卡片出现，或者地址栏变成 /search/
        cards = self._page.locator(self.CARD_COVER_SELECTOR).first
        if not await self._wait_visible(cards, timeout_s=15.0):
            try:
                now = await self._run(self._page.evaluate("location.href")) or ""
            except Exception:  # noqa: BLE001
                now = ""
            ctx.log("[快手] 搜完 15 秒还没看到作品卡片"
                    f"（当前地址 {now or '未知'}）", "warn")

    async def _prepare_search(self, ctx: CollectContext, keyword: str) -> None:
        """搜索结果页到位之后：快手**没有筛选**，什么都不用点。

        用户原话："因为没有筛选逻辑所以简单很多"。
        留这个空实现是为了明确表态——不是忘了写。
        """
        ctx.log("[快手] 搜索结果已就绪（这个平台没有排序/时间筛选，直接开始采）")

    async def _handle_captured_response(
        self, ctx: CollectContext, matched_type: str, url: str, body: Dict, response
    ) -> None:
        """处理拦截到的快手 API 响应。"""
        if body.get("result") != 1:
            return

        if matched_type == "kuaishou_profile" and self._current_keyword:
            # ⚠️ 关键字采集时，**主页作品接口的东西一条都不能要**。
            #
            # 点开一个视频，右边除了「评论」还有一个「TA 的作品」页签，
            # 快手会顺手把作者的其他作品拉回来（/rest/v/profile/feed）。
            # 上一版把它和搜索结果同等对待，于是：
            #   - 顺序表 _order 里混进了一堆**搜索结果里根本没有**的作品
            #     （日志里连着七条「胡同小天地」的斑鸠、榴莲、二手书店，
            #       跟"八大处公园"毫无关系）
            #   - 下标越走越远，最后报「第 27 张卡片还没加载出来（只有 26 张）」
            #   - 更糟的是**点开的卡片和日志里那条对不上**：画面上是
            #     @葡萄IN北京 的视频（248 条评论），日志记的却是胡同小天地，
            #     评论按 photoId 一过滤全被丢掉 → 「8 秒没有评论到货」
            #
            # 用户的原话："快手只需要按照展示依次采集就行"——
            # 展示的是搜索结果，主页作品不在其列。
            logger.debug("[快手] 关键字采集中，忽略主页作品接口 %s", url)
            return

        if matched_type in ("kuaishou_search", "kuaishou_profile"):
            feeds = body.get("feeds") or body.get("data", {}).get("feeds") or []
            for feed in feeds:
                # ⚠️ 存**整个 feed**，不是 feed["photo"]。
                # 借用的 KuaishouCollector._to_work 收的是 feed
                # （里面要取 feed["photo"] 和 feed["author"]），
                # 只存 photo 的话它拿不到 id 和作者，整条映射成 None——
                # 现象是"接口明明拦到了 3 条，产出却是 0 条"，而且一句报错都没有。
                if feed.get("photo"):
                    self._captured_works.append(feed)

        elif matched_type == "kuaishou_comment":
            # 从 request body 提取 photoId 和 rootCommentId
            photo_id, root_comment_id = await self._extract_request_params(response)
            # 页面**真正**打开的是哪条，以这个 photoId 为准：
            # 卡片上没有 id，点错了卡片只有这里能发现。
            if photo_id:
                self._comment_photo_id = photo_id
            comments = self._extract_comments_from_body(body)
            for comment in comments:
                comment["_work_id"] = photo_id
                comment["_root_comment_id"] = root_comment_id
            self._captured_comments.extend(comments)
            # 评论总数：**只有这个接口给得出**，搜索接口里是没有的。
            # 拿到就记下来，_apply_comment_count() 会把它写回作品行。
            total = nz.to_int(self._pick(body, "commentCountV2", "commentCount"))
            if photo_id and total:
                self._comment_counts[photo_id] = total
            # 空评论检测：游标到底（真实字段是 pcursorV2）且这一批是空的
            pcursor = nz.to_text(self._pick(body, "pcursorV2", "pcursor", default=""))
            if pcursor == "no_more" and len(comments) == 0:
                self._comment_api_empty_count += 1
                if self._comment_api_empty_count >= 3:
                    self._comment_api_empty = True
            else:
                self._comment_api_empty_count = 0
                self._comment_api_empty = False

        elif matched_type == "kuaishou_sub_comment":
            photo_id, root_comment_id = await self._extract_request_params(response)
            comments = self._extract_comments_from_body(body)
            for comment in comments:
                comment["_work_id"] = photo_id
                comment["_root_comment_id"] = root_comment_id
                comment["_is_reply"] = True
            self._captured_comments.extend(comments)

    #: 评论列表在响应里可能叫什么。**V2 在前**——真实响应用的就是 V2。
    COMMENT_LIST_KEYS = (
        "rootCommentsV2", "subCommentsV2", "rootComments", "subComments",
        "commentList", "subCommentList", "list", "comments", "items",
    )

    @classmethod
    def _extract_comments_from_body(cls, body: Dict) -> List[Dict]:
        """从响应体里取评论列表。

        ⚠️ 真实响应是**平铺**的，评论直接挂在顶层：

            {"result":1, "commentCountV2":305, "pcursorV2":"...",
             "rootCommentsV2":[{...}, ...]}

        上一版只在 `body["data"]` 里找（那是我自己编的形状），
        于是**接口拦到了、也解析成 0 条**——现象就是日志里
        「打开评论区后 8 秒没有评论到货」，看起来像页面没点开，
        实际上数据早就到手了。所以这里顶层和 data 都要找。
        """
        for scope in (body, body.get("data")):
            if isinstance(scope, list) and scope:
                return scope
            if not isinstance(scope, dict):
                continue
            for key in cls.COMMENT_LIST_KEYS:
                rows = scope.get(key)
                if isinstance(rows, list) and rows:
                    return rows
        return []

    @staticmethod
    def _pick(body: Dict, *names, default=None):
        """顶层找不到就去 data 里找（快手两种形状都出现过）。"""
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        for name in names:
            for scope in (body, data):
                if name in scope and scope[name] is not None:
                    return scope[name]
        return default

    @staticmethod
    async def _extract_request_params(response) -> tuple:
        """从 request body 中提取 photoId 和 rootCommentId。"""
        photo_id = ""
        root_comment_id = ""
        try:
            req = response.request
            post_data = req.post_data
            if post_data:
                try:
                    req_body = json.loads(post_data)
                except Exception:
                    from urllib.parse import parse_qs
                    req_body = {k: v[0] for k, v in parse_qs(post_data).items()}
                photo_id = str(req_body.get("photoId", ""))
                root_comment_id = str(req_body.get("rootCommentId", ""))
        except Exception as exc:
            logger.debug(f"[快手] 提取请求参数失败: {exc}")
        return photo_id, root_comment_id

    async def _extract_new_works(
        self, ctx: CollectContext, keyword: str
    ) -> List[WorkItem]:
        """从捕获的响应中提取新作品。"""
        new_works = []
        for feed in self._captured_works:
            # 兼容两种形状：整个 feed（正常）和裸 photo（万一别处传进来）
            photo = feed.get("photo") if isinstance(feed.get("photo"), dict) else feed
            photo_id = nz.to_text(photo.get("id"))
            if not photo_id:
                continue
            # ⚠️ 顺序表要**先记、无条件记**：它是"页面上第几张卡片"的映射，
            # 页面可不管这条我们采没采过——卡片照样在那儿占位。
            # 原来这一句在 _seen_work_ids 的 continue 后面，于是
            # 上一个关键字采过的那些不进表，后面每一条的下标都往前串一位。
            if photo_id not in self._order:
                self._order.append(photo_id)
            if photo_id in self._seen_work_ids:
                continue
            self._seen_work_ids.add(photo_id)
            work = self._to_work(ctx, feed, source_keyword=keyword)
            if work:
                new_works.append(work)
        self._captured_works.clear()
        return new_works

    async def _extract_new_comments(
        self, ctx: CollectContext, work_id: str
    ) -> List[CommentItem]:
        """从捕获的响应中提取新评论。"""
        new_comments = []
        for comment in self._captured_comments:
            comment_id = nz.to_text(
                comment.get("commentId") or comment.get("comment_id") or
                comment.get("id") or comment.get("cid")
            )
            if not comment_id or comment_id in self._seen_comment_ids:
                continue
            if comment.get("_work_id") and comment["_work_id"] != work_id:
                continue
            self._seen_comment_ids.add(comment_id)
            # ⚠️ 参数必须和 KuaishouCollector._to_comment 对得上：
            # 它没有 `what` 这个参数。多传一个关键字 → TypeError，
            # 而且要**跑到映射那一步**才炸，前面搜索、点开、翻评论全白费。
            # 这类"借函数但签名对不上"的坑，verify 脚本的 preflight 专门查。
            is_reply = bool(comment.get("_is_reply"))
            root_from_request = nz.to_text(comment.get("_root_comment_id"))
            item = self._to_comment(
                ctx, comment, work_id,
                depth=2 if is_reply else 1,
                # 一级评论没有父级；回复的父级优先用响应里的 replyToCommentId，
                # 没有就退回顶楼 id（那是请求 query 里的 rootCommentId）
                parent_id=(nz.to_text(comment.get("replyToCommentId")
                                      or comment.get("replyTo"))
                           or root_from_request) if is_reply else "",
                root_id=root_from_request or comment_id,
            )
            if item:
                new_comments.append(item)
        self._captured_comments.clear()
        return new_comments

    # ---------------- 快手专用操作 ----------------
    # ⚠️ 下面这些选择器**全部来自 2026-09-05 的真实录制**
    #    （record_page.py --channel kuaishou，149 个动作），不是猜的。
    #    上一版写的是 '[class*="comment"]'、'[class*="next"]' 这种通配，
    #    小红书那边已经证明过：猜出来的选择器实跑全错。

    #: 作品卡片的封面图——**点它**才会打开视频。img 的 alt 就是文案。
    CARD_COVER_SELECTOR = "div.cards div.card-container div.photo-card img.cover-img"
    #: 「联播」开关。带 is-active 表示**开着**，要点一下关掉，
    #: 否则一条播完自动跳下一条，采集节奏全乱。
    AUTOPLAY_SWITCH_SELECTOR = "div.control-area div.hover-tip.autoPlay span.toggle-switch"
    #: 开关所在的那一块。鼠标不悬停在上面时开关是 aria-disabled="true"，点不动
    AUTOPLAY_AREA_SELECTOR = "div.control-area div.hover-tip.autoPlay"
    #: 播放区本体，hover 上面那排控件才出来
    PLAYER_AREA_SELECTOR = "div.swiper-slide-active div.video-sidebar-layout"
    #: 右侧那一列按钮里的「评论」
    COMMENT_BUTTON_SELECTOR = "div.video-interact-panel div.hover-tip.commentPanel div.comment"
    #: 评论面板本体 / 一条评论 / 「查看更多回复」
    COMMENT_PANEL_SELECTOR = "div.comment-side div.comment-list"
    COMMENT_ITEM_SELECTOR = "div.comment-side div.comment-list div.comment-root"
    EXPAND_REPLY_SELECTOR = "div.comment-side div.comment-root div.btns span.expand"
    #: 关掉视频回到列表（左上角那个圆形叉）
    CLOSE_VIDEO_SELECTOR = "div.swiper-slide-active div.video-sidebar-layout div.close.circle-btn"

    async def _goto_work(self, ctx: CollectContext, work: WorkItem) -> bool:
        """到达这条作品：在搜索结果页上**点它的封面**。

        ⚠️ 不能跳 URL。录制实测：整个流程 URL 一直是
        `search/{关键字}?source=NewReco` 不变，视频详情是同页的 swiper
        覆盖层。跳 `/short-video/{id}` 会整页重载，把搜索结果冲掉，
        下一条就找不到卡片了（抖音那边踩过同样的坑）。

        卡片上**没有作品 id**，所以按**位置**点：搜索接口返回的顺序
        和 DOM 里卡片的顺序是一致的，用 `self._order` 里的下标定位。
        """
        if self._page is None:
            return False
        if self._open_work_id == work.work_id:
            return True
        self._idle_notified = False       # 换作品了，这句话可以再说一次
        self._comment_photo_id = ""       # 上一条的 photoId 不能拿来核对这一条
        await self._close_video()

        try:
            index = self._order.index(work.work_id)
        except ValueError:
            ctx.log(f"[快手] 作品 {work.work_id} 不在搜索结果的顺序表里，跳过", "warn")
            return False

        try:
            cards = self._page.locator(self.CARD_COVER_SELECTOR)
            total = await self._run(cards.count())
        except Exception as exc:  # noqa: BLE001
            if self.browser_gone(exc):
                # 浏览器整个没了，不是"这一条读不到"。置位让上层立刻收手，
                # 别再一条条往下跑、往库里写假的「没有评论」
                self._browser_is_gone = True
                ctx.log(f"[快手] 浏览器已经关闭：{exc}", "warn")
                return False
            ctx.log(f"[快手] 读卡片列表出错：{exc}", "warn")
            return False
        if index >= total:
            # 还没滚到那儿，先把它滚出来
            await self._simulate_scroll(times=2)
            try:
                total = await self._run(cards.count())
            except Exception:  # noqa: BLE001
                return False

        # ⚠️ 光靠下标不够。下标假设"接口返回的第 N 条 == 页面上第 N 张卡片"，
        # 这个假设一旦被破坏（比如接口混进了主页作品、或者页面插了广告卡），
        # 就会**点错卡片**：画面上放着 A，日志记的是 B，A 的评论按 photoId
        # 一过滤全被丢掉，最后报 B「没有评论」。
        # 所以再用**文案**核一遍：卡片上的 caption 就是作品正文，
        # img.cover-img 的 alt 也是它。对不上就在页面上按文案找那张真的。
        index = await self._locate_card(ctx, work, index, total)
        if index is None:
            return False

        card = cards.nth(index)
        try:
            await self._run(card.scroll_into_view_if_needed(timeout=5000))
            await asyncio.sleep(random.uniform(0.3, 0.7))
            await self._run(card.click(timeout=8000))
        except Exception as exc:  # noqa: BLE001
            if self.browser_gone(exc):
                self._browser_is_gone = True
                ctx.log(f"[快手] 浏览器已经关闭：{exc}", "warn")
                return False
            ctx.log(f"[快手] 点第 {index + 1} 张卡片出错：{exc}", "warn")
            return False

        # 点完确认视频真的开了：判据是关闭按钮出现了
        close = self._page.locator(self.CLOSE_VIDEO_SELECTOR).first
        if not await self._wait_visible(close, timeout_s=10.0):
            ctx.log(f"[快手] 点了第 {index + 1} 张卡片但视频没打开"
                    f"（{self.CLOSE_VIDEO_SELECTOR} 没出现）", "warn")
            return False
        self._open_work_id = work.work_id
        # 打开之后第一件事就是关联播——录制里就是这个顺序
        await self._disable_autoplay(ctx)
        return True

    @staticmethod
    def _norm_caption(text: str) -> str:
        """比对用的文案：去掉空白和话题标签，只留正文。

        卡片上的 caption 和接口里的 caption 偶尔差一点（省略号、话题渲染方式），
        所以比的是"正文前半段"而不是全等。
        """
        text = re.sub(r"#\S+", "", text or "")
        return re.sub(r"\s+", "", text)[:24]

    async def _card_caption(self, index: int) -> str:
        """读第 index 张卡片的文案。alt 和 caption 哪个有取哪个。"""
        if self._page is None:
            return ""
        try:
            img = self._page.locator(self.CARD_COVER_SELECTOR).nth(index)
            # 同样要短超时：找不到卡片时要遍历一屏 20+ 张，
            # 每张按默认 30 秒等下去，一条作品能耗掉十几分钟
            alt = await self._attr(img, "alt")
            if alt.strip():
                return alt
            cap = self._page.locator(
                f"{self.CARD_SELECTOR} {self.CARD_CAPTION_SELECTOR}").nth(index)
            return await self._run(cap.inner_text(timeout=2000)) or ""
        except Exception:  # noqa: BLE001
            return ""

    async def _locate_card(self, ctx: CollectContext, work: WorkItem,
                           index: int, total: int) -> "int | None":
        """确认第 index 张卡片就是 work；对不上就在页面上按文案找。

        返回真正该点的下标，找不到返回 None（调用方跳过这条）。
        """
        want = self._norm_caption(work.title or work.description)
        if index < total:
            if not want:
                return index                 # 没文案可比，只能信下标
            got = self._norm_caption(await self._card_caption(index))
            if got and (got.startswith(want[:12]) or want.startswith(got[:12])):
                return index                 # 对得上，正常路径
        # 对不上（或者下标已经越界）：按文案在页面上找
        for i in range(total):
            got = self._norm_caption(await self._card_caption(i))
            if got and want and (got.startswith(want[:12]) or want.startswith(got[:12])):
                if i != index:
                    ctx.log(f"[快手] 顺序表说是第 {index + 1} 张，但文案对不上；"
                            f"按文案找到第 {i + 1} 张，点它", "warn")
                return i
        ctx.log(f"[快手] 页面上 {total} 张卡片里没有《{(work.title or '')[:20]}》，"
                f"跳过这条（顺序表和页面对不上了）", "warn")
        return None

    async def _disable_autoplay(self, ctx: CollectContext) -> None:
        """关掉「联播」。

        录制里这是打开视频之后的第一个动作。不关的话一条播完会自动跳到
        下一条，采集器还以为停在原地，评论就会记到错误的作品名下。

        ⚠️ 实跑踩到的坑：刚打开视频那会儿，开关是**锁着**的——

            <span role="switch" aria-disabled="true"
                  class="toggle-switch is-active is-disabled size-small">

        Playwright 认 `aria-disabled="true"`，会一直等"元素可点"直到超时，
        报 `element is not enabled`。它所在的那块叫 `hover-tip`，
        也就是**鼠标不悬停在播放区上时这排控件是锁的**。
        所以顺序必须是：先把鼠标挪过去 → 等它解锁 → 再点。
        """
        if self._page is None or self._autoplay_off:
            return
        if self._autoplay_locked_streak >= self.AUTOPLAY_LOCK_GIVE_UP:
            return                       # 已经试过几条都锁着，别每条再白等
        try:
            switch = self._page.locator(self.AUTOPLAY_SWITCH_SELECTOR).first
            if not await self._wait_visible(switch, timeout_s=4.0):
                ctx.log("[快手] 没找到联播开关，跳过（可能这一版没有这个功能）")
                self._autoplay_off = True
                return
            # ⚠️ get_attribute 的默认超时是 **30 秒**。开关所在那块是 hover
            # 才出现的浮层，视频一关它就没了 —— 于是每条作品白等 30 秒，
            # 实跑日志里就是那句「Locator.get_attribute: Timeout 30000ms exceeded」。
            # 这里的判断本来就该是"看一眼"，给 2 秒足够。
            klass = await self._attr(switch, "class")
            if "is-active" not in klass:
                ctx.log("[快手] 联播本来就是关的")
                self._autoplay_off = True
                return

            # 先把鼠标挪到控件那块，这排是 hover 才激活的
            await self._hover_autoplay()
            unlocked = await self._switch_unlocked(switch, timeout_s=6.0)
            if unlocked:
                self._autoplay_locked_streak = 0
                await self._run(switch.click(timeout=5000))
            else:
                self._autoplay_locked_streak += 1
                ctx.log(f"[快手] 联播开关一直锁着（aria-disabled=true，"
                        f"连续第 {self._autoplay_locked_streak} 条），"
                        f"试一次强制点", "warn")
                # 这是个自定义 span，它的点击事件不一定真看 aria-disabled，
                # 强制点一下再验证——验证不过就照实说，不假装关掉了
                await self._run(switch.click(timeout=3000, force=True))

            await asyncio.sleep(random.uniform(0.4, 0.8))
            klass2 = await self._attr(switch, "class")
            if "is-active" in klass2:
                ctx.log("[快手] 点了联播开关但它还是开着，"
                        "后面可能会自动跳下一条（评论有记错作品的风险）", "warn")
            else:
                ctx.log("[快手] 已关闭联播")
                self._autoplay_off = True
                self._autoplay_locked_streak = 0
        except Exception as exc:  # noqa: BLE001
            self._autoplay_locked_streak += 1
            ctx.log(f"[快手] 关联播时出错（继续）：{exc}", "warn")

    async def _attr(self, locator, name: str, timeout_ms: int = 2000) -> str:
        """读属性，**带短超时**。读不到就当空字符串。

        Playwright 所有 locator 操作的默认超时是 30 秒。读属性这种
        "看一眼"的动作绝不能用默认值：元素脱离文档时会实打实地等满 30 秒，
        一条作品就废掉半分钟。
        """
        try:
            return await self._run(locator.get_attribute(name, timeout=timeout_ms)) or ""
        except Exception:  # noqa: BLE001
            return ""

    async def _hover_autoplay(self) -> None:
        """把鼠标挪到播放器控件那一块，让 hover-tip 这排控件激活。"""
        if self._page is None:
            return
        for selector in (self.AUTOPLAY_AREA_SELECTOR,
                         self.AUTOPLAY_SWITCH_SELECTOR,
                         self.PLAYER_AREA_SELECTOR):
            try:
                target = self._page.locator(selector).first
                box = await self._run(target.bounding_box())
                if not box:
                    continue
                await self._run(self._page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2, steps=8))
                await asyncio.sleep(random.uniform(0.3, 0.6))
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("[快手] hover %s 失败：%s", selector, exc)

    async def _switch_unlocked(self, switch, timeout_s: float = 6.0) -> bool:
        """等开关从「锁着」变成可点。

        判据是 `aria-disabled` 和 class 里的 `is-disabled` 都掉了。
        用轮询而不是 `expect`：这两个属性是 Vue 直接改的，
        没有事件可等，而且轮询过程中鼠标还停在控件上（hover 保持住）。
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            aria = await self._attr(switch, "aria-disabled")
            klass = await self._attr(switch, "class")
            if aria.lower() != "true" and "is-disabled" not in klass:
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.3)

    async def _open_comments(self) -> bool:
        """点右侧那个评论按钮，把评论面板打开。

        判据是**面板真的出现了**，不是"点了一下"——点空了却当成打开，
        评论会挂到上一条作品名下。
        """
        if self._page is None:
            return False
        ctx = self._ctx
        panel = self._page.locator(self.COMMENT_PANEL_SELECTOR).first
        if await self._wait_visible(panel, timeout_s=1.0):
            return True                      # 已经开着（快手会记住上次状态）
        try:
            # ⚠️ 这个按钮的容器 class 里也带 **hover-tip** ——
            # 和联播开关是同一族控件：鼠标不在播放区上时那一排是隐藏/锁着的。
            # 联播那边已经证实过这个规律（不 hover 就报 element is not enabled），
            # 所以这里先把鼠标挪过去再找。实跑里「没找到评论按钮」出现过两次。
            await self._hover_autoplay()
            button = self._page.locator(self.COMMENT_BUTTON_SELECTOR).first
            if not await self._wait_visible(button, timeout_s=6.0):
                if ctx:
                    ctx.log(f"[快手] 没找到评论按钮 {self.COMMENT_BUTTON_SELECTOR}", "warn")
                return False
            await self._run(button.click(timeout=5000))
        except Exception as exc:  # noqa: BLE001
            if ctx:
                ctx.log(f"[快手] 点评论按钮出错：{exc}", "warn")
            return False
        if await self._wait_visible(panel, timeout_s=8.0):
            await asyncio.sleep(random.uniform(0.5, 1.0))
            return True
        if ctx:
            ctx.log("[快手] 点了评论按钮但评论面板没出现", "warn")
        return False

    async def _park_over_comments(self) -> None:
        """把鼠标挪进评论面板再滚。

        滚轮滚的是**指针底下那个元素**。指针停在左边的视频上滚，
        滚的是视频不是评论——小红书那边就是这么白滚了一整轮。
        """
        if self._page is None:
            return
        try:
            box = await self._run(
                self._page.locator(self.COMMENT_PANEL_SELECTOR).first.bounding_box())
            if box:
                await self._run(self._page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2, steps=6))
                await asyncio.sleep(random.uniform(0.15, 0.3))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[快手] 鼠标挪到评论区失败：%s", exc)

    async def _scroll_comments(self, max_scrolls: int = 0) -> None:
        """滚评论区，顺手点开「查看更多回复」。"""
        if self._page is None:
            return
        limit = max_scrolls or 40
        ctx = self._ctx
        await self._park_over_comments()
        idle = 0
        for i in range(limit):
            if self._comment_api_empty:
                if ctx:
                    ctx.log(f"  │      （评论接口说没有更多了，滚了 {i} 屏收工）")
                break
            before = len(self._captured_comments)
            await self._run(self._page.mouse.wheel(0, random.randint(400, 700)))
            # 评论区最多滚 40 屏，这一句是**循环里**的：
            # 每屏多半秒，一条作品就多 20 秒。走 pacer 的 scroll 档，可配。
            await self.pacer.wait("scroll_seconds", ctx)
            expanded = await self._expand_replies()
            if len(self._captured_comments) > before or expanded:
                idle = 0
                if expanded and ctx:
                    ctx.log(f"  │      （第 {i + 1} 屏，展开了 {expanded} 处回复）")
            else:
                idle += 1
                if idle >= 4:
                    # ⚠️ 一条作品只说一次。上层为了保险会再调两轮
                    # （max_no_new=3），每轮都喊一遍的话，用户看到的是
                    # 三行一模一样的「连续 4 屏没有新评论」，而且带着模块
                    # 日志前缀插在方框中间，把版面冲乱。
                    if not self._idle_notified:
                        self._idle_notified = True
                        if ctx:
                            ctx.log(f"  │      （连续 {idle} 屏没有新评论，停止滚动）")
                        else:
                            logger.info("[快手] 连续 %d 屏没有新评论，停止滚动", idle)
                    break
        await self._expand_replies()

    async def _expand_replies(self) -> int:
        """点开视口内的「查看更多回复」。

        录制实测：`div.comment-root > div.comment-inner > div.btns > span.expand`，
        文案是「查看更多回复」，展开之后同一个位置变成「收起」——
        所以**只点文案是「查看更多」的那些**，不然会把刚展开的又收回去。
        """
        if self._page is None:
            return 0
        clicked = 0
        try:
            for _ in range(6):
                hit = await self._run(self._page.evaluate(
                    """(sel) => {
                        for (const el of document.querySelectorAll(sel)) {
                            if (el.dataset.smcClicked) continue;
                            const t = (el.textContent || '');
                            if (t.indexOf('收起') >= 0) continue;   // 别把展开的收回去
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            if (r.top < 0 || r.top > innerHeight) continue;
                            el.dataset.smcClicked = '1';
                            el.click();
                            return true;
                        }
                        return false;
                    }""", self.EXPAND_REPLY_SELECTOR))
                if not hit:
                    break
                clicked += 1
                await asyncio.sleep(random.uniform(0.5, 0.9))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[快手] 展开回复失败：%s", exc)
        return clicked

    #: 视频覆盖层背后的那张遮罩。它还在 = 视频没关干净，
    #: 顶栏的搜索框和「搜索」按钮都点不动（Playwright 报
    #: "<div class=player-pop-mask> intercepts pointer events"）
    PLAYER_MASK_SELECTOR = "div.player-pop-mask"

    async def _close_video(self, ctx: CollectContext = None) -> None:
        """关掉视频回到搜索结果列表（左上角圆形叉）。

        ⚠️ 判据是**遮罩真的消失**，不是"点了一下叉"。

        实跑现场（2026-09-05 20:03）：上一条作品的视频没关干净，
        换关键字时搜索框和「搜索」按钮全被 `div.player-pop-mask` 挡着，
        Playwright 重试 19 次、等满 8 秒还是点不到：

            <div class="player-pop-mask"> intercepts pointer events

        点了叉不等于关掉了：动画要时间，叉本身也可能被遮住。
        所以点完要盯着遮罩，没消失就升级手段（Escape → 点遮罩本身）。
        """
        if self._page is None:
            return
        self._open_work_id = ""
        for attempt in range(3):
            try:
                close = self._page.locator(self.CLOSE_VIDEO_SELECTOR).first
                if await self._wait_visible(close, timeout_s=2.0):
                    await self._run(close.click(timeout=4000))
                elif attempt == 0:
                    await self._run(self._page.keyboard.press("Escape"))
                else:
                    # 叉找不到、Escape 也没用：直接点遮罩，多数弹层点空白就关
                    mask = self._page.locator(self.PLAYER_MASK_SELECTOR).first
                    await self._run(mask.click(timeout=3000, force=True))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[快手] 关视频第 %d 次失败：%s", attempt + 1, exc)
                if self.browser_gone(exc):
                    self._browser_is_gone = True
                    return
            await asyncio.sleep(random.uniform(0.5, 1.0))
            if not await self._mask_present():
                return                      # 遮罩没了，真的关掉了
            if attempt == 0:
                await self._run(self._page.keyboard.press("Escape"))
                await asyncio.sleep(0.4)
                if not await self._mask_present():
                    return
        if ctx is not None:
            ctx.log("[快手] 视频覆盖层关不掉（player-pop-mask 还在），"
                    "顶栏的搜索框会被它挡住", "warn")

    async def _click_through_mask(self, ctx: CollectContext, locator,
                                  what: str, timeout_ms: int = 8000) -> None:
        """点它；被遮罩挡住就先把遮罩清掉再点一次。

        Playwright 的报错里会明说是谁挡的：

            <div class="player-pop-mask"> intercepts pointer events

        这时候干等没有意义——它不会自己消失，重试 19 次也是白等 8 秒。
        """
        try:
            await self._run(locator.click(timeout=timeout_ms))
            return
        except Exception as exc:  # noqa: BLE001
            if "intercepts pointer events" not in str(exc):
                raise
            ctx.log(f"[快手] {what}被覆盖层挡住了，先关掉视频再点", "warn")
        await self._close_video(ctx)
        await self._run(locator.click(timeout=timeout_ms))

    async def _mask_present(self) -> bool:
        """遮罩还在不在。在 = 页面还被覆盖层挡着。"""
        if self._page is None:
            return False
        try:
            mask = self._page.locator(self.PLAYER_MASK_SELECTOR).first
            return bool(await self._run(mask.is_visible(timeout=1000)))
        except Exception:  # noqa: BLE001
            return False

    # ---------------- 数据映射 ----------------
    async def collect_comments(self, ctx: CollectContext, work: WorkItem):
        """照常翻评论，翻完把**评论总数**写回作品行。

        为什么要多这一步：搜索接口里根本没有评论数
        （`feed["comment"]` 只有 `{"us_c": 0}`），只有评论接口的
        `commentCountV2` 是真的。不补这一下，作品行的"评论数"永远是 0，
        和评论表里几百条对不上——用户就是这么发现的。
        """
        async for comment in super().collect_comments(ctx, work):
            yield comment
        self._check_opened_work(ctx, work)
        self._apply_comment_count(ctx, work)

    def _check_opened_work(self, ctx: CollectContext, work: WorkItem) -> None:
        """核对页面上**真正**打开的是不是这一条。

        ⚠️ 卡片上没有作品 id，点错了没有任何直接迹象。唯一可信的证据是
        评论接口请求里的 photoId —— 那是页面自己发出去的。
        对不上说明点错了卡片：画面上放着 A，日志记的是 B，
        A 的评论按 photoId 一过滤全被丢掉，最后报 B「没有评论」。
        真实发生过（2026-09-05 八大处公园）：画面是 @葡萄IN北京 的视频、
        评论区明明写着「共 248 条评论」，日志却说胡同小天地的作品 0 条评论。
        这种时候**必须喊出来**，不能安静地记成"这条没有评论"。
        """
        opened = self._comment_photo_id
        if opened and opened != work.work_id:
            ctx.log(f"[快手] ⚠️ 点开的不是这一条！日志记的是 {work.work_id}，"
                    f"页面上实际打开的是 {opened}（评论接口请求里的 photoId）。"
                    f"这一条的评论作废，顺序表和页面对不上了", "warn")

    def _apply_comment_count(self, ctx: CollectContext, work: WorkItem) -> None:
        total = self._comment_counts.get(work.work_id)
        if not total:
            return
        # 作品还在缓冲区里没落库（采一条存一条：评论翻完才 flush），
        # 所以这时候改它是有效的
        if work.comment_cnt != total:
            work.comment_cnt = total
            ctx.log(f"[快手] 作品 {work.work_id} 评论总数 {total}（取自评论接口）")

    def _to_work(self, ctx: CollectContext, feed: Dict, source_keyword: str = "") -> WorkItem:
        """把快手 feed（含 photo 和 author）映射为 WorkItem。

        ⚠️ 参数是**整个 feed**，不是 feed["photo"]——借用的
        KuaishouCollector._to_work 里要取 feed["author"]。
        """
        from .kuaishou import KuaishouCollector
        return KuaishouCollector._to_work(self, ctx, feed, source_keyword=source_keyword)

    def _to_comment(
        self, ctx: CollectContext, raw: Dict, work_id: str,
        depth: int = 1, parent_id: str = "", root_id: str = "",
    ) -> CommentItem:
        """把快手评论映射为 CommentItem。

        ⚠️ 参数表必须和 `KuaishouCollector._to_comment` 一致。
        原来这里多了个 `what` 并且原样透传过去 —— 那个参数**不存在**，
        一调用就 TypeError，而且要跑到映射那一步才炸：
        搜索、点开视频、翻评论全做完了，最后一步全丢。
        """
        from .kuaishou import KuaishouCollector
        return KuaishouCollector._to_comment(
            self, ctx, raw, work_id,
            depth=depth, parent_id=parent_id, root_id=root_id,
        )
