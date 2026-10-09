"""任务日志的输出格式：四个平台必须长一个样。

用户实跑之后指定了这个方框格式（原本只在 verify_kuaishou_browser.py 里），
要求抖音/快手/小红书/微博全部照这个来。这里把格式钉住，
免得以后哪个平台"顺手"改回一行流水。
"""
from datetime import datetime

import pytest

from app.collectors.base import CommentItem, WorkItem
from app.core.constants import (
    CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_WEIBO, CHANNEL_XHS,
)
from app.scheduler.runner import (
    WORK_NOUN, log_comment_detail, log_work_detail, log_work_footer,
    sort_comments_for_log,
)


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(str(message))

    warn = warning = info


def make_work(channel):
    return WorkItem(
        channel=channel, work_id="3xzxqey42rgz2iy",
        work_url="https://www.kuaishou.com/short-video/3xzxqey42rgz2iy",
        author_id="3x96xr46png6y2k", author_name="致水",
        title="盘山公路 #风景都在路上而不是终点",
        description="盘山公路 #风景都在路上而不是终点",
        label="风景都在路上而不是终点",
        likes=36, comment_cnt=10, collection_cnt=0, shares=0,
        publish_time=datetime(2026, 4, 10, 22, 17, 25),
        source_keyword="盘山风景区", scenic_id="S1", scenic_name="盘山风景区",
    )


def make_comment(cid, name, content, when, *, level="level_1", root="", likes=0):
    return CommentItem(
        channel=CHANNEL_KUAISHOU, comment_id=cid, work_id="3xzxqey42rgz2iy",
        commenter_id="u" + cid, commenter_name=name, content=content,
        likes=likes, publish_time=when, comment_level=level,
        root_comment_id=root or cid, scenic_id="S1", scenic_name="盘山风景区",
    )


@pytest.mark.parametrize("channel", [
    CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_XHS, CHANNEL_WEIBO,
])
def test_work_box_same_shape_on_every_channel(channel):
    """四个平台同一个方框，只有"作品/笔记/微博"这个称呼跟着平台走。"""
    log = FakeLog()
    log_work_detail(log, make_work(channel), idx=8, total=100, channel=channel)
    text = "\n".join(log.lines)
    noun = WORK_NOUN[channel]

    assert log.lines[0].startswith(f"  ┌─ {noun} 8/100 "), log.lines[0]
    assert log.lines[-1].startswith("  ├"), log.lines[-1]
    # 九行字段一个都不能少——少一个就是有平台"精简"了日志
    for key in ("work_id", "发布时间", "作者", "标题/正文", "话题标签",
                "互动", "IP 属地", "来源关键字", f"{noun}链接"):
        assert f"│ {key}" in text, f"{channel} 少了「{key}」这行"
    assert "2026-04-10 22:17:25" in text, "发布时间没按 年-月-日 时:分:秒 打"
    assert "赞 36 / 评 10 / 藏 0 / 转 0" in text, "互动这行的格式变了"
    assert "致水  (3x96xr46png6y2k)" in text, "作者要带 id，排查时靠它对人"


def test_comment_lines_two_rows_and_indent():
    log = FakeLog()
    log_comment_detail(log, make_comment(
        "c1", "颜来有你", "只有出生在那里的人一看就知道",
        datetime(2026, 4, 27, 23, 19, 43), likes=2), 3)
    log_comment_detail(log, make_comment(
        "c2", "致水", "家乡才是根", datetime(2026, 4, 11, 21, 31, 52),
        level="level_2", root="c1"), 4)
    assert log.lines[0] == "  │      [  3] 颜来有你：只有出生在那里的人一看就知道"
    # 第二行是元信息：赞 · 时间 · 层级 · 评论ID（id 是按行找库的抓手）
    assert log.lines[1] == "  │            赞 2 · 2026-04-27 23:19:43 · level_1 · c1"
    # 二级评论要缩进并挂 ↳，否则一眼看不出层级
    assert log.lines[2].startswith("  │          ↳ [  4]"), log.lines[2]
    assert "level_2" in log.lines[3] and log.lines[3].endswith("· c2")


