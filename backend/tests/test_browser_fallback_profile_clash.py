"""同一个 profile 目录不能被开两次 —— 以及失败后不能留半成品。

实跑现场（2026-09-05，八大处公园-快手）：

    15:45:08 [kuaishou/小尹呀] 采集页面已就绪，12 个 Cookie      ← 接口模式开的
    15:45:14 [快手] 采集客户端就绪，账号 小尹呀
    15:45:14 [快手] 任务选的是「拟人」，跳过接口直接用浏览器模拟真人采集
    15:45:15 关键字失败：BrowserType.launch_persistent_context:
             Target page, context or browser has been closed   ← 拟人模式开第二个

Chrome 不会在同一个 user-data-dir 上开第二个实例：它把请求交给已经在跑的
那个进程，然后自己退出（exitCode=0），Playwright 只看到"进程没了"。

更糟的是后面：重试时 self._browser_collector 已经被赋值了，
`if ... is not None: return` 让每次重试都跳过 prepare，
采集器带着 _page=None 一路静默返回，八个关键字全 0 条，**任务还报成功**。
"""
import inspect

import pytest

from app.collectors.browser_capture import BrowserCaptureCollector
from app.collectors.douyin import DouyinCollector
from app.collectors.kuaishou import KuaishouCollector


def test_kuaishou_human_mode_does_not_open_the_api_browser_first():
    """拟人模式必须在 prepare 一开头就转手，不能先开一个接口用的浏览器。

    抖音一直是对的，快手漏了这一步——两个 launch_persistent_context
    指向同一个 profile 目录，就是实跑那个报错。
    """
    src = inspect.getsource(KuaishouCollector.prepare)
    head = src[:src.index("PageSession(")] if "PageSession(" in src else src
    assert "human_only(ctx)" in head, (
        "KuaishouCollector.prepare 在开 PageSession 之前没有判断拟人模式——"
        "会先开一个接口用的浏览器，拟人采集器再开第二个就撞同一个 profile"
    )
    assert "_init_browser_fallback" in head and "return" in head, (
        "判断了拟人却没有直接转手并 return"
    )
    # 抖音是参照物，别让它退化
    dy = inspect.getsource(DouyinCollector.prepare)
    assert "human_only(ctx)" in dy[:dy.index("_load_login_state")]


@pytest.mark.parametrize("collector", [KuaishouCollector, DouyinCollector])
def test_failed_prepare_does_not_leave_a_broken_collector_cached(collector):
    """prepare 失败必须把半成品清掉，否则重试会跳过 prepare 一路空跑。"""
    src = inspect.getsource(collector._init_browser_fallback)
    assert "except BaseException" in src, f"{collector.__name__} 没有兜住 prepare 失败"
    assert "self._browser_collector = None" in src, (
        f"{collector.__name__} prepare 失败后没有把 _browser_collector 清掉——"
        f"重试会因为 `if ... is not None: return` 直接跳过 prepare"
    )
    assert "raise" in src, "吞掉异常比留半成品还糟"


def test_kuaishou_hands_its_session_over_instead_of_opening_a_second_one():
    """混合模式中途降级：把开着的会话交接过去，不能再开第二个。"""
    src = inspect.getsource(KuaishouCollector._init_browser_fallback)
    assert "adopt_session" in src, (
        "混合模式降级时没有交接会话——接口模式的浏览器还开着"
        "（快手签名要常驻页面），再开一个就撞 profile"
    )
    assert "_detach_live_view" in src, "交接前要摘掉接口模式挂的实时画面"


def test_adopted_session_is_not_closed_by_the_borrower():
    """借来的会话由原持有者关，两边都关会把对方用着的页面关掉。"""
    src = inspect.getsource(BrowserCaptureCollector.cleanup)
    assert "_owns_session" in src, "cleanup 没区分会话是自己开的还是借来的"

    col = BrowserCaptureCollector.__new__(BrowserCaptureCollector)
    col._session = None
    col._owns_session = True
    col.adopt_session(object())
    assert col._owns_session is False, "adopt_session 之后所有权应该留在原持有者"


