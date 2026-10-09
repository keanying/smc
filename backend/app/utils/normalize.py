"""字段归一化工具：把各平台五花八门的原始值统一成入库格式。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Optional

# /Date(1786955245000+0800)/  —— 携程等 .NET 后端常见格式
DOTNET_DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")

# 常见中文时间格式
_CN_PATTERNS = [
    ("%Y-%m-%d %H:%M:%S", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")),
    ("%Y-%m-%d %H:%M", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")),
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("%Y/%m/%d %H:%M:%S", re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")),
    ("%Y/%m/%d", re.compile(r"^\d{4}/\d{2}/\d{2}$")),
    ("%Y年%m月%d日", re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$")),
]

#: 只有月日、**没有年份**的写法。小红书搜索卡片的 corner_tag_info 就是这样：
#: 一周以内显示「1小时前 / 2天前」，更早的直接显示「08-28」。
#: 年份得自己补——补错一年，时间窗过滤会把整批内容判成范围外。
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})[-/月](\d{1,2})日?$")

_CN_UNITS = {"万": 10_000, "w": 10_000, "W": 10_000, "亿": 100_000_000, "k": 1_000, "K": 1_000}


def to_datetime(value: Any, *, tz_offset_hours: int = 8) -> Optional[datetime]:
    """尽最大努力把任意时间表示转成 naive datetime（本地时区，默认东八区）。

    支持：秒/毫秒时间戳、/Date(...)/、常见字符串格式、相对时间（3天前）。
    解析不出来返回 None，由调用方决定是否存默认值。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is None else \
            value.astimezone(timezone(timedelta(hours=tz_offset_hours))).replace(tzinfo=None)

    # 数字时间戳
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = int(value)
        if number > 10_000_000_000_000:      # 微秒
            number //= 1_000_000
        elif number > 10_000_000_000:        # 毫秒
            number //= 1000
        if number <= 0:
            return None
        tz = timezone(timedelta(hours=tz_offset_hours))
        return datetime.fromtimestamp(number, tz=tz).replace(tzinfo=None)

    text = str(value).strip()

    match = DOTNET_DATE_RE.fullmatch(text)
    if match:
        milliseconds, offset = match.groups()
        utc_dt = datetime.fromtimestamp(int(milliseconds) / 1000, tz=timezone.utc)
        if not offset:
            return utc_dt.replace(tzinfo=None)
        sign = 1 if offset[0] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
        return utc_dt.astimezone(tz).replace(tzinfo=None)

    for fmt, pattern in _CN_PATTERNS:
        if pattern.match(text):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue

    relative = _parse_relative_cn(text)
    if relative is not None:
        return relative

    month_day = _parse_month_day(text)
    if month_day is not None:
        return month_day

    # RFC 2822：微博的 created_at 是这个格式
    # 例：Sun Aug 24 12:00:00 +0800 2026
    rfc = _parse_rfc2822(text, tz_offset_hours)
    if rfc is not None:
        return rfc

    try:  # ISO 8601 兜底
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone(timedelta(hours=tz_offset_hours)))
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def _parse_rfc2822(text: str, tz_offset_hours: int) -> Optional[datetime]:
    """解析 RFC 2822 时间串并换算到目标时区。

    微博用的是 "Sun Aug 24 12:00:00 +0800 2026" 这种年份在末尾的变体，
    标准库的 parsedate_to_datetime 认不出来，先重排成标准顺序再交给它。
    """
    from email.utils import parsedate_to_datetime

    candidates = [text]
    # 年份挪到时区前面：Sun Aug 24 12:00:00 +0800 2026 -> Sun, 24 Aug 2026 12:00:00 +0800
    match = re.match(
        r"^(\w{3})\s+(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+([+-]\d{4})\s+(\d{4})$",
        text,
    )
    if match:
        weekday, month, day, clock, offset, year = match.groups()
        candidates.insert(0, f"{weekday}, {day} {month} {year} {clock} {offset}")

    for candidate in candidates:
        try:
            parsed = parsedate_to_datetime(candidate)
        except (TypeError, ValueError):
            continue
        if parsed is None:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone(timedelta(hours=tz_offset_hours)))
        return parsed.replace(tzinfo=None)
    return None