def test_replies_are_sorted_under_their_root():
    """回复是后到的，必须排到顶楼下面再打。

    不排的话就是用户实跑截图里那样：七条一级评论，然后三条不知道
    挂在谁下面的二级评论。
    """
    r1 = make_comment("r1", "甲", "顶楼一", datetime(2026, 6, 2, 13, 14, 56))
    r2 = make_comment("r2", "乙", "顶楼二", datetime(2026, 4, 11, 14, 39, 33))
    k1 = make_comment("k1", "致水", "回复A", datetime(2026, 4, 11, 14, 45, 1),
                      level="level_2", root="r2")
    k2 = make_comment("k2", "致水", "回复B", datetime(2026, 4, 11, 21, 31, 52),
                      level="level_2", root="r2")
    # 到货顺序：两个顶楼在前，回复最后（真实情况就是这样）
    ordered = sort_comments_for_log([r1, r2, k1, k2])
    assert [c.comment_id for c in ordered] == ["r1", "r2", "k1", "k2"]
    # 顶楼按时间倒序（新的在上），回复挂在自己的顶楼下面、按时间正序
    r3 = make_comment("r3", "丙", "更新的顶楼", datetime(2026, 8, 1, 0, 0, 0))
    ordered = sort_comments_for_log([r1, r2, k2, k1, r3])
    assert [c.comment_id for c in ordered] == ["r3", "r1", "r2", "k1", "k2"]


def test_orphan_replies_are_not_swallowed():
    """顶楼没采到的回复也要打出来——排序不能把数据藏了。"""
    lone = make_comment("k9", "丁", "孤儿回复", datetime(2026, 4, 11, 1, 2, 3),
                        level="level_2", root="不存在的顶楼")
    ordered = sort_comments_for_log([lone])
    assert [c.comment_id for c in ordered] == ["k9"]


def test_missing_publish_time_does_not_crash_sorting():
    """时间缺失不能让排序炸掉（None 不能和 datetime 比大小）。"""
    a = make_comment("a", "甲", "有时间", datetime(2026, 4, 11, 1, 2, 3))
    b = make_comment("b", "乙", "没时间", None)
    ordered = sort_comments_for_log([b, a])
    assert {c.comment_id for c in ordered} == {"a", "b"}


def test_footer_closes_the_box():
    log = FakeLog()
    log_work_footer(log, 10)
    assert log.lines[0] == "      └─ 小计 10 条评论"
    log = FakeLog()
    log_work_footer(log, 0, "这条作品没有评论")
    assert log.lines[0] == "      └─ 小计 0 条评论（这条作品没有评论）"


# ---------------------------------------------------------------------------
# 方框必须闭合 —— 无论怎么退出
# ---------------------------------------------------------------------------
def test_comment_block_is_printed_on_every_exit_path():
    """被取消 / 登录失效 / 单条异常，方框都得闭合，攒着的评论都得打出来。

    实跑现场（2026-09-05）：作品 4/200 的框在 17:55:24 打开，
    库里进了 16 条评论，日志里一条明细都没有，框也一直没闭——
    因为"排序打印 + 小计"写在函数末尾，只要不是正常跑完就轮不到它。
    """
    import inspect
    from app.scheduler.runner import TaskRunner
    src = inspect.getsource(TaskRunner._collect_comments_for)
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "finally:" in body, "没有 finally——非正常出口时方框不会闭合"
    # 只能有一处调用（在 finally 里），多了会重复打印
    assert body.count("self._log_comments(") == 1, (
        f"_log_comments 被调用了 {body.count('self._log_comments(')} 处，"
        f"应该只在 finally 里调一次"
    )
    finally_idx = body.index("finally:")
    assert body.index("self._log_comments(") > finally_idx, "不在 finally 里"


def test_comment_button_is_hovered_first():
    """评论按钮的容器也带 hover-tip，和联播开关同一族：不 hover 就找不到。"""
    import inspect
    from app.collectors.kuaishou_browser import KuaishouBrowserCollector as K
    src = inspect.getsource(K._open_comments)
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "_hover_autoplay" in code, "点评论按钮前没把鼠标挪到播放区"
