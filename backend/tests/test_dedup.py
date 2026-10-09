"""跨关键字去重：同一条作品被多个关键字命中时只收一次。

一个景区通常配好几个关键字（"天山天池""天池风景区""天山天池景区"…），
它们命中同一条作品是常态。不去重的话：
  - 同一条作品被反复 upsert，写库量翻几倍
  - **它的评论会被重新翻一遍**，几十上百个请求白打，还多消耗风控额度

去重粒度和数据库唯一键一致（work_uk / comment_uk），
所以"重复"在内存里和库里是同一个定义。
"""
from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator, List

import time

import pytest

from app.collectors.base import (
    BaseCollector, CollectContext, CommentItem, TaskCancelled, WorkItem,
)
from app.scheduler.runner import _Buffer, TaskRunner


class _StubRepo:
    """只记下被要求写了什么，不真连库。"""

    def __init__(self):
        self.saved_works: List[WorkItem] = []
        self.saved_comments: List[CommentItem] = []
        self.skip_existing_seen = None

    async def save_works(self, items):
        self.saved_works.extend(items)
        return len(items), 0

    async def save_comments(self, items, *, skip_existing=True):
        self.skip_existing_seen = skip_existing
        self.saved_comments.extend(items)
        return len(items), 0

    async def save_authors(self, items):
        return len(items)

    def invalidate_overview_cache(self):
        pass


class _StubLog:
    def __init__(self):
        self.lines: List[str] = []

    def info(self, message, *a, **kw):
        self.lines.append(message)

    warn = info
    error = info


def _work(work_id: str, scenic_id: str = "S1", channel: str = "douyin",
          title: str = "风景真好") -> WorkItem:
    return WorkItem(
        scenic_id=scenic_id, scenic_name="天山天池", channel=channel,
        work_id=work_id, title=title, publish_time=datetime(2026, 8, 20),
    )


def _comment(comment_id: str, work_id: str = "W1") -> CommentItem:
    return CommentItem(
        scenic_id="S1", scenic_name="天山天池", channel="douyin",
        work_id=work_id, comment_id=comment_id, content="好看",
        publish_time=datetime(2026, 8, 21),
    )


@pytest.mark.asyncio
async def test_same_work_from_two_keywords_is_stored_once():
    repo = _StubRepo()
    buffer = _Buffer(repo, _StubLog())

    first = await buffer.add_work(_work("W1"))
    second = await buffer.add_work(_work("W1"))     # 另一个关键字命中同一条
    third = await buffer.add_work(_work("W2"))
    await buffer.flush()

    assert first is True
    assert second is False, "第二次应该被判为重复"
    assert third is True
    assert [w.work_id for w in repo.saved_works] == ["W1", "W2"]
    assert buffer.dup_works == 1


@pytest.mark.asyncio
async def test_same_work_id_in_another_scenic_is_not_a_duplicate():
    """去重必须带上景区。同一条微博同时命中两个景区时，两边各要一份——
    否则第二个景区的数据会凭空少掉，而且是静默少掉。"""
    repo = _StubRepo()
    buffer = _Buffer(repo, _StubLog())

    assert await buffer.add_work(_work("W1", scenic_id="S1")) is True
    assert await buffer.add_work(_work("W1", scenic_id="S2")) is True
    await buffer.flush()
    assert len(repo.saved_works) == 2


@pytest.mark.asyncio
async def test_duplicate_comments_are_skipped():
    repo = _StubRepo()
    buffer = _Buffer(repo, _StubLog())

    assert await buffer.add_comment(_comment("C1")) is True
    assert await buffer.add_comment(_comment("C1")) is False
    assert await buffer.add_comment(_comment("C2")) is True
    # 同一个评论 ID 挂在不同作品下不算重复
    assert await buffer.add_comment(_comment("C1", work_id="W9")) is True
    await buffer.flush()

    assert len(repo.saved_comments) == 3
    assert buffer.dup_comments == 1


@pytest.mark.asyncio
async def test_existing_comments_are_not_rewritten_by_default():
    repo = _StubRepo()
    buffer = _Buffer(repo, _StubLog())
    await buffer.add_comment(_comment("C1"))
    await buffer.flush()
    assert repo.skip_existing_seen is True, "默认只写新增评论"

    repo2 = _StubRepo()
    buffer2 = _Buffer(repo2, _StubLog(), refresh_existing_comments=True)
    await buffer2.add_comment(_comment("C1"))
    await buffer2.flush()
    assert repo2.skip_existing_seen is False, "开了开关才连老评论一起刷"


