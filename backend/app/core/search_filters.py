"""搜索的排序与时间范围筛选。

各平台的支持程度差别很大，先把事实摆清楚（都是照 MediaCrawler 与线上接口核过的）：

| 平台 | 服务端排序 | 服务端时间筛选 | 说明 |
|---|---|---|---|
| 抖音 | ✅ sort_type 0/1/2 | ✅ publish_time 0/1/7/180 | 两者都通过 filter_selected 传 |
| 小红书 | ✅ sort 五档 | ✅ filters 里的 filter_note_time | 预设档位，不能给任意日期 |
| 微博 | ✅ 综合/实时/热门 | ❌ | 移动端接口只收 type/q（timescope 是桌面站参数） |
| 快手 | ❌ | ❌ | 接口只收 keyword/pcursor/searchSessionId/page |

快手那一行不是没做，是**接口真的没有这两个参数**——
MediaCrawler 的 `visionSearchPhoto` GraphQL 和新版 `/rest/v/search/feed`
都只有那四个字段。所以快手只能靠客户端过滤。

因此这里的策略是两层：

  1. **服务端能筛的就在服务端筛** —— 少翻页、少请求、少被风控盯上
  2. **客户端时间窗兜底** —— 不管平台支不支持，落库前统一按时间窗过滤一遍

第 2 层让"按时间范围采集"这件事在**所有平台**上都成立，
而不是变成"抖音小红书能筛、快手不能"这种参差的体验。

按时间倒序采集时，还能靠时间窗**提前停下**：一旦翻到比窗口下界还早的内容，
后面只会更早，继续翻纯属浪费请求。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    CHANNEL_DOUYIN,
    CHANNEL_KUAISHOU,
    CHANNEL_WEIBO,
    CHANNEL_XHS,
)

# ---------------------------------------------------------------------------
# 可选项：value 是存进任务的稳定标识，label 是界面上显示的字
# ---------------------------------------------------------------------------

#: 排序方式。空列表 = 该平台接口不支持排序
SORT_OPTIONS: Dict[str, List[Dict[str, str]]] = {
    CHANNEL_DOUYIN: [
        {"value": "general", "label": "综合排序"},
        {"value": "latest", "label": "最新发布"},
        {"value": "most_like", "label": "最多点赞"},
    ],
    CHANNEL_XHS: [
        # ⚠️ 只有这三档。MediaCrawler 的 SearchSortType、RedCrack 的调用、
        # xhshow 里都只有 general / time_descending / popularity_descending。
        # 之前多出来的「最多评论 / 最多收藏」在任何参考实现里都不存在——
        # 服务端不认的排序值会被**静默忽略**，用户以为按评论排了，其实是综合排序。
        {"value": "general", "label": "综合"},
        {"value": "latest", "label": "最新"},
        {"value": "most_like", "label": "最多点赞"},
    ],
    CHANNEL_WEIBO: [
        # 对齐用户自己那套微博采集脚本的三档：综合 / 实时 / 热门
        {"value": "general", "label": "综合"},
        {"value": "latest", "label": "实时"},
        {"value": "most_like", "label": "热门"},
    ],
    CHANNEL_KUAISHOU: [],
}

#: 预设时间窗。天数用于换算成绝对区间
PRESET_WINDOWS: Dict[str, int] = {
    "day": 1,
    "week": 7,
    "half_year": 180,
}

PUBLISH_WITHIN_OPTIONS: List[Dict[str, str]] = [
    {"value": "unlimited", "label": "不限"},
    {"value": "day", "label": "一天内"},
    {"value": "week", "label": "一周内"},
    {"value": "half_year", "label": "半年内"},
    {"value": "custom", "label": "自定义日期区间"},
]

#: 服务端认预设档位的平台（抖音的 publish_time、小红书的 filter_note_time）
_PRESET_NATIVE = {CHANNEL_DOUYIN, CHANNEL_XHS}
#: 服务端认任意日期区间的平台。
#: ⚠️ 现在是空的。微博曾经在这里——理由是"移动端 containerid 就是把桌面版
#: s.weibo.com 的查询串搬过来的"，那是推断，不是核过的事实。
#: timescope 是**桌面站 s.weibo.com 的参数**，m.weibo.cn 的容器接口不认，
#: 带上它反而会让小众关键字退化成一张空提示卡（card_type=4），一条都搜不到。
#: 用户那套跑通的脚本，移动端搜索只发 type 和 q 两个字段。
_RANGE_NATIVE: set = set()


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------

@dataclass
class SearchFilters:
    """一次采集实际生效的筛选条件。"""

    channel: str = ""
    sort: str = "general"
    publish_within: str = "unlimited"
    start_date: str = ""
    end_date: str = ""
    #: 换算好的绝对时间窗（含两端），None 表示不限
    window: Optional[Tuple[datetime, datetime]] = None

    # ---- 服务端能力 ----
    @property
    def sort_supported(self) -> bool:
        return bool(SORT_OPTIONS.get(self.channel))

    @property
    def time_supported_natively(self) -> bool:
        if self.channel in _RANGE_NATIVE:
            return True
        return self.channel in _PRESET_NATIVE and self.publish_within in PRESET_WINDOWS

    # ---- 客户端过滤 ----
    def in_window(self, publish_time: Optional[datetime]) -> bool:
        """这条内容在时间窗里吗？

        没有时间窗时一律放行。**发布时间解析不出来的也放行**——
        宁可多收一条，也不要因为某个平台的时间格式没认出来就把数据全丢了。
        """
        if self.window is None or publish_time is None:
            return True
        start, end = self.window
        return start <= publish_time <= end

    def older_than_window(self, publish_time: Optional[datetime]) -> bool:
        """这条内容比时间窗还早吗？按时间倒序采集时用它决定要不要提前停。"""
        if self.window is None or publish_time is None:
            return False
        return publish_time < self.window[0]

    @property
    def sorted_by_time(self) -> bool:
        """当前排序是不是"新的在前"。只有这时候才能靠时间窗提前停。"""
        return self.sort == "latest"

    def describe(self) -> str:
        """写进任务日志的一行说明。"""
        sort_label = _label_of(SORT_OPTIONS.get(self.channel, []), self.sort)
        parts = []
        if sort_label:
            parts.append(f"排序 {sort_label}")
        if self.window:
            start, end = self.window
            parts.append(
                f"时间范围 {start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}"
                + ("（接口侧筛选）" if self.time_supported_natively else "（本地过滤）")
            )
        else:
            parts.append("时间不限")
        return "，".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel": self.channel,
            "sort": self.sort,
            "publish_within": self.publish_within,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "window": (
                [self.window[0].isoformat(), self.window[1].isoformat()]
                if self.window else None
            ),
            "native_time_filter": self.time_supported_natively,
        }


def _label_of(options: List[Dict[str, str]], value: str) -> str:
    for option in options:
        if option["value"] == value:
            return option["label"]
    return ""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def resolve(channel: str, params: Dict[str, Any], *, now: Optional[datetime] = None) -> SearchFilters:
    """从任务 params 里算出这个平台生效的筛选条件。

    读取顺序和采集数量一致：channel_params[平台] > 顶层。
    """
    from . import collect_params

    params = params or {}
    override = collect_params.channel_overrides(params, channel)

    def pick(key: str, default: Any) -> Any:
        for source in (override, params):
            if key in source and source[key] not in (None, ""):
                return source[key]
        return default

    sort = str(pick("sort", "general"))
    if sort not in {o["value"] for o in SORT_OPTIONS.get(channel, [])}:
        sort = "general"

    publish_within = str(pick("publish_within", "unlimited"))
    start_date = str(pick("start_date", "") or "")
    end_date = str(pick("end_date", "") or "")

    # 填了日期区间就按自定义走，不管 publish_within 写的是什么——
    # 用户明确给了日期，那就是他的意图
    if start_date or end_date:
        publish_within = "custom"

    window = _compute_window(publish_within, start_date, end_date, now=now)
    if window is None and publish_within == "custom":
        publish_within = "unlimited"

    return SearchFilters(
        channel=channel,
        sort=sort,
        publish_within=publish_within,
        start_date=start_date,
        end_date=end_date,
        window=window,
    )


def _compute_window(
    publish_within: str, start_date: str, end_date: str, *, now: Optional[datetime] = None
) -> Optional[Tuple[datetime, datetime]]:
    moment = now or datetime.now()

    if publish_within in PRESET_WINDOWS:
        days = PRESET_WINDOWS[publish_within]
        return (moment - timedelta(days=days), moment)

    if publish_within != "custom":
        return None

    start = _parse_date(start_date, end_of_day=False)
    end = _parse_date(end_date, end_of_day=True)
    if start is None and end is None:
        return None
    # 只填了一头就把另一头放到极端值，语义是"从这天起"/"到这天为止"
    start = start or datetime(1970, 1, 1)
    end = end or moment
    if start > end:
        start, end = end, start
    return (start, end)


def _parse_date(text: str, *, end_of_day: bool) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt in ("%Y-%m-%d", "%Y/%m/%d") and end_of_day:
            return parsed.replace(hour=23, minute=59, second=59)
        return parsed
    return None


def options_for(channel: str) -> Dict[str, Any]:
    """给前端渲染表单用：这个平台能设哪些筛选项。"""
    sorts = SORT_OPTIONS.get(channel, [])
    return {
        "channel": channel,
        "sorts": sorts,
        "sort_supported": bool(sorts),
        # 时间范围在所有平台都可以设：接口不支持的用本地过滤兜底
        "time_supported": True,
        "time_native": channel in _PRESET_NATIVE or channel in _RANGE_NATIVE,
        "publish_within": PUBLISH_WITHIN_OPTIONS,
        "note": _channel_note(channel),
    }


def _channel_note(channel: str) -> str:
    if channel == CHANNEL_KUAISHOU:
        return (
            "快手的搜索接口没有排序和时间筛选参数（GraphQL 只收 "
            "keyword/pcursor/searchSessionId/page），所以时间范围是采回来后本地过滤的，"
            "会比其他平台多翻几页。"
        )
    if channel == CHANNEL_WEIBO:
        return (
            "微博的移动端搜索接口只收关键字和排序，没有时间参数"
            "（timescope 是桌面站 s.weibo.com 的参数，这个接口不认，"
            "带上反而会搜不到内容），所以时间范围是采回来后本地过滤的。"
        )
    if channel in _PRESET_NATIVE:
        return "接口只认「一天内 / 一周内 / 半年内」这几档；填自定义日期区间的话，会在本地过滤。"
    return ""


def all_options() -> List[Dict[str, Any]]:
    return [options_for(c) for c in
            (CHANNEL_DOUYIN, CHANNEL_KUAISHOU, CHANNEL_XHS, CHANNEL_WEIBO)]