@pytest.mark.parametrize("method", ["collect_by_keyword", "collect_comments"])
def test_no_page_fails_loudly_instead_of_returning_zero(method):
    """没有页面要**炸**，不能安静地报 0 条。

    少采一条是小事；采了个寂寞还说自己成功，是必须炸出来的事。
    """
    src = inspect.getsource(getattr(BrowserCaptureCollector, method))
    assert "_require_page" in src, f"{method} 没有检查页面是否就绪"

    col = BrowserCaptureCollector.__new__(BrowserCaptureCollector)
    col._page = None
    col.channel = "kuaishou"
    with pytest.raises(RuntimeError, match="没有可用页面"):
        col._require_page("搜索关键字")
    # 有页面就不该拦
    col._page = object()
    col._require_page("搜索关键字")


# ---------------------------------------------------------------------------
# 浏览器整个没了 ≠ 这一条打不开
# ---------------------------------------------------------------------------
def test_browser_gone_is_told_apart_from_a_single_failure():
    """实跑里服务被 Ctrl+C 之后，采集器把"浏览器没了"当成"这一条打不开"，
    又往下跑了九条：每条开方框、每条报 0 条评论、每条写库，
    还把「连续 N 条都打不开」喊到 9。噪音盖住真正的原因，
    而且往库里写了九条假的"没有评论"。
    """
    B = BrowserCaptureCollector
    for msg in ("Mouse.wheel: Target page, context or browser has been closed",
                "Locator.count: Target page, context or browser has been closed",
                "Page.goto: Connection closed while reading from the driver"):
        assert B.browser_gone(RuntimeError(msg)), f"没认出来：{msg}"
    # 普通的单条失败不能被误判成"浏览器没了"，否则一条作品出问题就停整个平台
    for msg in ("Locator.click: Timeout 5000ms exceeded",
                "element is not enabled",
                "快手接口返回异常"):
        assert not B.browser_gone(RuntimeError(msg)), f"误判了：{msg}"


def test_runner_stops_the_platform_when_the_browser_is_gone():
    import inspect
    from app.scheduler.runner import TaskRunner
    src = inspect.getsource(TaskRunner._collect_comments_for)
    assert "browser_gone" in src, (
        "runner 没区分「浏览器没了」和「这一条评论采失败」——"
        "会一条条往下跑，把假的「没有评论」写进库"
    )


def test_attribute_reads_have_a_short_timeout():
    """读属性必须带短超时。

    Playwright 所有 locator 操作默认 30 秒。实跑日志里那句
    「Locator.get_attribute: Timeout 30000ms exceeded」就是这么来的：
    联播开关那块是 hover 浮层，视频一关元素就没了，于是干等 30 秒。
    """
    import inspect
    from app.collectors.kuaishou_browser import KuaishouBrowserCollector as K
    attr = inspect.getsource(K._attr)
    assert "timeout=timeout_ms" in attr and "2000" in attr, "_attr 没有短超时"
    # 联播那几处必须走 _attr，不能再直接 get_attribute
    autoplay = inspect.getsource(K._disable_autoplay)
    unlocked = inspect.getsource(K._switch_unlocked)
    for name, src in (("_disable_autoplay", autoplay), ("_switch_unlocked", unlocked)):
        # 只看代码行——注释里提到 get_attribute 是为了解释历史，不算
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        assert "get_attribute" not in code, f"{name} 还在直接 get_attribute（默认 30 秒）"
        assert "_attr" in code, f"{name} 没走带短超时的 _attr"


def test_order_table_is_cleared_between_keywords():
    """换关键字必须清空顺序表 —— 单个关键字永远测不出这个 bug。"""
    import inspect
    from app.collectors.kuaishou_browser import KuaishouBrowserCollector as K
    src = inspect.getsource(K._goto_search)
    assert "_order.clear()" in src, (
        "换关键字没清空顺序表：第二个关键字的第一条作品下标会是上一个"
        "关键字的长度，可它在新页面上是第 1 张卡片，全线错位"
    )