def test_dedup_summary_only_mentions_what_happened():
    buffer = _Buffer(_StubRepo(), _StubLog())
    assert buffer.dedup_summary() == ""
    buffer.dup_works = 3
    assert buffer.dedup_summary() == "作品 3 条"
    buffer.dup_comments = 5
    assert buffer.dedup_summary() == "作品 3 条，评论 5 条"


# ---------------------------------------------------------------------------
# 重复作品不再重复采评论
# ---------------------------------------------------------------------------

class _CountingCollector(BaseCollector):
    """记下每条作品的评论被翻了几次。"""

    channel = "douyin"
    supports_works = True

    def __init__(self):
        self.comment_calls: List[str] = []

    async def collect_by_keyword(self, ctx, keyword) -> AsyncIterator[WorkItem]:
        # 两个关键字都能搜到 W1，只有第二个关键字额外有 W2
        # title 里必须包含关键字，否则会被 runner 的关键字匹配过滤丢弃
        yield _work("W1", title=f"关于{keyword}的风景")
        if keyword == "天池风景区":
            yield _work("W2", title=f"关于{keyword}的风景")

    async def collect_comments(self, ctx, work) -> AsyncIterator[CommentItem]:
        self.comment_calls.append(work.work_id)
        yield _comment(f"C-{work.work_id}", work_id=work.work_id)


