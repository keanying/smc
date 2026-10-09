"""抖音浏览器模拟采集器：从 network 拦截获取作品和评论。

⚠️ 本文件里的每一个选择器都来自 probe_out 真实页面快照（2026-08，登录态，
   关键字「盘山风景区」），不是推测。快照编号写在各处注释里，页面改版时
   重新跑一次 probe_page.py 对照着改即可。

采集流程：
  1. 打开 douyin.com/jingxuan → 在搜索框逐字输入 → 点搜索按钮
  2. 切「多列」模式（很重要，见下）→ 点「筛选」→ 选排序与发布时间
  3. 逐个作品：进 modal → 关联播 → 开评论区 → 滚动 + 展开回复 → 下一条

关于「多列」模式（快照 04/05 的关键差异）：
    单列模式下搜索走 /general/search/stream/（流式），多列模式下走
    /general/search/single/（一次性 JSON）。两条都实现了解析
    （_handle_search_stream / _handle_search_result），所以**切不到多列
    不等于采不到数据**——差别在分页方式：single 是一次性 JSON，
    滚动加载更清晰；stream 是 chunked 多段拼接，解析更脆。
    所以仍然优先切多列，切不到就退到 stream，不再当成致命错误。

拦截的 API（顺序敏感）：
  - /aweme/v1/web/comment/list/reply/     子评论（必须在 comment 之前）
  - /aweme/v1/web/comment/list/           一级评论
  - /aweme/v1/web/general/search/single/  搜索（多列模式）
  - /aweme/v1/web/general/search/stream/  搜索（单列模式，兜底）
  - /aweme/v1/web/aweme/detail/           视频详情

⚠️ 评论接口的域名是 www-hj.douyin.com，不是 www.douyin.com（快照 09/11 实测）。
   这里按路径片段匹配，与域名无关，所以两个域名都能拦到。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any, Dict, List, Optional

from ..core.constants import CHANNEL_DOUYIN
from ..core.logging import get_logger
from ..utils import normalize as nz
from .base import CollectContext, CommentItem, WorkItem
from .browser_capture import BrowserCaptureCollector

logger = get_logger(__name__)


class DouyinBrowserCollector(BrowserCaptureCollector):
    """抖音浏览器模拟采集器。"""

    channel = CHANNEL_DOUYIN
    integrated = False
    #: 借用的 DouyinCollector._to_work 里会用 self.host 拼 work_url，
    #: 不定义的话映射到一半才炸（和 _first_url 同一类问题）
    host = "https://www.douyin.com"

    @property
    def _target_url_patterns(self) -> List[Dict[str, str]]:
        return [
            # 注意顺序：reply 必须在 comment 之前，因为 reply URL 包含 comment 的 URL 前缀
            {"url_contains": "/aweme/v1/web/comment/list/reply/", "type": "comment_reply"},
            {"url_contains": "/aweme/v1/web/comment/list/", "type": "comment_list"},
            {"url_contains": "/aweme/v1/web/general/search/single/", "type": "search_result"},
            # 单列模式走的流式接口。正常流程会切到多列用不上它，
            # 但切换失败时它是唯一的数据来源，留着兜底。
            {"url_contains": "/aweme/v1/web/general/search/stream/", "type": "search_stream"},
            {"url_contains": "/aweme/v1/web/aweme/detail/", "type": "video_detail"},
        ]

    #: 首页。搜索从这里开始输入（快照 03）
    HOME_URL = "https://www.douyin.com/jingxuan"

    @property
    def _search_url_template(self) -> str:
        # 实测搜索结果落在 /jingxuan/search/ 下，不是 /search/（快照 04/05）
        return "https://www.douyin.com/jingxuan/search/{keyword}?type=general"

    @property
    def _work_url_template(self) -> str:
        """⚠️ 只在点卡片彻底失败时兜底用，**正常流程不走这里**。

        曾经想当然地认为「构造 ?modal_id=xxx 的 URL 就能复现 modal」，
        实测是错的：goto 会**重新加载整个搜索页**，结果是
          - 筛选状态被冲掉（排序退回综合、时间退回不限）
          - modal 根本没打开，URL 还被重定向成 /search/关键字?type=general
        真人的操作是在结果页上**点卡片**，页面不重载，modal 叠上来。
        所以正常路径见 _goto_work。
        """
        return "https://www.douyin.com/video/{work_id}"

    def __init__(self, config, proxy_manager):
        super().__init__(config, proxy_manager)
        #: 拦到第几个搜索响应了。用它判断"筛选点下去有没有真的重新搜"，
        #: 比读 class 稳（class 是构建产物），也比 expect_response 安全（见下）
        self._search_resp_seq = 0
        #: 是否还在"搜索/筛选"阶段。
        #: 这个阶段里 stream 代表"换了一批结果"，可以整体替换缓冲区；
        #: 一旦开始逐条采集，就只许追加——否则一个迟到的 stream 会把
        #: 已经滚动加载进来的 single 数据整片抹掉，而且已 yield 的作品
        #: 会因为 _seen_work_ids 被清空而重复产出。
        self._search_phase = True
        #: 缓冲区里已有的 aweme_id，避免 single 分页之间重复
        self._buffered_ids: set = set()
        #: 搜索阶段收到的第几次列表响应。
        #: 「搜索 → 最新发布 → 半年内」正常是 3 次，日志里能直接核对。
        self._phase_resp_no = 0
        #: 下一次列表响应是不是"一次全新搜索的第一批"。
        #: 只有它才该整体替换缓冲区；同一次搜索后面跟着的批次要追加。
        #: 点搜索 / 点任一筛选项之前置 True，替换发生后立刻置 False。
        self._replace_next = True
        #: 最近一次列表响应（stream / single）**落地的时刻**，time.monotonic()。
        #: 拦截回调跑在浏览器循环上、判断跑在服务器循环上，两个循环的
        #: loop.time() 不是同一条时间轴，所以这里必须用进程级的 monotonic。
        self._last_search_resp_at = 0.0

    def _note_search_response(self) -> None:
        """登记"又落地了一次列表响应"。序号 + 时刻，两个都只在这里改。

        以前只记序号，`_pick_filter_option` 用"序号变了"当作"筛选生效了"。
        实测这个判据会被**上一次点击的迟到响应**提前满足：

            19:59:20.882  stream 落地      ← 属于"综合排序"那次搜索
            19:59:20.9xx  点「最新发布」
            19:59:21.426  single 落地      ← 其实还是上一次点击的尾巴
            19:59:21.4xx  「最新发布」被判定生效 → 立刻去点「半年内」
            19:59:22.353  stream 落地      ← 这才是「最新发布」真正的结果

        也就是说筛选之间的因果关系整个错开了一拍。真正属于最后一个筛选
        的那批数据有没有被采用，全靠 _prepare_search 末尾那 2 秒静默兜底，
        属于侥幸。所以改成：点之前先等安静（把迟到的收干净），点之后要求
        看到一个**时刻晚于点击时刻**的响应。
        """
        self._search_resp_seq += 1
        self._last_search_resp_at = time.monotonic()

    async def _handle_captured_response(
        self, ctx: CollectContext, matched_type: str, url: str, body: Dict, response
    ) -> None:
        """处理拦截到的抖音 API 响应。"""
        if matched_type in ("search_result", "search_stream"):
            self._note_search_response()

        # ⚠️ single 是**主数据源**：搜索排序确定之后，作品（包括往下滚动
        # 加载出来的每一页）都从这个接口来。一律追加 + 按 aweme_id 去重，
        # 取用时再统一按 create_time 倒序（见 _extract_new_works）。
        if matched_type == "search_result":
            entries = body.get("data") or []
            awemes = []
            for entry in entries:
                aweme = entry.get("aweme_info")
                if not aweme:
                    mix_items = (entry.get("aweme_mix_info") or {}).get("mix_items") or []
                    aweme = mix_items[0] if mix_items else None
                if aweme:
                    awemes.append(aweme)
            # 规则和 stream 完全一致，只看**处在哪个阶段**：
            #   搜索/筛选阶段 → 每一次响应都是一批全新结果，整体替换。
            #     「搜索 → 点最新发布 → 点半年内」会打三次，取最后那次即可，
            #     替换语义天然做到这点，不需要额外记"这是第几次"。
            #   采集阶段     → 这是滚动加载的下一页，追加。
            in_search = getattr(self, "_search_phase", True)
            replace = self._should_replace()
            added = self._buffer_works(awemes, replace=replace)
            # 序号已经在函数开头 _note_search_response() 里加过了，这里不能再加
            if in_search:
                self._phase_resp_no = getattr(self, "_phase_resp_no", 0) + 1
                logger.info(
                    "[抖音] single 第 %d 次列表响应（搜索阶段）：%d 条 → %s。当前缓冲 %d 条",
                    self._phase_resp_no, len(awemes),
                    "整体替换（新一次搜索，之前那次作废）"
                    if replace else f"追加 {added} 条（同一次搜索的后续批次）",
                    len(self._captured_works))
            else:
                logger.info("[抖音] single 返回 %d 条 → 追加 %d 条（滚动加载），"
                            "当前缓冲 %d 条",
                            len(awemes), added, len(self._captured_works))

        elif matched_type == "video_detail":
            aweme = body.get("aweme_detail")
            if aweme:
                self._captured_works.append(aweme)

        elif matched_type == "comment_list":
            comments = body.get("comments") or []
            for comment in comments:
                comment["_work_id"] = self._extract_aweme_id(url, body)
            self._captured_comments.extend(comments)
            # 空评论检测
            if not comments:
                self._comment_api_empty_count += 1
                if self._comment_api_empty_count >= 2:
                    self._comment_api_empty = True
            else:
                self._comment_api_empty_count = 0
                self._comment_api_empty = False

        elif matched_type == "comment_reply":
            comments = body.get("comments") or []
            for comment in comments:
                comment["_work_id"] = self._extract_aweme_id(url, body)
                comment["_is_reply"] = True
            self._captured_comments.extend(comments)

    def _should_replace(self) -> bool:
        """这一批列表数据该整体替换缓冲区，还是追加？

        ⚠️ 这里踩过一个很贵的坑，别改回去。

        原来的规则是"搜索阶段一律替换"。但实测一次筛选点击会打**两个**接口：

            19:59:22.353  stream  10 条   ← 「半年内」这次搜索的第一批
            19:59:22.859  single   9 条   ← 同一次搜索的第二批（接着往下的一页）

        按"搜索阶段一律替换"，后面那 9 条会把前面那 10 条整片抹掉。
        而列表是按发布时间倒序排的，被抹掉的恰恰是**最新的那 10 条**。
        用户看到的现象就是：明明选了「最新发布 + 半年内」，采到的最新一条
        却停在 12 天前——不是没采到，是被同一次搜索的第二页顶掉了。

        正确的界线不是"阶段"，而是"**是不是换了一次搜索**"：
        点搜索、点每一个筛选项，都会开启一次新搜索 → 那一批替换；
        紧随其后的批次（以及采集阶段的滚动加载）→ 一律追加 + 去重。
        """
        if not getattr(self, "_search_phase", True):
            return False          # 采集阶段：滚动加载，只追加
        if getattr(self, "_replace_next", True):
            self._replace_next = False
            return True           # 新搜索的第一批：作废上一次搜索的结果
        return False              # 同一次搜索的后续批次：追加

    def _buffer_works(self, awemes: List[Dict], *, replace: bool) -> int:
        """把作品放进缓冲区。replace=True 表示"这是一批全新的结果"。

        不管从哪个接口来，最终都汇进这一个缓冲区，由 _extract_new_works
        统一按 create_time 倒序取用——这样"从哪来"就不影响"按什么顺序点"。
        """
        # 拦截回调是异步触发的，理论上可能早于某些初始化，
        # 所以这里按需兜底，不假设属性一定存在
        if not hasattr(self, "_buffered_ids"):
            self._buffered_ids = set()
        if replace:
            self._captured_works.clear()
            self._buffered_ids.clear()
            self._seen_work_ids.clear()
        added = 0
        for aweme in awemes:
            aid = nz.to_text(aweme.get("aweme_id"))
            if not aid or aid in self._buffered_ids:
                continue
            self._buffered_ids.add(aid)
            self._captured_works.append(aweme)
            added += 1
        return added

    async def _handle_captured_raw(
        self, ctx: CollectContext, matched_type: str, url: str, text: str, response
    ) -> None:
        """处理 general/search/stream 的流式响应。

        实测的正文长这样（用户提供的抓包样例）：

            af7c{"status_code":0,"data":[...]}{"ack":...}{"status_code":0,"data":[...]}…

        三个特征，缺一个都解不出来：
          1. 开头 `af7c` 是 HTTP chunked 的长度标记，不是 JSON
          2. 后面是**多个** JSON 对象首尾相接，不是一个
          3. 中间夹着 {"ack":...} 这种心跳对象，没有 data

        所以只能用 JSONDecoder.raw_decode 一个一个往下啃。

        ⚠️ 更关键的是语义：**stream = 一次全新的搜索**。
        搜索一次、点「最新发布」一次、点「半年内」一次——每次都会重新发 stream，
        每次的结果都是一个**完整的新列表**，把上一次整个作废。
        所以这里拿到 stream 就清空缓冲区，而不是追加；
        最后留下的自然就是最后一次筛选的那一批。
        （滚动加载更多走的是 single，那个才是追加。）
        """
        if matched_type != "search_stream":
            return

        # 先登记"这一刻确实落地了一次搜索响应"。
        # 就算下面一条作品都没解出来（比如全是心跳），对"筛选点下去有没有
        # 重新搜"这个判断来说它也是有效信号，不能因为没数据就不记时刻。
        self._note_search_response()

        decoder = json.JSONDecoder()
        objs: List[Dict] = []
        pos, n = 0, len(text)
        while pos < n:
            # 跳过 chunk 长度标记、换行、以及对象之间的任何噪音
            while pos < n and text[pos] not in "{[":
                pos += 1
            if pos >= n:
                break
            try:
                obj, end = decoder.raw_decode(text, pos)
                objs.append(obj)
                pos = end
            except ValueError:
                pos += 1

        fresh: List[Dict] = []
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            entries = obj.get("data")
            if not isinstance(entries, list):
                continue  # {"ack":...} 这类心跳对象
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                aweme = entry.get("aweme_info")
                if not aweme:
                    mix = (entry.get("aweme_mix_info") or {}).get("mix_items") or []
                    aweme = mix[0] if mix else None
                if aweme:
                    fresh.append(aweme)

        if not fresh:
            logger.debug("[抖音] 流式搜索响应里没解出作品（%d 个 JSON 对象）", len(objs))
            return

        # 搜索/筛选阶段：这是一批全新结果，整体替换。
        # 采集开始之后：只追加——迟到的 stream 不能把已经滚出来的数据抹掉。
        replace = self._should_replace()
        added = self._buffer_works(fresh, replace=replace)
        logger.info("[抖音] stream 解出 %d 条（%d 个 JSON 分块）→ %s，当前缓冲 %d 条",
                    len(fresh), len(objs),
                    "整体替换（新一次搜索）" if replace else f"追加 {added} 条",
                    len(self._captured_works))

    @staticmethod
    def _extract_aweme_id(url: str, body: Dict) -> str:
        """从 URL 或响应体中提取 aweme_id。"""
        match = re.search(r"aweme_id=(\d+)", url)
        if match:
            return match.group(1)
        if isinstance(body, dict):
            if "aweme_id" in body:
                return str(body["aweme_id"])
            detail = body.get("aweme_detail", {})
            if detail and "aweme_id" in detail:
                return str(detail["aweme_id"])
        return ""

    async def _extract_new_works(
        self, ctx: CollectContext, keyword: str
    ) -> List[WorkItem]:
        """从捕获的响应中提取新作品。"""
        # 按发布时间**倒序**取：页面在「最新发布」模式下就是这个顺序，
        # 保持一致才能"页面上第几张卡片 = 我要点的第几条"。
        # 接口返回本来就大致有序，但多次响应合并后未必，所以显式排一次。
        ordered = sorted(
            self._captured_works,
            key=lambda a: nz.to_int(a.get("create_time")) or 0, reverse=True,
        )
        new_works = []
        for aweme in ordered:
            aweme_id = nz.to_text(aweme.get("aweme_id"))
            if not aweme_id or aweme_id in self._seen_work_ids:
                continue
            self._seen_work_ids.add(aweme_id)
            work = self._to_work(ctx, aweme, source_keyword=keyword)
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
            comment_id = nz.to_text(comment.get("cid"))
            if not comment_id or comment_id in self._seen_comment_ids:
                continue
            if comment.get("_work_id") and comment["_work_id"] != work_id:
                continue
            self._seen_comment_ids.add(comment_id)
            item = self._to_comment(
                ctx, comment, work_id,
                depth=2 if comment.get("_is_reply") else 1,
                parent_id=nz.to_text(comment.get("reply_id")) or "",
                root_id=nz.to_text(comment.get("reply_id")) or comment_id,
            )
            if item:
                new_comments.append(item)
        self._captured_comments.clear()
        return new_comments

    # ---------------- 抖音专用操作 ----------------
    # ---------------- 搜索：模拟真人输入 ----------------
    #: 点筛选前 / 确认后，要求"连续这么多秒没有新的列表响应"才算安静。
    SEARCH_QUIET_SECONDS = 0.8
    #: 等安静最多等这么久（还在持续来数据就不等了，按现状继续）。
    SEARCH_QUIET_TIMEOUT = 8.0
    #: 点了筛选之后，最多等这么久确认"搜索确实重新发起了"。
    FILTER_CONFIRM_TIMEOUT = 15.0

    #: 筛选面板的分组标签 → 选项文案（快照 06/07 实测）
    #: 面板结构：分组标签在上（y=124/200/276/352/428），
    #: 对应选项行在标签下方约 28px 处。
    #: ⚠️「不限」在四个分组里都出现，全局按文本找必然点错，只能按分组定位。
    SORT_LABELS = {
        "general": "综合排序",
        "latest": "最新发布",
        # 快照里 排序依据 那一行只有「综合排序」「最新发布」两档，
        # 没看到「最多点赞」。留着映射，找不到会记日志跳过，不报错。
        "most_like": "最多点赞",
    }
    TIME_LABELS = {
        "unlimited": "不限", "day": "一天内",
        "week": "一周内", "half_year": "半年内",
    }

    async def _goto_search(self, ctx: CollectContext, keyword: str) -> None:
        """在搜索框逐字输入关键字，再点搜索按钮。

        选择器来自快照 03：
            [data-e2e="searchbar-input"]   placeholder="搜索你感兴趣的内容"
            [data-e2e="searchbar-button"]  文案「搜索」
        两个都是 data-e2e，比 class 稳得多（class 每次构建都可能变）。
        """
        # 同一个采集器会跑多个关键字，每个关键字都要重新进入搜索阶段
        self._search_phase = True
        self._phase_resp_no = 0
        # 这次搜索的第一批结果要作废上一个关键字的残留（只作废一次，见 _should_replace）
        self._replace_next = True
        if not hasattr(self, "_buffered_ids"):
            self._buffered_ids = set()
        self._buffered_ids.clear()
        ctx.log(f"[抖音] 打开首页 {self.HOME_URL}")
        await self._run(self._page.goto(
            self.HOME_URL, wait_until="domcontentloaded", timeout=60_000))
        await asyncio.sleep(random.uniform(1.5, 2.5))

        try:
            box = self._page.locator('[data-e2e="searchbar-input"]').first
            await self._run(box.click(timeout=8000))
            await asyncio.sleep(random.uniform(0.2, 0.5))
            # 先清空：搜索框里可能残留上一个关键字
            await self._run(box.fill(""))
            # 逐字输入，字间隔随机——一次性 fill 出来的输入事件不像真人
            for ch in keyword:
                await self._run(box.type(ch, delay=random.uniform(60, 180)))
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await self._run(
                self._page.locator('[data-e2e="searchbar-button"]').first.click(timeout=8000))
            ctx.log(f"[抖音] 已输入关键字并点击搜索：{keyword}")
            await self._run(
                self._page.wait_for_load_state("domcontentloaded", timeout=30_000))
        except Exception as exc:  # noqa: BLE001
            # 输入框找不到就退回直开 URL——能采到数据比"像真人"重要
            ctx.log(f"[抖音] 模拟输入失败（{exc}），退回直接打开搜索 URL", "warn")
            await super()._goto_search(ctx, keyword)

    async def collect_by_keyword(self, ctx: CollectContext, keyword: str):
        """抖音专用：**先收集齐、再一次性排序、然后按倒序逐条产出**。

        为什么要覆盖基类的做法：基类是"取一批 → 产出 → 滚动 → 再取一批"，
        每批各自排序。可 single 是分页到货的，第 2 页里完全可能有比第 1 页
        更新的作品（页面刚好在这期间刷新了一批），于是产出顺序就乱了——
        表现就是"点开作品的顺序和页面上从上到下的顺序对不上"。

        改成两段式：
          1. 先只管滚动，把作品收进缓冲区，直到够数或者滚不出新的了
          2. 全部到手后**整体**按 create_time 倒序排一次，再逐条产出
        这样"第 N 个产出的作品"就严格等于"按发布时间倒数第 N 条"，
        和页面上「最新发布」的排列一致，点击顺序自然也就对了。

        代价是首条作品要等收集完才出来（慢几十秒），但换来顺序可预期，
        而且采集本来就是后台任务，这点延迟无所谓。
        """
        self._current_keyword = keyword
        self._is_capturing = True
        self._ctx = ctx

        await self._goto_search(ctx, keyword)
        await asyncio.sleep(random.uniform(2, 4))
        await self._prepare_search(ctx, keyword)
        self.note_step(f"「{keyword}」搜索结果已就绪，开始逐条采集")

        target = int(ctx.max_works or 0) or 100
        try:
            # ---- 第 1 段：滚动收集，直到够数或没有新的了 ----
            # ⚠️ 这里的"收够 target 就停止滚动"依赖一个前提：
            # 「最新发布」排序下，抖音的分页本身就是从新到旧，
            # 所以后面的页只会更旧，不会藏着更新的作品。
            # 前提不成立的话（比如换成「最多点赞」排序），
            # 停早了就可能漏掉更新的——那种排序下这个策略要重新想。
            stagnant, rounds = 0, 0
            while len(self._captured_works) < target and stagnant < 3 and rounds < 60:
                ctx.raise_if_cancelled()
                before = len(self._captured_works)
                fp_before = await self._page_scroll_fingerprint()
                await self._simulate_scroll(times=2)
                await self._simulate_click_load_more()
                await asyncio.sleep(random.uniform(1.2, 2.0))
                rounds += 1
                fp_after = await self._page_scroll_fingerprint()
                page_moved = fp_after.get("scrollSum") != fp_before.get("scrollSum")
                if len(self._captured_works) == before:
                    stagnant += 1
                    ctx.log(f"[抖音] 第 {rounds} 轮滚动没有新作品"
                            f"（页面{'有' if page_moved else '没有'}滚动，"
                            f"当前 {len(self._captured_works)} 条）", "warn")
                else:
                    stagnant = 0
                    ctx.log(f"[抖音] 已收集 {len(self._captured_works)}/{target} 条作品"
                            f"（滚动 {rounds} 轮）")
            if stagnant >= 3:
                # 一条都没收到就停下，先确认不是被跳回登录页了。
                # 掉登录时页面同样是空的，表现和"这个筛选条件下就这么多"一样。
                if not self._captured_works:
                    await self.recheck_login(ctx, reason="一条作品都没收到")
                fp = await self._page_scroll_fingerprint()
                ctx.log(f"[抖音] 连续 3 轮没有新作品，收集结束，共 {len(self._captured_works)} 条。"
                        f"页面高度 {fp.get('bodyH')}，滚动位置 {fp.get('scrollSum')}"
                        f"——若页面确实在滚动却仍无新作品，说明这个筛选条件下就这么多")

            # ---- 第 2 段：整体排序，按倒序产出 ----
            ordered = sorted(
                self._captured_works,
                key=lambda a: nz.to_int(a.get("create_time")) or 0, reverse=True,
            )
            ctx.log(f"[抖音] 收集完毕共 {len(ordered)} 条，已按发布时间倒序，"
                    f"开始逐条点开（上限 {target} 条）")
            if ordered:
                newest = nz.to_datetime(ordered[0].get("create_time"))
                oldest = nz.to_datetime(ordered[-1].get("create_time"))
                ctx.log(f"[抖音] 时间跨度：{newest} → {oldest}")

            emitted = 0
            for aweme in ordered:
                ctx.raise_if_cancelled()
                if emitted >= target:
                    ctx.log(f"[抖音] 已达上限 {target} 条，停止产出")
                    break
                aweme_id = nz.to_text(aweme.get("aweme_id"))
                if not aweme_id or aweme_id in self._seen_work_ids:
                    continue
                self._seen_work_ids.add(aweme_id)
                work = self._to_work(ctx, aweme, source_keyword=keyword)
                if work is None:
                    continue
                emitted += 1
                ctx.log(f"[抖音] 第 {emitted}/{min(len(ordered), target)} 条："
                        f"{aweme_id}  {work.publish_time}")
                yield work
            self._captured_works.clear()
        finally:
            self._is_capturing = False

    async def _prepare_search(self, ctx: CollectContext, keyword: str) -> None:
        """搜索结果出来后：先切多列，再点筛选。顺序不能反——
        切换列表模式会重新拉一次搜索，会把刚设好的筛选冲掉。
        """
        await self._ensure_grid_mode(ctx)
        applied = await self._apply_filters(ctx)

        # 缓冲区里现在应该是**最后一次 stream 返回的那个完整列表**
        # ——每来一次 stream 就整体替换一次（见 _handle_captured_raw），
        # 所以"搜索 → 点最新发布 → 点半年内"三次 stream 之后，
        # 留下的自然是最后那一次，也就是「最新发布 + 半年内」的结果。
        # 这里只做核对：为 0 说明最后那次响应还没落地或没解出来。
        if applied:
            for _ in range(6):
                if self._captured_works:
                    break
                await asyncio.sleep(0.8)
            n = len(self._captured_works)
            if n:
                ctx.log(f"[抖音] 筛选后拦到 {n} 条结果，只采这一批"
                        f"（筛选前的已在点击时丢弃）")
            else:
                ctx.log("[抖音] 筛选生效了，但没拦到筛选后的搜索结果——"
                        "接下来靠向下滚动触发 single 拿数据", "warn")

        # ⚠️ 关阶段之前先静默等一会儿。
        # 「搜索 → 最新发布 → 半年内」会打三次列表接口，最后那次可能比
        # _apply_filters 返回还晚一点到。这时候要是已经把阶段关了，
        # 它就会被当成"滚动加载"去**追加**——结果缓冲区里混着
        # 「最新发布」那一批和「半年内」那一批，取到的就不是最新那次了。
        # 所以：等到连续 2 秒没有新的列表响应，才认为搜索阶段真的结束。
        if not await self._wait_search_quiet(2.0, timeout=20.0):
            ctx.log("[抖音] 等了 20 秒列表接口还在持续返回，先按现有结果开始采集", "warn")

        self._search_phase = False
        ctx.log(f"[抖音] 搜索阶段结束：共收到 {getattr(self, '_phase_resp_no', 0)} 次列表响应，"
                f"采用最后一次的 {len(self._captured_works)} 条，"
                f"之后按 create_time 倒序逐条点开")
        self._report_time_span(ctx)

    def _report_time_span(self, ctx: CollectContext) -> None:
        """把搜索阶段拿到的这批作品的时间跨度打出来，并对明显不对劲的情况报警。

        用户实测遇到过：排序选了「最新发布」+「半年内」，结果最新的一条是
        12 天前的。那说明缓冲区里留下的**不是**最后一次筛选的结果——
        要么筛选没真的生效，要么被上一次点击的迟到响应顶替了。
        这个日志就是让这种情况一眼可见，不用再回头翻时间戳对账。
        """
        stamps = [nz.to_int(a.get("create_time")) or 0 for a in self._captured_works]
        stamps = [t for t in stamps if t > 0]
        if not stamps:
            ctx.log("[抖音] 缓冲区里的作品没有 create_time，无法核对时间跨度", "warn")
            return
        newest, oldest = max(stamps), min(stamps)
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))  # noqa: E731
        ctx.log(f"[抖音] 本批 {len(stamps)} 条作品时间跨度：{fmt(newest)} → {fmt(oldest)}")
        lag_days = (time.time() - newest) / 86400.0
        if lag_days > 3:
            ctx.log(f"[抖音] ⚠️ 排序按「最新发布」，但最新的一条已经是 "
                    f"{lag_days:.1f} 天前（{fmt(newest)}）。"
                    f"通常意味着留在缓冲区里的不是最后一次筛选的结果——"
                    f"检查上面「已选中筛选项」几条日志的先后顺序", "warn")

    #: 列表模式开关的文案。抖音这个开关是"显示对面模式"的语义：
    #: 当前是单列时按钮写「多列」，切过去之后按钮变成「单列」。
    #: 所以找不到「多列」有两种完全不同的含义，见 _scan_mode_toggle。
    GRID_LABEL = "多列"
    SINGLE_LABEL = "单列"
    #: 工具栏渲染完的最长等待。无头模式下整条流程更快，
    #: 探测时刻更早，这个等待就是给页面留出挂载时间的。
    GRID_TOGGLE_TIMEOUT = 12.0

    async def _scan_mode_toggle(self) -> Dict[str, Any]:
        """扫一遍页面，看「多列」/「单列」这两个开关分别在不在、可不可见。

        为什么不用 get_by_text 直接判断：文案可能挂在 title / aria-label 上
        （图标按钮很常见），也可能被包在一层 span 里。这里一次扫描把
        「文本节点」和「属性」两条路都覆盖掉，返回结构化结果，
        调用方才能区分"页面还没渲染好"和"本来就已经是多列了"。
        """
        try:
            return await self._run(self._page.evaluate(
                """(labels) => {
                    const out = {};
                    for (const label of labels) out[label] = {text: 0, attr: 0};
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        const st = getComputedStyle(el);
                        return st.visibility !== 'hidden' && st.display !== 'none';
                    };
                    for (const el of document.querySelectorAll('*')) {
                        // 只看元素**自己**的文本节点，避免整个 body 都被算成命中
                        const own = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent).join('').trim();
                        for (const label of labels) {
                            if (own === label && visible(el)) out[label].text++;
                            const attr = (el.getAttribute('title') || '') + '|' +
                                         (el.getAttribute('aria-label') || '');
                            if (attr.includes(label) && visible(el)) out[label].attr++;
                        }
                    }
                    return out;
                }""", [self.GRID_LABEL, self.SINGLE_LABEL]))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[抖音] 扫列表模式开关失败：%s", exc)
            return {}

    @staticmethod
    def _toggle_present(scan: Dict[str, Any], label: str) -> bool:
        info = (scan or {}).get(label) or {}
        return bool(info.get("text") or info.get("attr"))

    def _grid_toggle_locator(self, label: str):
        """「多列」/「单列」的定位器：先按文本，再按 title/aria-label 兜底。"""
        # ⚠️ 不能写成 `locator('text="多列", [title*="多列"]')`：
        # 逗号并列走的是 CSS 引擎，`text=` 不是合法 CSS，整条选择器会匹配不到。
        # 用 or_ 把三种定位方式并起来才对。
        return (
            self._page.get_by_text(label, exact=True)
            .or_(self._page.locator(f'[title*="{label}"]'))
            .or_(self._page.locator(f'[aria-label*="{label}"]'))
        ).first

    async def _ensure_grid_mode(self, ctx: CollectContext) -> bool:
        """切到「多列」。快照 05 证明多列走 single/ 接口，单列走 stream/。

        ⚠️ 这里以前写的是 `btn.is_visible(timeout=3000)`。那个 timeout 是
        **被忽略**的（Playwright 的 is_visible 不等待，实测 91ms 就返回），
        所以整段逻辑实际是"看一眼，没有就放弃"。无头模式跑得更快、探测更早，
        于是稳定复现「没找到「多列」按钮」。改成真的等。
        """
        deadline = time.monotonic() + self.GRID_TOGGLE_TIMEOUT
        scan: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            scan = await self._scan_mode_toggle()
            if (self._toggle_present(scan, self.GRID_LABEL)
                    or self._toggle_present(scan, self.SINGLE_LABEL)):
                break
            await asyncio.sleep(0.4)

        # 已经是多列：按钮此时写的是「单列」（点它会切回去），不要点
        if (not self._toggle_present(scan, self.GRID_LABEL)
                and self._toggle_present(scan, self.SINGLE_LABEL)):
            ctx.log("[抖音] 当前已经是多列模式（开关显示「单列」），无需切换")
            return True

        if not self._toggle_present(scan, self.GRID_LABEL):
            ctx.log(f"[抖音] 等了 {self.GRID_TOGGLE_TIMEOUT:.0f} 秒也没等到「多列」开关，"
                    f"保持当前模式——搜索结果改从 stream/ 接口拿（有兜底解析，"
                    f"数据不会丢，只是分页方式不同）", "warn")
            return False

        try:
            btn = self._grid_toggle_locator(self.GRID_LABEL)
            if not await self._wait_visible(btn, timeout_s=4.0):
                ctx.log("[抖音] 「多列」开关扫到了但定位不到，保持当前模式", "warn")
                return False
            # 换列表模式会重新拉一次搜索，同样是"新一批作废旧一批"
            self._replace_next = True
            await self._run(btn.click(timeout=8000))
            await asyncio.sleep(random.uniform(1.0, 1.8))
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[抖音] 点「多列」失败：{exc}", "warn")
            return False

        # 点完核对一次。抖音这个开关是"显示对面模式"的写法，切过去之后
        # 文案会变成「单列」。但不能把"「多列」必须消失"当成硬条件——
        # 万一它其实是个两个字都常驻的分段控件，那样判就会把成功当成失败，
        # 白报一条警告。所以分三种情况说清楚。
        for _ in range(8):
            after = await self._scan_mode_toggle()
            grid = self._toggle_present(after, self.GRID_LABEL)
            single = self._toggle_present(after, self.SINGLE_LABEL)
            if single and not grid:
                ctx.log("[抖音] 已切换到多列模式（搜索接口走 single/）")
                return True
            if single and grid:
                ctx.log("[抖音] 已点「多列」；页面上「多列」「单列」两个字都在，"
                        "无法用文案确认，按已切换处理")
                return True
            await asyncio.sleep(0.4)
        ctx.log("[抖音] 点了「多列」但开关没翻过来，按未切换处理"
                "（结果会走 stream/ 兜底）", "warn")
        return False

    async def _apply_filters(self, ctx: CollectContext) -> bool:
        """点开筛选面板，按任务参数选排序与发布时间。

        判断筛选是否真的生效，不看 class（class 是构建产物，随时会变），
        而是等一个新的 general/search/ 请求——快照 06 与 07 的差别正是
        07 多出了 search/stream 与 search/single 两个请求。
        """
        filters = ctx.search_filters
        sort_label = self.SORT_LABELS.get(getattr(filters, "sort", "general"))
        time_label = self.TIME_LABELS.get(getattr(filters, "publish_within", "unlimited"))
        if sort_label in (None, "综合排序") and time_label in (None, "不限"):
            ctx.log("[抖音] 排序与时间都是默认值，跳过筛选面板")
            return True

        # ⚠️ 点一次不一定就开：抖音这个面板有时要点两次（opinion-hub 的
        # 「核心修复」第 4 条记着这事），而且开了之后还有动画时间。
        # 所以这里不靠固定 sleep，而是**轮询等分组标签出现**——
        # 标签在了才说明面板真的渲染完了，可以往下点选项。
        #
        # ⚠️ 还有一层：第一次点常常是**点空**的。元素已经在 DOM 里、也可见，
        # Playwright 就认为可以点了，但抖音的事件处理器还没绑上去，
        # 这一下就落在了"渲染完成"和"可交互"之间的缝里。
        # 无头模式没有窗口管理和真实绘制开销，整条流程更快，更容易踩进这条缝
        # ——用户报的「点了「筛选」但面板没出现（第 1 次）」就是它。
        # 对策：点之前先等入口稳定可见 + 一小段静置，别一渲染出来就抢着点。
        opened = False
        entry = self._page.get_by_text("筛选", exact=True).first
        if not await self._wait_visible(entry, timeout_s=10.0):
            ctx.log("[抖音] 等了 10 秒没看到「筛选」入口，本次不筛选", "warn")
            return False
        await asyncio.sleep(random.uniform(0.5, 0.9))   # 留给事件绑定

        for attempt in (1, 2, 3):
            try:
                entry = self._page.get_by_text("筛选", exact=True).first
                await self._run(entry.click(timeout=8000))
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"[抖音] 第 {attempt} 次点「筛选」失败：{exc}", "warn")
                await asyncio.sleep(0.8)
                continue
            if await self._wait_filter_panel(timeout_s=5.0):
                opened = True
                if attempt > 1:
                    ctx.log(f"[抖音] 筛选面板已展开（点了 {attempt} 次）")
                else:
                    ctx.log("[抖音] 筛选面板已展开")
                break
            if attempt < 3:
                # 第一次点空是这个页面的常态（渲染完成 ≠ 事件已绑），
                # 重试一下就好，不必当成异常吓人；第二次还不行才值得警告。
                ctx.log(f"[抖音] 第 {attempt} 次点「筛选」没打开，等一下重试"
                        f"（页面事件多半还没绑上）",
                        "info" if attempt == 1 else "warn")
                await asyncio.sleep(1.0)
        if not opened:
            ctx.log("[抖音] 筛选面板始终没展开，本次不筛选（重跑 probe_page.py "
                    "拍一张筛选面板，看分组标签是不是改名了）", "warn")
            return False

        ok = True
        if sort_label and sort_label != "综合排序":
            ok &= await self._pick_filter_option(ctx, "排序依据", sort_label)
        if time_label and time_label != "不限":
            ok &= await self._pick_filter_option(ctx, "发布时间", time_label)

        # 关掉面板，免得挡住结果列表
        try:
            await self._run(self._page.keyboard.press("Escape"))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(random.uniform(1.0, 2.0))
        return ok

    async def _wait_filter_panel(self, timeout_s: float = 4.0) -> bool:
        """轮询等筛选面板渲染出来：以「排序依据」这个分组标签出现为准。

        用标签而不是用面板容器判断，是因为标签文案是产品语义（不轻易改），
        容器 class 是构建产物（每次发版都可能变）。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                found = await self._run(self._page.evaluate(
                    """() => Array.from(document.querySelectorAll('*')).some(el => {
                        const own = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent).join('').trim();
                        if (own !== '排序依据') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    })"""))
                if found:
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.4)
        return False

    async def _pick_filter_option(
        self, ctx: CollectContext, group_label: str, option_text: str
    ) -> bool:
        """在指定分组里点选项。

        为什么按坐标而不按 DOM 层级：面板里「不限」出现四次（发布时间、
        视频时长、搜索范围、内容形式各一个），全局文本匹配必然点错；
        而分组标签与其选项行的**几何关系**是稳定的——标签在上，
        选项行在标签下方 20~70px 之内（快照 06 实测：标签 y=124/200/276，
        选项行 y=152/228/304）。层级结构会随改版变，这个几何关系不会。
        """
        try:
            hit = await self._run(self._page.evaluate(
                """([group, option]) => {
                    const all = Array.from(document.querySelectorAll('*'));
                    const own = el => Array.from(el.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent).join('').trim();
                    const label = all.find(el => own(el) === group);
                    if (!label) return { error: 'no_group' };
                    const lr = label.getBoundingClientRect();
                    const cands = all.filter(el => {
                        if (own(el) !== option) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0
                            && r.top > lr.top && r.top - lr.bottom < 60;
                    });
                    if (!cands.length) return { error: 'no_option' };
                    const r = cands[0].getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                }""",
                [group_label, option_text],
            ))
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[抖音] 定位筛选项「{group_label}/{option_text}」出错：{exc}", "warn")
            return False

        if not isinstance(hit, dict) or hit.get("error"):
            reason = {"no_group": f"找不到分组标签「{group_label}」",
                      "no_option": f"分组「{group_label}」里没有「{option_text}」"}.get(
                          hit.get("error") if isinstance(hit, dict) else "", "未知原因")
            ctx.log(f"[抖音] 筛选未生效：{reason}（页面可能改版了，"
                    f"重跑 probe_page.py 看筛选面板）", "warn")
            return False

        # ⚠️ 这里**不能**用 `async with self._page.expect_response(...)`。
        # 那个上下文管理器的 __aexit__ 要在"页面所属的循环"上等结果，
        # 而我们跑在服务器循环上——await 永远不会返回，连 timeout 都不会触发
        # （超时也是由那个循环驱动的）。症状就是点完就卡死，日志停在这一行。
        # 换成自己看拦截回调登记的"最近一次列表响应时刻"：回调跑在浏览器
        # 循环上、只写一个 float，我们在自己的循环里只读，两边不打架。
        # 不用手工清缓冲了：点筛选会触发一次新的 stream，
        # 而 stream 的处理逻辑本身就是"新列表整体替换旧的"（见 _handle_captured_raw）。
        #
        # ⚠️ 点之前必须先等安静（见 _note_search_response 的说明）：
        # 上一次点击的响应可能还在路上，落地时刻会晚于我们这次的点击时刻，
        # 于是"看到一个更晚的响应"这个判据被它冒名顶替，整条筛选链错开一拍。
        # 先把在途的收干净，时间轴才是干净的。
        await self._wait_search_quiet(self.SEARCH_QUIET_SECONDS,
                                      timeout=self.SEARCH_QUIET_TIMEOUT)

        # 这一点下去会开启一次全新的搜索：它带回来的第一批数据要作废上一批，
        # 但**只作废一次**——紧跟着的第二批是同一次搜索的续页，必须追加。
        self._replace_next = True
        click_at = time.monotonic()
        try:
            await self._run(self._page.mouse.click(hit["x"], hit["y"]))
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[抖音] 点击筛选项「{group_label}/{option_text}」失败：{exc}", "warn")
            return False

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.FILTER_CONFIRM_TIMEOUT
        while loop.time() < deadline:
            if self._search_resp_at() > click_at:
                ctx.log(f"[抖音] 已选中筛选项「{group_label} = {option_text}」"
                        f"（点击后 {self._search_resp_at() - click_at:.2f}s "
                        f"拿到新的搜索结果）")
                # 结果可能不止一批（stream 之后还跟着一次 single），
                # 等它整批落完再返回，免得下一个筛选又被尾巴顶替。
                await self._wait_search_quiet(self.SEARCH_QUIET_SECONDS,
                                              timeout=self.SEARCH_QUIET_TIMEOUT)
                return True
            await asyncio.sleep(0.2)
        ctx.log(f"[抖音] 点了「{group_label}/{option_text}」但 "
                f"{self.FILTER_CONFIRM_TIMEOUT:.0f} 秒内没等到新的搜索请求，"
                f"筛选可能没生效（也可能这个档位本来就是当前值，没触发重搜）", "warn")
        return False

    def _search_resp_at(self) -> float:
        """最近一次列表响应落地的时刻。拦截回调可能早于初始化，按需兜底。"""
        return getattr(self, "_last_search_resp_at", 0.0)

    async def _wait_search_quiet(self, quiet: float, *, timeout: float) -> bool:
        """等到"连续 quiet 秒没有新的列表响应落地"。

        返回是否真的等到了安静（False = 撞上 timeout，说明还在持续来数据）。
        """
        loop = asyncio.get_event_loop()
        hard_deadline = loop.time() + timeout
        while loop.time() < hard_deadline:
            mark = self._search_resp_at()
            await asyncio.sleep(quiet)
            if self._search_resp_at() == mark:
                return True
        return False

    # ---------------- 打开作品：点卡片，不跳 URL ----------------
    async def _goto_work(self, ctx: CollectContext, work) -> bool:
        """在搜索结果页上点开这条作品的卡片。

        为什么必须点而不是跳 URL：见 _work_url_template 的说明。

        怎么认出是哪张卡片：多列模式下的卡片**既没有 href 也没有 data-e2e**
        （快照 05 实测：带 /video/ 的链接 0 个），所以只能靠**描述文字**认。
        搜索接口返回的 desc 和卡片上显示的是同一段文字，取前若干个字做前缀匹配，
        命中后点它的中心点。

        打开成功的判据是 URL 里出现 modal_id——这个信号比"某个元素出现了"可靠，
        而且顺便能确认打开的是不是我们要的那条。
        """
        target_id = str(work.work_id)
        if f"modal_id={target_id}" in (self._page.url or ""):
            ctx.log(f"[抖音] 作品 {target_id} 的弹窗已经开着，直接用")
            return True

        # 上一条作品的弹窗还开着的话先关掉，否则点不到底下的卡片
        if "modal_id=" in (self._page.url or ""):
            try:
                await self._run(self._page.keyboard.press("Escape"))
                await asyncio.sleep(random.uniform(0.6, 1.0))
            except Exception:  # noqa: BLE001
                pass

        prefix = (work.title or "").strip()[:12]
        if not prefix:
            ctx.log(f"[抖音] 作品 {target_id} 没有标题文字，无法在页面上定位卡片", "warn")
            return False

        # 卡片可能在首屏之外（列表是懒加载的），找不到就往下滚一段再找。
        # 文字比对前把所有空白压掉：接口里的 desc 常带换行，
        # 渲染到卡片上会变成空格，一字不差地比必然对不上。
        #
        # ⚠️ 两级匹配，缺一个都会大面积失败：
        #   1) 直接文字 startsWith —— 最精确，正常情况走这条
        #   2) 子树文字 contains  —— 兜底。抖音的描述里带 #话题 时会被拆成
        #      多个元素（`<span>文案</span><a>#话题</a>`），此时容器自己的
        #      直接文字是空的、每个片段又都短于前缀，第 1 条必然找不到。
        #      实测日志里连续 9 条「结果页上找不到卡片」就是这么来的。
        #      兜底时要取**最深**的那个匹配元素，否则会命中整个列表容器，
        #      点下去等于点在页面中间的空白处。
        find_js = """([prefix, minLen]) => {
                const norm = t => (t || '').replace(/\s+/g, '');
                const want = norm(prefix);
                if (want.length < minLen) return { error: 'prefix_too_short' };

                const own = el => Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join('');
                const els = Array.from(document.querySelectorAll('*'));

                const box = el => {
                    el.scrollIntoView({ block: 'center' });
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) return { error: 'invisible' };
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                };

                // 1) 自己直接带这段文字
                const direct = els.find(el => norm(own(el)).startsWith(want));
                if (direct) return { ...box(direct), how: 'direct' };

                // 2) 子树里含这段文字，取最深的那个（没有子元素也含它的）
                const contains = els.filter(el => norm(el.textContent).includes(want));
                if (!contains.length) return { error: 'not_found' };
                const deepest = contains.filter(el => !contains.some(
                    other => other !== el && el.contains(other)));
                const target = deepest[0] || contains[contains.length - 1];
                return { ...box(target), how: 'subtree' };
            }"""

        # 找不到时的几种补救，按代价从小到大排：
        #   往下滚 —— 卡片在首屏之外（列表懒加载）
        #   回到顶部 —— ⚠️ 这条是后加的：逐条点开时页面已经滚到一万多像素，
        #     而作品是按发布时间倒序点的，越往后越旧、卡片位置越靠上。
        #     抖音的长列表会把滚出视野很远的卡片从 DOM 里摘掉，
        #     只往下滚是永远找不回来的——实测日志里连续 9 条
        #     「结果页上找不到卡片」就是卡在这里。回到顶部能让它们重新渲染。
        #   缩短前缀 —— 描述被平台截断/改写，长前缀对不上
        hit = None
        attempts = (
            ("看当前页面", 12, None),
            ("往下滚一屏", 12, "down"),
            ("回到列表顶部", 12, "top"),
            ("缩短前缀再找", 6, None),
        )
        for label, prefix_len, move in attempts:
            if move == "down":
                await self._run(self._page.mouse.wheel(0, 900))
                await asyncio.sleep(random.uniform(0.8, 1.3))
            elif move == "top":
                await self._run(self._page.evaluate("() => window.scrollTo(0, 0)"))
                await asyncio.sleep(random.uniform(1.0, 1.6))

            probe = (work.title or "").strip()[:prefix_len]
            hit = await self._run(self._page.evaluate(find_js, [probe, 4]))
            if isinstance(hit, dict) and not hit.get("error"):
                if label != "看当前页面" or hit.get("how") == "subtree":
                    ctx.log(f"[抖音] 作品 {target_id} 的卡片是靠「{label}」找到的"
                            f"（{hit.get('how')}，前缀 {len(probe)} 字）")
                break

        if not isinstance(hit, dict) or hit.get("error"):
            # 卡片找不到（懒加载还没渲染、页面又刷新了一批、描述被截断…）。
            # 退路：如果弹窗还开着，就用「下一条」箭头一条条翻过去找。
            # 这条路不依赖页面上有没有那张卡片，只依赖抖音自己的作品顺序。
            ctx.log(f"[抖音] 结果页上找不到作品 {target_id} 的卡片"
                    f"（按「{prefix}…」匹配，往下滚过、也回顶部找过、还试了短前缀），"
                    f"改用「下一条」箭头翻找", "warn")
            return await self._walk_to_work(ctx, target_id)

        await asyncio.sleep(random.uniform(0.4, 0.8))
        await self._run(self._page.mouse.click(hit["x"], hit["y"]))

        # 等 URL 出现 modal_id。用 URL 判断而不是等某个元素，
        # 既能确认弹窗开了，又能确认开的是不是这一条。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 10.0
        while loop.time() < deadline:
            url = self._page.url or ""
            if "modal_id=" in url:
                opened = url.split("modal_id=")[1].split("&")[0]
                if opened == target_id:
                    ctx.log(f"[抖音] 已点开作品 {target_id}")
                    return True
                ctx.log(f"[抖音] 点开的是 {opened}，不是要的 {target_id}"
                        f"（描述前缀撞车了），跳过这条免得评论记错作品", "warn")
                return False
            await asyncio.sleep(0.4)
        ctx.log(f"[抖音] 点了卡片但 10 秒内 URL 没出现 modal_id，作品 {target_id} 可能没打开", "warn")
        return False

    async def _walk_to_work(self, ctx: CollectContext, target_id: str,
                            max_steps: int = 12) -> bool:
        """用「下一条」箭头往后翻，直到 URL 的 modal_id 等于目标作品。

        为什么需要这条路：按描述文字点卡片，前提是**那张卡片正好在页面上**。
        但作品列表是懒加载的，而且筛选/滚动之后页面上的那一批随时会变，
        所以"接口返回了这条作品"不等于"页面上现在有这张卡片"。
        箭头翻页走的是抖音自己维护的作品顺序，跟页面渲染了哪些卡片无关。

        代价是每翻一条都要等页面加载，所以限制步数——翻不到就老实跳过，
        不能为了一条作品在这儿磨半天。
        """
        if "modal_id=" not in (self._page.url or ""):
            ctx.log(f"[抖音] 弹窗没开着，没法用箭头翻找，跳过作品 {target_id}", "warn")
            return False

        for step in range(1, max_steps + 1):
            try:
                arrow = await self._run(self._page.evaluate(
                    """() => {
                        const els = Array.from(document.querySelectorAll(
                            '[data-e2e="video-switch-next-arrow"]'));
                        const vis = els.map(el => el.getBoundingClientRect())
                            .filter(r => r.width > 0 && r.height > 0
                                         && r.top >= 0 && r.bottom <= window.innerHeight);
                        if (!vis.length) return null;
                        const r = vis[0];
                        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                    }"""))
                if not arrow:
                    ctx.log(f"[抖音] 第 {step} 步找不到「下一条」箭头，停止翻找", "warn")
                    return False
                await self._run(self._page.mouse.click(arrow["x"], arrow["y"]))
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"[抖音] 翻下一条失败：{exc}", "warn")
                return False

            await asyncio.sleep(random.uniform(1.0, 1.6))
            url = self._page.url or ""
            if "modal_id=" in url:
                now = url.split("modal_id=")[1].split("&")[0]
                if now == target_id:
                    ctx.log(f"[抖音] 翻了 {step} 条后找到作品 {target_id}")
                    return True

        ctx.log(f"[抖音] 往后翻了 {max_steps} 条仍没遇到作品 {target_id}，跳过这条", "warn")
        return False

    # ---------------- 关闭联播 ----------------
    async def _disable_autoplay(self, ctx: Optional[CollectContext] = None) -> bool:
        """关掉播放器的「连播」开关。

        ctx 允许为空：这个方法会被 _open_comments 调用，而那条路径上
        ctx 不一定传得到，缺了也只是少几行任务日志，不该整个采集崩掉。

        快照 08 实测：连播开关不在页面上，而在 xgplayer 的设置浮层里，
        没有 data-e2e，结构是
            .xgplayer-setting-label            ← 一整块（清屏 / 连播各一块）
              ├── .xg-switch                   ← 真正要点的开关
              └── .xgplayer-setting-title      ← 文案「连播」
        浮层要先 hover 播放器右下角的 [data-e2e="video-play-more"] 才出来。

        ⚠️ 开关的"当前是开还是关"我没有实测依据（快照只拍到一个状态），
        所以这里的做法是：点一下，然后回读 class 是否变化。变了就认为
        切换成功；没变说明选择器指错了，记警告而不是默默继续。
        """
        def say(msg: str, level: str = "info") -> None:
            if ctx is not None:
                ctx.log(msg, level)
            else:
                getattr(logger, "warning" if level == "warn" else "info")(msg)

        # 浮层怎么出来的快照里看不出（拍到时它已经开着了），所以按真人的路子来：
        #   1. 鼠标先移到播放器上 —— 播放器控件默认是隐藏的，不划过去不出来
        #   2. 再 hover 右下角的设置入口；不行就点一下
        # 每一步都轮询等"**可见的**设置项"出现，而不是等元素存在——
        # 上一轮就栽在这：元素在 DOM 里但不可见，照样被找到、被点，
        # 点了当然没反应（日志「点了连播开关但状态没变化」）。
        async def visible_switch():
            """返回可见的连播开关坐标，没有则返回 None。"""
            try:
                return await self._run(self._page.evaluate(
                    """() => {
                        const labels = Array.from(
                            document.querySelectorAll('.xgplayer-setting-label'));
                        for (const el of labels) {
                            if (!(el.textContent || '').includes('连播')) continue;
                            const sw = el.querySelector('.xg-switch');
                            if (!sw) continue;
                            const r = sw.getBoundingClientRect();
                            if (r.width < 1 || r.height < 1) continue;
                            if (r.top < 0 || r.bottom > window.innerHeight) continue;
                            return { x: r.left + r.width / 2, y: r.top + r.height / 2,
                                     before: sw.className };
                        }
                        return null;
                    }"""))
            except Exception:  # noqa: BLE001
                return None

        async def poll_switch(timeout_s: float):
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout_s
            while loop.time() < deadline:
                got = await visible_switch()
                # 必须是带坐标的字典才算数：JS 返回别的形状（页面改版、
                # 注入脚本干扰）时不能当成功，否则下一行取 x 直接 KeyError
                if isinstance(got, dict) and "x" in got and "y" in got:
                    return got
                await asyncio.sleep(0.3)
            return None

        # 1) 鼠标先划过播放器，把控件唤出来
        try:
            box = await self._run(self._page.evaluate(
                """() => {
                    const v = document.querySelector('[data-e2e="feed-active-video"]');
                    if (!v) return null;
                    const r = v.getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height * 0.6 };
                }"""))
            if box:
                await self._run(self._page.mouse.move(box["x"], box["y"]))
                await asyncio.sleep(random.uniform(0.5, 0.9))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[抖音] 移到播放器上失败：%s", exc)

        # 2) 找视口内可见的设置入口（同样有两个，屏幕外那个不能用）
        result = await poll_switch(1.5)
        if result is None:
            for how in ("hover", "click"):
                entry = await self._run(self._page.evaluate(
                    """() => {
                        const els = Array.from(
                            document.querySelectorAll('[data-e2e="video-play-more"]'));
                        const vis = els.map(el => el.getBoundingClientRect())
                            .filter(r => r.width > 0 && r.height > 0
                                         && r.top >= 0 && r.bottom <= window.innerHeight);
                        if (!vis.length) return null;
                        const r = vis[0];
                        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                    }"""))
                if not entry:
                    logger.debug("[抖音] 视口内没有可见的播放器设置入口")
                    break
                try:
                    if how == "hover":
                        await self._run(self._page.mouse.move(entry["x"], entry["y"]))
                    else:
                        await self._run(self._page.mouse.click(entry["x"], entry["y"]))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[抖音] %s 设置入口失败：%s", how, exc)
                    continue
                result = await poll_switch(3.0)
                if result:
                    logger.info("[抖音] 播放器设置浮层已展开（%s）", how)
                    break

        if not result:
            say("[抖音] 没找到**可见的**连播开关——设置浮层没展开。"
                "继续采集，但视频播完可能会自动跳到下一条", "warn")
            return False

        # 3) 判断当前是开还是关。依据是**悬浮提示的文案**（用户截图实测）：
        #     连播开着 → 提示「关闭自动连播 K」，开关红色
        #     连播关了 → 提示「自动连播 K」，开关灰色
        # 用文案不用 class：文案是产品语义，class 是构建产物随时会变。
        # 提示要 hover 到开关上才出来，所以先把鼠标移过去。
        await self._run(self._page.mouse.move(result["x"], result["y"]))
        await asyncio.sleep(random.uniform(0.4, 0.7))

        state = await self._autoplay_state()
        if state == "off":
            say("[抖音] 连播本来就是关的（提示显示「自动连播」），不用动")
            return True
        if state == "unknown":
            say("[抖音] 读不到连播的悬浮提示，改成点一次再回读验证", "warn")

        await self._run(self._page.mouse.click(result["x"], result["y"]))
        await asyncio.sleep(random.uniform(0.4, 0.8))

        after_state = await self._autoplay_state()
        if after_state == "off":
            say("[抖音] 已关闭连播（提示变成「自动连播」）")
            return True
        if after_state == "on":
            # 说明点之前其实是关的（提示没读准），这一下反而打开了，点回去
            say("[抖音] 点了一下反而把连播打开了，再点回去", "warn")
            await self._run(self._page.mouse.click(result["x"], result["y"]))
            await asyncio.sleep(random.uniform(0.4, 0.8))
            if await self._autoplay_state() == "off":
                say("[抖音] 已关闭连播")
                return True

        # 提示读不到时，退回看开关的 class 有没有变化
        again = await visible_switch()
        after = (again or {}).get("before", "")
        if after and after != result.get("before"):
            say("[抖音] 已切换连播开关（按 class 变化判断）")
            return True
        say("[抖音] 点了连播开关但状态没变化，可能点空了", "warn")
        return False

    async def _autoplay_state(self) -> str:
        """按悬浮提示的文案判断连播状态：on / off / unknown。

        提示写的是**动作**不是状态：
            「关闭自动连播」→ 点了会关 → 现在是**开着**的
            「自动连播」    → 点了会开 → 现在是**关着**的
        两个文案有包含关系，必须先判有没有「关闭」二字，顺序反了永远判成 on。
        """
        try:
            txt = await self._run(self._page.evaluate(
                """() => {
                    const own = el => Array.from(el.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent).join('').trim();
                    for (const el of document.querySelectorAll('*')) {
                        const t = own(el);
                        if (!t || !t.includes('自动连播')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 1 || r.height < 1) continue;
                        return t;
                    }
                    return '';
                }"""))
        except Exception:  # noqa: BLE001
            return "unknown"
        if not txt:
            return "unknown"
        return "on" if "关闭" in txt else "off"

    async def _open_comments(self) -> bool:
        """打开评论区。

        ⚠️ 不能用 locator(...).first —— 快照 08/09 实测：弹窗里**同时存在两个**
        [data-e2e="feed-comment-icon"]，一个在 y≈598（当前这条，看得见），
        一个在 y≈1559（下一条视频，在屏幕外）。`.first` 取的是 DOM 顺序第一个，
        很可能就是屏幕外那个，点了等于没点——症状就是"未能打开评论区"。
        所以一律用 JS 按**视口内可见**来挑，再按坐标点。
        """
        if self._page is None:
            return False
        ctx = getattr(self, "_ctx", None)

        def say(msg: str, level: str = "info") -> None:
            if ctx is not None:
                ctx.log(msg, level)
            else:
                getattr(logger, "warning" if level == "warn" else "info")(msg)

        # 进作品页第一件事：关掉连播，否则播完会自动跳下一条，
        # 评论区跟着换人，采到的评论会挂到错误的作品上。
        # ⚠️ 必须在打开评论区**之前**做：评论区展开后会盖住播放器右下角，
        # 设置入口就点不到了（夹具实测：先开评论区再关连播必然失败）。
        await self._disable_autoplay(ctx)

        # 已经开着就别再点了，再点一次反而会关掉
        if await self._comment_list_visible():
            say("[抖音] 评论区已经开着")
            return True

        hit = await self._run(self._page.evaluate(
            """() => {
                const els = Array.from(
                    document.querySelectorAll('[data-e2e="feed-comment-icon"]'));
                // 只要视口内的那个：屏幕外的是下一条视频的图标
                const vis = els.map(el => ({ el, r: el.getBoundingClientRect() }))
                    .filter(o => o.r.width > 0 && o.r.height > 0
                                 && o.r.top >= 0 && o.r.bottom <= window.innerHeight);
                if (!vis.length) return { error: 'no_visible_icon', total: els.length };
                const r = vis[0].r;
                return { x: r.left + r.width / 2, y: r.top + r.height / 2,
                         total: els.length };
            }"""))

        if isinstance(hit, dict) and not hit.get("error"):
            await self._run(self._page.mouse.click(hit["x"], hit["y"]))
            if await self._wait_comment_list(8.0):
                say(f"[抖音] 已打开评论区（页面上共 {hit.get('total')} 个评论图标，"
                    f"点的是视口内那个）")
                return True
            say("[抖音] 点了评论图标但评论区没出现", "warn")
        else:
            total = hit.get("total") if isinstance(hit, dict) else "?"
            say(f"[抖音] 视口内没有可见的评论图标（页面上共 {total} 个，都在屏幕外）", "warn")

        # 兜底：点「评论」两个字
        try:
            el = self._page.get_by_text("评论", exact=True).first
            if await self._wait_visible(el, timeout_s=2.0):
                await self._run(el.click())
                if await self._wait_comment_list(6.0):
                    say("[抖音] 已通过「评论」文字打开评论区")
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[抖音] 点「评论」文字失败：%s", exc)

        say("[抖音] 未能打开评论区", "warn")
        return False

    async def _comment_list_visible(self) -> bool:
        try:
            return bool(await self._run(self._page.evaluate(
                """() => {
                    const el = document.querySelector('[data-e2e="comment-list"]');
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 10 && r.height > 10;
                }""")))
        except Exception:  # noqa: BLE001
            return False

    async def _wait_comment_list(self, timeout_s: float) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if await self._comment_list_visible():
                return True
            await asyncio.sleep(0.4)
        return False

    #: 用来"往哪个区域滚"的两种目标：评论面板 / 作品列表
    async def _area_rect(self, kind: str) -> Optional[Dict]:
        """返回要滚动的区域中心点。找不到就返回 None。"""
        js = {
            "comments": """() => {
                const el = document.querySelector('[data-e2e="comment-list"]');
                if (!el) return null;
                const r = el.getBoundingClientRect();
                if (r.width < 50 || r.height < 50) return null;
                return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
            }""",
            "works": """() => {
                // 作品列表没有稳定标记，用"卡片最密集的那一列"的中心：
                // 取所有可见的作品描述块，算它们的横向中位数
                const els = [...document.querySelectorAll('[data-e2e="scroll-list"], ul, div')]
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 400 && r.height > 400 && r.top < window.innerHeight;
                    });
                if (!els.length) return { x: window.innerWidth * 0.45,
                                          y: window.innerHeight * 0.6 };
                const r = els[0].getBoundingClientRect();
                return { x: Math.min(r.left + r.width / 2, window.innerWidth * 0.6),
                         y: window.innerHeight * 0.6 };
            }""",
        }[kind]
        try:
            return await self._run(self._page.evaluate(js))
        except Exception:  # noqa: BLE001
            return None

    async def _page_scroll_fingerprint(self) -> Dict:
        """作品列表的"进度指纹"：页面滚动位置 + 已渲染的卡片数。"""
        try:
            return await self._run(self._page.evaluate(
                """() => {
                    let tops = window.scrollY || 0;
                    for (const el of document.querySelectorAll('div,ul,main,section')) {
                        if (el.scrollTop) tops += el.scrollTop;
                    }
                    return { scrollSum: Math.round(tops),
                             bodyH: document.body.scrollHeight };
                }"""))
        except Exception:  # noqa: BLE001
            return {}

    async def _simulate_scroll(self, times: int = 3) -> None:
        """滚动**作品列表**。覆盖基类的 window.scrollBy。

        ⚠️ 和评论区是同一类坑：基类用 window.scrollBy，而抖音搜索结果
        往往滚的是内部容器，window 根本不动 —— 表现就是"滚了半天没有新作品"，
        采完第一屏的 9 条就结束了。
        这里改成：鼠标移到列表区域 → 滚轮 → 指纹没变就退回 JS 推容器。
        """
        if self._page is None:
            return
        for _ in range(max(1, times)):
            before = await self._page_scroll_fingerprint()
            rect = await self._area_rect("works")
            if rect:
                try:
                    await self._run(self._page.mouse.move(rect["x"], rect["y"]))
                    await self._run(self._page.mouse.wheel(0, random.randint(600, 1000)))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[抖音] 列表滚轮失败：%s", exc)
            await asyncio.sleep(random.uniform(0.6, 1.0))

            after = await self._page_scroll_fingerprint()
            if after.get("scrollSum") != before.get("scrollSum"):
                continue

            # 滚轮没让页面动，直接推：window + 所有能滚的容器
            try:
                await self._run(self._page.evaluate(
                    """() => {
                        const dy = 900;
                        window.scrollBy(0, dy);
                        for (const el of document.querySelectorAll('div,ul,main,section')) {
                            if (el.scrollHeight - el.clientHeight > 200) el.scrollTop += dy;
                        }
                    }"""))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(random.uniform(0.5, 0.9))

    async def _comment_progress(self) -> Dict:
        """评论区的"进度指纹"。

        ⚠️ 不再依赖"找到滚动容器"——实测在真实页面上那套探测会失败
        （日志里的「找不到滚动容器」），一失败就直接放弃，连滚都没滚，
        102 条评论只拿到 10 条。
        改成看**结果**：评论条数、最后一条的位置、以及所有可滚动祖先的
        scrollTop 之和。三者只要有一个变了，就说明这一滚是有效的。
        """
        try:
            return await self._run(self._page.evaluate(
                """() => {
                    const root = document.querySelector('[data-e2e="comment-list"]');
                    if (!root) return { ok: false };
                    const items = root.querySelectorAll('[data-e2e="comment-item"]');
                    const last = items[items.length - 1];
                    let tops = 0;
                    let cur = root;
                    for (let i = 0; cur && i < 10; i++, cur = cur.parentElement) {
                        tops += cur.scrollTop || 0;
                    }
                    for (const el of root.querySelectorAll('*')) {
                        if (el.scrollTop) tops += el.scrollTop;
                    }
                    return {
                        ok: true,
                        count: items.length,
                        lastTop: last ? Math.round(last.getBoundingClientRect().top) : 0,
                        scrollSum: Math.round(tops),
                        text: (root.textContent || '').length,
                    };
                }"""))
        except Exception:  # noqa: BLE001
            return {"ok": False}

    async def _scroll_comment_panel(self, delta_y: int) -> bool:
        """把评论面板往下滚一段。返回 True 表示确实滚动了（有新进展）。

        步骤：鼠标移进评论面板 → 滚轮 → 看进度指纹有没有变。
        滚轮没效果时，再退回直接改所有可滚动元素的 scrollTop。

        ⚠️ 关键教训：滚轮作用在**鼠标当前所在的元素**上。打开评论区时鼠标
        停在视频区的评论图标上，直接滚轮滚的是视频列表，评论面板纹丝不动。
        """
        before = await self._comment_progress()
        if not before.get("ok"):
            return False

        rect = await self._area_rect("comments")
        if rect:
            await self._run(self._page.mouse.move(rect["x"], rect["y"]))
            await self._run(self._page.mouse.wheel(0, delta_y))
            await asyncio.sleep(random.uniform(0.5, 0.9))
            after = await self._comment_progress()
            if self._progress_changed(before, after):
                return True

        # 滚轮无效（自定义滚动实现之类）：把能滚的都试着推一把
        try:
            moved = await self._run(self._page.evaluate(
                """(dy) => {
                    const root = document.querySelector('[data-e2e="comment-list"]');
                    if (!root) return false;
                    const cands = [root, ...root.querySelectorAll('*')];
                    let cur = root.parentElement;
                    for (let i = 0; cur && i < 10; i++, cur = cur.parentElement) cands.push(cur);
                    let any = false;
                    for (const el of cands) {
                        if (el.scrollHeight - el.clientHeight < 20) continue;
                        const b = el.scrollTop;
                        el.scrollTop = b + dy;
                        if (el.scrollTop > b) any = true;
                    }
                    if (!any) {
                        // 最后一招：把最后一条评论滚进视野
                        const items = root.querySelectorAll('[data-e2e="comment-item"]');
                        const last = items[items.length - 1];
                        if (last) { last.scrollIntoView({ block: 'end' }); return true; }
                    }
                    return any;
                }""", delta_y))
        except Exception:  # noqa: BLE001
            moved = False
        if not moved:
            return False
        await asyncio.sleep(random.uniform(0.4, 0.7))
        after = await self._comment_progress()
        return self._progress_changed(before, after)

    @staticmethod
    def _progress_changed(a: Dict, b: Dict) -> bool:
        """两次进度指纹之间有没有实质变化。"""
        if not b.get("ok"):
            return False
        return (b.get("count", 0) > a.get("count", 0)
                or b.get("scrollSum", 0) != a.get("scrollSum", 0)
                or b.get("lastTop", 0) != a.get("lastTop", 0)
                or b.get("text", 0) > a.get("text", 0))

    async def _scroll_comments(self, max_scrolls: int = 80) -> None:
        """滚动评论区 + 即时展开回复。"""
        if self._page is None:
            return

        # 重置评论 API 空数据标志
        self._comment_api_empty = False
        self._comment_api_empty_count = 0

        stuck = 0
        scroll_count = 0
        while scroll_count < max_scrolls:
            # 检查评论 API 是否返回空数据
            if self._comment_api_empty:
                logger.info("[抖音] 评论 API 返回空数据，停止滚动")
                break

            # 随机滚动距离
            delta_y = random.choices(
                [200, 300, 400, 500, 600],
                weights=[15, 35, 30, 15, 5],
                k=1
            )[0]

            moved = await self._scroll_comment_panel(delta_y)
            scroll_count += 1
            if not moved:
                stuck += 1
                if stuck >= 3:
                    pr = await self._comment_progress()
                    logger.info("[抖音] 评论面板连续 3 次没有新进展，判定到底"
                                "（页面上已渲染 %s 条评论）", pr.get("count", "?"))
                    self._comments_exhausted = True
                    break
            else:
                stuck = 0

            # 每次滚动后检查并展开所有可见的回复按钮
            await self._expand_replies()

            # 到底的判据是**两个条件同时成立**：
            #   1. 出现「暂时没有更多评论」
            #   2. 没有任何未展开的「展开 N 条回复」
            # 只看第 1 条会漏：一级评论确实到底了，但中间还压着没展开的二级回复，
            # 这时候走人，那些回复就永远采不到了。
            if scroll_count % 3 == 0:
                if await self._comments_bottom_reached():
                    pending = await self._pending_reply_count()
                    if pending == 0:
                        pr = await self._comment_progress()
                        logger.info("[抖音] 「暂时没有更多评论」且没有待展开的回复，"
                                    "这条作品采完（页面已渲染 %s 条评论）",
                                    pr.get("count", "?"))
                        self._comments_exhausted = True
                        break
                    logger.info("[抖音] 已到底但还有 %d 个「展开N条回复」没点，"
                                "先展开再走", pending)
                    # 从评论区顶部重新扫一遍，把之前滚过去时漏掉的展开按钮补上
                    await self._run(self._page.mouse.wheel(0, -4000))
                    await asyncio.sleep(random.uniform(0.5, 0.9))
                    for _ in range(12):
                        await self._expand_replies()
                        await self._run(self._page.mouse.wheel(0, 600))
                        await asyncio.sleep(random.uniform(0.4, 0.7))
                        if await self._pending_reply_count() == 0:
                            break
                    logger.info("[抖音] 回复补展开完毕，剩余 %d 个",
                                await self._pending_reply_count())
                    self._comments_exhausted = True
                    break

        logger.info(f"[抖音] 评论采集完成，共滚动 {scroll_count} 次")

    async def _comments_bottom_reached(self) -> bool:
        """评论区是否出现了「暂时没有更多评论」。

        限定在 [data-e2e="comment-list"] 里找，且要求元素**可见**——
        这行字在评论没加载完时也可能先渲染在 DOM 里。
        """
        try:
            return bool(await self._run(self._page.evaluate(
                """() => {
                    const root = document.querySelector('[data-e2e="comment-list"]');
                    if (!root) return false;
                    for (const el of root.querySelectorAll('*')) {
                        const own = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent).join('').trim();
                        if (!own.includes('暂时没有更多评论')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    return false;
                }""")))
        except Exception:  # noqa: BLE001
            return False

    async def _pending_reply_count(self) -> int:
        """评论区里还有几个没点开的「展开 N 条回复」。

        只数「展开N条回复」，不数「收起」——「收起」说明那一条已经展开过了。
        """
        try:
            return int(await self._run(self._page.evaluate(
                """() => {
                    const root = document.querySelector('[data-e2e="comment-list"]');
                    if (!root) return 0;
                    const re = /^(展开\\s*\\d+\\s*条回复|查看(更多|全部)回复)$/;
                    let n = 0;
                    for (const el of root.querySelectorAll('*')) {
                        const own = Array.from(el.childNodes)
                            .filter(x => x.nodeType === 3)
                            .map(x => x.textContent).join('').trim();
                        if (re.test(own)) n++;
                    }
                    return n;
                }""")) or 0)
        except Exception:  # noqa: BLE001
            return 0

    async def _expand_replies(self) -> bool:
        """展开视口内的「展开 N 条回复」。

        ⚠️ 这里原来在整个页面上找包含「展开」两个字的元素，会误点两样东西：
          1. 视频描述下面的「展开」（快照 11 里 class=Y2Kcfa1Y…，
             点了只是把简介展开，白点一次）
          2. 评论区外任何带「展开」的控件
        快照 11 显示真正的按钮文案是「展开1条回复」这种带数字的形态，
        而且一定在 [data-e2e="comment-list"] 里面。所以这里两条都收紧：
        **限定容器** + **文案必须匹配「展开N条…」**。
        """
        if self._page is None:
            return False
        expanded_any = False
        try:
            # 通过 JS 一次性收集所有展开按钮
            btn_infos = await self._run(self._page.evaluate("""
                () => {
                    const results = [];
                    const root = document.querySelector('[data-e2e="comment-list"]');
                    if (!root) return results;
                    // 「展开1条回复」「展开 3 条回复」「查看更多回复」都收，
                    // 光秃秃一个「展开」不收——那是视频简介的展开
                    const re = /^(展开\s*\d+\s*条回复|查看(更多|全部)回复)$/;
                    const candidates = root.querySelectorAll(
                        'span, button, a, div[role="button"], div');
                    for (const el of candidates) {
                        const own = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent).join('').trim();
                        if (!re.test(own)) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 &&
                            rect.top >= 0 && rect.top < window.innerHeight) {
                            results.push({
                                text: own,
                                x: rect.left + rect.width / 2,
                                y: rect.top + rect.height / 2,
                            });
                        }
                    }
                    return results;
                }
            """))
            for btn in btn_infos:
                try:
                    await self._run(self._page.mouse.click(btn["x"], btn["y"]))
                    await asyncio.sleep(random.uniform(0.3, 0.6))
                    expanded_any = True
                except Exception:
                    continue
        except Exception as exc:
            logger.debug(f"[抖音] 展开回复失败: {exc}")
        return expanded_any

    async def _switch_next_work(self) -> bool:
        """切换到下一个视频。

        优先用 [data-e2e="video-switch-next-arrow"]（快照 08 实测存在）——
        点箭头是明确的"下一条"语义，比在视频区滚轮可靠：滚轮的滚动量、
        鼠标落点、页面当前状态都会影响结果，滚多滚少都可能跳过作品。
        箭头找不到时才退回原来的滚轮方案。
        """
        if self._page is None:
            return False
        try:
            arrow = self._page.locator('[data-e2e="video-switch-next-arrow"]').first
            if await self._wait_visible(arrow, timeout_s=2.0):
                await self._run(arrow.click())
                await asyncio.sleep(random.uniform(0.8, 1.5))
                logger.info("[抖音] 点击下一条箭头切换作品")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[抖音] 点下一条箭头失败，退回滚轮：%s", exc)
        try:
            # 鼠标移到视频区域，滚动切换
            video_area = await self._run(
                self._page.query_selector('video, [class*="video"], [class*="player"]'))
            if video_area:
                box = await self._run(video_area.bounding_box())
                if box:
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2
                    await self._run(self._page.mouse.move(x, y))
                    await asyncio.sleep(random.uniform(0.2, 0.4))
                    await self._run(self._page.mouse.wheel(0, 500))
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    return True
        except Exception as exc:
            logger.debug(f"[抖音] 切换视频失败: {exc}")
        return False

    # ---------------- 数据映射 ----------------
    # ⚠️ 下面两个 _to_* 是"借用" DouyinCollector 的实现（把 self 传过去），
    # 好处是字段映射只有一份，API 采集和浏览器采集的落库结构永远一致。
    # 代价是：那份实现在 self 上用到的**所有** helper，这个类也必须有。
    # _first_url 就是这么漏掉的——只有真跑到映射那一步才会炸。
    @staticmethod
    def _first_url(node: Any) -> str:
        from .douyin import DouyinCollector
        return DouyinCollector._first_url(node)

    def _to_work(self, ctx: CollectContext, aweme: Dict, source_keyword: str = "") -> WorkItem:
        """把抖音 aweme 映射为 WorkItem。"""
        from .douyin import DouyinCollector
        return DouyinCollector._to_work(self, ctx, aweme, source_keyword=source_keyword)

    def _to_comment(
        self, ctx: CollectContext, raw: Dict, work_id: str,
        depth: int = 1, parent_id: str = "", root_id: str = "",
    ) -> CommentItem:
        """把抖音评论映射为 CommentItem。

        ⚠️ DouyinCollector._to_comment 的签名是
            (ctx, raw, aweme_id, depth, parent_id, root_id="")
        没有 what 参数。这里以前多传了一个 what="评论"，
        会在采第一条评论时抛 TypeError——和 _first_url 是同一类问题：
        借用别人的实现，就得严格照着别人的签名调。
        """
        from .douyin import DouyinCollector
        return DouyinCollector._to_comment(
            self, ctx, raw, work_id,
            depth=depth, parent_id=parent_id, root_id=root_id,
        )
