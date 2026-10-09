"""模型输出提取与解析。

大模型的输出永远不能假设是干净 JSON。这里按「从宽到严」四级兜底：
    1. 直接 ``json.loads``
    2. 剥 markdown 代码围栏（```json ... ```）
    3. 括号配平扫描，抠出第一个完整的 JSON 对象（能处理"前面有一段解释"的情况）
    4. 常见畸形修补：单引号、尾逗号、中文引号、True/False/None

解析出来的只是「结构合法」的原始字典（:class:`RawLabel`），
业务合法性（维度是否在白名单、关键词方向是否一致）由 labeling/postprocess.py 负责。
两件事分开，出问题时能一眼看出是模型格式坏了还是标注标错了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["RawLabel", "ParseError", "parse_label_output", "extract_json_object"]


class ParseError(ValueError):
    """模型输出无法解析成约定结构。"""


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.+?)\s*```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "，": ",", "：": ":"})


@dataclass
class RawLabel:
    """模型输出的原始标注结构（未做业务校验）。"""

    relevant: bool = True
    sentiment: Any = None
    confidence: float = 0.0
    dimensions: List[Dict[str, Any]] = field(default_factory=list)
    entities: List[Dict[str, Any]] = field(default_factory=list)
    keywords: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""


# ---------------------------------------------------------------------------
# JSON 抠取
# ---------------------------------------------------------------------------
def extract_json_object(text: str) -> Dict[str, Any]:
    """从任意文本中提取第一个 JSON 对象。

    Args:
        text: 模型原始输出。

    Returns:
        解析出的字典。

    Raises:
        ParseError: 四级兜底全部失败。
    """
    if not text or not text.strip():
        raise ParseError("模型输出为空")

    candidates: List[str] = []

    stripped = text.strip()
    candidates.append(stripped)

    # 级别 2：markdown 代码围栏
    fence = _FENCE.search(stripped)
    if fence:
        candidates.append(fence.group(1).strip())

    # 级别 3：括号配平扫描
    scanned = _scan_balanced_object(stripped)
    if scanned:
        candidates.append(scanned)

    last_error: Optional[Exception] = None
    for candidate in candidates:
        for variant in (candidate, _repair(candidate)):
            if not variant:
                continue
            try:
                parsed = json.loads(variant)
            except (json.JSONDecodeError, TypeError) as exc:
                last_error = exc
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                # 模型偶尔把单条结果包成数组
                return parsed[0]
            last_error = ParseError(f"顶层不是对象而是 {type(parsed).__name__}")

    raise ParseError(f"无法从输出中解析 JSON：{last_error}；原文前 200 字：{stripped[:200]}")


def _scan_balanced_object(text: str) -> Optional[str]:
    """从第一个 ``{`` 起做括号配平扫描，返回第一个完整对象的字符串。

    扫描时必须跳过字符串字面量内部的括号与转义，否则
    ``{"reason": "他说{很好}"}`` 这种内容会把配平算错。
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def _repair(text: str) -> str:
    """修补常见畸形 JSON。仅在严格解析失败后作为兜底使用。"""
    if not text:
        return text
    fixed = text.translate(_SMART_QUOTES)
    fixed = _TRAILING_COMMA.sub(r"\1", fixed)
    fixed = re.sub(r"\bTrue\b", "true", fixed)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"\bNone\b", "null", fixed)
    return fixed


# ---------------------------------------------------------------------------
# 结构解析
# ---------------------------------------------------------------------------
def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "是", "相关", "有关"}:
        return True
    if text in {"false", "no", "0", "否", "无关", "不相关"}:
        return False
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:            # NaN
        return default
    return max(0.0, min(1.0, num))


def _as_dict_list(value: Any, *, str_key: str) -> List[Dict[str, Any]]:
    """把可能是 ``["排队久"]`` 或 ``[{"word":"排队久"}]`` 的字段统一成字典列表。"""
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({str_key: item.strip()})
    return out


def parse_label_output(text: str) -> RawLabel:
    """把模型输出文本解析成 :class:`RawLabel`。

    这里只做「字段取到 + 类型归一」，任何取值是否合法都不判断。

    Raises:
        ParseError: JSON 无法提取，或缺少 ``sentiment`` 这种致命字段。
    """
    payload = extract_json_object(text)

    # 少数模型会把结果套一层 result / data
    for wrapper in ("result", "data", "label", "output"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict) and ("sentiment" in inner or "dimensions" in inner):
            payload = inner
            break

    sentiment = payload.get("sentiment")
    if sentiment is None:
        sentiment = payload.get("sentiment_label", payload.get("overall_sentiment"))
    if sentiment is None:
        raise ParseError("输出缺少 sentiment 字段")

    return RawLabel(
        relevant=_as_bool(payload.get("relevant"), default=True),
        sentiment=sentiment,
        confidence=_as_float(payload.get("confidence"), default=0.0),
        dimensions=_as_dict_list(payload.get("dimensions") or payload.get("dimension_tags"),
                                 str_key="dim1"),
        entities=_as_dict_list(payload.get("entities") or payload.get("entity_tags"),
                               str_key="value"),
        keywords=_as_dict_list(payload.get("keywords") or payload.get("keyword_tags"),
                               str_key="word"),
        reason=str(payload.get("reason") or "")[:200],
    )
