"""采集容错：单条数据、单个关键字出问题都不能把整个平台带走。

这一组测试全都对应线上真实发生过的事故：
  - 微博一条作品的 pics 是字符串不是字典 → 整个平台采集结束，0 条入库
  - 快手一条作品评论区关了 → 剩下的关键字全不采
  - 取消任务时 progress 写 NULL → 任务永远卡在 running
"""
from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator, List

import pytest

from app.collectors.base import (
    BaseCollector, CollectContext, CommentItem, WorkItem,
)
from app.scheduler.runner import TaskRunner, _Buffer


class _Stub(BaseCollector):
    """不连配置、不连代理的采集器基类，只用来跑 runner 的循环。"""

    def __init__(self):
        pass


class _Repo:
    def __init__(self):
        self.works: List[WorkItem] = []
        self.comments: List[CommentItem] = []

    async def save_works(self, items):
        self.works.extend(items)
        return len(items), 0

    async def save_comments(self, items, *, skip_existing=True):
        self.comments.extend(items)
        return len(items), 0

    async def save_authors(self, items):
        return len(items)

    def invalidate_overview_cache(self):
        pass


class _Log:
    def __init__(self):
        self.lines: List[str] = []

    def info(self, message, *a, **kw):
        self.lines.append(str(message))

    warn = info
    error = info


def _ctx():
    from app.core import search_filters as sf
    ctx = CollectContext(
        scenic_id="S1", scenic_name="天山天池", task_id="T1",
        max_works=50, max_comments_per_work=50, collect_comments=False,
    )
    ctx.filters = sf.resolve("weibo", {})
    ctx.log = lambda *a, **kw: None
    return ctx


TEST_DB = "scenic_media_resilience"


@pytest.fixture()
async def db_repo():
    """真实 MySQL 上的任务仓储。progress 那个坑是列约束，用假对象测不出来。"""
    import os

    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"

    from app.core.config import load_config, reset_cache
    from app.core.db import Database
    from app.repositories.task_repo import TaskRepository

    reset_cache()
    cfg = load_config(use_cache=False)
    db = Database(cfg)
    await db.connect()
    try:
        yield TaskRepository(db)
    finally:
        await db.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        await db.close()
        reset_cache()


def _no_sleep(monkeypatch) -> None:
    """把重试之间的退避等待去掉，别让测试真的睡 2+4 秒。

    ⚠️ 不能写成 `lambda *_: asyncio.sleep(0)` —— 打完补丁之后
    asyncio.sleep 就是这个 lambda 本身，它调自己会无限递归。
    """
    async def _instant(*args, **kwargs):
        return None

    import app.scheduler.runner as runner_module
    monkeypatch.setattr(runner_module.asyncio, "sleep", _instant)


def _runner(max_retries: int = 3) -> TaskRunner:
    runner = TaskRunner.__new__(TaskRunner)
    runner.max_retries = max_retries
    return runner


# ---------------------------------------------------------------------------
# 一条数据解析不了，跳过就行
# ---------------------------------------------------------------------------

class _BadFieldCollector(_Stub):
    """第二条作品的字段格式没见过，映射时会抛异常。"""

    channel = "weibo"
    supports_works = True

    async def collect_by_keyword(self, ctx, keyword) -> AsyncIterator[WorkItem]:
        for raw in ({"id": "W1"}, {"id": "W2", "bad": True}, {"id": "W3"}):
            work = self.safe_map(ctx, self._map, ctx, raw)
            if work is None:
                continue
            yield work

    def _map(self, ctx, raw):
        if raw.get("bad"):
            # 复刻线上那个：pics 里是字符串，代码却当字典用
            return "字符串".get("large")     # noqa: B018
        # title 里必须包含关键字，否则会被 runner 的关键字匹配过滤丢弃
        return self.new_work(ctx, work_id=raw["id"], title=f"关于天山天池的风景",
                             publish_time=datetime(2026, 8, 20))