@pytest.mark.asyncio
async def test_duplicate_work_does_not_refetch_its_comments():
    """第二个关键字命中同一条作品时，评论不该再翻一遍。

    两次命中相隔几秒，评论区不可能变，几十个请求纯属白打。
    （跨任务运行仍然会重新采——那才是"有没有新评论"该关心的尺度。）
    """
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    collector = _CountingCollector()
    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)

    ctx = CollectContext(
        scenic_id="S1", scenic_name="天山天池", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("douyin", {})

    runner = TaskRunner.__new__(TaskRunner)     # 只用它的采集循环，不要调度那一套
    runner.max_retries = 1                      # 这条测试不关心重试，跑一遍就好
    await runner._collect_keyword(
        collector, ctx,
        {"keywords": ["天山天池", "天池风景区"]},
        buffer, log,
    )
    await buffer.flush()

    assert collector.comment_calls == ["W1", "W2"], (
        f"W1 的评论被翻了不止一次：{collector.comment_calls}"
    )
    assert [w.work_id for w in repo.saved_works] == ["W1", "W2"]
    assert buffer.dup_works == 1
    assert any("重复" in line for line in log.lines), log.lines


# ---------------------------------------------------------------------------
# 一条作品的评论失败，不能把整个平台带走
# ---------------------------------------------------------------------------

class _FlakyCommentCollector(BaseCollector):
    """第二条作品的评论区"关掉了"，接口直接报错。"""

    channel = "kuaishou"
    supports_works = True

    def __init__(self):
        self.tried: List[str] = []

    async def collect_by_keyword(self, ctx, keyword) -> AsyncIterator[WorkItem]:
        for work_id in ("W1", "W2", "W3"):
            # title 里必须包含关键字，否则会被 runner 的关键字匹配过滤丢弃
            yield _work(work_id, channel="kuaishou", title=f"关于{keyword}的风景")

    async def collect_comments(self, ctx, work) -> AsyncIterator[CommentItem]:
        self.tried.append(work.work_id)
        if work.work_id == "W2":
            raise RuntimeError("快手接口报错：result=2")
        yield _comment(f"C-{work.work_id}", work_id=work.work_id)


@pytest.mark.asyncio
async def test_one_works_comment_failure_does_not_stop_the_channel():
    """评论区关掉 / 作品被删是常态，平台回的就是普通接口错误。

    原来这个错会一路冒到 _run_channel 的 except，于是
    「一条作品评论区关了」== 「这个平台剩下的关键字全不采了」。
    """
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    collector = _FlakyCommentCollector()
    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)

    ctx = CollectContext(
        scenic_id="S1", scenic_name="天山天池", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("kuaishou", {})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(
        collector, ctx, {"keywords": ["天山天池"]}, buffer, log,
    )
    await buffer.flush()

    assert collector.tried == ["W1", "W2", "W3"], "W2 失败后必须继续采 W3"
    assert [w.work_id for w in repo.saved_works] == ["W1", "W2", "W3"]
    # W2 的评论没了，另外两条还在
    assert {c.work_id for c in repo.saved_comments} == {"W1", "W3"}


@pytest.mark.asyncio
async def test_login_failure_during_comments_still_propagates():
    """登录态失效是平台级问题，再采下去每条都会失败，必须往上冒。"""
    from app.collectors.base import LoginRequired
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    class _LoggedOut(_FlakyCommentCollector):
        async def collect_comments(self, ctx, work):
            self.tried.append(work.work_id)
            raise LoginRequired("登录态失效")
            yield   # pragma: no cover - 让它成为异步生成器

    collector = _LoggedOut()
    buffer = _Buffer(_StubRepo(), _StubLog())
    ctx = CollectContext(
        scenic_id="S1", scenic_name="天山天池", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("kuaishou", {})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    with pytest.raises(LoginRequired):
        await runner._collect_keyword(
            collector, ctx, {"keywords": ["天山天池"]}, buffer, _StubLog(),
        )
    assert collector.tried == ["W1"], "第一条就该停，不该把剩下的也试一遍"


# ---------------------------------------------------------------------------
# 拟人模式的前提：runner 必须**边产出边采评论**
# ---------------------------------------------------------------------------
class _OrderRecordingCollector:
    """记录 runner 到底是"产出一条就采一条"还是"全产出完再回头采"。

    这个顺序对拟人模式是**功能性**的，不是风格问题：
    小红书的搜索结果是回收式虚拟列表（DOM 里只留 29 张的滚动窗口），
    等全部产出完再回头点卡片，卡片早就不在 DOM 里了，条条点不开。
    """

    channel = "xiaohongshu"
    supports_works = True

    def __init__(self):
        self.events: List[str] = []

    @staticmethod
    def limit_reached(count, limit):
        return bool(limit) and count >= limit

    async def collect_by_keyword(self, ctx, keyword):
        for i in (1, 2, 3):
            self.events.append(f"yield-W{i}")
            yield _work(f"W{i}", title=f"关于{keyword}的风景")

    async def collect_comments(self, ctx, work):
        self.events.append(f"comments-{work.work_id}")
        yield _comment(f"C-{work.work_id}", work_id=work.work_id)


@pytest.mark.asyncio
async def test_runner_collects_comments_between_yields_not_after():
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    collector = _OrderRecordingCollector()
    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(
        scenic_id="S1", scenic_name="盘山", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("xiaohongshu", {})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(collector, ctx, {"keywords": ["盘山"]},
                                  buffer, log)

    assert collector.events == [
        "yield-W1", "comments-W1",
        "yield-W2", "comments-W2",
        "yield-W3", "comments-W3",
    ], (
        f"runner 不再是交错的了：{collector.events}。"
        "两段式会让拟人采集器条条点不开卡片——虚拟列表已经把它们回收了"
    )


@pytest.mark.asyncio
async def test_dropped_by_content_filter_says_which_title_and_why():
    """被内容过滤丢掉的作品，日志要说出是**哪一条**、为什么。

    只报总数的话，用户看到的是"采到 20 条"但库里几条都没有，
    完全想不到原因是"搜的是『盘山风景区』，笔记标题里只写了『盘山』"。
    """
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    class _OffTopic:
        channel = "xiaohongshu"
        supports_works = True

        @staticmethod
        def limit_reached(count, limit):
            return bool(limit) and count >= limit

        async def collect_by_keyword(self, ctx, keyword):
            yield _work("W1", title="早知有盘山，何必下江南")   # 不含完整关键字
            yield _work("W2", title="天津盘山风景区攻略")        # 含

        async def collect_comments(self, ctx, work):
            yield _comment(f"C-{work.work_id}", work_id=work.work_id)

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(
        scenic_id="S1", scenic_name="盘山", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("xiaohongshu", {})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(collector := _OffTopic(), ctx,
                                  {"keywords": ["盘山风景区"]}, buffer, log)
    assert collector is not None
    await buffer.flush()

    dropped = [ln for ln in log.lines if "内容过滤" in ln and "早知有盘山" in ln]
    assert dropped, f"没说清被丢的是哪一条：{log.lines}"
    assert any("补充词" in ln for ln in log.lines), "没告诉用户怎么办"
    assert [w.work_id for w in repo.saved_works] == ["W2"]


@pytest.mark.asyncio
async def test_time_window_says_which_title_and_when():
    """被时间窗跳过的，要打出标题和它的发布时间。

    和内容过滤丢的东西现象一模一样（都是"采到了却不入库"），
    只报总数分不清是哪一层。用户实测就误判过一次：明明是内容过滤丢的，
    却以为是时间窗。
    """
    from datetime import datetime
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    class _OldAndNew:
        channel = "xiaohongshu"
        supports_works = True

        @staticmethod
        def limit_reached(count, limit):
            return bool(limit) and count >= limit

        async def collect_by_keyword(self, ctx, keyword):
            old = _work("W1", title=f"{keyword}的老笔记", channel="xiaohongshu")
            old.publish_time = datetime(2020, 1, 1)      # 窗口外
            yield old
            yield _work("W2", title=f"{keyword}的新笔记", channel="xiaohongshu")

        async def collect_comments(self, ctx, work):
            yield _comment(f"C-{work.work_id}", work_id=work.work_id)

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(
        scenic_id="S1", scenic_name="八大处", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("xiaohongshu", {"publish_within": "half_year"})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(_OldAndNew(), ctx, {"keywords": ["八大处"]},
                                  buffer, log)
    await buffer.flush()

    dropped = [ln for ln in log.lines if "[时间窗]" in ln]
    assert dropped, f"时间窗跳过了内容却没说是哪一条：{log.lines}"
    assert "老笔记" in dropped[0] and "2020-01-01" in dropped[0], dropped[0]
    assert [w.work_id for w in repo.saved_works] == ["W2"]


@pytest.mark.asyncio
async def test_work_without_publish_time_is_not_dropped_by_window():
    """没有发布时间的**不该**被时间窗丢掉——这是 in_window 的既定行为。

    钉住它是因为反过来改（"没时间就丢"）看着很合理，但那会在
    feed 偶尔不到货时把整批内容悄悄丢光。
    """
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    class _NoTime:
        channel = "xiaohongshu"
        supports_works = True

        @staticmethod
        def limit_reached(count, limit):
            return bool(limit) and count >= limit

        async def collect_by_keyword(self, ctx, keyword):
            w = _work("W1", title=f"{keyword}攻略", channel="xiaohongshu")
            w.publish_time = None
            yield w

        async def collect_comments(self, ctx, work):
            yield _comment("C-W1", work_id="W1")

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(
        scenic_id="S1", scenic_name="八大处", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("xiaohongshu", {"publish_within": "day"})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(_NoTime(), ctx, {"keywords": ["八大处"]},
                                  buffer, log)
    await buffer.flush()

    assert [w.work_id for w in repo.saved_works] == ["W1"], (
        "没有发布时间的被时间窗丢了——feed 偶尔不到货时会整批丢光"
    )
    assert not [ln for ln in log.lines if "[时间窗]" in ln]


@pytest.mark.asyncio
async def test_every_work_says_whether_it_was_stored_and_why_no_comments():
    """每条作品都要说清"入库了没、要不要采评论"。

    以前四个分支一声不吭，只在最后报几个总数，用户看到的是
    "日志刷了一堆笔记，库里好像没有"——分不清是没入库还是入库了没采评论。
    """
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    class _Three:
        channel = "xiaohongshu"
        supports_works = True

        @staticmethod
        def limit_reached(count, limit):
            return bool(limit) and count >= limit

        async def collect_by_keyword(self, ctx, keyword):
            yield _work("W1", title=f"{keyword}攻略", channel="xiaohongshu")
            yield _work("W2", title=f"{keyword}打卡", channel="xiaohongshu")
            yield _work("W1", title=f"{keyword}攻略", channel="xiaohongshu")  # 重复

        async def collect_comments(self, ctx, work):
            yield _comment(f"C-{work.work_id}", work_id=work.work_id)

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(
        scenic_id="S1", scenic_name="八大处", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
    )
    ctx.filters = sf.resolve("xiaohongshu", {})
    # W2 今天已经采过评论了
    ctx.params["_collected_today"] = {"W2"}

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(_Three(), ctx, {"keywords": ["八大处"]},
                                  buffer, log)
    await buffer.flush()

    # 每条作品都开一个方框，里面是完整字段（版式见 test_task_runner_logging.py）
    boxes = [ln for ln in log.lines if ln.startswith("  ┌─ ")]
    assert len(boxes) == 3, f"不是每条都打了明细：{boxes}"
    # 小红书这条路上称呼是「笔记」，不是「作品」
    assert all("笔记" in ln for ln in boxes), boxes
    assert len([ln for ln in log.lines if ln.startswith("  │ 作者")]) == 3
    assert len([ln for ln in log.lines if ln.startswith("  │ 发布时间")]) == 3
    # 三条的去向都要说清楚——方框收尾那行会讲为什么没翻评论
    fate = [ln for ln in log.lines if "小计" in ln]
    assert any("今天已经采过它的评论" in ln for ln in fate), fate
    assert any("不重复翻评论" in ln for ln in fate), fate
    # 三条都进了缓冲区（重复的那条不会重复落库）
    assert [w.work_id for w in repo.saved_works] == ["W1", "W2"]
    # 写库那一下要报平台、表名、作品 id
    stored = [ln for ln in log.lines if "[存储]" in ln]
    assert stored and "src_opinion_social_work_di" in stored[0], stored


# ---------------------------------------------------------------------------
# 「采到了但没入库」：拟人模式下攒够 200 条要一个多小时
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slow_crawl_writes_to_db_without_waiting_for_200_items():
    """慢速采集必须**按时间**写库，不能只等条数攒满。

    拟人模式一条笔记十几二十秒，攒够 FLUSH_THRESHOLD(200) 要一个多小时。
    这段时间里数据全在内存：用户看到"日志在刷、数据页是空的"，
    任务一旦中途被停掉或进程被杀，这一个多小时就全丢了。
    """
    import app.scheduler.runner as R

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log) if False else R._Buffer(repo, log)

    # 假装上一次写库是很久以前——模拟"慢速采集攒了很久"
    buffer._last_flush = time.monotonic() - R.FLUSH_MAX_SECONDS - 1
    await buffer.add_work(_work("W1", channel="xiaohongshu"))

    assert repo.saved_works, (
        "只攒了 1 条就该写库了（离上次写库已经超过 "
        f"{R.FLUSH_MAX_SECONDS} 秒），实际还压在内存里"
    )
    assert [w.work_id for w in repo.saved_works] == ["W1"]
    stored = [ln for ln in log.lines if "[存储]" in ln]
    assert stored, f"写库了却没在日志里说：{log.lines}"
    assert "src_opinion_social_work_di" in stored[0], "没写明存到哪张表"
    assert "xiaohongshu" in stored[0], "没写明是哪个平台"
    assert "W1" in stored[0], "没写明是哪条作品"


@pytest.mark.asyncio
async def test_each_work_is_stored_before_moving_to_the_next_one():
    """**采一条存一条**：每采完一条作品（连同它的评论）就落库，不攒批。

    攒批的后果是：拟人模式一条笔记十几二十秒，攒到 200 条要一个多小时，
    中途任务被停、进程被杀、机器重启，这一个多小时全没了——
    而日志里明明一条条都采到了。
    """
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    # 记录"第 N 条作品采完时，库里已经有几条"
    snapshots = []

    class _Watching:
        channel = "xiaohongshu"
        supports_works = True

        @staticmethod
        def limit_reached(count, limit):
            return bool(limit) and count >= limit

        async def collect_by_keyword(self, ctx, keyword):
            for i in range(1, 6):
                # 产出下一条之前，先看看上一条到底进库没有
                snapshots.append(len(repo.saved_works))
                yield _work(f"W{i}", title=f"{keyword}攻略{i}",
                            channel="xiaohongshu")

        async def collect_comments(self, ctx, work):
            yield _comment(f"C-{work.work_id}", work_id=work.work_id)

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(
        scenic_id="S1", scenic_name="八大处", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
        logger=log,
    )
    ctx.filters = sf.resolve("xiaohongshu", {})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(_Watching(), ctx, {"keywords": ["八大处"]},
                                  buffer, log)

    # 第 1 条产出前库里 0 条；之后每产出一条，前面的都已经在库里了
    assert snapshots == [0, 1, 2, 3, 4], (
        f"不是「采一条存一条」：产出第 N 条时库里的条数依次是 {snapshots}，"
        "期望 [0, 1, 2, 3, 4]"
    )
    assert len(repo.saved_works) == 5

    # 评论也一样：跟着它那条作品一起落库，不会拖到最后
    assert len(repo.saved_comments) == 5, (
        f"评论没有跟着作品一起落库：{len(repo.saved_comments)}"
    )


@pytest.mark.asyncio
async def test_comments_are_logged_with_id_author_time_for_every_platform():
    """评论明细（评论ID/作者/发布时间）由 runner 统一打，抖音小红书一个样。"""
    from app.core import search_filters as sf
    from app.scheduler.runner import TaskRunner

    class _WithComments:
        channel = "douyin"
        supports_works = True

        @staticmethod
        def limit_reached(count, limit):
            return bool(limit) and count >= limit

        async def collect_by_keyword(self, ctx, keyword):
            yield _work("W1", title=f"{keyword}攻略")

        async def collect_comments(self, ctx, work):
            top = _comment("C1", work_id="W1")
            reply = _comment("C2", work_id="W1")
            reply.comment_level = "level_2"
            reply.comment_parent_id = "C1"
            yield top
            yield reply

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(
        scenic_id="S1", scenic_name="天山天池", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=True,
        logger=log,
    )
    ctx.filters = sf.resolve("douyin", {})

    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = 1
    await runner._collect_keyword(_WithComments(), ctx, {"keywords": ["天山天池"]},
                                  buffer, log)
    await buffer.flush()

    # 版式是用户实跑后指定的（见 test_task_runner_logging.py）：
    # 每条评论两行——第一行「谁说了什么」，第二行「赞 · 时间 · 层级 · 评论ID」。
    one = [ln for ln in log.lines if "level_1" in ln and "C1" in ln]
    two = [ln for ln in log.lines if "level_2" in ln and "C2" in ln]
    assert one, f"一级评论没打明细：{log.lines}"
    assert two, f"二级评论没打明细：{log.lines}"
    assert "2026-08-21" in one[0], f"没打发布时间：{one[0]}"
    # 二级要缩进并挂 ↳，一眼看得出层级
    assert "↳" in two[0], f"二级没缩进标记：{two[0]}"
    # 评论也要有存储行
    stored = [ln for ln in log.lines if "[存储]" in ln and "评论" in ln]
    assert stored and "src_opinion_social_work_comment_di" in stored[0], stored


# ---------------------------------------------------------------------------
# 中途退出：方框要闭合，攒着的评论要打出来
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("boom,label", [
    (TaskCancelled("用户点了取消"), "被取消"),
    (RuntimeError("评论区关了"), "单条异常"),
])
async def test_partial_comments_are_still_printed(boom, label):
    """采到一半退出，已经拿到的评论必须打出来、方框必须闭合。

    实跑现场：作品 4/200 的框打开后，库里进了 16 条评论，
    日志里一条明细都没有，框也没闭——用户看到的是"日志格式没保持"。
    """
    class _Halfway:
        channel = "kuaishou"

        async def collect_comments(self, ctx, work):
            yield _comment("C1", work_id=work.work_id)
            yield _comment("C2", work_id=work.work_id)
            raise boom

    repo = _StubRepo()
    log = _StubLog()
    buffer = _Buffer(repo, log)
    ctx = CollectContext(scenic_id="S1", scenic_name="八大处", task_id="T1",
                         max_comments_per_work=50, logger=log)
    ctx.log = lambda m, level="info": log.lines.append(str(m))
    runner = TaskRunner.__new__(TaskRunner)
    work = _work("W1", title="八大处公园", channel="kuaishou")

    try:
        await runner._collect_comments_for(_Halfway(), ctx, work, buffer)
    except BaseException:
        pass          # 抛不抛是另一回事，这里只管日志

    printed = [ln for ln in log.lines if "C1" in ln or "C2" in ln]
    closed = [ln for ln in log.lines if "小计" in ln]
    assert printed, f"{label}：拿到的评论一条都没打出来 → {log.lines}"
    assert closed, f"{label}：方框没闭合（没有小计那行）→ {log.lines}"
    assert "2 条评论" in closed[0], f"{label}：小计数不对 → {closed[0]}"
