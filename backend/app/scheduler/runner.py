"""任务执行引擎：把一条任务展开成"平台 x 目标"的采集，边采边写库。

一条任务的执行顺序：
    1. 解析景区（scenic_id -> scenic_name），所有落库数据都带上这两个字段
    2. 逐个平台执行；某个平台失败（比如账号过期）只跳过该平台，不整单失败
    3. 关键字模式：搜作品 -> 逐个作品采评论
       主页模式：  取该作者全部作品 -> 逐个采评论
       POI 模式：  生成合成作品 -> 采点评（携程/同程）
    4. 数据攒批写库，攒满 200 条或一个目标采完就 flush

取消：任何时候 set 了 cancel_event，会在下一条数据之间停下并标记为已取消。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..collectors.base import (
    BaseCollector,
    CollectContext,
    CollectorRegistry,
    CollectTarget,
    CommentItem,
    LoginRequired,
    TaskCancelled,
    WorkItem,
)
from ..core.config import Config
from ..core import collect_params, content_filter, search_filters
from ..core.constants import (
    AccountStatus, CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_LABELS,
    CHANNEL_WEIBO, CHANNEL_XHS, CHANNELS_POI_BASED, CHANNELS_WITH_WORKS, TaskStatus,
)
from ..browser.lease import DEFAULT_WAIT_TIMEOUT_SECONDS, BrowserLease
from ..browser.slots import SlotWaitTimeout
from ..core.logging import get_logger
from ..db import tables
from ..repositories.data_repo import DataRepository, comment_uk, work_uk
from ..repositories.scenic_repo import ScenicRepository
from ..repositories.task_repo import TaskRepository
from .task_logger import TaskLogger

logger = get_logger(__name__)

#: 攒批阈值。**主路径已经是"一条作品采完就落库"**（见 _collect_keyword），
#: 这两个阈值只在**单条作品内部**起兜底作用：一条作品有几千条评论时，
#: 不至于全压在内存里等这条作品采完。
FLUSH_THRESHOLD = 200
#: ⚠️ 光有条数阈值是**不够**的。拟人模式一条笔记要十几二十秒，
#: 攒够 200 条要一个多小时——这段时间里数据全在内存里，库里一条都没有。
#: 用户看到的就是"日志在刷、数据页是空的"，而且任务一旦中途出错或被停掉，
#: 这一个多小时的采集就全丢了（finally 里的 flush 遇到进程被杀也救不回来）。
#: 所以再加一条**时间**阈值：离上次写库超过这么久就先写一批。
#: 接口模式下一批几十条几秒钟就攒满，走的还是条数那条路，批量写的收益没丢。
FLUSH_MAX_SECONDS = 15.0
#: 一条作品最多逐条打印多少条评论。一条作品 500 条评论全打出来日志没法看。
COMMENT_LOG_LIMIT = 30

#: 采集模式在日志里怎么写。用户在页面上看到的就是这几个词。
_ENGINE_LABELS = {"api": "API", "hybrid": "混合", "human": "拟人"}

#: 连续多少条作品的评论都采失败，就判定是系统性故障、停掉这个平台。
#: 单条失败是常态（作品被删、评论区关了、作者设了权限）；
#: 连续失败说明是采集器/登录态/风控层面的问题，继续跑只是把 200 条作品
#: 挨个"跳过"一遍，既浪费时间又让人误以为任务跑成功了。
COMMENT_FAIL_STREAK_LIMIT = 5


def _work_text(work: WorkItem) -> str:
    """拿来做内容匹配的那段文字：标题 + 描述 + 标签。"""
    return " ".join(filter(None, [
        work.title or "",
        work.description or "",
        work.label or "",
    ]))


def _work_matches_keyword(work: WorkItem, keyword: str, rules=None) -> bool:
    """这条作品留不留。

    规则由任务的「内容过滤」配置决定（见 core/content_filter.py）：
    关掉就全留；开着就要求标题/描述/标签里含搜索关键字，
    或者含用户自己配的补充词之一。
    rules 为 None 时按默认规则（开启 + 只按搜索关键字）——改造前的行为。
    """
    if rules is None:
        from ..core.content_filter import ContentFilter
        rules = ContentFilter()
    return rules.matches(_work_text(work), keyword)


def _short(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


#: 各平台管"一条内容"叫什么。日志里用平台自己的说法，用户看着不别扭。
WORK_NOUN = {
    CHANNEL_XHS: "笔记",
    CHANNEL_DOUYIN: "作品",
    CHANNEL_KUAISHOU: "作品",
    CHANNEL_WEIBO: "微博",
}
#: 方框的宽度（半角字符数）。四个平台一个样，别各调各的。
BOX_RULE = 64


def _fmt_time(value) -> str:
    """时间统一成 2026-05-25 22:09:09；没有就写 -。"""
    if value is None or value == "":
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value)


def log_work_detail(log: TaskLogger, work: WorkItem, idx: int = 0,
                    total: int = 0, channel: str = "") -> None:
    """一条作品的完整字段，按**落库时的样子**打印成一个方框。

    ⚠️ 放在 runner 而不是各个采集器里，是为了**四个平台长一个样**。
    以前抖音一条作品只有一行流水、小红书是另一套，出问题时得用两套方法排查。

    格式是从 verify_kuaishou_browser.py 搬过来的（用户实跑之后指定要这个）：

        ┌─ 作品 8/100 ──────────────────────────
        │ work_id    3xzxqey42rgz2iy
        │ 发布时间       2026-04-10 22:17:25
        │ 作者         致水  (3x96xr46png6y2k)
        │ 标题/正文      盘山公路 #风景都在路上而不是终点
        │ 话题标签       风景都在路上而不是终点
        │ 互动         赞 36 / 评 0 / 藏 0 / 转 0
        │ IP 属地      -
        │ 来源关键字      盘山风景区
        │ 笔记链接       https://www.kuaishou.com/short-video/3xzxqey42rgz2iy
        ├────────────────────────────────────────

    字段名不等宽是因为中文占两格而 f-string 按字符数补齐——
    这是刻意保留的，和用户认可的那份输出一模一样，别"顺手对齐"。
    """
    noun = WORK_NOUN.get(channel or work.channel or "", "作品")
    pos = f" {idx}/{total}" if idx and total else (f" 第 {idx} 条" if idx else "")
    log.info(f"  ┌─ {noun}{pos} " + "─" * 52)
    rows = [
        ("work_id", work.work_id),
        ("发布时间", _fmt_time(work.publish_time)),
        ("作者", f"{work.author_name or '匿名'}  ({work.author_id or '-'})"),
        ("标题/正文", _short(work.title or work.description, 60) or "-"),
        ("话题标签", work.label or "-"),
        ("互动", f"赞 {work.likes} / 评 {work.comment_cnt} /"
                 f" 藏 {work.collection_cnt} / 转 {work.shares}"),
        ("IP 属地", work.location or "-"),
        ("来源关键字", work.source_keyword or "-"),
        (f"{noun}链接", work.work_url or "-"),
    ]
    for key, value in rows:
        log.info(f"  │ {key:<10} {value}")
    log.info("  ├" + "─" * BOX_RULE)


def log_comment_detail(log: TaskLogger, comment: CommentItem, no: int) -> None:
    """一条评论，两行：正文一行、赞/时间/层级一行。二级评论缩进并挂 ↳。"""
    level = getattr(comment, "comment_level", "") or "level_1"
    indent = "      " if level.endswith("1") else "          ↳ "
    content = _short(comment.content, 52) or "（空）"
    log.info(f"  │{indent}[{no:>3}] {comment.commenter_name or '匿名'}：{content}")
    # 评论 id 挂在第二行末尾：用户指定的版式是第一行"谁说了什么"，
    # 第二行是元信息。id 是排查时按行找库唯一的抓手（用户早先明确要过），
    # 放在这儿既不动他认可的第一行，也不至于丢掉。
    log.info(f"  │{indent}      赞 {comment.likes} · "
             f"{_fmt_time(comment.publish_time)} · {level} · {comment.comment_id}")


def log_work_footer(log: TaskLogger, count: int, note: str = "") -> None:
    """方框收尾：小计多少条评论。"""
    tail = f"（{note}）" if note else ""
    log.info(f"      └─ 小计 {count} 条评论{tail}")


def sort_comments_for_log(comments: List[CommentItem]) -> List[CommentItem]:
    """把评论排成"顶楼 → 它的回复"的顺序再打印。

    为什么要排：回复是**后到**的（要滚到那儿、点开「查看更多回复」才拉），
    按到货顺序打的话，一条作品的日志是"七条一级评论，然后三条不知道
    挂在谁下面的二级评论"——用户实跑截图里就是这个样子，看不出树形。

    顶楼按发布时间**倒序**（和平台自己的排法一致，新的在上），
    每个顶楼下面的回复按时间**正序**（一段对话从头读下来才顺）。
    时间缺失的排在最后，不参与比较（None 不能和 datetime 比大小）。
    """
    roots: List[CommentItem] = []
    replies: Dict[str, List[CommentItem]] = {}
    for c in comments:
        level = getattr(c, "comment_level", "") or "level_1"
        if level.endswith("1"):
            roots.append(c)
        else:
            replies.setdefault(c.root_comment_id or "", []).append(c)

    far_past = datetime.min
    roots.sort(key=lambda c: c.publish_time or far_past, reverse=True)
    ordered: List[CommentItem] = []
    for root in roots:
        ordered.append(root)
        kids = replies.pop(root.comment_id, [])
        kids.sort(key=lambda c: c.publish_time or far_past)
        ordered.extend(kids)
    # 顶楼没被采到的孤儿回复也要打出来，不能因为排序把数据"藏"了
    for leftover in replies.values():
        leftover.sort(key=lambda c: c.publish_time or far_past)
        ordered.extend(leftover)
    return ordered


def _limits_line(channel: str, limits: Dict[str, Any]) -> str:
    """把本平台生效的上限写进任务日志，用户改了数量能立刻看出有没有生效。"""
    unlimited = "不限"
    comments = limits["max_comments_per_work"] or unlimited
    if channel in CHANNELS_POI_BASED:
        pages = limits.get("max_pages") or "不限（一直翻到没有数据）"
        return (
            f"本次采集上限：点评 {comments} 条，最多翻 {pages} 页，"
            f"评论层级 {limits['max_comment_level']}"
        )
    works = limits["max_works"] or unlimited
    if not limits["collect_comments"]:
        return f"本次采集上限：作品 {works} 条（本平台已关闭评论采集）"
    return (
        f"本次采集上限：作品 {works} 条，每条作品评论 {comments} 条，"
        f"评论层级 {limits['max_comment_level']}"
    )


class TaskRunner:
    def __init__(
        self,
        config: Config,
        *,
        task_repo: TaskRepository,
        scenic_repo: ScenicRepository,
        data_repo: DataRepository,
        proxy_manager,
        browser_manager,
    ):
        self.config = config
        self.tasks = task_repo
        self.scenics = scenic_repo
        self.data = data_repo
        self.proxy_manager = proxy_manager
        self.browser_manager = browser_manager
        # 单账号配额与冷却。建一次，全 runner 共用——会话时长记在它身上，
        # 每次新建就等于每条任务都从 0 开始计时，单次时长上限形同虚设。
        from ..core.account_quota import AccountQuota
        from ..core.redis_client import get_redis

        self.quota = AccountQuota(config, get_redis(), data_repo.db)
        # 采集出错重试几次再放弃。3 次是个平衡：偶发的网络抖动/限流基本能扛过去，
        # 真的坏了也不会在一个采不到的关键字上耗太久。
        self.max_retries = max(1, int((config.crawl or {}).get("max_retries", 3) or 3))

    def _skip_collected_today(self) -> bool:
        """当天已采成功的作品，重跑时要不要跳过评论。默认开。"""
        return bool((self.config.crawl or {}).get("skip_works_collected_today", True))

    # ---------------- 入口 ----------------
    async def run(self, task: Dict[str, Any], cancel_event: Optional[asyncio.Event] = None) -> Dict[str, int]:
        task_id = task["task_id"]
        log = TaskLogger(task_id, self.tasks)
        cancel_event = cancel_event or asyncio.Event()
        stats = {"new_works": 0, "updated_works": 0, "new_comments": 0, "updated_comments": 0}
        started = time.time()

        await self.tasks.mark_running(task_id)
        log.info(f"任务开始：{task['task_name']}")

        try:
            scenic_id = task.get("scenic_id") or ""
            scenic_name = ""
            if scenic_id:
                scenic = await self.scenics.get_scenic(scenic_id)
                if not scenic:
                    raise ValueError(f"任务绑定的景区不存在：{scenic_id}")
                scenic_name = scenic["scenic_name"]
                log.info(f"景区：{scenic_name}（{scenic_id}）")

            channels: List[str] = task.get("channels") or []
            if not channels:
                raise ValueError("任务没有选择任何平台")

            for index, channel in enumerate(channels, start=1):
                if cancel_event.is_set():
                    raise TaskCancelled("任务已被取消")
                channel_log = log.bind(channel)
                try:
                    channel_stats = await self._run_channel(
                        task, channel, scenic_id, scenic_name, channel_log, cancel_event
                    )
                    for key in stats:
                        stats[key] += channel_stats.get(key, 0)
                except TaskCancelled:
                    raise
                except SlotWaitTimeout as exc:
                    # 排队等超时。这是**失败**不是"跳过"——用户要能一眼看出
                    # "这条任务什么都没采，是因为浏览器一直被占着"，
                    # 而不是在成功的任务里找一行灰色的跳过日志。
                    channel_log.error(
                        f"{channel} 没能拿到浏览器：{exc}。"
                        f"通常是有登录窗口开着没关，或者同平台的任务排得太满"
                    )
                except LoginRequired as exc:
                    # ⚠️ 需要人来处理：发通知，然后**等**（而不是直接跳过）。
                    # 用户的要求："如果是因为需要登录，验证的 采集暂停"。
                    # 直接跳过的话通知发了也没意义——人处理完，
                    # 这一轮早就跑完了，还得再手动跑一次。
                    channel_log.error(f"{channel} 需要人工介入：{exc}")
                    resumed = await self._pause_for_human(
                        channel, exc, channel_log, cancel_event,
                        task_id=task_id, scenic_name=scenic_name)
                    if resumed:
                        channel_log.info(f"{channel} 已恢复，重试这个平台")
                        try:
                            channel_stats = await self._run_channel(
                                task, channel, scenic_id, scenic_name,
                                channel_log, cancel_event,
                            )
                            for key in stats:
                                stats[key] += channel_stats.get(key, 0)
                        except TaskCancelled:
                            raise
                        except Exception as exc2:  # noqa: BLE001
                            channel_log.error(f"{channel} 恢复后仍失败：{exc2}")
                    else:
                        channel_log.error(f"跳过 {channel}：一直没人处理")
                except Exception as exc:  # noqa: BLE001
                    channel_log.error(f"{channel} 采集失败：{exc}")
                    logger.exception("平台 %s 采集异常", channel)
                    # 采集报错也要通知——用户要求里的"或出现错误"
                    await self._notify(channel, "error", str(exc),
                                       task_id=task_id, scenic_name=scenic_name)

                await self.tasks.update_progress(task_id, int(index / len(channels) * 100))

            elapsed = time.time() - started
            log.info(
                f"任务完成，用时 {elapsed:.1f}s；"
                f"作品 新增 {stats['new_works']} / 更新 {stats['updated_works']}，"
                f"评论 新增 {stats['new_comments']} / 更新 {stats['updated_comments']}"
            )
            await self.tasks.mark_finished(task_id, TaskStatus.COMPLETED.value, stats=stats)

        except TaskCancelled as exc:
            log.warn(f"任务已取消：{exc}")
            await self.tasks.mark_finished(
                task_id, TaskStatus.CANCELED.value, error=str(exc), stats=stats
            )
        except asyncio.CancelledError:
            # ⚠️ 必须单独接。CancelledError 在 3.8 之后继承的是 BaseException，
            # **不会**被下面的 `except Exception` 接住。
            # 看门狗超时后会 handle.cancel()，走的正是这条路——
            # 以前这里没有分支，异常直接穿过去，`mark_finished` 一次都没调，
            # 任务在库里**永远停在"运行中"**。用户看到的就是
            # "日志里已经释放浏览器了，任务状态还挂着"。
            log.warn("任务被强制取消（多半是看门狗超时后强杀），标记为已取消")
            try:
                await self.tasks.mark_finished(
                    task_id, TaskStatus.CANCELED.value,
                    error="超时被强制取消", stats=stats,
                )
            finally:
                await log.flush()
            raise      # 取消必须继续往上抛，否则 asyncio 认为它被吞了
        except Exception as exc:  # noqa: BLE001
            log.error(f"任务失败：{exc}")
            logger.exception("任务 %s 执行异常", task_id)
            await self.tasks.mark_finished(
                task_id, TaskStatus.FAILED.value, error=str(exc), stats=stats
            )
        finally:
            await log.flush()

        return stats

    # ---------------- 单平台 ----------------
    async def _content_filter_for(self, channel: str, scenic_id: str,
                                 params: Dict[str, Any]):
        """把景区的附关键字 / 过滤关键字并进这次采集的过滤规则。

        词表现在挂在**景区**上（一个景区配一次，所有任务共用），
        而不是每建一个任务重填一遍——同一个景区跑十个任务要填十遍，
        改一次要改十处，漏改一处就出现"两个任务采出来的东西不一样"。

        只对有作品概念的四个平台生效（抖音/快手/小红书/微博）。
        携程、同程是按 POI 拉点评的，压根没有"搜索关键字"这回事，
        给它们套一层关键字过滤只会把点评全丢掉。
        """
        rules = content_filter.ContentFilter.from_params(params)
        if channel not in CHANNELS_WITH_WORKS or not scenic_id:
            return rules
        try:
            words = await self.scenics.filter_words_for(scenic_id)
        except Exception as exc:      # noqa: BLE001
            # ⚠️ 读不到词表**不能**当成"没有过滤词"就往下跑：
            # 附关键字读不到 → 本该留的内容被丢；过滤词读不到 → 本该丢的存进来。
            # 但也不该把整条任务拦死。折中：保留任务自带的配置，并且**大声说**
            # 这次的景区词表没生效，用户看日志能对上"这次结果怎么不一样"。
            logger.warning("[过滤] 读景区 %s 的词表失败，这次只用任务自带的配置：%s",
                           scenic_id, exc)
            return rules
        if words.get("aux"):
            # 景区的附关键字和任务里残留的补充词取并集，保序去重
            merged = list(rules.extra_keywords)
            seen = {w.lower() for w in merged}
            for word in words["aux"]:
                if word.lower() not in seen:
                    seen.add(word.lower())
                    merged.append(word)
            rules.extra_keywords = merged[:content_filter.MAX_AUX_KEYWORDS]
            # 配了附关键字就得真的按"关键字 + 附关键字"去筛，
            # 否则模式还停在 keyword，词填了却一个都不参与匹配
            rules.mode = content_filter.MODE_KEYWORD_PLUS
        if words.get("exclude"):
            rules.exclude_keywords = words["exclude"][:content_filter.MAX_EXCLUDE_KEYWORDS]
        return rules

    async def _run_channel(
        self, task: Dict[str, Any], channel: str, scenic_id: str, scenic_name: str,
        log: TaskLogger, cancel_event: asyncio.Event,
    ) -> Dict[str, int]:
        if not self.config.platform(channel).get("enabled", True):
            log.warn(f"{channel} 在配置里被禁用，跳过")
            return {}

        collector: BaseCollector = CollectorRegistry.create(
            channel, self.config, self.proxy_manager
        )
        # 这一步必须在账号检查之前：未接入的平台如果先撞上"没有可用账号"，
        # 用户会以为是账号配错了，跑去反复登录，而真实原因是采集器还没实现。
        if not collector.integrated:
            log.warn(
                f"{CHANNEL_LABELS.get(channel, channel)} 采集器尚未接入（第二阶段实现），"
                f"已跳过该平台"
            )
            return {}

        # 采集数量按平台解析：作品型平台是「作品数 + 每作品评论数」，
        # 携程/同程这种点评型平台只有「点评总数」一个维度。
        params = task.get("params") or {}
        limits = collect_params.resolve(self.config, params, channel)
        channel_params = collect_params.channel_overrides(params, channel)
        ctx = CollectContext(
            scenic_id=scenic_id,
            scenic_name=scenic_name,
            task_id=task["task_id"],
            max_works=limits["max_works"],
            max_comments_per_work=limits["max_comments_per_work"],
            max_comment_level=limits["max_comment_level"],
            enable_sub_comments=limits["enable_sub_comments"],
            collect_comments=limits["collect_comments"],
            # 采集模式：api（只走接口）/ hybrid（先接口，没数据换拟人）/ human（全程拟人）
            collect_engine=limits.get("collect_engine", "hybrid"),
            account_name=channel_params.get("account_name")
            or params.get("account_name", ""),
            filters=search_filters.resolve(channel, params),
            content_filter=await self._content_filter_for(channel, scenic_id, params),
            params={**params, "max_pages": limits.get("max_pages", 0)},
            cancel_event=cancel_event,
            logger=log,
            # 单个关键字的时间预算：到点换下一个关键字，别让一个关键字
            # 把整条任务的时间吃光（实测有过连续点不开、空转到看门狗超时的情况）
            keyword_budget_seconds=float(
                self.config.get("crawl.keyword_budget_minutes", 0) or 0) * 60,
        )
        # 账号分组：平台级 account_group > 任务级 account_group。
        # 同组账号轮换采集，分摊单账号的请求量与风控压力。
        ctx.params["account_group"] = (
            channel_params.get("account_group") or params.get("account_group") or ""
        )
        log.info(_limits_line(channel, limits))
        if channel not in CHANNELS_POI_BASED:
            log.info(f"搜索条件：{ctx.filters.describe()}")
            # 过滤配置一定要打出来：用户改了设置最想确认的就是"生效没有"，
            # 而"采到的条数变少了"这种现象，光看结果分不清是过滤还是平台没内容
            log.info(ctx.content_rules.describe())

        # 登录态的拿法由各平台自己决定，这里只把入口交出去：
        #   抖音 —— 接口签名在 Node 里算，页面只用来取 msToken/Cookie，
        #            取完就把浏览器关掉（见 douyin.prepare）
        #   快手 —— 每次请求的签名都要调页面里的 __ks_realm，浏览器必须常驻
        #   小红书 / 微博 —— 只要 Cookie 串，走静默刷新，不开浏览器
        ctx.params["browser_manager"] = self.browser_manager
        # 实时画面面板上要显示是哪条任务在跑，不然多条任务一起跑时分不清
        ctx.params["task_name"] = task.get("task_name") or ""

        # 浏览器租约：谁在什么时候占住哪个账号的浏览器，统一由它管。
        #   · 全程要浏览器的（拟人模式、快手）→ 这里就占住，占不到就排队等
        #   · 只是偶尔开一下的（抖音 API 模式刷 Cookie）→ 采集器临时 acquire
        # token 用 任务+平台，保证同一条任务重复 acquire 不会被自己挡住。
        lease = BrowserLease(
            self.browser_manager, channel,
            token=f"{task['task_id']}:{channel}",
            reason=f"采集「{scenic_name or scenic_id}」",
            preferred_account=ctx.account_name,
            group=ctx.params.get("account_group", ""),
            log=log,
            cancel_event=cancel_event,
            wait_timeout=float(self.config.get(
                "browser.slot_wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
        )
        ctx.params["browser_lease"] = lease
        ctx.params["browser_slot_token"] = lease.token

        engine = collector.engine_of(ctx)
        holds_browser_all_run = (
            collector.needs_login
            and (engine == "human" or collector.needs_browser_for_api(ctx))
        )
        if holds_browser_all_run:
            # ⚠️ 这里是"排队而不是跳过"的落点。
            # 改造前撞上账号被占用会抛 LoginRequired，被上层吞成"跳过该平台"，
            # 用户建的任务安安静静什么都没采。现在改成等前面那个跑完。
            why = "拟人模式全程需要浏览器" if engine == "human" else "该平台的接口签名需要常驻页面"
            account_name = await lease.acquire(why)
            # 后面 PageSession 要开的就是这个账号，别再各挑各的
            ctx.account_name = account_name
        elif collector.needs_login:
            account = await self.browser_manager.accounts.pick_active(
                channel, preferred=ctx.account_name,
                group=ctx.params.get("account_group", ""),
            )
            if account:
                group_note = (
                    f"（分组 {account['account_group']}）"
                    if account.get("account_group") and account["account_group"] != "default"
                    else ""
                )
                # ⚠️ 必须把挑中的号写回 ctx。以前这里只打了条日志就完了，
                #    于是下面的配额闸门看到 ctx.account_name 是空的、
                #    直接跳过——**Cookie 这条路上配额等于没生效**。
                #    而且采集器后面会自己再挑一次号，挑出来的可能跟这里
                #    日志里写的不是同一个，排查起来能把人绕晕。
                ctx.account_name = account["account_name"]
                log.info(f"将使用账号 [{account['account_name']}]{group_note} 的登录态（静默获取）")

        # 配额闸门：这个账号今天还能不能接着用。
        #
        # ⚠️ 放在拿到账号**之后**、开采之前。冷却中的账号本来就被
        #    pick_active 排掉了，走到这里说明它是新挑出来的——但日配额
        #    和连续工作时长要在这里判，判过了才开始计时。
        quota = getattr(self, "quota", None)
        if quota is not None and collector.needs_login and ctx.account_name:
            verdict = await quota.check(channel, ctx.account_name)
            if not verdict.ok:
                log.warn(f"⏸ {verdict.reason}。这一轮跳过 {channel}，"
                         f"等冷却结束或换个账号再来")
                return {"new_works": 0, "updated_works": 0,
                        "new_comments": 0, "updated_comments": 0}
            quota.start_session(channel, ctx.account_name)

        # 轮换锁：这个号派上用场了，锁定期内下一轮先让别的号上。
        # 放在配额闸门**之后**——被配额拦下来的那一轮根本没采，不该上锁。
        if collector.needs_login and ctx.account_name:
            await self.browser_manager.accounts.mark_rotation_used(
                channel, ctx.account_name)

        # 今天已经采过的作品，重跑时直接跳过它们的评论。
        # ⚠️ 只在通道开始时查一次，之后不刷新：本次运行自己写进去的作品
        # 不该被算成"今天已采过"，否则关键字重试时会把刚采的也跳掉——
        # 那不是省事，是真的漏采。本次运行内的重复由 _Buffer.seen_works 管。
        # 采集水印（关键字 4 小时 / 作品 3 天，都落 Redis）。
        # 建一次放进 ctx，关键字循环和作品循环共用同一个实例。
        try:
            from ..core.dedup_marks import DedupMarks

            from ..core.redis_client import get_redis

            marks = DedupMarks(get_redis(), self.config)
            ctx.params["_dedup_marks"] = marks
            log.info(marks.describe())
        except Exception as exc:  # noqa: BLE001
            # 水印是优化，不是必需。建不起来就照常采，别把任务拦下来。
            ctx.params["_dedup_marks"] = None
            log.warn(f"采集水印初始化失败，本轮不去重：{exc}")

        ctx.params["_collected_today"] = set()
        if scenic_id and self._skip_collected_today():
            try:
                done = await self.data.work_ids_collected_since(
                    scenic_id, channel, datetime.now().replace(
                        hour=0, minute=0, second=0, microsecond=0),
                )
                ctx.params["_collected_today"] = done
                if done:
                    log.info(
                        f"今天已经采到过评论的作品有 {len(done)} 条，本轮跳过它们的评论"
                        f"（只更新作品数据）。评论为 0 的**不算**，会重新翻——"
                        f"否则采集器出问题那几轮的空结果会把它们当天永久锁死"
                    )
            except Exception as exc:  # noqa: BLE001
                # 查不到就当没有——这只是个优化，不该因为它把采集拦下来
                log.warn(f"读取今日已采作品失败（{exc}），本轮不做当日去重")

        proxy_state = "已启用" if self.proxy_manager.enabled_for(channel) else "未启用"
        log.info(f"{channel} 开始采集（模式：{_ENGINE_LABELS.get(engine, engine)}），代理{proxy_state}")

        await collector.prepare(ctx)
        buffer = _Buffer(
            self.data, log,
            refresh_existing_comments=bool(
                (self.config.crawl or {}).get("refresh_existing_comments", False)
            ),
        )
        try:
            collect_type = task.get("collect_type", "keyword")
            if channel in CHANNELS_POI_BASED or collect_type == "poi":
                await self._collect_poi(collector, ctx, task, channel, buffer, log)
            elif collect_type == "creator":
                await self._collect_creator(collector, ctx, task, channel, buffer, log)
            else:
                await self._collect_keyword(collector, ctx, task, buffer, log)
        finally:
            await buffer.flush()
            await collector.cleanup()
            # 出口只有一条：不管采集器中途开过几次浏览器，租约都在这里还回去，
            # 后面排队的任务才动得了。
            lease.release()

        duplicated = buffer.dedup_summary()
        if duplicated:
            log.info(f"{channel} 本轮跳过重复：{duplicated}（多个关键字命中同一条内容）")
        return buffer.stats

    # ---------------- 三种采集方式 ----------------
    # ------------------------------------------------------------------ 通知
    async def _notify(self, channel: str, kind: str, detail: str = "", *,
                      account: str = "", task_id: str = "",
                      scenic_name: str = "") -> bool:
        """发一条飞书通知。**任何失败都吞掉**——通知挂了不能带崩采集。"""
        try:
            from ..notify import NotifyEvent, get_notifier

            return await get_notifier().send(NotifyEvent(
                channel=channel, kind=kind, detail=detail, account=account,
                task_id=task_id, scenic_name=scenic_name))
        except Exception as exc:  # noqa: BLE001
            logger.debug("发通知失败（忽略）：%s", exc)
            return False

    @staticmethod
    def _classify_login_issue(exc: Exception) -> str:
        """把 LoginRequired 的原因分成通知里的「验证项」。

        分类只影响通知文案，分错了不影响功能——所以宁可粗一点，
        也别为了精确去解析平台的错误码（那些随时会变）。
        """
        text = str(exc)
        if any(w in text for w in ("验证码", "captcha", "滑块", "拼图")):
            return "captcha"
        if any(w in text for w in ("二次验证", "短信", "验证手机", "2fa", "两步")):
            return "two_factor"
        if any(w in text for w in ("失效", "过期", "expired", "invalid")):
            return "account_invalid"
        return "login_required"

    async def _channel_has_account(self, channel: str) -> bool:
        """这个平台**有没有配过**账号（不管当前能不能用）。

        用来区分"账号失效了，等人重新登录"和"从来就没配过账号"。
        查不出来时返回 True（宁可等一等，也别把真的能恢复的情况漏掉）。
        """
        accounts = getattr(self.browser_manager, "accounts", None)
        if accounts is None:
            return True
        try:
            # 只要 total，page_size 给 1 就够——这张表里 cookies 是 LONGTEXT，
            # 多拉一行就多拉一整串 Cookie。
            page = await accounts.list(channel=channel, page=1, page_size=1)
            if isinstance(page, dict):
                return int(page.get("total") or 0) > 0
            return bool(page)
        except Exception:  # noqa: BLE001
            # 查不出来时宁可返回 True 去等一等，也别把"其实能恢复"的情况
            # 当成"没配账号"直接跳过。
            return True

    async def _pause_for_human(
        self, channel: str, exc: Exception, log: TaskLogger, cancel_event,
        *, task_id: str = "", scenic_name: str = "",
    ) -> bool:
        """需要登录/验证时暂停等人处理。返回是否等到了（True = 可以重试）。

        怎么判断"人处理好了"：**账号重新可用**。这是唯一可信的信号——
        人可能在别的机器上登录、可能换了个账号，只看"有没有人点按钮"
        会漏掉这些情况。所以这里轮询账号仓库，等到有可用账号就继续。

        ⚠️ 等待期间要一直响应取消。用户点了停止不能还在这儿干等。
        """
        cfg = (self.config.get("notify") or {})
        kind = self._classify_login_issue(exc)
        await self._notify(channel, kind, str(exc),
                           task_id=task_id, scenic_name=scenic_name)

        # ⚠️ **压根没配过账号 ≠ 账号需要重新登录**。
        # 前者等多久都没用——没人会去"恢复"一个从来不存在的账号，
        # 而任务会白白卡 30 分钟，日志上还写着"等待人工处理"，
        # 看着像在干活。这种情况直接照旧报错跳过。
        if not await self._channel_has_account(channel):
            log.error(f"{channel} 一个账号都没配，等待没有意义——"
                      f"去账号管理里添加并登录一个账号再跑")
            return False

        if not cfg.get("pause_on_login_required", True):
            log.info(f"{channel} 需要人工介入，但配置里关掉了「暂停等待」，直接跳过")
            return False

        timeout = float(cfg.get("pause_timeout_minutes", 30)) * 60
        if timeout <= 0:
            return False
        log.warn(f"⏸ {channel} 已暂停，等待人工处理（最多 {timeout / 60:.0f} 分钟）。"
                 f"处理完不用点任何按钮——检测到账号恢复就会自动继续")

        deadline = time.time() + timeout
        poll = 15.0
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelled("等待人工处理时任务被取消")
            await asyncio.sleep(poll)
            # 判据是"这个平台有 ACTIVE 账号"——人可能在别的机器上登录、
            # 也可能换了个账号，只看"有没有人点按钮"会漏掉这些情况。
            try:
                account = await self.browser_manager.accounts.pick_active(channel)
            except Exception as exc:  # noqa: BLE001
                logger.debug("等待期间查账号失败（继续等）：%s", exc)
                account = None
            usable = bool(account) and str(
                (account or {}).get("status") or "") == AccountStatus.ACTIVE.value
            if usable:
                left = int(deadline - time.time())
                log.info(f"检测到 {channel} 账号 [{account.get('account_name', '')}] "
                         f"已恢复（等了 {int(timeout - left) // 60} 分钟），继续采集")
                return True
        log.error(f"{channel} 等了 {timeout / 60:.0f} 分钟仍没有可用账号，放弃这个平台")
        return False

    def _marks(self, ctx: CollectContext):
        """取采集水印工具。没有 Redis 就返回 None（不去重，但照常采）。"""
        marks = ctx.params.get("_dedup_marks")
        return marks

    async def _collect_keyword(
        self, collector: BaseCollector, ctx: CollectContext, task: Dict[str, Any],
        buffer: "_Buffer", log: TaskLogger,
    ) -> None:
        keywords = [k for k in (task.get("keywords") or []) if k]
        if not keywords:
            log.warn("任务没有配置关键字，跳过关键字采集")
            return

        filters = ctx.search_filters
        failed_keywords: List[str] = []
        skipped_keywords: List[str] = []
        marks = self._marks(ctx)
        for keyword in keywords:
            ctx.raise_if_cancelled()
            # ⚠️ 水印在**重试之外**判断：命中了整个关键字都不跑，
            # 不是跑一半再跳。用户的诉求是"重新开启任务别重复采"。
            if marks is not None and marks.keyword_done(
                    ctx.scenic_id, collector.channel, keyword):
                left = marks.keyword_ttl_left(ctx.scenic_id, collector.channel, keyword)
                mins = max(1, left // 60) if left > 0 else "?"
                skipped_keywords.append(keyword)
                log.info(f"[水印] 关键字 [{keyword}] 刚采过，跳过"
                         f"（还有约 {mins} 分钟过期；想立刻重采就在设置里"
                         f"关掉 crawl.dedup.keyword_enabled）")
                continue
            try:
                await self._retrying(
                    f"关键字 [{keyword}]", log,
                    lambda kw=keyword: self._collect_one_keyword(
                        collector, ctx, kw, buffer, log
                    ),
                )
                # 只有**真的跑完**才打水印。失败的不打——
                # 打了的话下次重跑会以为采过了，那条关键字就真丢了。
                if marks is not None:
                    marks.mark_keyword(ctx.scenic_id, collector.channel, keyword)
            except (TaskCancelled, LoginRequired):
                raise
            except Exception as exc:  # noqa: BLE001
                # 重试完还是不行：记下来，继续下一个关键字。
                # 一个词采不到不该让同一个景区的其他词全部落空。
                failed_keywords.append(keyword)
                log.error(f"关键字 [{keyword}] 重试 {self.max_retries} 次仍失败，跳过：{exc}")
        if skipped_keywords:
            log.info(f"[水印] 本轮跳过 {len(skipped_keywords)} 个刚采过的关键字："
                     f"{'、'.join(skipped_keywords[:8])}"
                     f"{'…' if len(skipped_keywords) > 8 else ''}")
        if failed_keywords:
            log.warn(
                f"本轮有 {len(failed_keywords)} 个关键字没采成："
                f"{'、'.join(failed_keywords)}"
            )

    async def _retrying(self, label: str, log: TaskLogger, action):
        """重试 max_retries 次再放弃。取消和登录失效不重试。

        ⚠️ 重试整个关键字看着浪费，其实很便宜：`_Buffer` 会把已经收过的
        作品判成重复，既不重复入库、**也不重复采它的评论**。
        所以第二次跑基本只是把前面翻过的页面快速走一遍，真正的开销
        只有翻页请求本身。
        """
        last: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                await action()
                return
            except (TaskCancelled, LoginRequired):
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.max_retries:
                    delay = min(2 ** attempt, 15)
                    log.warn(
                        f"{label} 第 {attempt}/{self.max_retries} 次失败（{exc}），"
                        f"{delay}s 后重试"
                    )
                    await asyncio.sleep(delay)
        raise last or RuntimeError(f"{label} 失败")

    async def _collect_one_keyword(
        self, collector: BaseCollector, ctx: CollectContext, keyword: str,
        buffer: "_Buffer", log: TaskLogger,
    ) -> None:
        filters = ctx.search_filters
        marks = self._marks(ctx)
        work_skipped = 0
        log.info(f"搜索关键字：{keyword}")
        count = 0
        skipped = 0
        duplicated = 0
        keyword_missed = 0
        excluded = 0
        collected_today = 0
        async for work in collector.collect_by_keyword(ctx, keyword):
            # 关键字匹配过滤：作品的标题/描述/标签里必须包含搜索关键字，
            # 否则说明是平台推荐/广告/无关内容，直接丢弃不存储。
            if not _work_matches_keyword(work, keyword, ctx.content_rules):
                keyword_missed += 1
                # ⚠️ 把**前几条被丢掉的标题**打出来。
                # 只报一个总数（"12 条不含关键字被丢弃"）的话，看日志的人
                # 根本想不到原因是"搜的是『盘山风景区』，而笔记标题里
                # 只写了『盘山』"——实测搜「盘山风景区」，20 条笔记里
                # 只有 1 条标题/正文含完整关键字，其余全被这一层丢了，
                # 表现是"采到了却几乎不入库、评论也不采"。
                # 打出标题，用户一眼能看出该去加补充词。
                if keyword_missed <= 5:
                    log.info(
                        f"[内容过滤] 丢弃《{(work.title or '')[:30]}》"
                        f"——标题/正文/标签里没有「{keyword}」。"
                        f"如果这条其实相关，去任务的「内容过滤」里加补充词"
                        f"（比如只写「盘山」），或者直接关掉过滤"
                    )
                elif keyword_missed == 6:
                    log.info("[内容过滤] 后面被丢弃的不再逐条打印，"
                             "结束时会报总数")
                continue

            # 第三道：过滤关键字。命中就丢，一票否决。
            # ⚠️ 位置很关键——必须在"留存判定"**之后**：
            # 一条内容哪怕完美命中附关键字，只要正文里出现了过滤词
            # （代运营、加微信这类），照样丢。放在前面的话语义就变成
            # "先排除再看相关性"，和用户要的顺序不一样。
            hit_word = ctx.content_rules.blocked_by(_work_text(work))
            if hit_word:
                excluded += 1
                # 报**是哪个词**命中的。用户可能配了 200 个过滤词，
                # 只说"被过滤词丢弃"的话，误伤了根本不知道该删哪个，
                # 只能一个个删着试。
                if excluded <= 5:
                    log.info(
                        f"[过滤词] 丢弃《{(work.title or '')[:30]}》"
                        f"——命中过滤关键字「{hit_word}」"
                    )
                elif excluded == 6:
                    log.info("[过滤词] 后面被丢弃的不再逐条打印，结束时会报总数")
                continue

            # 时间窗兜底过滤：接口支不支持筛选，最终落库的都在范围内。
            # 快手的搜索接口压根没有时间参数，全靠这一层。
            if not filters.in_window(work.publish_time):
                skipped += 1
                # 和内容过滤一样：把**前几条**的标题和它的发布时间打出来。
                # 这一层和内容过滤丢的东西长得一模一样（都是"采到了却不入库"），
                # 只报总数的话根本分不清是哪一层丢的。
                # 特别是 publish_time 为 None 的情况——那不是"不在窗口内"，
                # 是**根本没拿到发布时间**，得去查 feed 有没有到货。
                if skipped <= 5:
                    # 注：publish_time 为 None 的**不会**走到这里——
                    # in_window() 对解析不出时间的内容一律放行
                    # （宁可多收一条，也不要因为时间格式没认出来就整片丢）。
                    # 所以能走到这里的，一定是"有时间、而且确实在窗口外"。
                    window = filters.window
                    span = (f"{window[0]:%Y-%m-%d} ~ {window[1]:%Y-%m-%d}"
                            if window else "不限")
                    log.info(
                        f"[时间窗] 跳过《{(work.title or '')[:30]}》——"
                        f"发布于 {work.publish_time}，不在 {span} 内"
                    )
                elif skipped == 6:
                    log.info("[时间窗] 后面被跳过的不再逐条打印，结束时会报总数")
                # 按"最新发布"排的话，翻到比窗口还早就可以停了——
                # 后面只会更早，继续翻纯属浪费请求和风控额度
                if filters.sorted_by_time and filters.older_than_window(work.publish_time):
                    log.info(
                        f"关键字 [{keyword}] 已翻过时间范围下界，提前结束"
                        f"（跳过 {skipped} 条范围外内容）"
                    )
                    break
                continue

            work.source_keyword = keyword
            # ⚠️ 作品水印：命中就**整条跳过**，连评论都不翻。
            # 这是省时间的大头——一条作品的评论在拟人模式下要十几秒。
            # 用 claim_work（SETNX）而不是先查后写：两个任务同时跑同一个
            # 景区时，先查后写会双双认为"没人采过"，然后都去采一遍。
            if marks is not None and not marks.claim_work(
                    collector.channel, work.work_id):
                work_skipped += 1
                if work_skipped <= 5:
                    log.info(f"[水印] 跳过《{(work.title or '')[:24]}》"
                             f"（{work.work_id} 近 3 天采过）")
                elif work_skipped == 6:
                    log.info("[水印] 后面被跳过的作品不再逐条打印，结束时报总数")
                continue
            fresh = await buffer.add_work(work)
            count += 1
            # ⚠️ getattr 而不是 self.quota：TaskRunner 在别处（测试、
            #    以及某些只跑一段流程的入口）会用 __new__ 造出来，
            #    没走完 __init__。配额是闸门，闸门装不上不该让采集停摆。
            quota = getattr(self, "quota", None)
            if quota is not None and fresh and collector.needs_login \
                    and ctx.account_name:
                await quota.record_works(channel, ctx.account_name, 1)
                # 每 10 条复查一次。每条都查等于每条一次 SELECT，
                # 而配额本来就是个粗粒度的闸门，差十条无所谓。
                if count % 10 == 0:
                    verdict = await quota.check(channel, ctx.account_name)
                    if not verdict.ok:
                        log.warn(f"⏸ {verdict.reason}。本轮 {channel} 到此为止")
                        break
            # ⚠️ 每条都说清"它到底怎么了"。以前这里四个分支一声不吭，
            # 只在关键字循环结束时报几个总数，于是用户看到的是
            # "日志里刷了一堆笔记，库里却好像没有" —— 分不清是没入库、
            # 还是入库了但没采评论。作品在**上面那行就已经写进缓冲区**了
            # （四个分支都写），差别只在要不要接着翻评论。
            log_work_detail(log, work, idx=count, total=ctx.max_works,
                            channel=collector.channel)
            if not fresh:
                # 前面某个关键字已经采过这条了。**评论也不用再翻一遍**——
                # 两次命中相隔几秒，评论区不会变，几十个请求纯属白打。
                duplicated += 1
                log_work_footer(log, 0, "前面的关键字采过它，不重复翻评论")
            elif work.work_id in ctx.params.get("_collected_today", ()):
                # 今天已经采过这条了（上一次运行、或者这次的前一轮重试）。
                # 作品本身照样写一遍（点赞/评论数会变，upsert 很便宜），
                # 但不再点开它翻评论——那才是拟人模式下真正贵的部分。
                collected_today += 1
                log_work_footer(log, 0, "今天已经采过它的评论，本轮不再翻")
            elif not ctx.collect_comments:
                log_work_footer(log, 0, "本平台关闭了评论采集")
            else:
                await self._collect_comments_for(collector, ctx, work, buffer)
            # ⚠️ **采一条存一条**：这条作品和它的评论一起落库，再去下一条。
            # 不攒批的理由不是性能，是**别丢数据**：拟人模式一条笔记十几二十秒，
            # 攒到 200 条要一个多小时，中途任务被停掉、进程被杀、机器重启，
            # 这一个多小时就全没了，而日志里明明一条条都采到了。
            # 代价是每条作品多两次 execute_many（作品一次、评论一次），
            # 在这个量级上可以忽略。
            await buffer.flush()
            if collector.limit_reached(count, ctx.max_works):
                log.info(f"关键字 [{keyword}] 已达作品数上限 {ctx.max_works}")
                break
        tail = f"，另有 {skipped} 条不在时间范围内被跳过" if skipped else ""
        if work_skipped:
            tail += f"，{work_skipped} 条命中作品水印被跳过（近 3 天采过）"
        if keyword_missed:
            tail += (f"，{keyword_missed} 条不含关键字「{keyword}」被内容过滤丢弃"
                     f"（丢得多就去景区的「附关键字」里加词，或关掉过滤）")
        if excluded:
            tail += (f"，{excluded} 条命中「过滤关键字」被丢弃"
                     f"（误伤了就去景区的「过滤关键字」里删词）")
        if duplicated:
            tail += f"，{duplicated} 条与前面的关键字重复（未重复入库、也没重复采评论）"
        if collected_today:
            tail += f"，{collected_today} 条今天已经采过（没有重复翻评论）"
        log.info(f"关键字 [{keyword}] 采集到 {count} 条作品{tail}")
        await buffer.flush()

    async def _collect_creator(
        self, collector: BaseCollector, ctx: CollectContext, task: Dict[str, Any],
        channel: str, buffer: "_Buffer", log: TaskLogger,
    ) -> None:
        targets = await self._resolve_targets(task, channel, "creator")
        if not targets:
            log.warn(f"{channel} 没有配置主页采集目标，跳过")
            return

        filters = ctx.search_filters
        for target in targets:
            ctx.raise_if_cancelled()
            label = target.name or target.value
            try:
                await self._retrying(
                    f"主页 [{label}]", log,
                    lambda t=target: self._collect_one_creator(
                        collector, ctx, t, buffer, log
                    ),
                )
            except (TaskCancelled, LoginRequired):
                raise
            except Exception as exc:  # noqa: BLE001
                log.error(f"主页 [{label}] 重试 {self.max_retries} 次仍失败，跳过：{exc}")

    async def _collect_one_creator(
        self, collector: BaseCollector, ctx: CollectContext,
        target: CollectTarget, buffer: "_Buffer", log: TaskLogger,
    ) -> None:
        filters = ctx.search_filters
        log.info(f"采集主页：{target.name or target.value}")
        count = 0
        skipped = 0
        async for work in collector.collect_by_creator(ctx, target):
            # 主页作品接口天然按时间倒序，所以越过窗口下界就能直接停
            if not filters.in_window(work.publish_time):
                skipped += 1
                # 和内容过滤一样：把**前几条**的标题和它的发布时间打出来。
                # 这一层和内容过滤丢的东西长得一模一样（都是"采到了却不入库"），
                # 只报总数的话根本分不清是哪一层丢的。
                # 特别是 publish_time 为 None 的情况——那不是"不在窗口内"，
                # 是**根本没拿到发布时间**，得去查 feed 有没有到货。
                if skipped <= 5:
                    # 注：publish_time 为 None 的**不会**走到这里——
                    # in_window() 对解析不出时间的内容一律放行
                    # （宁可多收一条，也不要因为时间格式没认出来就整片丢）。
                    # 所以能走到这里的，一定是"有时间、而且确实在窗口外"。
                    window = filters.window
                    span = (f"{window[0]:%Y-%m-%d} ~ {window[1]:%Y-%m-%d}"
                            if window else "不限")
                    log.info(
                        f"[时间窗] 跳过《{(work.title or '')[:30]}》——"
                        f"发布于 {work.publish_time}，不在 {span} 内"
                    )
                elif skipped == 6:
                    log.info("[时间窗] 后面被跳过的不再逐条打印，结束时会报总数")
                if filters.older_than_window(work.publish_time):
                    log.info("已翻过时间范围下界，该主页提前结束")
                    break
                continue
            fresh = await buffer.add_work(work)
            count += 1
            log_work_detail(log, work, idx=count, total=ctx.max_works,
                            channel=collector.channel)
            # 主页目标和关键字可能采到同一条作品，同样不重复入库/不重复采评论
            if fresh and ctx.collect_comments:
                await self._collect_comments_for(collector, ctx, work, buffer)
            # 采一条存一条，理由同关键字那条路
            await buffer.flush()
            if collector.limit_reached(count, ctx.max_works):
                break
        author = await collector.fetch_author(ctx, target.value)
        if author is not None:
            await buffer.add_author(author)
        tail = f"，另有 {skipped} 条不在时间范围内被跳过" if skipped else ""
        log.info(f"主页 [{target.name or target.value}] 采集到 {count} 条作品{tail}")
        await buffer.flush()

    async def _collect_poi(
        self, collector: BaseCollector, ctx: CollectContext, task: Dict[str, Any],
        channel: str, buffer: "_Buffer", log: TaskLogger,
    ) -> None:
        targets = await self._resolve_targets(task, channel, "poi")
        if not targets:
            log.warn(
                f"{channel} 没有配置 POI（携程需要 POI_ID，同程需要 sid），"
                f"请在景区管理里给该景区添加对应平台的采集目标"
            )
            return

        for target in targets:
            ctx.raise_if_cancelled()
            label = target.name or target.value
            try:
                await self._retrying(
                    f"POI [{label}]", log,
                    lambda t=target: self._collect_one_poi(
                        collector, ctx, t, buffer, log
                    ),
                )
            except (TaskCancelled, LoginRequired):
                raise
            except Exception as exc:  # noqa: BLE001
                log.error(f"POI [{label}] 重试 {self.max_retries} 次仍失败，跳过：{exc}")

    async def _collect_one_poi(
        self, collector: BaseCollector, ctx: CollectContext,
        target: CollectTarget, buffer: "_Buffer", log: TaskLogger,
    ) -> None:
        label = target.name or target.value
        log.info(f"采集景区点评：{label}（{target.value}）")

        # 合成作品：让携程/同程的数据也能挂在"作品→评论"这棵树上
        if hasattr(collector, "poi_work"):
            await buffer.add_work(collector.poi_work(ctx, target))

        count = 0
        async for comment in collector.collect_by_poi(ctx, target):
            await buffer.add_comment(comment)
            count += 1
        log.info(f"POI [{label}] 采集到 {count} 条点评")
        await buffer.flush()

    async def _collect_comments_for(
        self, collector: BaseCollector, ctx: CollectContext, work: WorkItem, buffer: "_Buffer",
    ) -> None:
        """采一条作品的评论。**单条失败不能把整个平台带走。**

        ⚠️ 这里的 try 是有来历的：评论区关掉、作品被删、作者设了权限，
        平台回的都是普通的接口错误（快手是 result != 1）。
        原来这个错会一路冒到 _run_channel 的 except，
        于是"一条作品评论区关了" == "这个平台剩下的关键字全不采了"。
        取消是例外，必须继续往上抛，否则用户点了停止会停不下来。
        """
        count = 0
        # ⚠️ 先攒着，等这条作品的评论**全部到齐**再排序打印。
        # 不能边到边打：回复是后到的（要滚到那儿、点开「查看更多回复」才拉），
        # 按到货顺序打出来就是"七条一级评论 + 三条不知道挂谁下面的二级评论"。
        # 攒的只是**打印用的引用**，入库还是来一条 add 一条，
        # 中途被取消也不会丢数据。
        printable: List[CommentItem] = []
        # ⚠️ **无论怎么退出，方框都必须闭合**。
        #
        # 上一版把"排序后逐条打印 + 小计"放在函数末尾，于是只要不是正常跑完
        # ——被取消、登录态失效、单条异常 return——就一行评论都不打，
        # 方框永远开着。实跑现场：作品 4/200 的框在 17:55:24 打开，
        # 库里进了 16 条评论，日志里一条明细都没有，框也没闭。
        # 用户看到的是"日志格式没保持"。
        # try/finally 保证它**正好执行一次**，任何出口都一样。
        try:
            try:
                async for comment in collector.collect_comments(ctx, work):
                    fresh = await buffer.add_comment(comment)
                    count += 1
                    # 只打新的：重复的那些是多关键字命中同一条作品带来的，
                    # 再打一遍纯属刷屏。
                    if fresh:
                        printable.append(comment)
            finally:
                self._log_comments(ctx.logger, printable, count)
        except TaskCancelled:
            raise
        except LoginRequired:
            # 登录态失效是平台级问题，再采下去每条都会失败，让它往上冒
            raise
        except Exception as exc:  # noqa: BLE001
            # 浏览器整个没了 = 平台级故障，不能按"这一条失败"处理。
            # 实跑里服务被 Ctrl+C 之后，采集器又往下跑了九条，
            # 每条都开方框、报 0 条评论、写库——噪音盖住真正的原因，
            # 还往库里写了九条假的"没有评论"。
            gone = getattr(collector, "browser_gone", None)
            if callable(gone) and gone(exc):
                raise RuntimeError(
                    f"浏览器/页面已经关闭（{exc}），停止这个平台的采集。"
                    f"服务被停掉、或者浏览器崩了都会这样；"
                    f"继续跑只会一条条报假的「没有评论」"
                ) from exc
            streak = ctx.params.get("_comment_fail_streak", 0) + 1
            ctx.params["_comment_fail_streak"] = streak
            ctx.log(
                f"作品 {work.work_id} 的评论采集失败（{exc}），"
                f"跳过这条继续采下一条", "warn",
            )
            # ⚠️ 连续失败要停下来，别一路"跳过"到底。
            # 真实发生过：拟人模式下评论走错了路（去调没建的接口客户端），
            # 于是 61 条作品在同一秒里全部"跳过这条继续采下一条"——
            # 任务最后还报成功，日志里只有一屏看着像单条异常的告警。
            # 单条失败是常态（作品被删、评论区关了）；**连续**失败是系统性故障。
            if streak >= COMMENT_FAIL_STREAK_LIMIT:
                raise RuntimeError(
                    f"连续 {streak} 条作品的评论都采集失败，判定为系统性故障，"
                    f"停止这个平台的采集。最后一条错误：{exc}"
                ) from exc
            return
        # 成功一条就清零：零散失败不该慢慢累积到阈值
        ctx.params["_comment_fail_streak"] = 0

    @staticmethod
    def _log_comments(log: TaskLogger, comments: List[CommentItem],
                      total: int) -> None:
        """把一条作品的评论排好序打出来，最后收个尾。"""
        ordered = sort_comments_for_log(comments)
        for no, comment in enumerate(ordered[:COMMENT_LOG_LIMIT], start=1):
            log_comment_detail(log, comment, no)
        if len(ordered) > COMMENT_LOG_LIMIT:
            log.info(f"  │      …… 还有 {len(ordered) - COMMENT_LOG_LIMIT} 条不再逐条打印")
        log_work_footer(log, total, "这条作品没有评论" if not total else "")

    async def _resolve_targets(
        self, task: Dict[str, Any], channel: str, target_type: str
    ) -> List[CollectTarget]:
        """目标优先取任务里显式指定的；没指定就用景区下配置的该平台目标。"""
        explicit = [
            t for t in (task.get("targets") or [])
            if isinstance(t, dict) and t.get("channel", channel) == channel
            and t.get("target_type", target_type) == target_type
        ]
        if explicit:
            return [
                CollectTarget(
                    target_type=target_type,
                    value=str(t["target_id"]),
                    name=t.get("target_name", ""),
                    url=t.get("target_url", ""),
                    extra=t.get("extra") or {},
                )
                for t in explicit
            ]

        scenic_id = task.get("scenic_id")
        if not scenic_id:
            return []
        rows = await self.scenics.list_targets(
            scenic_id, channel=channel, target_type=target_type, enabled_only=True
        )
        return [
            CollectTarget(
                target_type=target_type,
                value=str(r["target_id"]),
                name=r.get("target_name") or "",
                url=r.get("target_url") or "",
                extra=r.get("extra") or {},
            )
            for r in rows
        ]


class _Buffer:
    """攒批写库：减少往返，同时保证任务中途取消时已采数据不丢。

    ⚠️ 同时负责**本次任务内的去重**。一个景区通常配好几个关键字
    （"天山天池""天池风景区""天山天池景区"…），它们命中同一条作品是常态——
    实测重合率能到三成。不去重的话：
      - 同一条作品被反复 upsert，写库量翻几倍
      - 更要命的是**它的评论会被重新翻一遍**，几十上百个请求白打，
        还平白多消耗风控额度
    去重的粒度和数据库唯一键一致（work_uk / comment_uk），
    所以"重复"的定义在内存里和库里是同一个，不会出现两边判断不一致。
    """

    def __init__(self, data_repo: DataRepository, log: TaskLogger,
                 *, refresh_existing_comments: bool = False):
        self.data = data_repo
        self.log = log
        self.refresh_existing_comments = refresh_existing_comments
        self.works: List[WorkItem] = []
        self.comments: List[CommentItem] = []
        self.authors: List[Any] = []
        #: 上次写库的时刻，用来判断"攒得够久了没"
        self._last_flush = time.monotonic()
        #: 本次任务已经处理过的 work_uk / comment_uk
        self.seen_works: set = set()
        self.seen_comments: set = set()
        #: 因为重复而跳过的条数，跑完在日志里报出来
        self.dup_works = 0
        self.dup_comments = 0
        self.stats = {
            "new_works": 0, "updated_works": 0,
            "new_comments": 0, "updated_comments": 0,
        }

    async def add_work(self, work: WorkItem) -> bool:
        """返回 False 表示这条作品本次任务里已经收过了，没有再写一遍。"""
        item = work.finalize()
        key = work_uk(item.scenic_id, item.channel, item.work_id)
        if key in self.seen_works:
            self.dup_works += 1
            return False
        self.seen_works.add(key)
        self.works.append(item)
        await self._flush_if_due()
        return True

    async def add_comment(self, comment: CommentItem) -> bool:
        """返回 False 表示这条评论本次任务里已经收过了。"""
        item = comment.finalize()
        key = comment_uk(item.scenic_id, item.channel, item.work_id, item.comment_id)
        if key in self.seen_comments:
            self.dup_comments += 1
            return False
        self.seen_comments.add(key)
        self.comments.append(item)
        await self._flush_if_due()
        return True

    async def _flush_if_due(self) -> None:
        """攒够了、或者攒得够久了，就写一批。"""
        pending = len(self.works) + len(self.comments)
        if not pending:
            return
        if (pending >= FLUSH_THRESHOLD
                or time.monotonic() - self._last_flush >= FLUSH_MAX_SECONDS):
            await self.flush()

    def dedup_summary(self) -> str:
        parts = []
        if self.dup_works:
            parts.append(f"作品 {self.dup_works} 条")
        if self.dup_comments:
            parts.append(f"评论 {self.dup_comments} 条")
        return "，".join(parts)

    async def add_author(self, author) -> None:
        self.authors.append(author.finalize())

    async def _submit_for_labeling(self, comments: List[CommentItem]) -> None:
        """把刚落库的评论推给标注引擎（边采边标）。

        ⚠️ **绝不能把采集带崩**。标注挂了最多是这批评论没标签，
        下次「一键补标」还能补回来；而采集因为标注挂掉，丢的是数据本身，
        那是补不回来的。所以这里吞掉一切异常。

        没开开关、引擎不可用时是**零成本**的：manager 里第一句就返回。
        """
        if not comments:
            return
        try:
            from ..labeling import get_manager

            manager = get_manager()
            if not manager.enabled:
                return
            rows = [{
                "channel": c.channel, "work_id": c.work_id,
                "scenic_id": c.scenic_id, "scenic_name": c.scenic_name,
                "comment_id": c.comment_id, "content": c.content or "",
                "likes": c.likes, "publish_time": c.publish_time,
                "commenter_name": c.commenter_name,
                "extra_content": c.extra_content,
            } for c in comments if c.comment_id and c.content]
            accepted = await manager.submit_comments(rows)
            if accepted:
                self.log.info(f"[标注] 已推送 {accepted} 条评论进标注队列")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[标注] 推送失败（不影响采集）：%s", exc)

    async def flush(self) -> None:
        """真正写库。每写一批都报**平台 / 条数 / 表名 / 具体 id**。

        以前这里一声不吭，"到底有没有入库"只能去翻数据库。
        现在日志里能直接看到写进了哪张表、写了哪几条。
        """
        self._last_flush = time.monotonic()
        if self.works:
            channels = sorted({w.channel for w in self.works})
            ids = [w.work_id for w in self.works]
            new, updated = await self.data.save_works(self.works)
            self.log.info(
                f"[存储] {'/'.join(channels)} 作品 {len(ids)} 条 → "
                f"{tables.WORKS}（新增 {new}，更新 {updated}）："
                f"{'、'.join(ids[:5])}{'…' if len(ids) > 5 else ''}"
            )
            self.stats["new_works"] += new
            self.stats["updated_works"] += updated
            self.works.clear()
        if self.comments:
            channels = sorted({c.channel for c in self.comments})
            by_work: Dict[str, int] = {}
            for c in self.comments:
                by_work[c.work_id] = by_work.get(c.work_id, 0) + 1
            total = len(self.comments)
            new, updated = await self.data.save_comments(
                self.comments, skip_existing=not self.refresh_existing_comments
            )
            detail = "、".join(f"{wid}×{n}" for wid, n in list(by_work.items())[:5])
            self.log.info(
                f"[存储] {'/'.join(channels)} 评论 {total} 条 → "
                f"{tables.COMMENTS}（新增 {new}，已有 {updated}）："
                f"{detail}{'…' if len(by_work) > 5 else ''}"
            )
            self.stats["new_comments"] += new
            self.stats["updated_comments"] += updated
            # ⚠️ 边采边标：**落库之后**才推给标注引擎。
            # 顺序反了就会出现"引擎按主键 UPDATE 时那一行还不存在"，
            # 表现是「影响 0 行」的一条 WARNING，数据静默丢标签。
            await self._submit_for_labeling(self.comments)
            self.comments.clear()
        if self.authors:
            count = len(self.authors)
            await self.data.save_authors(self.authors)
            self.log.info(f"[存储] 作者 {count} 条 → {tables.AUTHORS}")
            self.authors.clear()
        # 刚写完数据，概览的缓存就不准了
        self.data.invalidate_overview_cache()
