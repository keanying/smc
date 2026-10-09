"""小红书浏览器模拟采集器：从 network 拦截获取笔记和评论。

⚠️ 本文件里的每一个选择器和接口路径都来自 `probe_page.py` 的真实快照
   （2026-09-02，登录态，关键字「盘山风景区」，10 步）。快照编号写在各处注释里，
   页面改版时重新跑一次 `python probe_page.py --channel xiaohongshu` 对照着改。

## 快照推翻了三个原本的假设，都很要命

1. **搜索接口是 `so.xiaohongshu.com` 的 `/api/sns/web/v2/search/notes`（POST）**，
   不是 v1。按 v1 拦的话一条都拦不到，表现是"搜索页明明出结果了，采到 0 条"。

2. **搜索响应里的 note_card 是残缺的**，只有
   `type / user / interact_info / cover / image_list / corner_tag_info`，
   外加**可有可无**的 `display_title`。也就是说：
     · 没有 `note_id`（id 在外层 `item.id` 上）
     · **没有 `desc`、没有 `time`**
   而 runner 在 yield 之后立刻要用标题/正文做关键字过滤、用 publish_time 做
   时间窗过滤（见 scheduler/runner.py 的 `_work_matches_keyword` /
   `filters.in_window`）。拿残缺卡片直接 yield，绝大多数笔记会被判成
   "不含关键字"或"不在时间范围"而丢掉——而且看起来像是"搜不到"。
   所以必须**先打开笔记拿到 `/v1/feed` 的完整数据，再 yield**。
   接口版 XhsCollector 用 `_with_detail` 额外请求 /feed 也是同一个原因。

3. **点开笔记走的就是 `/explore/{id}?xsec_token=…&xsec_source=pc_search`**
   （快照 07 的页面 URL 逐字如此），并且这一下会同时打 `/v1/feed` 和
   `/v2/comment/page`——一次打开，作品详情和评论一起到手。

## 流程（两段式，和抖音同构）

  第 1 段：搜索页上只管滚动，把 note_id + xsec_token 收进缓冲区
  第 2 段：逐条打开笔记 → 等 /v1/feed 落地 → 用完整数据产出 WorkItem
           → runner 紧接着来采评论，此时页面就停在这条笔记上，不用再开一次

## 拦截的 API（快照 network.jsonl 实测）

  - /api/sns/web/v2/comment/sub/page  子评论（GET，root_comment_id 在 query 里）
  - /api/sns/web/v2/comment/page      一级评论（GET）
  - /api/sns/web/v2/search/notes      搜索（POST，so.xiaohongshu.com）
  - /api/sns/web/v1/search/notes      搜索的旧版路径，留着兜底
  - /api/sns/web/v1/feed              笔记详情（POST，edith.xiaohongshu.com）

## 筛选面板：**hover 展开，不是 click**

用户实测确认：鼠标移到「筛选」上面板才出来，移开就收；点一下反而可能
把它 toggle 掉。第二轮快照能证明这点——脚本用 click 点了一下，
拍出来的 04a 里面板压根不在 DOM 里（连「排序依据」这个组标题都搜不到），
入口文案还停在「筛选」。所以 `_open_filter_panel` 是 hover 优先、click 兜底，
并且用**组标题**（排序依据 / 笔记类型 / …）判断面板到底开没开。

面板里的真实文案（用户截图逐字核对）：

    排序依据  综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏
    笔记类型  不限 / 视频 / 图文
    发布时间  不限 / 一天内 / 一周内 / 半年内
    搜索范围  不限 / 已看过 / 未看过 / 已关注
    位置距离  不限 / 同城 / 附近

真人的动线（用户描述）：鼠标移到「筛选」上 → 面板弹出 → 往下移到「最新」
点一下 → 再移到时间那一项点一下。**中途不能离开面板区域**，一移开就收起。
所以代码里：面板还开着就直接往下点，不跑回入口重新 hover；
选项直接挨个试着点，不做"先扫描再决定"——扫描和点击之间隔得越久越容易扑空。

## 搜索：先像真人一样在首页输入

直开结果页 URL 是能出结果的（快照 02 就是这么拍的），但"从来不经过首页、
每次都精确落在结果页"本身就是个很扎眼的特征。所以先开首页、在
`#search-input` 里逐字敲、点 `.search-icon`，任何一步不顺再退回直开 URL。
（搜索框和按钮的选择器来自用户提供的「输入搜索.html」。）
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.constants import CHANNEL_XHS
from ..core.logging import get_logger
from ..utils import normalize as nz
from .base import CollectContext, CommentItem, WorkItem
from .browser_capture import BrowserCaptureCollector

logger = get_logger(__name__)

#: 搜索结果卡片。快照 02/04 实测：section.note-item[data-note-id]，
#: 里面的 a.cover 的 href 带着 xsec_token
CARD_SELECTOR = "section.note-item[data-note-id]"
#: 笔记弹窗的遮罩和关闭按钮（快照 07）
MODAL_SELECTOR = "div.note-detail-mask"
MODAL_CLOSE_SELECTOR = "div.note-detail-mask div.close-circle"
#: 筛选入口（快照 02：div.filter 里是 span「筛选」；展开并选过之后
#: 变成 div.filter.active + span「已筛选」——这就是"筛选生效了"的判据之一）
FILTER_ENTRY_SELECTOR = "div.filter"
#: 「展开 N 条回复」（快照 08 实测：div.show-more，文案形如「展开 3 条回复」）
SHOW_MORE_SELECTOR = "div.show-more"
#: 首页搜索框。**两套版式都要认**：
#:   经典版 `#search-input`（用户提供的「输入搜索.html」）
#:   AI 版   `textarea#search-input-in-feeds`（2026-09-03 录制实测）
#: AI 版整页挂 `html.ai-layout-active`，搜索结果落在 `/search_result_ai`。
#: 只写经典版的后果是实跑时报"DOM 里压根没有 #search-input"，
#: 每次都退回直开 URL——录制里这个账号命中的正是 AI 版。
SEARCH_INPUT_SELECTORS = (
    "#search-input",
    "textarea#search-input-in-feeds",
    "textarea[name='aiSearchTextarea']",
)
#: 搜索按钮。AI 版是右下角那个圆形提交图标，没有 `.search-icon`。
SEARCH_BUTTON_SELECTORS = (
    "div.input-button .search-icon",
    "div.bottom-box-right-submit-button img.submit-button",
    "div.submit-button-wrapper",
)
#: 筛选面板的选项容器（录制实测：
#: div.filter-container > div.filters-wrapper > div.filters > div.tag-container
#:   > div.tags > span，span 的自身文本就是「最新」「半年内」）。
#: 面板是 **hover 时才建、鼠标移开就从 DOM 里删掉**——录制里 150 帧
#: DOM 快照没有一帧含「半年内」，因为每次拍快照时鼠标都已经离开了。
FILTER_PANEL_SCOPE = "div.filter-container, div.filters-wrapper"
#: 左侧边栏宽度（录制实测 164px）。鼠标停在这个范围里滚轮，
#: 滚的不是笔记列表——所以滚列表之前一定要把指针挪到右边内容区。
SIDEBAR_WIDTH = 164


class XhsBrowserCollector(BrowserCaptureCollector):
    """小红书浏览器模拟采集器。"""

    channel = CHANNEL_XHS
    integrated = True
    #: 借用 XhsCollector._to_work 时它会用 self.web_host 拼 work_url。
    #: 不定义的话映射到一半才炸（和抖音 host 同一类问题）。
    web_host = "https://www.xiaohongshu.com"

    #: 打开笔记后最多等多久等 /v1/feed 落地
    FEED_WAIT_SECONDS = 8.0
    #: 评论区一次最多滚多少屏
    MAX_COMMENT_SCROLLS = 80
    #: 搜索阶段最多滚多少屏（够 max_works 就提前停）
    MAX_SEARCH_SCROLLS = 60
    #: 连续多少条笔记打不开就放弃这个关键字。
    #: 单条打不开是常态；连续打不开是页面状态坏了，再点也是空转。
    OPEN_FAIL_STREAK_LIMIT = 5

    @staticmethod
    def _video_urls(note: Dict) -> List[str]:
        """借 XhsCollector 的实现。

        ⚠️ 借函数就得把它用到的东西一起借齐。`XhsCollector._to_work` 里会调
        `self._video_urls(...)`，而这里的 self 是本类——不补这一行，
        映射到一半才抛 AttributeError，前面的搜索、拦截全白跑。
        （`web_host` 是同一个坑，抖音那边的 `host` 也是。）
        """
        from .xhs import XhsCollector
        return XhsCollector._video_urls(note)

    @staticmethod
    def _corner_publish_time(note):
        """同上：`_to_work` 会调 `self._corner_publish_time` 取搜索卡片
        角标里的发布时间（「2天前」「08-28」）。不借过来的话，跑到映射
        那一步才 AttributeError，前面搜索、点开、采评论的功夫全白费。
        verify_xhs_browser.py 的「字段映射依赖的属性齐全」就是查这个的。
        """
        from .xhs import XhsCollector
        return XhsCollector._corner_publish_time(note)

    @property
    def _target_url_patterns(self) -> List[Dict[str, str]]:
        return [
            {"url_contains": "/api/sns/web/v2/comment/sub/page", "type": "xhs_sub_comment"},
            {"url_contains": "/api/sns/web/v2/comment/page", "type": "xhs_comment"},
            # ⚠️ v2 在前。快照实测跑的是 v2（so.xiaohongshu.com，POST）；
            # v1 留着是怕不同账号/灰度走老路径，不是主路径。
            {"url_contains": "/api/sns/web/v2/search/notes", "type": "xhs_search"},
            {"url_contains": "/api/sns/web/v1/search/notes", "type": "xhs_search"},
            {"url_contains": "/api/sns/web/v1/feed", "type": "xhs_feed"},
        ]

    HOME_URL = "https://www.xiaohongshu.com"

    @property
    def _search_url_template(self) -> str:
        # 快照 02 的真实地址带 &type=51（笔记搜索）
        return "https://www.xiaohongshu.com/search_result?keyword={keyword}&type=51"

    @property
    def _work_url_template(self) -> str:
        return "https://www.xiaohongshu.com/explore/{work_id}"

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        #: note_id -> xsec_token（搜索响应给，DOM 的 a.cover href 里也有一份）
        self._tokens: Dict[str, str] = {}
        #: 列表里混进来的非笔记卡片数（「大家都在搜」这类推荐位）
        self._skipped_not_note = 0
        #: 当前这条笔记的评论区一共翻了多少屏（collect_comments 会分多次调）
        self._note_scrolls = 0
        #: 当前这条笔记已经打印了多少条评论
        self._note_comment_count = 0
        #: 基类的 collect_comments 到底有没有来采这条（_goto_work 是它的第一步）
        self._comments_attempted_for = ""
        #: 这条笔记的评论区有没有真的被滚进过视野
        self._scrolled_to_comments = False
        #: 这条笔记的字段是从哪来的：/v1/feed 还是搜索卡片
        self._feed_source = ""
        #: 还没用过的停顿时长（_pace 用，空了就重新洗牌）
        self._pace_bag: List[float] = []
        #: note_id -> 搜索卡片（残缺：没有 desc / time）
        self._cards: Dict[str, Dict] = {}
        #: note_id -> /v1/feed 的完整笔记（有 title / desc / time / tag_list）
        self._details: Dict[str, Dict] = {}
        #: 搜索结果的出现顺序。dict 在 3.7+ 有序，但显式留一份更清楚
        self._order: List[str] = []
        #: 当前页面停在哪条笔记上。runner 采完作品紧接着采评论，
        #: 这个标记让 _goto_work 不必再打开一次同一条笔记
        self._open_note_id = ""
        #: 搜索结果列表还在不在（一旦 goto 走了笔记页就没了，后续只能继续 goto）
        self._list_alive = False
        #: 评论翻页状态
        self._comment_has_more = True
        self._last_comment_resp_at = 0.0
        #: 最近一次搜索响应落地的时刻。
        #: ⚠️ 用 time.monotonic()：拦截回调在浏览器循环、判断在服务器循环，
        #: 两个循环的 loop.time() 不是同一条时间轴。
        self._last_search_resp_at = 0.0
        self._search_pages = 0
        #: 鼠标当前坐标。分步移动要知道从哪儿出发
        self._mouse = (0, 0)

    # ================= 响应处理 =================
    async def _handle_captured_response(
        self, ctx: CollectContext, matched_type: str, url: str, body: Dict, response
    ) -> None:
        # 小红书失败时是 {"success": false, "code": -xxxx, "msg": "..."}。
        # 静默 return 会让上层看到"没数据"，分不清是没搜到还是被风控了——
        # 这两种情况的处理完全不同，所以必须说出来。
        if not (body.get("success") or body.get("code") in (0, None)):
            msg = nz.to_text(body.get("msg")) or nz.to_text(body.get("message"))
            ctx.log(f"[小红书] 接口返回失败：code={body.get('code')} {msg}"
                    f"（{matched_type}）", "warn")
            return

        data = body.get("data") or {}
        if matched_type == "xhs_search":
            self._absorb_search(data)
        elif matched_type == "xhs_feed":
            self._absorb_feed(data)
        elif matched_type == "xhs_comment":
            self._absorb_comments(data, url, is_reply=False)
        elif matched_type == "xhs_sub_comment":
            self._absorb_comments(data, url, is_reply=True)

    def _absorb_search(self, data: Dict) -> None:
        """搜索结果。items 里混着广告位和「相关搜索」这类非笔记条目，
        只有带 note_card 的才是笔记。"""
        self._search_pages += 1
        self._last_search_resp_at = time.monotonic()
        added = 0
        for item in data.get("items") or []:
            note = item.get("note_card")
            if not note:
                continue
            # ⚠️ 快照实测：搜索的 note_card 里**没有** note_id，
            # id 在外层 item 上。只读 note_card 的话一条都认不出来。
            note_id = nz.to_text(item.get("id") or note.get("note_id"))
            if not note_id:
                continue
            # ⚠️ token 同样在**外层 item** 上。取错地方的话笔记页打不开，
            # 报的是"笔记不可浏览"，完全看不出根因是 token 丢了。
            token = nz.to_text(item.get("xsec_token"))
            if token:
                self._tokens[note_id] = token
            if note_id not in self._cards:
                self._order.append(note_id)
                added += 1
            self._cards[note_id] = note
        logger.info("[小红书] 搜索第 %d 批：新增 %d 条，累计 %d 条",
                    self._search_pages, added, len(self._order))

    def _absorb_feed(self, data: Dict) -> None:
        """笔记详情。快照实测字段：note_id / title / desc / time / ip_location /
        image_list / tag_list / interact_info / user / last_update_time。
        这才是能满足 runner 过滤的那份数据。"""
        for item in data.get("items") or []:
            note = item.get("note_card")
            if not note:
                continue
            note_id = nz.to_text(note.get("note_id") or item.get("id"))
            if note_id:
                note.setdefault("note_id", note_id)
                self._details[note_id] = note

    def _absorb_comments(self, data: Dict, url: str, *, is_reply: bool) -> None:
        note_id = self._extract_query(url, "note_id")
        # ⚠️ 子评论的响应体里**没有** root_comment_id（快照实测），
        # 它只在请求的 query 里。不从 URL 取的话，每条回复都会把自己
        # 当成一级评论的根，评论树整个是平的。
        root_from_url = self._extract_query(url, "root_comment_id")
        for comment in data.get("comments") or []:
            comment["_work_id"] = note_id
            comment["_is_reply"] = is_reply
            if is_reply and root_from_url:
                comment.setdefault("root_comment_id", root_from_url)
            self._captured_comments.append(comment)
            # 一级评论的响应里直接带着前几条回复（快照实测 sub_comments 字段），
            # 白拿的不要浪费——少一次 sub/page 请求就少一次被风控盯上的机会
            if not is_reply:
                for sub in comment.get("sub_comments") or []:
                    sub["_work_id"] = note_id
                    sub["_is_reply"] = True
                    sub.setdefault("root_comment_id", comment.get("id"))
                    self._captured_comments.append(sub)
        # 只有一级评论的 has_more 代表"这条笔记的评论翻完了"；
        # 子评论的 has_more 说的是某一条评论的回复，拿它停整个评论区会早停
        if not is_reply:
            self._comment_has_more = bool(data.get("has_more"))
        self._last_comment_resp_at = time.monotonic()

    @staticmethod
    def _extract_query(url: str, key: str) -> str:
        """从 URL 的 query 里取一个参数。

        不写死 id 的长度或字符集范围：写死了平台一改格式就整片取不到，
        表现是"评论都被算到别的笔记名下"，极难归因。
        """
        match = re.search(rf"[?&]{re.escape(key)}=([^&#]+)", url)
        return match.group(1) if match else ""

    # ================= 搜索：先像真人一样输入 =================
    async def _goto_search(self, ctx: CollectContext, keyword: str) -> None:
        """先开首页、在搜索框里逐字敲、再点搜索按钮；不行才直开结果页 URL。

        直开 URL 是能出结果的（快照 02 就是这么拍的），但"从来不经过首页、
        每次都精确落在结果页"本身就是个很扎眼的特征。抖音那边是同样的取舍。
        任何一步不顺就退回直开 URL——搜索是整条链的入口，不能卡在这。
        """
        self.note_step(f"搜索「{keyword}」")
        try:
            await self._run(self._page.goto(
                self.HOME_URL, wait_until="domcontentloaded", timeout=60_000))
            await asyncio.sleep(random.uniform(1.5, 2.5))

            # ⚠️ 两件事一起处理：
            # 1) **等久一点**——首页是个很重的 SPA，domcontentloaded 触发得
            #    非常早，而这又是浏览器冷启动后的第一次导航，给到 15 秒。
            # 2) **两套版式都试**——2026-09-03 的 probe 和录制双双证实：
            #    这个账号的首页根本没有 `#search-input`，只有 AI 版的
            #    `textarea#search-input-in-feeds`。以前只认经典版，
            #    结果每一次跑都静悄悄地退回直开 URL，从来没走过输入这条路。
            box = None
            used = ""
            for sel in SEARCH_INPUT_SELECTORS:
                cand = self._page.locator(sel).first
                # 只有第一个选择器值得等 15 秒；页面已经渲染完之后，
                # 后面几个是同一时刻的事实判断，等久了纯属浪费
                wait = 15.0 if sel is SEARCH_INPUT_SELECTORS[0] else 1.0
                if await self._wait_visible(cand, timeout_s=wait):
                    box, used = cand, sel
                    break
            if box is not None:
                ctx.log(f"[小红书] 首页搜索框命中 {used}")
                await self._run(box.click(timeout=5000))
                await asyncio.sleep(random.uniform(0.3, 0.6))
                await self._run(box.fill(""))
                for ch in keyword:
                    await self._run(box.type(ch, delay=random.uniform(60, 160)))
                await asyncio.sleep(random.uniform(0.4, 0.9))

                for sel in SEARCH_BUTTON_SELECTORS:
                    btn = self._page.locator(sel).first
                    if await self._wait_visible(btn, timeout_s=1.5):
                        await self._run(btn.click(timeout=5000))
                        break
                else:
                    # AI 版的提交按钮是张 <img>，个别版本上它不可点；
                    # 回车在两套版式上都能提交
                    await self._run(box.press("Enter"))

                # 认"到没到结果页"看 URL，不看有没有卡片：卡片是异步来的，
                # 早了会误判成失败，然后白白退回直开 URL 再刷一遍
                # `search_result` 这个子串同时覆盖两套结果页：
                # 经典版 `/search_result?keyword=`、AI 版 `/search_result_ai?keyword=`
                for _ in range(20):
                    # page.url 是同步属性，不用（也不能）走 _run
                    if "search_result" in (self._page.url or ""):
                        ctx.log(f"[小红书] 已在搜索框输入「{keyword}」并搜索")
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                        return
                    await asyncio.sleep(0.5)
                ctx.log("[小红书] 输入后没跳到搜索结果页，改为直开 URL", "warn")
            else:
                # 分清"压根没有这个元素"和"有但还没显示出来"——
                # 前者是选择器该改了，后者只是还没渲染完。只说"没找到"
                # 会让人去翻选择器，而真正的原因可能只是慢。
                try:
                    in_dom = await self._run(self._page.evaluate(
                        "(sels) => sels.filter(s => !!document.querySelector(s))",
                        list(SEARCH_INPUT_SELECTORS)))
                except Exception:  # noqa: BLE001
                    in_dom = []
                why = (f"{'、'.join(in_dom)} 在 DOM 里但一直不可见" if in_dom
                       else f"DOM 里 {'、'.join(SEARCH_INPUT_SELECTORS)} 一个都没有")
                ctx.log(f"[小红书] 首页搜索框等了 15 秒还用不了（{why}），"
                        f"改为直开 URL", "warn")
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[小红书] 模拟输入失败（{exc}），改为直开 URL", "warn")

        url = self._search_url_template.format(keyword=keyword)
        ctx.log(f"[小红书] 打开搜索页：{url}")
        await self._run(self._page.goto(
            url, wait_until="domcontentloaded", timeout=60_000))

    # ================= 两段式采集 =================
    async def collect_by_keyword(
        self, ctx: CollectContext, keyword: str
    ) -> AsyncIterator[WorkItem]:
        """**边滚边开**：DOM 里现在有哪些卡片，就先把它们开完，再往下滚。

        ⚠️ 上一版是"先滚够 100 条，再逐条打开"，实跑时 100% 失败
        （每一条都是「点不到卡片，改为直接打开笔记页」）。原因是
        **搜索结果是虚拟列表**：滚出视口的卡片会被从 DOM 里移除。
        实测证据——快照 02 有 22 张卡片，往下滚一屏后的快照 03 有 30 张，
        两者交集只有 21：**滚一屏就已经回收掉一张了**。滚到 104 条时，
        最前面那几十条的 DOM 节点早就不在了，`section.note-item[data-note-id=…]`
        自然一个也查不到。

        而"退回直开 /explore/{id}?xsec_token=…"这条兜底路是**没用**的：
        实跑证明整页加载笔记页**不会**触发 /v1/feed，所以既拿不到正文和
        发布时间，也拿不到评论——每条还白等 8 秒。所以现在点不到卡片就跳过，
        不再假装还有退路。

        循环形状因此变成：
            看 DOM 里有没有没开过的卡片 → 有就开一条（弹窗里 feed 和评论一起到）
            → 产出 → runner 顺势采评论 → 关弹窗 → 再看
            → 没有了才往下滚一屏
        这也正是真人的操作顺序。
        """
        self._current_keyword = keyword
        self._is_capturing = True
        self._ctx = ctx
        self._order.clear()
        self._cards.clear()
        self._details.clear()

        try:
            await self._goto_search(ctx, keyword)
            await asyncio.sleep(random.uniform(2, 4))
            self._list_alive = True
            await self._apply_filters(ctx)
            self.note_step(f"「{keyword}」开始边滚边采")

            emitted = 0
            idle = 0
            scrolls = 0
            skipped = 0
            open_fail_streak = 0
            #: 这个关键字最晚跑到什么时候（0 = 不限）
            budget = float(getattr(ctx, "keyword_budget_seconds", 0) or 0)
            deadline = (time.monotonic() + budget) if budget > 0 else None
            while not self.limit_reached(emitted, ctx.max_works):
                ctx.raise_if_cancelled()
                # 单个关键字的时间预算。到点就收，让 runner 去跑下一个关键字——
                # 否则一个关键字卡住会把整条任务的时间全吃掉，
                # 后面的关键字一个都轮不上。
                if deadline is not None and time.monotonic() > deadline:
                    ctx.log(f"[小红书] 关键字「{keyword}」已经跑了 "
                            f"{budget / 60:.0f} 分钟，到时间了，换下一个关键字"
                            f"（已产出 {emitted} 条）")
                    break
                note_id = await self._next_unseen_card()
                if not note_id:
                    if idle >= 4 or scrolls >= self.MAX_SEARCH_SCROLLS:
                        # ⚠️ 一条都没采到就收尾，最常见的原因不是"这个关键字
                        #    真没内容"，而是**被跳回登录页了**——页面上没有
                        #    任何卡片，表现和"搜不到"一模一样。
                        #    所以空手而归时必须复查一次登录态：掉了就抛出去
                        #    发通知，而不是安安静静地说"没有新卡片"。
                        if emitted == 0:
                            await self.recheck_login(ctx, reason="一条笔记都没采到")
                        ctx.log(f"[小红书] 列表里没有新卡片了，结束"
                                f"（共产出 {emitted} 条，跳过 {skipped} 条）")
                        break
                    # 滚之前先确认指针在列表上：关完弹窗时它停在左上角
                    # 关闭按钮那儿（录制实测 24,24），滚轮落在边栏上什么都不动
                    await self._move_to(*await self._feed_point(), steps=6)
                    await self._run(self._page.mouse.wheel(
                        0, random.randint(600, 1100)))
                    await asyncio.sleep(random.uniform(1.0, 1.8))
                    scrolls += 1
                    idle += 1
                    continue
                idle = 0
                self._seen_work_ids.add(note_id)

                work = await self._open_and_build(ctx, note_id, keyword, emitted + 1)
                if work is None:
                    skipped += 1
                    open_fail_streak += 1
                    # ⚠️ **连续**打不开就别再耗下去了。
                    # 用户实测：连着 12 条都是「点了卡片但弹窗没出现」，
                    # 每条还要等 8 秒 feed + 1~8 秒停顿，就这么空转到
                    # 看门狗 6 小时超时把整个任务强杀掉。
                    # 单条打不开是常态（笔记被删、卡片被回收）；
                    # **连续**打不开说明这一轮的页面状态已经坏了
                    # （被风控挡了、弹窗容器改版、页面卡住），
                    # 继续点下去一条也采不到——不如把这个关键字收了，
                    # 让 runner 去跑下一个关键字。
                    if open_fail_streak >= self.OPEN_FAIL_STREAK_LIMIT:
                        # 连续打不开的头号原因就是登录态掉了（弹窗打不开、
                        # 或者点一下就被跳走），先复查再下结论
                        await self.recheck_login(
                            ctx, reason=f"连续 {open_fail_streak} 条打不开")
                        ctx.log(
                            f"[小红书] 连续 {open_fail_streak} 条笔记都打不开，"
                            f"判定这一轮页面状态已经坏了，放弃关键字「{keyword}」"
                            f"（已产出 {emitted} 条）。"
                            f"常见原因：被风控挡了、或者弹窗容器 class 改版了——"
                            f"跑一次 record_page.py 看看卡片还点不点得开",
                            "warn")
                        break
                    await self._close_modal()
                    await self._pace(ctx)
                    continue
                open_fail_streak = 0
                # 评论是 runner 在下面这个 yield 处采的，采完才回到这里。
                # 前后各数一次 _seen_comment_ids，差值就是这条笔记的评论数——
                # 这是唯一能拿到"每条笔记采了多少"的位置。
                before_c = len(self._seen_comment_ids)
                self._note_scrolls = 0
                self._note_comment_count = 0
                self._comments_attempted_for = ""
                self._scrolled_to_comments = False
                yield work
                emitted += 1
                got = len(self._seen_comment_ids) - before_c
                if self._comments_attempted_for != note_id:
                    # ⚠️ 0 条评论有**两种**完全不同的原因，必须分开说，
                    # 但也别把原因说死——这里只知道"调用方没来采"，
                    # 不知道为什么。两种常见情形：
                    #   · runner 的内容过滤 / 时间窗把这条 work 丢了
                    #   · runner 有意跳过（和前面的关键字重复、今天已经采过、
                    #     或者这个平台关掉了评论采集）
                    #   · 调用方是**两段式**的（先把笔记全采完再回头采评论）——
                    #     那样不但这行会条条出现，而且回头再开笔记时卡片
                    #     早被虚拟列表回收了，会条条点不开
                    ctx.log(f"[小红书]      计数：【0】条评论 —— "
                            f"产出后调用方没有紧接着采这条的评论"
                            f"（内容过滤/时间窗丢弃、重复或今天已采过而"
                            f"有意跳过，都会走到这里；上面几行日志会说明是哪种）")
                else:
                    ctx.log(f"[小红书]      计数：【{got}】条评论"
                            f"（评论区翻了 {self._note_scrolls} 屏）")
                ctx.log(f"[小红书] ── 已产出 {emitted} 条笔记")
                # runner 已经在上面那个 yield 处把这条的评论采完了，
                # 现在才可以关弹窗——关早了评论区就没了
                await self._close_modal()
                await self._pace(ctx)
        finally:
            self._is_capturing = False

    #: 两条笔记之间的停顿区间（秒）。真人不会匀速点，所以不取固定值，
    #: 也不取"每次都 random.uniform(1,8)"——那样均值恒在 4.5 秒，
    #: 一小时下来仍然是条笔直的线。这里做成**轮换**：
    #: 把区间打散成一个乱序序列走一遍，走完重新洗牌，
    #: 于是短停顿和长停顿都会出现，长期均值也不会暴露成一个常数。
    #: ⚠️ 已废弃：节奏改成按平台配置（crawl.pace）。留着只为不破坏引用它的地方。
    PACE_RANGE = (1.0, 8.0)

    async def _pace(self, ctx: CollectContext) -> None:
        """采完一条笔记之后停一下再看下一条。

        ⚠️ 这里原来是写死的 1~8 秒（PACE_RANGE），改不了也看不见。
        现在交给 `self.pacer`，四个平台共用一套、按平台可配
        （`crawl.pace.xiaohongshu.work_seconds`）。
        洗牌袋和分段睡（可取消）都在 pacer 里，行为不变。
        """
        await self.pacer.wait("work_seconds", ctx, note="再看下一条")

    async def _next_unseen_card(self) -> str:
        """当前 DOM 里第一个还没开过的卡片 id。

        每次都重新读 DOM，不缓存：虚拟列表会回收节点，而且用户实测
        "手动关掉笔记弹窗之后列表会变"——缓存下来的 id 很可能已经不在页面上了。
        顺带把 token 也收一遍（token 在 a.cover 的 href 里）。
        """
        await self._harvest_cards_from_dom()
        if self._page is None:
            return ""
        try:
            rows = await self._run(self._page.evaluate(
                """(sel) => Array.from(document.querySelectorAll(sel)).map(el => ({
                        id: el.getAttribute('data-note-id') || '',
                        // ⚠️ 判据是**有没有带 token 的封面链接**，不是 id 长什么样。
                        // 列表里混着「大家都在搜」这种推荐词卡片：它同样是
                        // section.note-item、同样有 data-note-id（形如
                        // 97f78c57-0fb4-…#1788425134971 的 UUID#时间戳），
                        // 但里面是 div.query-note-wrapper，没有封面也点不开。
                        // 录制实测 113 张卡片里有 5 张是这种。
                        openable: !!el.querySelector('a[href*="xsec_token="]'),
                   }))""", CARD_SELECTOR))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 读 DOM 卡片失败：%s", exc)
            return ""
        for row in rows or []:
            note_id = row.get("id") or ""
            if not note_id or note_id in self._seen_work_ids:
                continue
            if not row.get("openable"):
                # 记下来别再看它，否则每一轮都会重新挑中同一张，卡死在这
                self._seen_work_ids.add(note_id)
                self._skipped_not_note += 1
                logger.info("[小红书] 跳过第 %d 张非笔记卡片（%s，"
                            "多半是「大家都在搜」这类推荐位，没有封面链接）",
                            self._skipped_not_note, note_id[:40])
                continue
            return note_id
        return ""

    async def _scroll_search(self, ctx: CollectContext, target: int) -> None:
        """在搜索页上滚，直到收够 target 条或者滚不出新的了。"""
        idle = 0
        for i in range(self.MAX_SEARCH_SCROLLS):
            # DOM 里也有 note_id 和 token（a.cover 的 href），
            # 顺手收一遍：接口万一漏拦一批，这里还能兜住
            await self._harvest_cards_from_dom()
            if len(self._order) >= target:
                logger.info("[小红书] 已收够 %d 条，停止滚动", len(self._order))
                return
            before = len(self._order)
            await self._move_to(*await self._feed_point(), steps=6)
            await self._run(self._page.mouse.wheel(0, random.randint(600, 1100)))
            await asyncio.sleep(random.uniform(1.0, 1.8))
            if len(self._order) > before:
                idle = 0
            else:
                idle += 1
                if idle >= 4:
                    ctx.log(f"[小红书] 连续 {idle} 屏没有新笔记，搜索阶段结束"
                            f"（共 {len(self._order)} 条）")
                    return
        ctx.log(f"[小红书] 滚到上限 {self.MAX_SEARCH_SCROLLS} 屏，"
                f"共 {len(self._order)} 条")

    async def _harvest_cards_from_dom(self) -> int:
        """从列表 DOM 里补 note_id 和 xsec_token。

        快照 02/04 实测卡片长这样：
            <section class="note-item" data-note-id="6a97fce9…">
              <a class="cover mask ld" href="/search_result/6a97fce9…?xsec_token=…">
        接口是主路径，这里是**兜底**：拦截万一漏了一批（页面用了缓存、
        响应体解析失败），至少 id 和 token 还在，笔记照样打得开。
        """
        if self._page is None:
            return 0
        try:
            found = await self._run(self._page.evaluate(
                """(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
                    const a = el.querySelector('a[href*="xsec_token="]');
                    const href = a ? a.getAttribute('href') : '';
                    const m = href.match(/xsec_token=([^&]+)/);
                    return {id: el.getAttribute('data-note-id') || '',
                            openable: !!a,
                            token: m ? decodeURIComponent(m[1]) : ''};
                })""", CARD_SELECTOR))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 从 DOM 收卡片失败：%s", exc)
            return 0
        added = 0
        for row in found or []:
            note_id = nz.to_text(row.get("id"))
            # 没有封面链接的不是笔记（「大家都在搜」这类推荐位），
            # 收进来只会把"累计 N 条"这个数虚高，然后在打开时才发现点不动
            if not note_id or not row.get("openable"):
                continue
            if row.get("token") and note_id not in self._tokens:
                self._tokens[note_id] = row["token"]
            if note_id not in self._cards:
                self._cards[note_id] = {}
                self._order.append(note_id)
                added += 1
        return added

    async def _open_and_build(
        self, ctx: CollectContext, note_id: str, keyword: str, seq: int = 0
    ) -> Optional[WorkItem]:
        """打开一条笔记，等 /v1/feed 落地，用完整数据造 WorkItem。"""
        if not await self._open_note(ctx, note_id):
            return None

        # 等详情到货。等不到就退回残缺卡片——能采总比不采好，
        # 但要说清楚，因为这种 work 很可能被 runner 的关键字/时间过滤丢掉
        deadline = time.monotonic() + self.FEED_WAIT_SECONDS
        while note_id not in self._details and time.monotonic() < deadline:
            await asyncio.sleep(0.3)

        note = self._details.get(note_id)
        card = self._cards.get(note_id) or {}
        if note is None:
            # ⚠️ 这个分支才是"没等到 feed"。上一版把警告误写在了下面的 elif 里，
            # 于是 feed **成功到货**时反而喊"没等到 feed，只能用残缺字段"——
            # 用户看到的是明明有正文有时间却在报警。改回来了。
            note = dict(card)
            note.setdefault("note_id", note_id)
            self._feed_source = "搜索卡片"
            # 也别一律报警：AI 版的搜索卡片**自带** desc 和 time（实测），
            # 这时候没有 feed 照样够用。真正该报的是"卡片里也缺东西"。
            missing = [name for name, key in (("正文", "desc"), ("发布时间", "time"))
                       if not card.get(key)]
            if not missing:
                ctx.log(f"[小红书] 笔记 {note_id} 没等到 /v1/feed，"
                        f"但搜索卡片自带正文和发布时间，够用")
            else:
                fallback = ("，发布时间用卡片角标推算"
                            if "发布时间" in missing and card.get("corner_tag_info")
                            else "")
                ctx.log(f"[小红书] 笔记 {note_id} 等了 "
                        f"{self.FEED_WAIT_SECONDS:.0f} 秒没等到 /v1/feed，"
                        f"退回搜索卡片，缺 {'、'.join(missing)}{fallback}", "warn")
        else:
            self._feed_source = "/v1/feed"
            if card.get("corner_tag_info") and not note.get("corner_tag_info"):
                # 把搜索卡片上的 corner_tag_info（「2天前」「08-28」这种发布时间）
                # 带到 feed 数据上。feed 正常时用它的 time 就够了，但 feed 偶尔
                # 会缺 time——那时候有这个兜底，publish_time 至少精确到天，
                # 不会因为 None 被 runner 的时间窗整片丢掉。
                note = dict(note)
                note["corner_tag_info"] = card["corner_tag_info"]

        work = self._to_work(ctx, note, note_id, self._tokens.get(note_id, ""),
                             source_keyword=keyword)
        if work is not None:
            self._log_work(ctx, seq, work)
        return work

    @staticmethod
    def _short(text: str, n: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= n else text[:n] + "…"

    def _log_work(self, ctx: CollectContext, seq: int, work: WorkItem) -> None:
        """采集循环的进度行：第几条、哪条笔记、字段是从哪来的。

        ⚠️ 作品的**字段明细**（作者/发布时间/内容）不在这里打——
        那一份统一由 runner 的 log_work_detail 输出，所有平台一个格式。
        在两个地方各打一份的话，同一条作品的同样字段会出现两遍。
        这里只说"采集器这一侧发生了什么"。
        """
        when = getattr(work, "publish_time", None)
        ctx.log(f"[小红书]【{seq}】笔记 {work.work_id}"
                f"  《{self._short(getattr(work, 'title', ''), 24) or '无标题'}》"
                f"  {when or '发布时间未知'}"
                f"（字段来自 {self._feed_source or '?'}）")

    # ================= 打开一条笔记 =================
    async def _open_note(self, ctx: CollectContext, note_id: str) -> bool:
        """在结果列表上点这条笔记的卡片，等笔记弹窗出来。

        ⚠️ **只有点卡片这一条路**，没有兜底。
        原来还留了个"点不到就直开 /explore/{id}?xsec_token=…"的退路，
        实跑证明那条路是**没用的**：整页加载笔记页**不会**触发 /v1/feed，
        所以正文、发布时间、评论一个都拿不到，还要白等 8 秒超时。
        用户那次跑的日志里，104 条笔记条条如此。
        与其假装还有退路，不如点不到就跳过并说清楚。
        """
        if self._open_note_id == note_id:
            return True
        # ⚠️ 必须重新打开拦截开关。
        # 基类的 collect_comments 结束时会把 _is_capturing 置回 False，而
        # runner 是**交错**跑的：产出一条 → 立刻采它的评论 → 再回来产出下一条。
        # 于是从第二条起，打开笔记时的 /v1/feed 会被拦截回调直接丢掉——
        # 现象是"只有第一条有标题和发布时间，后面全是空的"，
        # 然后被 runner 的关键字/时间过滤整片丢掉，看起来像"只采到一条"。
        self._is_capturing = True
        title = nz.to_text((self._cards.get(note_id) or {}).get("display_title"))
        self.note_step(f"打开笔记：{title[:30]}" if title else f"打开笔记 {note_id}")

        self._reset_note_state()
        # 上一条的弹窗必须先关掉：div.note-detail-mask 是 position:fixed
        # 铺满整屏的，留着它下一张卡片根本点不到
        await self._close_modal()
        if await self._click_card(ctx, note_id):
            self._open_note_id = note_id
            return True
        # 具体是哪一步不行，_click_card 里已经分别记过日志了，这里不再重复
        return False

    def _reset_note_state(self) -> None:
        self._open_note_id = ""
        self._comment_has_more = True
        self._last_comment_resp_at = 0.0

    async def _click_card(self, ctx: CollectContext, note_id: str) -> bool:
        """在结果列表上点这条笔记的封面。

        和筛选面板一样用**鼠标坐标**，不用 `locator.click()`：
        列表上方常年浮着搜索栏、筛选面板这些定位元素，Playwright 的
        可操作性判定一旦认为"被挡住"就直接超时，而报出来只有一句
        "点不开"——分不清是卡片没了、被挡住了、还是弹窗没开。

        所以这里每一步失败都说清是**哪一步**：
        找不到节点 / 滚过去之后仍不可见 / 点了但弹窗没出来。
        """
        selector = f'{CARD_SELECTOR}[data-note-id="{note_id}"]'
        # 1) 节点还在不在（虚拟列表会回收）
        try:
            exists = await self._run(self._page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    return true;
                }""", selector))
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[小红书] 卡片 {note_id} 定位出错：{exc}", "warn")
            return False
        if not exists:
            ctx.log(f"[小红书] 卡片 {note_id} 已不在 DOM 里（虚拟列表回收了），跳过",
                    "warn")
            return False
        await asyncio.sleep(random.uniform(0.4, 0.8))

        # 2) 取封面链接的坐标。取 a.cover 而不是整张卡片：
        #    卡片下半部分是作者行，点那儿会跳到作者主页
        box = await self._element_box(selector=f"{selector} a.cover")
        if box is None:
            box = await self._element_box(selector=selector)
        if box is None:
            ctx.log(f"[小红书] 卡片 {note_id} 在 DOM 里但滚过去仍不可见"
                    f"（可能被浮层挡着或高度为 0），跳过", "warn")
            return False

        # 3) 按坐标点
        try:
            await self._move_to(box["x"], box["y"], steps=6)
            await asyncio.sleep(random.uniform(0.15, 0.35))
            await self._run(self._page.mouse.click(box["x"], box["y"]))
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[小红书] 点卡片 {note_id} 出错：{exc}", "warn")
            return False

        # 4) 点完要确认弹窗真的开了：点空了却当成打开，
        #    评论会挂到上一条笔记名下
        for _ in range(24):
            if await self._modal_open():
                await asyncio.sleep(random.uniform(0.8, 1.4))
                return True
            await asyncio.sleep(0.3)
        ctx.log(f"[小红书] 点了卡片 {note_id}（坐标 {box['x']},{box['y']}）"
                f"但 {MODAL_SELECTOR} 没出现——多半是点空了，"
                f"或者弹窗的容器 class 变了", "warn")
        return False

    async def _modal_open(self) -> bool:
        try:
            return bool(await self._run(self._page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }""", MODAL_SELECTOR)))
        except Exception:  # noqa: BLE001
            return False

    async def _close_modal(self) -> None:
        """关掉笔记弹窗回到列表（快照 10：关掉之后 URL 变回搜索页）。"""
        if self._page is None or not self._list_alive:
            return
        try:
            close = self._page.locator(MODAL_CLOSE_SELECTOR).first
            if await self._wait_visible(close, timeout_s=2.0):
                await self._run(close.click(timeout=5000))
            else:
                await self._run(self._page.keyboard.press("Escape"))
            await asyncio.sleep(random.uniform(0.5, 1.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 关弹窗失败：%s", exc)
        self._open_note_id = ""

    async def _looks_unavailable(self) -> bool:
        """页面在说"这条笔记看不了"。

        判据用**页面文案**而不是 HTTP 状态：这种情况服务端回的是 200，
        只是渲染了一个提示页，看状态码完全看不出来。
        """
        if self._page is None:
            return False
        try:
            return bool(await self._run(self._page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText : '';
                    return ['当前笔记暂时无法浏览', '笔记不见了', '内容不存在',
                            '你访问的页面不见了'].some(w => t.includes(w));
                }""")))
        except Exception:  # noqa: BLE001
            return False

    # ================= 筛选 =================
    #: 筛选面板的真实文案（2026-09-02 用户截图逐字核对）。面板分五组：
    #:   排序依据  综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏
    #:   笔记类型  不限 / 视频 / 图文
    #:   发布时间  不限 / 一天内 / 一周内 / 半年内
    #:   搜索范围  不限 / 已看过 / 未看过 / 已关注
    #:   位置距离  不限 / 同城 / 附近
    #: 底部还有「重置」「收起」。这里只用到前两组需要的项，
    #: 但把整组文案记全，是为了 _panel_open() 能拿组标题当"面板开了没"的判据。
    PANEL_GROUPS = ("排序依据", "笔记类型", "发布时间", "搜索范围", "位置距离")
    #: 每个筛选项的候选文案。第一个是截图确认过的，后面几个是改版兜底。
    SORT_CANDIDATES = {
        "general": (),                       # 综合是默认值，不用点
        "latest": ("最新", "最新发布"),
        "most_like": ("最多点赞", "点赞最多"),
        "most_comment": ("最多评论",),
        "most_collect": ("最多收藏",),
    }
    TIME_CANDIDATES = {
        "unlimited": (),
        "day": ("一天内", "24小时内"),
        "week": ("一周内", "7天内"),
        "half_year": ("半年内", "近半年"),
    }

    async def _apply_filters(self, ctx: CollectContext) -> bool:
        """按任务参数选排序和发布时间。

        用户描述的真人动线，代码照着走：
            鼠标移到「筛选」上 → 面板弹出 → **往下移到「最新」点一下**
            → 再移到时间那一项点一下

        关键在于**中途不能离开面板区域**——鼠标一移开面板就自动收起。
        所以这里：
          · 面板已经开着就**不再重新 hover 入口**（上一项点完面板常常还开着，
            多此一举地跑回入口反而多一次移动、多一次收起的机会）
          · 选项不靠"先扫一遍文本再决定点谁"，而是直接挨个试着点——
            扫描和点击之间隔得越久，面板越可能已经收了
          · 扫描出来的文本只用来**写日志**，方便文案变了的时候照着改
        """
        filters = ctx.search_filters
        wanted: List[tuple] = []
        sort = getattr(filters, "sort", "general")
        within = getattr(filters, "publish_within", "unlimited")
        if self.SORT_CANDIDATES.get(sort):
            wanted.append(("排序依据", self.SORT_CANDIDATES[sort]))
        else:
            ctx.log(f"[小红书] 排序＝{sort}，是默认值（综合），不用点")
        if self.TIME_CANDIDATES.get(within):
            wanted.append(("发布时间", self.TIME_CANDIDATES[within]))
        else:
            # ⚠️ 这一句是专门为了消除误会：任务里时间窗填 unlimited 时，
            # 「一天内 / 一周内 / 半年内」**本来就不会去点**。以前这里
            # 一声不吭，看日志的人只会以为"时间那一项点失败了"。
            ctx.log(f"[小红书] 发布时间＝{within}，任务没设时间窗，"
                    f"不点「一天内 / 一周内 / 半年内」"
                    if within in ("", "unlimited", None)
                    else f"[小红书] 发布时间＝{within}，"
                         f"不在候选表里（可选：{'、'.join(k for k in self.TIME_CANDIDATES if k != 'unlimited')}）")
        if not wanted:
            ctx.log("[小红书] 排序与时间都是默认值，跳过筛选面板")
            return True

        ok = True
        logged = False
        for group, candidates in wanted:
            # 面板还开着就直接往下点，别跑回入口再 hover 一次
            if not await self._wait_panel_open(0.4):
                before = await self._visible_texts()
                if not await self._open_filter_panel(ctx):
                    return False
                opts = sorted(await self._visible_texts() - before)
                if opts and not logged:
                    ctx.log(f"[小红书] 筛选面板里的选项：{'、'.join(opts[:40])}")
                    logged = True

            clicked_at = time.monotonic()
            hit = ""
            for cand in candidates:
                if await self._click_option(cand):
                    hit = cand
                    break
            if not hit:
                ok = False
                ctx.log(f"[小红书] 「{group}」这一项没点中"
                        f"（试过 {'、'.join(candidates)}）。上面那行"
                        f"「筛选面板里的选项」是面板里的真实文案，"
                        f"照着改 SORT_CANDIDATES / TIME_CANDIDATES 即可", "warn")
                continue
            # 点完要等一个**时刻晚于点击**的搜索响应，才算筛选真的生效。
            # 只看"有没有响应"会被上一次点击的迟到响应顶替——抖音那边
            # 就是这么错开一拍的（见 douyin_browser._note_search_response）。
            if await self._wait_search_after(clicked_at, timeout_s=10.0):
                ctx.log(f"[小红书] 已选中筛选项「{hit}」，结果已刷新")
            else:
                ok = False
                ctx.log(f"[小红书] 点了「{hit}」但 10 秒内没有新的搜索响应，"
                        f"这一项可能没生效", "warn")
            await asyncio.sleep(random.uniform(0.6, 1.1))

        # ⚠️ 收工前必须把鼠标挪开，让面板收起来。
        # 面板靠 hover 维持，而 _open_filter_panel 结束时鼠标就停在入口上——
        # 不挪开的话它会一直挂在页面右侧（实测约占 450×570 像素），
        # 把右边两列的卡片整片盖住，接下来点那些卡片就会点到面板上。
        await self._dismiss_filter_panel()

        # 收工前问一句"到底生效了没"。判据是入口自己变了样：
        # 选过之后 div.filter → div.filter.active，里面的 span 从「筛选」
        # 变成「已筛选」（probe 的 05/06 两步逐字核对过，而且关掉笔记弹窗
        # 回到列表之后它还在）。这比"有没有新的搜索响应"更贴近人眼看到的东西。
        if not await self._filter_active():
            ok = False
            ctx.log("[小红书] 筛选点完了，但入口没变成「已筛选」——"
                    "这一轮筛选很可能没落到搜索条件上", "warn")
        return ok

    async def _filter_active(self) -> bool:
        if self._page is None:
            return False
        try:
            return bool(await self._run(self._page.evaluate(
                """() => {
                    if (document.querySelector('div.filter.active')) return true;
                    const t = document.querySelector('div.filter');
                    return !!t && (t.innerText || '').includes('已筛选');
                }""")))
        except Exception:  # noqa: BLE001
            return False

    async def _dismiss_filter_panel(self) -> None:
        """把鼠标挪到笔记列表上，让 hover 出来的面板收起。

        ⚠️ 挪去哪里很讲究。上一版挪到 (60, 400)——那是**左侧边栏**里
        （录制实测 div.side-bar 宽 164px）。面板确实收了，但指针从此停在
        边栏上，而 `page.mouse.wheel` 滚的是**指针底下那个元素**，
        于是接下来滚列表一下也滚不动，表现是"列表里没有新卡片了"。
        所以要挪到内容区（边栏右边、列表上方偏中间）。
        """
        if self._page is None:
            return
        try:
            await self._move_to(*await self._feed_point(), steps=10)
            await asyncio.sleep(random.uniform(0.4, 0.8))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 收起筛选面板失败：%s", exc)

    async def _feed_point(self) -> tuple:
        """笔记列表上一个适合停鼠标 / 滚轮的点。

        取 `div.feeds-container` 的中心；取不到就退回"边栏右边一点、
        竖直方向中间"这个经验值。绝不能落在 x < SIDEBAR_WIDTH 的地方。
        """
        default = (SIDEBAR_WIDTH + 260, 500)
        if self._page is None:
            return default
        try:
            got = await self._run(self._page.evaluate(
                """() => {
                    const el = document.querySelector('div.feeds-container')
                            || document.querySelector('div.feeds-wrapper');
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0) return null;
                    return [Math.round(r.left + r.width / 2),
                            Math.round(Math.min(Math.max(innerHeight / 2, 120),
                                                innerHeight - 80))];
                }"""))
        except Exception:  # noqa: BLE001
            got = None
        if not got or got[0] <= SIDEBAR_WIDTH:
            return default
        return (int(got[0]), int(got[1]))

    async def _park_over_comments(self) -> None:
        """把鼠标挪进笔记弹窗的**右侧评论栏**再滚。

        弹窗左边是图片区（`div.media-container`，经典版实测 968px 宽里占了
        大半），滚轮落在那上面滚的是图片轮播，不是评论——评论就永远停在
        第一页。评论区在 `div#noteContainer > div.interaction-container >
        div.note-scroller` 里，坐标现算，不写死。
        """
        if self._page is None:
            return
        try:
            got = await self._run(self._page.evaluate(
                """() => {
                    const el = document.querySelector('div.note-scroller')
                            || document.querySelector('div.comments-el')
                            || document.querySelector('div.interaction-container');
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return null;
                    return [Math.round(r.left + r.width / 2),
                            Math.round(r.top + r.height / 2)];
                }"""))
            if got:
                await self._move_to(int(got[0]), int(got[1]), steps=6)
                await asyncio.sleep(random.uniform(0.15, 0.3))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 鼠标挪到评论区失败：%s", exc)

    async def _element_box(self, label: str = "", selector: str = "") -> Optional[Dict]:
        """找一个**可见**元素的中心坐标。

        判据和 `_visible_texts()` 用的是**同一套**（自身文本节点 + 可见性），
        所以"日志里看得到"和"点得到"永远一致。
        上一版这里用的是 `get_by_text(...)` + Playwright 的可操作性判定，
        实跑的表现是"面板里的选项全打进日志了，却报『这一项没点中』"——
        看得见点不着，最难查的那种。
        """
        if self._page is None:
            return None
        try:
            return await self._run(self._page.evaluate(
                """({label, selector}) => {
                    const vis = (el) => {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return null;
                        if (r.bottom < 0 || r.top > innerHeight) return null;
                        const st = getComputedStyle(el);
                        if (st.visibility === 'hidden' || st.display === 'none') return null;
                        return {x: Math.round(r.left + r.width / 2),
                                y: Math.round(r.top + r.height / 2),
                                w: Math.round(r.width), h: Math.round(r.height)};
                    };
                    const pool = selector
                        ? document.querySelectorAll(selector)
                        : document.querySelectorAll('*');
                    let best = null;
                    for (const el of pool) {
                        if (label) {
                            const own = Array.from(el.childNodes)
                                .filter(n => n.nodeType === 3)
                                .map(n => n.textContent).join('').trim();
                            if (own !== label) continue;
                        }
                        const box = vis(el);
                        if (!box) continue;
                        // 同名的挑最小的那个：文字通常在最内层，
                        // 取到外层大容器的话点击会落在空白处
                        if (!best || box.w * box.h < best.w * best.h) best = box;
                    }
                    return best;
                }""", {"label": label, "selector": selector}))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 取元素坐标失败（%s/%s）：%s", label, selector, exc)
            return None

    async def _move_to(self, x: int, y: int, steps: int = 12) -> None:
        """把鼠标**一步步**挪过去，而不是瞬移。

        面板靠 hover 维持，瞬移到远处会先触发一次 mouseleave，
        面板在指针落地之前就收了。分步移动才是真人的样子，
        也是用户描述的动线：移到「筛选」→ 往下移到「最新」→ 再移到时间那项。
        """
        await self._run(self._page.mouse.move(x, y, steps=steps))
        self._mouse = (x, y)

    async def _open_filter_panel(self, ctx: CollectContext) -> bool:
        """把鼠标移到「筛选」上，等面板展开。

        ⚠️ **是 hover 展开，不是 click**（用户实测）。点一下反而可能 toggle 掉。
        判据是「排序依据」这类**组标题**出现——组标题是产品语义，
        不像容器 class 那样每次发版都变。
        """
        box = await self._element_box(selector=FILTER_ENTRY_SELECTOR)
        if box is None:
            ctx.log(f"[小红书] 没找到筛选入口 {FILTER_ENTRY_SELECTOR}，本次不筛选",
                    "warn")
            return False
        await self._move_to(box["x"], box["y"])
        if await self._wait_panel_open(4.0):
            return True
        # hover 没出来才试点一下——有的版本可能是点开的
        await self._run(self._page.mouse.click(box["x"], box["y"]))
        if await self._wait_panel_open(4.0):
            return True
        ctx.log("[小红书] 鼠标移到「筛选」上、又点了一下，面板都没展开"
                "（判据是「排序依据」这类组标题有没有出现）", "warn")
        return False

    async def _wait_panel_open(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for group in self.PANEL_GROUPS:
                if await self._element_box(label=group):
                    return True
            await asyncio.sleep(0.3)
        return False

    async def _click_option(self, text: str) -> bool:
        """点面板里的一个选项：先把鼠标挪过去，再按坐标点。

        全程用 `page.mouse` 而不是 `locator.click()`：
          · 分步移动 → 中途不会离开 hover 区域，面板不会提前收
          · 按坐标点 → 绕开 Playwright 的可操作性判定
            （上一版就是卡在这一步，日志里能看到选项却点不动）
        """
        # 先在**面板范围内**找，找不到才全页找。
        # 「最新」这类文案在页面别处也可能出现（推荐流的角标之类），
        # 全页找有概率点到面板外面去，那一下还会把面板 hover 掉。
        # 面板结构（录制实测）：
        #   div.filter-container > div.filters-wrapper > div.filters
        #     > div.tag-container > div.tags > span   ← span 的自身文本就是选项
        box = await self._element_box(
            label=text, selector=f"{FILTER_PANEL_SCOPE.replace(', ', ' span, ')} span")
        if box is None:
            box = await self._element_box(label=text)
        if box is None:
            return False
        await self._move_to(box["x"], box["y"], steps=8)
        await asyncio.sleep(random.uniform(0.15, 0.3))
        # 挪过去之后重新取一次坐标：面板可能有展开动画，位置会变
        again = await self._element_box(label=text)
        if again is not None:
            box = again
        try:
            await self._run(self._page.mouse.click(box["x"], box["y"]))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 点选项「%s」失败：%s", text, exc)
            return False

    async def _visible_texts(self) -> set:
        """页面上所有可见的短文本。用来对比出"面板里多了什么"——只用于写日志。"""
        if self._page is None:
            return set()
        try:
            got = await self._run(self._page.evaluate(
                """() => {
                    const out = [];
                    for (const el of document.querySelectorAll('*')) {
                        const own = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent).join('').trim();
                        if (!own || own.length > 8) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        const st = getComputedStyle(el);
                        if (st.visibility === 'hidden' || st.display === 'none') continue;
                        out.push(own);
                    }
                    return out;
                }"""))
            return set(got or [])
        except Exception:  # noqa: BLE001
            return set()

    async def _wait_search_after(self, since: float, timeout_s: float) -> bool:
        """等一个**落地时刻晚于 since** 的搜索响应。

        判据是时刻而不是"有没有响应"：只看有没有的话，会被上一次点击的
        迟到响应提前满足，整条筛选链错开一拍（抖音那边踩过，
        见 douyin_browser._note_search_response 的注释）。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._last_search_resp_at > since:
                return True
            await asyncio.sleep(0.3)
        return False

    # ================= 评论 =================
    async def _goto_work(self, ctx: CollectContext, work: WorkItem) -> bool:
        """采评论时到达这条笔记。

        绝大多数情况下页面**已经**停在它上面了——collect_by_keyword 刚打开过，
        评论也在那一下里一起请求了。这里只在被跳过、或中途换了页面时才重开。
        """
        # 基类 collect_comments 的第一步就是调它——用它记录"评论采集真的
        # 到达过这条"，好把"没评论"和"根本没来采"区分开
        self._comments_attempted_for = work.work_id
        if self._open_note_id == work.work_id:
            return True
        if not self._tokens.get(work.work_id):
            token = self._token_from_work(work)
            if token:
                self._tokens[work.work_id] = token
        return await self._open_note(ctx, work.work_id)

    @staticmethod
    def _token_from_work(work: WorkItem) -> str:
        """搜索阶段的 token 存进了 extra_content，续跑时从那里捞回来。"""
        try:
            extra = json.loads(getattr(work, "extra_content", "") or "{}")
        except Exception:  # noqa: BLE001
            return ""
        return nz.to_text(extra.get("xsec_token")) if isinstance(extra, dict) else ""

    async def _open_comments(self) -> bool:
        """把笔记弹窗**滚到评论区**。

        评论确实在打开笔记那一下就请求过了（录制实测：同一步里
        /v1/feed 和 /v2/comment/page 一起打），所以没有"点开评论"这个动作。
        但弹窗右栏一开是停在正文顶部的，评论在正文下面——用户反馈
        "没看见作品页滚动到评论位置"，说的就是这个：看起来像没在读评论。

        所以这里像真人一样往下滚，直到 div.comments-el 进入视野。
        这不只是"看着像"：有的笔记正文很长，评论区压根没进过视口，
        后续的翻页滚动是从正文里开始滚的，白滚好几屏。
        """
        ctx = self._ctx
        await asyncio.sleep(random.uniform(0.6, 1.2))
        await self._park_over_comments()
        for i in range(8):
            if await self._comments_in_view():
                if ctx is not None:
                    ctx.log(f"[小红书]      已滚到评论区"
                            f"（往下滚了 {i} 下）")
                self._scrolled_to_comments = True
                return True
            await self._run(self._page.mouse.wheel(0, random.randint(400, 700)))
            await asyncio.sleep(random.uniform(0.4, 0.8))
        if ctx is not None:
            ctx.log("[小红书]      滚了 8 下也没把评论区滚进视野"
                    "（这条笔记可能没有评论区）", "warn")
        return True

    async def _comments_in_view(self) -> bool:
        """评论区有没有进到视口里。

        判据是 div.comments-el 的位置，不是"滚了几下"：正文长短差很多，
        按次数滚要么不够要么滚过头。
        """
        if self._page is None:
            return False
        try:
            return bool(await self._run(self._page.evaluate(
                """() => {
                    const el = document.querySelector('div.comments-el');
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.height <= 0) return false;
                    return r.top < innerHeight * 0.9;
                }""")))
        except Exception:  # noqa: BLE001
            return False

    def _log_comment(self, ctx: CollectContext, item: CommentItem) -> None:
        """只数数，不打内容——评论的明细由 runner 统一打，避免打两遍。"""
        self._note_comment_count += 1

    async def _scroll_comments(self, max_scrolls: int = 0) -> None:
        """往下滚评论区，每滚一屏触发一页 comment/page，顺手展开回复。

        三个停止条件，缺一不可：
          1. 接口说 has_more=false —— 正常结束
          2. 连续几屏没有新的评论响应 —— 接口不再触发（到底了，或者被限流）
          3. 滚够上限 —— 兜底，别在异常页面上无限滚下去
        只留 1 的话，接口一旦不再返回就会一直滚到上限；只留 3 的话，
        每条笔记都要白滚 80 屏。
        """
        if self._page is None:
            return
        limit = max_scrolls or self.MAX_COMMENT_SCROLLS
        idle = 0
        ctx = self._ctx
        # 指针现在还停在刚才点卡片的位置——弹窗一开，那个位置很可能落在
        # 左边的图片区上。先挪进右边的评论栏，否则滚轮滚的是图片轮播
        await self._park_over_comments()
        # 每一屏都报一次进度：用户要的是"翻评论的时候能看到在翻什么"。
        # 只在**有变化**的那几屏说话，不然 80 屏会刷屏。
        # ⚠️ 这里不做"这条笔记一共多少条"的汇总：collect_comments 会在
        # 产出循环里**反复**调本函数（每次滚 5 屏），而 _captured_comments
        # 每取一次就被清空——在这儿汇总会打出一串「翻完 0 屏，0 条」。
        # 汇总放在 collect_by_keyword 里，一条笔记一行。
        seen = len(self._captured_comments)
        scrolled = 0
        for i in range(limit):
            if not self._comment_has_more:
                logger.info("[小红书] 评论接口 has_more=false，滚了 %d 屏收工", i)
                break
            before = self._last_comment_resp_at
            await self._run(self._page.mouse.wheel(0, random.randint(500, 900)))
            await asyncio.sleep(random.uniform(0.8, 1.4))
            expanded = await self._expand_replies()
            scrolled += 1
            now = len(self._captured_comments)
            if now > seen or expanded:
                if ctx is not None and expanded:
                    # 评论本身已经逐条打过了，这里只报"又展开了几处回复"——
                    # 不然每屏一行汇总 + 每条一行内容，同一件事说两遍
                    ctx.log(f"[小红书]        （第 {scrolled} 屏，"
                            f"展开了 {expanded} 处回复）")
                seen = now
            if self._last_comment_resp_at > before:
                idle = 0
            else:
                idle += 1
                if idle >= 4:
                    logger.info("[小红书] 连续 %d 屏没有新的评论响应，停止滚动", idle)
                    break
        else:
            logger.info("[小红书] 滚到上限 %d 屏，停止", limit)
        # 收尾再展开一轮：最后一屏加载出来的回复还没点过
        await self._expand_replies()
        self._note_scrolls += scrolled

    async def _expand_replies(self) -> int:
        """点开视口内的「展开 N 条回复」。

        快照 08 实测：`<div class="show-more">展开 3 条回复</div>`。
        点一下会打 /v2/comment/sub/page，回复就从拦截里进来了。
        只点视口内的：屏幕外的点了也不会加载，白白多几十次无效点击。
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
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            if (r.top < 0 || r.top > innerHeight) continue;
                            el.dataset.smcClicked = '1';
                            el.click();
                            return true;
                        }
                        return false;
                    }""", SHOW_MORE_SELECTOR))
                if not hit:
                    break
                clicked += 1
                await asyncio.sleep(random.uniform(0.5, 0.9))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[小红书] 展开回复失败：%s", exc)
        if clicked:
            logger.info("[小红书] 展开了 %d 处「展开 N 条回复」", clicked)
        return clicked

    async def _extract_new_comments(
        self, ctx: CollectContext, work_id: str
    ) -> List[CommentItem]:
        new_comments: List[CommentItem] = []
        for raw in self._captured_comments:
            comment_id = nz.to_text(raw.get("id"))
            if not comment_id or comment_id in self._seen_comment_ids:
                continue
            # 拦截是全局的，上一条笔记的迟到响应可能落到这一条头上——
            # 那会把别人的评论记到这条笔记名下，比少采严重得多
            if raw.get("_work_id") and raw["_work_id"] != work_id:
                continue
            self._seen_comment_ids.add(comment_id)
            is_reply = bool(raw.get("_is_reply"))
            # ⚠️ 父子关系的两个字段各管各的，别混：
            #   target_comment.id  = **直接回复的那一条**。回复别人的回复时
            #                        它是那条回复的 id，不是楼主评论的 id
            #   root_comment_id    = 整个楼的顶楼评论。sub/page 的**响应体里没有**
            #                        （用户给的真实数据核对过），只在请求 query 里，
            #                        所以在 _absorb_comments 里从 URL 取了塞进来
            # 只有一条回复时这两个恰好相等，容易看不出区别；一旦出现
            # "回复的回复"，拿 target 当 root 会把整棵树挂错层。
            target = raw.get("target_comment") or {}
            item = self._to_comment(
                ctx, raw, work_id,
                depth=2 if is_reply else 1,
                # 一级评论没有父级——就算响应里带了 target_comment 也不能用，
                # 否则 level_1 会莫名其妙多出一个父节点
                parent_id=(nz.to_text(raw.get("parent_comment_id"))
                           or nz.to_text(target.get("id"))) if is_reply else "",
                root_id=nz.to_text(raw.get("root_comment_id")) or comment_id,
            )
            if item:
                new_comments.append(item)
                self._log_comment(ctx, item)
        self._captured_comments.clear()
        return new_comments

    async def _extract_new_works(
        self, ctx: CollectContext, keyword: str
    ) -> List[WorkItem]:
        """基类的"边滚边产出"这里用不上——本类覆盖了 collect_by_keyword，
        走的是"先收齐再逐条补全"。留一个空实现，免得基类的默认版本抛异常。"""
        return []

    async def _switch_next_work(self) -> bool:
        """每条笔记各自打开，没有"就地翻下一条"这回事。

        顺手把弹窗关掉：留着它会挡住列表，下一条卡片点不到。
        """
        await self._close_modal()
        return True

    # ================= 数据映射 =================
    def _to_work(self, ctx: CollectContext, note: Dict, note_id: str,
                 xsec_token: str, source_keyword: str = "") -> Optional[WorkItem]:
        """复用接口版的映射：字段名、图片/视频的取法、时间戳单位全都一样，
        另写一份迟早会漂。

        ⚠️ 参数顺序必须和 XhsCollector._to_work 一致
        （ctx, note, note_id, xsec_token, source_keyword）。
        改造前这里是按 `(ctx, note, source_keyword=…)` 调的，一跑就 TypeError——
        这份文件此前从来没有真正跑起来过。
        """
        from .xhs import XhsCollector
        return XhsCollector._to_work(self, ctx, note, note_id, xsec_token,
                                     source_keyword=source_keyword)

    def _to_comment(self, ctx: CollectContext, raw: Dict, work_id: str,
                    depth: int = 1, parent_id: str = "",
                    root_id: str = "") -> Optional[CommentItem]:
        from .xhs import XhsCollector
        return XhsCollector._to_comment(self, ctx, raw, work_id,
                                        depth=depth, parent_id=parent_id,
                                        root_id=root_id)
