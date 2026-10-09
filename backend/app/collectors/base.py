"""采集器基类与统一数据模型。

所有平台采集器都产出同样的 WorkItem / CommentItem / AuthorItem，
入库层因此完全不需要认识具体平台。平台差异只体现在三件事：
  1. 怎么翻页拿到列表
  2. 怎么把原始 JSON 映射成统一字段（各平台自己实现 _to_work / _to_comment）
  3. 要不要登录态（needs_login）

采集方法一律是异步生成器，好处是：
  - 边采边写库，长任务不会把结果堆在内存里
  - 取消信号能在任意一条数据之间生效，不用等整页跑完
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.config import Config
from ..core.constants import CHANNEL_LABELS, comment_level
from ..core.logging import get_logger
from ..proxy.manager import ProxiedClient, ProxyManager
from ..utils import normalize as nz

logger = get_logger(__name__)


# ===================================================================
# 统一数据模型
# ===================================================================

@dataclass
class WorkItem:
    """社交媒体作品（作品/帖子/笔记）。字段与 src_opinion_social_work_di 表一一对应。"""
    channel: str
    work_id: str
    scenic_id: str = ""
    scenic_name: str = ""
    work_url: str = ""
    author_id: str = ""
    author_name: str = ""
    title: Optional[str] = None
    description: Optional[str] = None
    label: Optional[str] = None
    image_list: Optional[str] = None
    video_list: Optional[str] = None
    likes: int = 0
    collection_cnt: int = 0
    comment_cnt: int = 0
    shares: int = 0
    location: str = ""
    publish_time: Optional[datetime] = None
    crawl_time: Optional[datetime] = None
    extra_content: Optional[str] = None
    source_keyword: str = ""
    task_id: str = ""

    def finalize(self) -> "WorkItem":
        """入库前统一截断与补默认值，避免 Data too long 打断整批写入。"""
        self.work_id = nz.truncate(self.work_id, 600)
        self.work_url = nz.truncate(self.work_url, 600)
        self.author_id = nz.truncate(self.author_id, 600)
        self.author_name = nz.truncate(self.author_name, 600)
        self.scenic_id = nz.truncate(self.scenic_id, 100)
        self.scenic_name = nz.truncate(self.scenic_name, 100)
        self.location = nz.truncate(nz.clean_location(self.location), 50)
        self.source_keyword = nz.truncate(self.source_keyword, 200)
        self.task_id = nz.truncate(self.task_id, 64)
        self.crawl_time = self.crawl_time or datetime.now()
        return self


@dataclass
class CommentItem:
    """社交媒体评论。字段与 src_opinion_social_work_comment_di 表一一对应。

    AI 标注字段（sentiment_* / *_tags）本期不写值，
    表里已建好列，后续接入标注服务时无需改表。
    """
    channel: str
    comment_id: str
    work_id: str = ""
    scenic_id: str = ""
    scenic_name: str = ""
    comment_level: str = "level_1"
    comment_parent_id: str = ""
    root_comment_id: str = ""
    commenter_id: str = ""
    commenter_name: str = ""
    image_list: Optional[str] = None
    video_list: Optional[str] = None
    location: str = ""
    content: Optional[str] = None
    likes: int = 0
    sub_comment_count: int = 0
    extra_content: Optional[str] = None
    publish_time: Optional[datetime] = None
    crawl_time: Optional[datetime] = None
    task_id: str = ""

    def finalize(self) -> "CommentItem":
        self.comment_id = nz.truncate(self.comment_id, 300)
        self.comment_parent_id = nz.truncate(self.comment_parent_id, 600)
        self.root_comment_id = nz.truncate(self.root_comment_id or self.comment_id, 300)
        self.work_id = nz.truncate(self.work_id, 600)
        self.commenter_id = nz.truncate(self.commenter_id, 600)
        self.commenter_name = nz.truncate(self.commenter_name, 600)
        self.scenic_id = nz.truncate(self.scenic_id, 100)
        self.scenic_name = nz.truncate(self.scenic_name, 100)
        self.location = nz.truncate(nz.clean_location(self.location), 50)
        self.task_id = nz.truncate(self.task_id, 64)
        self.crawl_time = self.crawl_time or datetime.now()
        return self


@dataclass
class AuthorItem:
    """创作者，数据中心「作品→创作者」维度展示用。"""
    channel: str
    author_id: str
    author_name: str = ""
    avatar: str = ""
    signature: Optional[str] = None
    gender: str = ""
    location: str = ""
    home_url: str = ""
    fans_count: int = 0
    follow_count: int = 0
    works_count: int = 0
    liked_count: int = 0
    extra_content: Optional[str] = None
    crawl_time: Optional[datetime] = None

    def finalize(self) -> "AuthorItem":
        self.author_id = nz.truncate(self.author_id, 600)
        self.author_name = nz.truncate(self.author_name, 600)
        self.avatar = nz.truncate(self.avatar, 600)
        self.home_url = nz.truncate(self.home_url, 600)
        self.gender = nz.truncate(self.gender, 20)
        self.location = nz.truncate(nz.clean_location(self.location), 50)
        self.crawl_time = self.crawl_time or datetime.now()
        return self


@dataclass
class CollectTarget:
    """一个采集目标：关键字、POI 或作者主页。"""
    target_type: str          # keyword | poi | creator | detail
    value: str                # 关键字文本 / POI_ID / sid / 作者ID / 作品链接
    name: str = ""
    url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectContext:
    """一次采集运行的上下文：景区归属、任务归属、限额、取消信号。"""
    scenic_id: str
    scenic_name: str
    task_id: str = ""
    max_works: int = 100
    max_comments_per_work: int = 500
    max_comment_level: int = 3
    enable_sub_comments: bool = True
    collect_comments: bool = True
    #: 单个关键字最多跑多少秒，0 = 不限。到点就换下一个关键字——
    #: 否则一个关键字被卡住会把整条任务的时间吃光，后面的关键字一个都轮不上。
    #: 来自 crawl.keyword_budget_minutes。
    keyword_budget_seconds: float = 0.0
    account_name: str = ""
    #: 采集方式，建任务时选：
    #:   api    = 只走接口。失败就报错，不换路（想尽快知道接口坏没坏时用）
    #:   hybrid = 先走接口，接口被风控/拿不到数据再自动换成拟人（默认）
    #:   human  = 直接拟人：开浏览器模拟真人搜索、点作品、翻评论
    collect_engine: str = "hybrid"
    #: 排序与时间范围筛选（由 runner 按平台解析好传进来）
    filters: Any = None
    #: 内容过滤规则（ContentFilter）。决定搜到的作品留不留、要不要为它采评论。
    #: 由 runner 从任务 params 解析好传进来；为 None 时按默认规则
    #: （开启 + 只按搜索关键字），也就是改造前的行为。
    content_filter: Any = None
    params: Dict[str, Any] = field(default_factory=dict)
    cancel_event: Optional[asyncio.Event] = None
    logger: Any = logger

    def cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise TaskCancelled("任务已被取消")

    def log(self, message: str, level: str = "info") -> None:
        getattr(self.logger, level, self.logger.info)(message)

    @property
    def content_rules(self):
        """永远返回一个可用的内容过滤对象，省得每个调用点都判空。"""
        if self.content_filter is None:
            from ..core.content_filter import ContentFilter
            self.content_filter = ContentFilter()
        return self.content_filter

    @property
    def search_filters(self):
        """永远返回一个可用的筛选对象，省得每个采集器都判空。"""
        if self.filters is None:
            from ..core.search_filters import SearchFilters
            self.filters = SearchFilters()
        return self.filters


@asynccontextmanager
async def browser_window(lease, note: str = ""):
    """临时开一下浏览器这段时间里，先把账号占住。

    lease 为 None（单测、或调度层没给租约）时什么都不做，直接放行——
    采集器不该因为"没有租约"就跑不起来。
    """
    if lease is None:
        yield ""
        return
    async with lease.temporary(note) as account_name:
        yield account_name


class TaskCancelled(Exception):
    """用户主动取消任务。"""


class LoginRequired(Exception):
    """需要有效登录态但没有可用账号。"""


# ===================================================================
# 采集器基类
# ===================================================================

class BaseCollector(ABC):
    """所有平台采集器的公共父类。

    子类必须声明 channel，并实现自己支持的采集方式；
    不支持的方式保持基类默认（产出空序列），调度层会跳过。
    """

    channel: str = ""
    #: 该平台是否有"作品"概念。携程/同程只有点评，为 False
    supports_works: bool = True
    #: 是否需要登录态
    needs_login: bool = False
    #: 采集逻辑是否已经接入。False 表示只有骨架，调度层会跳过并明确告知
    integrated: bool = True
    #: 支持哪些采集方式
    supported_targets: tuple = ("keyword",)

    def __init__(self, config: Config, proxy_manager: ProxyManager):
        self.config = config
        self.proxy_manager = proxy_manager
        self.platform_config = config.platform(self.channel)

    # ---------------- 生命周期 ----------------
    async def prepare(self, ctx: CollectContext) -> None:
        """采集开始前的准备：取登录态、初始化客户端等。默认什么也不做。"""

    async def cleanup(self) -> None:
        """采集结束后释放资源。默认什么也不做。"""

    #: 采集器有没有**真的向平台确认过**登录态（不是靠 Cookie key 猜的）。
    #: 决定"第一页就空"该报错还是该放过，见 handle_empty_first_page。
    login_verified: bool = False

    def handle_empty_first_page(
        self, ctx: "CollectContext", keyword: str, data: Any,
        *, clues: Optional[List[str]] = None,
    ) -> None:
        """搜索第一页就空——该报错，还是该当成"这个词真没内容"？

        分水岭是**登录态有没有被真正验证过**（不是"看着像登录了"）：

        - 验证过（比如微博问过 /api/config）→ 静默失败的可能性基本排除，
          剩下最可能的就是这个词确实没内容。记一条警告，继续下一个关键字。
          「宝珠洞索道」这种小众词就是这样，当成失败的话会白白重试三轮，
          日志里还是一大段吓人的红字。
        - 没验证过 → 有可能是拿着游客 Cookie 在空跑，必须报错，
          否则就是一句「采集到 0 条作品」，用户完全无从判断哪里出了问题。
        """
        if self.login_verified:
            label = CHANNEL_LABELS.get(self.channel, self.channel)
            ctx.log(
                f"[{label}] 关键字 [{keyword}] 没有搜到内容。"
                f"登录态自检是通过的，所以基本可以排除没登录——"
                f"多半这个词确实没有相关微博，或者内容都不在所选时间范围内。",
                "warn",
            )
            return
        raise self.empty_first_page(keyword, data, clues=clues)

    def empty_first_page(
        self, keyword: str, data: Any, *, clues: Optional[List[str]] = None
    ) -> RuntimeError:
        """搜索第一页就没结果时用的报错。

        为什么要报错而不是安静结束：接口没报错、只是结果为空，这种「静默失败」
        在日志里长这样——

            关键字 [天山天池] 第 1 页无数据，结束
            关键字 [天山天池] 采集到 0 条作品
            任务完成；作品 新增 0 / 更新 0

        用户完全无法判断是这个词真没内容、还是被风控了、还是少传了参数。
        第一页就空基本不可能是"采完了"，所以按失败处理，并把线索都摆出来。
        """
        label = CHANNEL_LABELS.get(self.channel, self.channel)
        detail = ""
        if isinstance(data, dict):
            detail = f"接口返回的顶层字段：{sorted(data.keys())[:12]}。"
        reason = "；".join(clues) if clues else "参数看起来是齐的，多半是当前出口 IP 被限流"
        return RuntimeError(
            f"[{label}] 关键字 [{keyword}] 第 1 页就没有结果。{detail}"
            f"可能原因：{reason}。"
            f"建议：换一个代理 IP、降低采集频率，或重新导入一份新鲜的 Cookie；"
            f"也可以先用浏览器搜一下这个词，确认确实有内容。"
        )

    def cookie_clues(self, cookie_header: str) -> List[str]:
        """Cookie 层面的可疑点，拼进上面的报错里。"""
        count = len([p for p in (cookie_header or "").split(";") if p.strip()])
        clues = []
        if count == 0:
            clues.append("完全没有 Cookie")
        elif count < 20:
            clues.append(f"Cookie 只有 {count} 个，登录态可能不完整")
        return clues

    def make_client(self, **kwargs) -> ProxiedClient:
        """拿一个带代理轮换的 HTTP 客户端——所有平台走同一套代理策略。"""
        return ProxiedClient(self.config, self.proxy_manager, self.channel, **kwargs)

    # ---------------- 采集入口 ----------------
    async def collect_by_keyword(
        self, ctx: CollectContext, keyword: str
    ) -> AsyncIterator[WorkItem]:
        """按关键字搜索作品。不支持的平台不实现。"""
        return
        yield  # pragma: no cover - 让函数成为异步生成器

    async def collect_by_creator(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[WorkItem]:
        """采集指定用户主页的全部作品。不支持的平台不实现。"""
        return
        yield  # pragma: no cover

    async def collect_by_poi(
        self, ctx: CollectContext, target: CollectTarget
    ) -> AsyncIterator[CommentItem]:
        """按景区 POI 采集点评（携程 POI_ID / 同程 sid）。"""
        return
        yield  # pragma: no cover

    async def collect_comments(
        self, ctx: CollectContext, work: WorkItem
    ) -> AsyncIterator[CommentItem]:
        """采集某个作品下的评论，含多级子评论。"""
        return
        yield  # pragma: no cover

    async def fetch_author(
        self, ctx: CollectContext, author_id: str
    ) -> Optional[AuthorItem]:
        """拿创作者详情。拿不到返回 None，不影响主流程。"""
        return None

    # ---------------- 给子类用的小工具 ----------------
    @staticmethod
    def poi_homepage(target: CollectTarget, fallback: str) -> str:
        """合成作品的 work_url：采集目标里**手工指定了景区主页就用它**，
        没指定才用按 POI ID 拼出来的默认链接。

        携程/同程/去哪儿同一个景区常有好几个页面（门票页、攻略页、点评页），
        按 ID 拼出来的那个未必是使用方要的那个，所以以人填的为准。
        """
        return (target.url or "").strip() or fallback

    def new_work(self, ctx: CollectContext, work_id: str, **kwargs) -> WorkItem:
        return WorkItem(
            channel=self.channel,
            work_id=str(work_id),
            scenic_id=ctx.scenic_id,
            scenic_name=ctx.scenic_name,
            task_id=ctx.task_id,
            **kwargs,
        )

    def new_comment(
        self, ctx: CollectContext, comment_id: str, work_id: str, depth: int = 1, **kwargs
    ) -> CommentItem:
        return CommentItem(
            channel=self.channel,
            comment_id=str(comment_id),
            work_id=str(work_id),
            scenic_id=ctx.scenic_id,
            scenic_name=ctx.scenic_name,
            comment_level=comment_level(depth),
            task_id=ctx.task_id,
            **kwargs,
        )

    # ---------------- 采集方式（api / hybrid / human）----------------
    #: 改名前存进库的旧值，读到就映射过来（auto=混合，browser=拟人）
    ENGINE_ALIASES = {"auto": "hybrid", "browser": "human"}

    @classmethod
    def engine_of(cls, ctx: "CollectContext") -> str:
        """这次任务选的采集方式。字段缺失或值不认识都按 hybrid 处理。"""
        value = (getattr(ctx, "collect_engine", "") or "").strip().lower()
        value = cls.ENGINE_ALIASES.get(value, value)
        return value if value in ("api", "hybrid", "human") else "hybrid"

    def human_only(self, ctx: "CollectContext") -> bool:
        """用户是不是明确要求"只用拟人"——那就别浪费一轮必然被拦的接口请求。"""
        return self.engine_of(ctx) == "human"

    def may_switch_to_human(self, ctx: "CollectContext") -> bool:
        """接口出问题时允不允许自动换成拟人。

        选了「仅接口」的人要的就是"接口坏了立刻报错"，
        偷偷换条路把数据采回来反而掩盖了问题。
        """
        return self.engine_of(ctx) != "api"

    def needs_browser_for_api(self, ctx: "CollectContext") -> bool:
        """接口采集本身需不需要一个常驻浏览器。

        抖音/快手的签名要调页面里的 JS，所以接口模式也得有页面；
        小红书/微博/携程/同程纯算签名或只要 Cookie，不需要。
        子类按需覆盖——默认按"需要"处理是危险的（会白开浏览器），
        所以默认返回 False，需要的平台自己声明。
        """
        return False

    def limit_reached(self, count: int, limit: int) -> bool:
        return limit > 0 and count >= limit

    def safe_map(self, ctx: "CollectContext", fn, *args, what: str = "作品", **kwargs):
        """把一条原始数据映射成 WorkItem / CommentItem，**出错就跳过这一条**。

        ⚠️ 这一层不是"防御性编程"的客套话，是踩出来的：
        微博有一条作品的 `pics` 元素是字符串而不是字典，
        字段映射里一句 `p.get(...)` 抛了 AttributeError，
        这个异常从生成器里冒出去，直接把**整个平台的采集**结束掉了——
        日志上就一行「weibo 采集失败：'str' object has no attribute 'get'」，
        看不出是某一条微博的图片格式不一样，也看不出前面已经采到的还在不在。

        平台随时会给出没见过的字段形状。一条解析不了就跳一条，
        绝不能让它决定整轮任务的命运。返回 None，调用方本来就要判空。
        """
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 解析%s失败，跳过这一条：%s", self.channel, what, exc)
            logger.debug("解析失败的原始数据：%s", str(args[1:])[:500], exc_info=True)
            ctx.log(f"[{self.channel}] 有 1 条{what}的字段格式不认识，已跳过（{exc}）", "warn")
            return None


class CollectorRegistry:
    """采集器注册表：按 channel 取实现类。"""

    _registry: Dict[str, type] = {}

    @classmethod
    def register(cls, collector_cls: type) -> type:
        channel = getattr(collector_cls, "channel", "")
        if not channel:
            raise ValueError(f"{collector_cls.__name__} 未声明 channel")
        cls._registry[channel] = collector_cls
        return collector_cls

    @classmethod
    def get(cls, channel: str) -> type:
        if channel not in cls._registry:
            raise KeyError(
                f"没有注册 {channel} 的采集器，已注册：{sorted(cls._registry)}"
            )
        return cls._registry[channel]

    @classmethod
    def create(cls, channel: str, config: Config, proxy_manager: ProxyManager) -> BaseCollector:
        return cls.get(channel)(config, proxy_manager)

    @classmethod
    def available(cls) -> List[str]:
        return sorted(cls._registry)
