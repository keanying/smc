"""采集数量的解析：把任务上的 params 变成某个平台实际生效的上限。

为什么要按平台分开设：
    抖音 / 快手 / 小红书 / 微博 有「作品」和「作品下的评论」两层，
    需要分别设「采多少条作品」和「每条作品采多少评论」；
    携程 / 同程 没有作品，只有景区点评一层，
    对它们说「每个作品采 500 条评论」是别扭的，用户想设的是「这个景区一共采多少条点评」。

所以任务的 params 长这样：

    {
      // 全局默认，没配平台级时用它（也兼容改造之前建的老任务）
      "max_works": 100,
      "max_comments_per_work": 500,
      "max_comment_level": 3,
      "enable_sub_comments": true,
      "collect_comments": true,

      // 平台级覆盖，只写想改的键即可
      "channel_params": {
        "douyin":    {"max_works": 50,  "max_comments_per_work": 200},
        "weibo":     {"max_works": 100, "collect_comments": false},
        "ctrip":     {"max_comments": 1000},
        "tongcheng": {"max_comments": 500}
      }
    }

优先级：channel_params[平台] > params 顶层 > config.yaml 的 crawl.* 默认值。
0 一律表示「不限制」，和采集器里的 limit_reached 语义保持一致。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .constants import (
    ALL_CHANNELS, CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_XHS,
    CHANNELS_POI_BASED, CHANNELS_WITH_WORKS,
)

#: 搜索筛选项，作品型平台都能设（具体每项服务端支不支持见 search_filters）
FILTER_KEYS = ("sort", "publish_within", "start_date", "end_date")

#: 采集方式三选一：
#:   api    只走接口。快，但接口一被风控就没数据，且不自动换路。
#:   hybrid 先走接口，接口拿不到数据（被风控/返回空）再换成拟人。默认。
#:   human  直接拟人：开浏览器模拟真人搜索、点开作品、翻评论。
#: 只有抖音/快手/小红书写了拟人采集器，其它平台恒为 api。
COLLECT_ENGINES = ("api", "hybrid", "human")
DEFAULT_COLLECT_ENGINE = "hybrid"

#: 改名前的旧值（库里已有的任务还存着这些），读的时候映射过来。
#: 不做这层映射的话，老任务会被 _as_enum 判成非法值而回落到默认，
#: 用户明明选过"仅浏览器"却被悄悄改成混合。
LEGACY_ENGINE_ALIASES = {"auto": "hybrid", "browser": "human", "api": "api"}
#: 真正能选「拟人」的平台——前端据此决定要不要显示这个选择器，
#: 免得在微博上给出一个选了也没用的选项。
SUPPORTS_BROWSER_ENGINE = (CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_XHS)

#: 作品型平台可以设的键
WORK_CHANNEL_KEYS = (
    "max_works", "max_comments_per_work", "max_comment_level",
    "enable_sub_comments", "collect_comments", "collect_engine",
    *FILTER_KEYS,
)
#: 点评型平台可以设的键（max_comments 是这一层的总条数）
POI_CHANNEL_KEYS = (
    "max_comments", "max_pages", "max_comment_level", "enable_sub_comments",
)
#: 点评型平台没有关键字搜索，所以没有排序/时间筛选项

#: 每个键的类型，用于校验与归一
_INT_KEYS = {
    "max_works", "max_comments_per_work", "max_comments",
    "max_comment_level", "max_pages",
}
_BOOL_KEYS = {"enable_sub_comments", "collect_comments"}
#: 取值受限的键：不在集合里的一律回落到默认值，而不是原样存进 DB
_ENUM_KEYS = {"collect_engine": (COLLECT_ENGINES, DEFAULT_COLLECT_ENGINE)}


def _as_enum(value, key: str) -> str:
    allowed, default = _ENUM_KEYS[key]
    text = str(value or "").strip().lower()
    if key == "collect_engine":
        text = LEGACY_ENGINE_ALIASES.get(text, text)
    return text if text in allowed else default


def allowed_keys(channel: str) -> tuple[str, ...]:
    return POI_CHANNEL_KEYS if channel in CHANNELS_POI_BASED else WORK_CHANNEL_KEYS


def has_works(channel: str) -> bool:
    return channel in CHANNELS_WITH_WORKS


def channel_overrides(params: Dict[str, Any], channel: str) -> Dict[str, Any]:
    """取出某平台的覆盖项，容忍 channel_params 缺失或类型不对。"""
    per_channel = params.get("channel_params")
    if not isinstance(per_channel, dict):
        return {}
    values = per_channel.get(channel)
    return dict(values) if isinstance(values, dict) else {}


def resolve(config, params: Dict[str, Any], channel: str) -> Dict[str, Any]:
    """算出这个平台本次真正要用的采集上限。

    返回的键与 CollectContext 的字段同名，调用方直接展开即可。
    """
    params = params or {}
    override = channel_overrides(params, channel)

    def pick(key: str, config_key: str, fallback):
        """平台级 -> 顶层 -> 配置默认。显式写了 0 也算数，所以不能用 or。"""
        for source in (override, params):
            if key in source and source[key] is not None and source[key] != "":
                return source[key]
        return config.get(config_key, fallback)

    max_comment_level = _as_int(
        pick("max_comment_level", "crawl.max_comment_level", 3), 3
    )
    enable_sub = _as_bool(
        pick("enable_sub_comments", "crawl.enable_sub_comments", True), True
    )

    if channel in CHANNELS_POI_BASED:
        # 点评型：没有作品这一层。max_works 强制为 1（一个 POI 目标 = 一条合成作品），
        # 用户设的「采集点评数」落到 max_comments_per_work 上，
        # 因为那正是「一条合成作品下能采多少条评论」。
        max_comments = _as_int(
            pick("max_comments", "crawl.default_max_comments_per_poi", 1000), 1000
        )
        # 翻页上限。0 = 一直翻到接口没有数据为止（第一次全量拉取通常想要这个）
        max_pages = _as_int(
            pick("max_pages", f"platforms.{channel}.max_pages", 300), 300
        )
        return {
            "max_works": 0,
            # 点评型平台没有拟人采集器，固定走接口
            "collect_engine": "api",
            "max_comments_per_work": max_comments,
            "max_pages": max_pages,
            "max_comment_level": max_comment_level,
            "enable_sub_comments": enable_sub,
            "collect_comments": True,      # 点评就是全部内容，关掉等于什么都不采
        }

    return {
        "max_pages": 0,          # 作品型平台按作品数收口，不用页数
        # api / hybrid / human，见 CollectContext.collect_engine
        "collect_engine": _as_enum(
            pick("collect_engine", "crawl.collect_engine", DEFAULT_COLLECT_ENGINE),
            "collect_engine"),
        "max_works": _as_int(pick("max_works", "crawl.default_max_works", 100), 100),
        "max_comments_per_work": _as_int(
            pick("max_comments_per_work", "crawl.default_max_comments_per_work", 500), 500
        ),
        "max_comment_level": max_comment_level,
        "enable_sub_comments": enable_sub,
        "collect_comments": _as_bool(pick("collect_comments", "crawl.collect_comments", True), True),
    }


def normalize(params: Dict[str, Any], channels: List[str]) -> Dict[str, Any]:
    """清洗前端传来的 params：丢掉不认识的平台、把类型摆正、去掉平台用不上的键。

    这样存进 DB 的 channel_params 永远是干净的，
    编辑任务时前端读回来也不会看到一堆无意义的键。
    """
    params = dict(params or {})
    raw = params.get("channel_params")
    cleaned: Dict[str, Dict[str, Any]] = {}

    if isinstance(raw, dict):
        for channel, values in raw.items():
            if channel not in ALL_CHANNELS or channel not in channels:
                continue          # 没选的平台不留残留配置
            if not isinstance(values, dict):
                continue
            keep: Dict[str, Any] = {}
            for key in allowed_keys(channel):
                if key not in values or values[key] is None or values[key] == "":
                    continue
                keep[key] = (
                    _as_int(values[key], 0) if key in _INT_KEYS
                    else _as_bool(values[key], True) if key in _BOOL_KEYS
                    else _as_enum(values[key], key) if key in _ENUM_KEYS
                    else values[key]
                )
            if keep:
                cleaned[channel] = keep

    if cleaned:
        params["channel_params"] = cleaned
    else:
        params.pop("channel_params", None)

    # 内容过滤是任务级的（不分平台）：清洗成固定形状再存
    if "content_filter" in params:
        from .content_filter import normalize as normalize_content_filter
        params["content_filter"] = normalize_content_filter(params["content_filter"])

    for key in list(params):
        if key in _INT_KEYS and params[key] not in (None, ""):
            params[key] = _as_int(params[key], 0)
        elif key in _BOOL_KEYS and params[key] not in (None, ""):
            params[key] = _as_bool(params[key], True)
        elif key in _ENUM_KEYS and params[key] not in (None, ""):
            params[key] = _as_enum(params[key], key)
    return params


def summarize(config, params: Dict[str, Any], channels: List[str]) -> List[Dict[str, Any]]:
    """给前端展示用：每个平台最终生效的数量是多少。

    编辑任务时用户最想确认的就是这个——「我改的到底生效没有」。
    """
    rows = []
    for channel in channels:
        limits = resolve(config, params, channel)
        rows.append({
            "channel": channel,
            "has_works": has_works(channel),
            "max_works": limits["max_works"],
            "max_comments": limits["max_comments_per_work"],
            "max_pages": limits.get("max_pages", 0),
            "max_comment_level": limits["max_comment_level"],
            "enable_sub_comments": limits["enable_sub_comments"],
            "collect_comments": limits["collect_comments"],
            # 让前端在概览里直接显示"这个平台走哪条路采"。
            # supports_browser_engine=False 的平台不该显示选择器——
            # 选了也没用，只会让人以为设置没生效。
            "collect_engine": limits.get("collect_engine", DEFAULT_COLLECT_ENGINE),
            "supports_browser_engine": channel in SUPPORTS_BROWSER_ENGINE,
            "customized": bool(channel_overrides(params, channel)),
        })
    return rows


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    return default