def _parse_month_day(text: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """把「08-28」「12-31」这种只有月日的写法补上年份。

    规则：先按**今年**算；如果算出来的日期比今天还晚（比如现在是 9 月，
    卡片写的是「12-31」），那它只可能是**去年**的——平台不会展示未来的
    发布时间。这条不是猜的：只有月日的写法本身就意味着"不在最近一周内"，
    而未来日期在这里没有任何合理解释。

    ⚠️ 宁可返回 None 也不要瞎补：publish_time 是时间窗过滤的输入，
    补错一年会让整批内容被判成范围外，现象是"明明搜得到却一条不入库"。
    """
    match = _MONTH_DAY_RE.match(text)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    now = now or datetime.now()
    for year in (now.year, now.year - 1):
        try:
            guess = datetime(year, month, day)
        except ValueError:      # 2 月 30 日这种，换一年也不会对
            return None
        # 允许当天：卡片上的「09-04」在 9 月 4 日当天出现是正常的，
        # 只是它一般会显示成「N小时前」。用日期比较而不是时刻比较，
        # 免得把今天早上发的内容推到去年。
        if guess.date() <= now.date():
            return guess
    return None


def _parse_relative_cn(text: str) -> Optional[datetime]:
    """解析「刚刚 / 5分钟前 / 3小时前 / 2天前 / 昨天 12:30」这类相对时间。"""
    now = datetime.now()
    if text in ("刚刚", "刚才", "现在"):
        return now
    match = re.match(r"^(\d+)\s*(秒|分钟|分|小时|天|周|月|年)前$", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        deltas = {
            "秒": timedelta(seconds=amount),
            "分": timedelta(minutes=amount),
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
            "周": timedelta(weeks=amount),
            "月": timedelta(days=30 * amount),
            "年": timedelta(days=365 * amount),
        }
        return now - deltas[unit]
    if text.startswith("昨天"):
        return now - timedelta(days=1)
    if text.startswith("前天"):
        return now - timedelta(days=2)
    return None


def to_int(value: Any, default: int = 0) -> int:
    """把「1.2万」「3.4w」「1,234」「12个」这类计数统一成整数。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return default
    for unit, multiplier in _CN_UNITS.items():
        if unit in text:
            number_part = text.replace(unit, "").strip()
            try:
                return int(float(number_part) * multiplier)
            except ValueError:
                return default
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return default
    try:
        return int(float(match.group()))
    except ValueError:
        return default


def to_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def to_json_list(value: Any) -> Optional[str]:
    """图片/视频列表统一存 JSON 数组字符串；空列表存 NULL 而不是 '[]'。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            return text
        return json.dumps([text], ensure_ascii=False)
    if isinstance(value, Iterable):
        items = [item for item in value if item]
        if not items:
            return None
        return json.dumps(items, ensure_ascii=False)
    return json.dumps([value], ensure_ascii=False)


def to_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def clean_location(value: Any) -> str:
    """发布地址：去掉「IP属地：」这类前缀，字段只有 50 字符。"""
    text = to_text(value).strip()
    for prefix in ("IP属地：", "IP属地:", "IP归属地：", "来自", "发布于"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text[:50]


def truncate(value: Any, limit: int, default: str = "") -> str:
    """按数据库列宽截断，避免 Data too long 直接把整批写入打断。"""
    text = to_text(value, default)
    return text if len(text) <= limit else text[:limit]


# 一个"资源字典"里可能出现的 URL 键，按优先级排列。
# 顺序很重要：同程一张图会同时给 originalImgUrl / imgUrl / smallImgUrl，
# 全收下来就是同一张图存三条；douyin 的 url_list 里则是同一个资源的多个 CDN 镜像。
# 所以规则是「一个 dict = 一个资源，取优先级最高的那个键」。
_URL_KEYS = (
    # 原图/大图优先，缩略图垫底
    "imageSrcUrl",                      # 携程：images[].imageSrcUrl 才是原图
    "originalImgUrl", "origin_url", "originUrl", "origin", "large",
    "videoUrl", "video_url", "playUrl", "play_url",
    "url", "url_list", "urlList", "download_url", "download_url_list",
    "play_addr", "download_addr", "src",
    "imgUrl", "imageUrl", "picUrl", "photoUrl",
    "medium_url", "thumb_url", "smallImgUrl",
    "imageThumbUrl",                    # 携程缩略图，只有没有原图时才用
)


def normalize_url(value: str) -> str:
    """把协议相对 URL 补全成 https。

    同程返回的是 //pic5.40017.cn/i/ori/xxx.jpg 这种形式，
    直接存库的话前端拼不出可访问地址。
    """
    text = (value or "").strip()
    if text.startswith("//"):
        return "https:" + text
    return text


def collect_urls(*candidates: Any) -> List[str]:
    """从多个可能的字段里收集 URL，去重保序。

    - 列表：每个元素是一个独立资源，全部收
    - 字典：一个资源，只取优先级最高的那个 URL 键
    - 协议相对 URL（//host/path）会补成 https
    """
    urls: List[str] = []
    seen = set()

    def _add(text: str) -> None:
        item = normalize_url(text)
        if item.startswith("http") and item not in seen:
            seen.add(item)
            urls.append(item)

    def _walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            _add(node)
        elif isinstance(node, dict):
            for key in _URL_KEYS:
                if key in node and node[key]:
                    value = node[key]
                    if isinstance(value, str):
                        _add(value)
                    elif isinstance(value, (list, tuple)):
                        # 同一资源的多个镜像地址，取第一个可用的即可
                        for mirror in value:
                            before = len(urls)
                            _walk(mirror)
                            if len(urls) > before:
                                break
                    else:
                        _walk(value)
                    return  # 一个 dict 只产出一个资源
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                _walk(item)

    for candidate in candidates:
        _walk(candidate)
    return urls