@pytest.mark.asyncio
async def test_one_unparsable_item_does_not_end_the_channel():
    """线上原样：微博一条作品的 pics 元素是字符串，
    `p.get(...)` 抛 AttributeError，异常从生成器冒出去，
    整个平台的采集就此结束——日志里只有一行
    「weibo 采集失败：'str' object has no attribute 'get'」。
    """
    collector = _BadFieldCollector()
    repo, log = _Repo(), _Log()
    buffer = _Buffer(repo, log)

    await _runner()._collect_one_keyword(
        collector, _ctx(), "天山天池", buffer, log,
    )
    await buffer.flush()

    assert [w.work_id for w in repo.works] == ["W1", "W3"], (
        "坏的那条要跳过，前后两条都得留下"
    )


# ---------------------------------------------------------------------------
# 关键字级重试
# ---------------------------------------------------------------------------

class _FlakyKeywordCollector(_Stub):
    """前两次翻页就炸，第三次才正常。"""

    channel = "douyin"
    supports_works = True

    def __init__(self, fail_times: int):
        super().__init__()
        self.fail_times = fail_times
        self.attempts = 0

    async def collect_by_keyword(self, ctx, keyword) -> AsyncIterator[WorkItem]:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"接口抽风 #{self.attempts}")
        # title 里必须包含关键字，否则会被 runner 的关键字匹配过滤丢弃
        yield self.new_work(ctx, work_id="W1", title=f"关于{keyword}的风景",
                            publish_time=datetime(2026, 8, 20))


@pytest.mark.asyncio
async def test_keyword_is_retried_before_giving_up(monkeypatch):
    _no_sleep(monkeypatch)

    collector = _FlakyKeywordCollector(fail_times=2)
    repo, log = _Repo(), _Log()
    buffer = _Buffer(repo, log)

    await _runner()._collect_keyword(
        collector, _ctx(), {"keywords": ["天山天池"]}, buffer, log,
    )
    await buffer.flush()

    assert collector.attempts == 3, "默认要重试到第 3 次"
    assert [w.work_id for w in repo.works] == ["W1"]


@pytest.mark.asyncio
async def test_failed_keyword_does_not_stop_the_others(monkeypatch):
    """一个词重试完还是不行，也不能连累同一个景区的其他词。"""
    _no_sleep(monkeypatch)

    class _OneBadKeyword(_Stub):
        channel = "douyin"
        supports_works = True

        async def collect_by_keyword(self, ctx, keyword) -> AsyncIterator[WorkItem]:
            if keyword == "坏词":
                raise RuntimeError("这个词永远失败")
            # title 里必须包含关键字，否则会被 runner 的关键字匹配过滤丢弃
            yield self.new_work(ctx, work_id=f"W-{keyword}", title=f"关于{keyword}的风景",
                                publish_time=datetime(2026, 8, 20))

    repo, log = _Repo(), _Log()
    buffer = _Buffer(repo, log)
    await _runner()._collect_keyword(
        _OneBadKeyword(), _ctx(), {"keywords": ["好词1", "坏词", "好词2"]}, buffer, log,
    )
    await buffer.flush()

    assert [w.work_id for w in repo.works] == ["W-好词1", "W-好词2"]
    assert any("坏词" in line and "跳过" in line for line in log.lines), log.lines


@pytest.mark.asyncio
async def test_cancel_is_never_retried():
    """取消不能重试，否则用户点了停止要等三轮才停得下来。"""
    from app.collectors.base import TaskCancelled

    class _Cancelled(_Stub):
        channel = "douyin"
        supports_works = True

        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def collect_by_keyword(self, ctx, keyword) -> AsyncIterator[WorkItem]:
            self.attempts += 1
            raise TaskCancelled("任务已被取消")
            yield   # pragma: no cover

    collector = _Cancelled()
    buffer = _Buffer(_Repo(), _Log())
    with pytest.raises(TaskCancelled):
        await _runner()._collect_keyword(
            collector, _ctx(), {"keywords": ["天山天池"]}, buffer, _Log(),
        )
    assert collector.attempts == 1


# ---------------------------------------------------------------------------
# 连续失败要停下来，不能一路"跳过"到底
# ---------------------------------------------------------------------------

class _BrokenCommentsCollector(_Stub):
    """评论怎么采都失败——复刻拟人模式下走错路那次事故。"""

    channel = "douyin"
    supports_works = True

    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    async def collect_comments(self, ctx, work) -> AsyncIterator[CommentItem]:
        self.calls += 1
        raise self.error
        yield  # pragma: no cover - 让它成为异步生成器


@pytest.mark.asyncio
async def test_repeated_comment_failures_stop_the_channel():
    """线上原样：拟人模式下评论去调了根本没建的接口客户端，
    61 条作品在同一秒里全部"评论采集失败，跳过这条继续采下一条"，
    任务最后还报成功——用户以为采到了，其实一条评论都没有。

    单条失败要继续（作品被删、评论区关了是常态），
    但**连续**失败是系统性故障，必须停下来报错。
    """
    from app.scheduler.runner import COMMENT_FAIL_STREAK_LIMIT

    collector = _BrokenCommentsCollector(
        AttributeError("'NoneType' object has no attribute 'get_json'")
    )
    runner = _runner()
    ctx = _ctx()
    logs: List[str] = []
    ctx.log = lambda message, level="info": logs.append(str(message))
    buffer = _Buffer(_Repo(), _Log())

    work = WorkItem(channel="douyin", work_id="W0", scenic_id="S1", scenic_name="天山天池")

    # 前 N-1 条：跳过，继续
    for index in range(COMMENT_FAIL_STREAK_LIMIT - 1):
        work.work_id = f"W{index}"
        await runner._collect_comments_for(collector, ctx, work, buffer)

    # 第 N 条：判定为系统性故障，停掉这个平台
    work.work_id = "W_last"
    with pytest.raises(RuntimeError, match="连续"):
        await runner._collect_comments_for(collector, ctx, work, buffer)

    assert collector.calls == COMMENT_FAIL_STREAK_LIMIT
    assert any("get_json" in line for line in logs), "错误原文要留在日志里，否则没法排查"


@pytest.mark.asyncio
async def test_scattered_comment_failures_do_not_stop_the_channel():
    """零散失败不该累积到阈值——成功一条就清零。

    不这样的话，一个采了几百条作品的任务里，
    偶发的 5 次失败（哪怕中间隔着几十条成功的）也会把整个平台掐掉。
    """
    from app.scheduler.runner import COMMENT_FAIL_STREAK_LIMIT

    class _Flaky(_Stub):
        channel = "douyin"
        supports_works = True

        def __init__(self):
            self.n = 0

        async def collect_comments(self, ctx, work) -> AsyncIterator[CommentItem]:
            self.n += 1
            if self.n % 2:                      # 奇数次失败，偶数次成功
                raise RuntimeError("评论区关闭了")
            yield CommentItem(
                channel="douyin", comment_id=f"C{self.n}", work_id=work.work_id,
                scenic_id="S1", scenic_name="天山天池", content="好看",
            )

    collector = _Flaky()
    runner = _runner()
    ctx = _ctx()
    ctx.log = lambda *a, **kw: None
    buffer = _Buffer(_Repo(), _Log())
    work = WorkItem(channel="douyin", work_id="W", scenic_id="S1", scenic_name="天山天池")

    # 失败/成功交替跑够 2 倍阈值的轮次，一次都不该抛
    for _ in range(COMMENT_FAIL_STREAK_LIMIT * 2):
        await runner._collect_comments_for(collector, ctx, work, buffer)

    assert ctx.params.get("_comment_fail_streak", 0) == 0


# ---------------------------------------------------------------------------
# 当天已经采过的作品，重跑时不重复翻评论
# ---------------------------------------------------------------------------

class _TwoWorksCollector(_Stub):
    """每次调用都吐同样两条作品，模拟"关键字重试/任务重跑"。"""

    channel = "douyin"
    supports_works = True

    def __init__(self):
        self.comment_calls: List[str] = []

    async def collect_by_keyword(self, ctx, keyword) -> AsyncIterator[WorkItem]:
        for work_id in ("W_OLD", "W_NEW"):
            yield self.new_work(
                ctx, work_id=work_id, title=f"天山天池的风景 {work_id}",
                publish_time=datetime(2026, 8, 20),
            )

    async def collect_comments(self, ctx, work) -> AsyncIterator[CommentItem]:
        self.comment_calls.append(work.work_id)
        yield CommentItem(
            channel="douyin", comment_id=f"C-{work.work_id}", work_id=work.work_id,
            scenic_id="S1", scenic_name="天山天池", content="好看",
        )


@pytest.mark.asyncio
async def test_works_collected_today_skip_their_comments():
    """线上原样：关键字失败重试后，134 条作品被从头点一遍。

    拟人模式下每条作品要真实点开、翻评论——重复翻一遍就是几十分钟。
    作品本身还是要写（点赞/评论数会变，upsert 很便宜），
    贵的是评论，那部分跳过。
    """
    collector = _TwoWorksCollector()
    runner = _runner()
    ctx = _ctx()
    ctx.collect_comments = True
    ctx.params["_collected_today"] = {"W_OLD"}      # 今天已经采过这条
    logs: List[str] = []
    ctx.log = lambda message, level="info": logs.append(str(message))
    repo = _Repo()
    buffer = _Buffer(repo, _Log())

    await runner._collect_one_keyword(collector, ctx, "天山天池", buffer, _Log())
    await buffer.flush()

    assert collector.comment_calls == ["W_NEW"], "今天已采过的作品又被翻了一遍评论"
    stored = {w.work_id for w in repo.works}
    assert stored == {"W_OLD", "W_NEW"}, "作品本身还是要写（数据会变）"


@pytest.mark.asyncio
async def test_nothing_is_skipped_when_the_set_is_empty():
    """第一次跑（或关掉了这个开关）时不能少采。"""
    collector = _TwoWorksCollector()
    runner = _runner()
    ctx = _ctx()
    ctx.collect_comments = True
    ctx.log = lambda *a, **kw: None
    buffer = _Buffer(_Repo(), _Log())

    await runner._collect_one_keyword(collector, ctx, "天山天池", buffer, _Log())
    assert collector.comment_calls == ["W_OLD", "W_NEW"]


# ---------------------------------------------------------------------------
# 看门狗强杀之后，任务状态不能一直挂在"运行中"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_force_cancelled_task_is_marked_not_left_running():
    """`handle.cancel()` 抛的是 CancelledError，它**不是** Exception 的子类。

    以前 runner.run 只接了 TaskCancelled 和 Exception，CancelledError 直接穿过去，
    mark_finished 一次都没调——任务在库里永远停在"运行中"，既占着并发名额，
    页面上也一直转圈。用户的现象是"日志里已经释放浏览器了，任务状态还挂着"。
    """
    import asyncio
    from app.core.constants import TaskStatus
    from app.scheduler.runner import TaskRunner

    marked = {}

    class _Tasks:
        async def mark_running(self, task_id):
            marked["running"] = True

        async def mark_finished(self, task_id, status, error=None, stats=None):
            marked["status"] = status
            marked["error"] = error

        async def update_progress(self, *a, **kw):
            pass

        async def append_log(self, *a, **kw):
            pass

        async def add_logs(self, *a, **kw):
            pass

    class _Scenics:
        async def get_scenic(self, scenic_id):
            # 采集刚开始就被强杀
            raise asyncio.CancelledError()

    runner = TaskRunner.__new__(TaskRunner)
    runner.tasks = _Tasks()
    runner.scenics = _Scenics()

    with pytest.raises(asyncio.CancelledError):
        await runner.run({"task_id": "T1", "task_name": "测试任务",
                          "scenic_id": "S1", "channels": ["xiaohongshu"]})

    assert marked.get("status") == TaskStatus.CANCELED.value, (
        f"被强杀之后状态是 {marked.get('status')!r}，"
        "应该是「已取消」——不然任务永远挂在运行中"
    )
    assert "取消" in (marked.get("error") or "")
