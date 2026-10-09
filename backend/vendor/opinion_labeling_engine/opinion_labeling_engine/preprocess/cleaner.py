"""内容清洗：进模型之前的第一道闸。

要解决的问题（需求：先对 content 过滤掉纯表情包、特殊符号的内容）：
    1. 纯表情 / 纯符号 / 纯数字 / 纯标点的评论没有任何标注价值，
       送进模型只会浪费 token 并且大概率被瞎标成正向。
    2. 平台特有的表情占位符（小红书 ``[微笑R]``、微博 ``[doge]``、
       抖音 ``[比心]``）在文本里是方括号包裹的 ASCII/中文，不是 Unicode emoji，
       靠 emoji 码位过滤是滤不掉的，必须单独处理。
    3. 「哈哈哈哈哈哈哈」「6666666」这类重复字符灌水，去重后长度不足同样无效。

输出的 ``CleanResult`` 同时给出两份文本：
    - ``text``：喂给模型的干净正文（已剥离表情与冗余空白、已截断）
    - ``original``：原文，后处理阶段用它做关键词溯源校验（关键词必须来自原文）
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from ..config import CleaningConfig

__all__ = ["CleanResult", "ContentCleaner", "InvalidReason"]


class InvalidReason:
    """无效内容的原因码，直接进日志与监控指标。"""

    EMPTY = "empty"                 # 原文为空
    EMOJI_ONLY = "emoji_only"       # 纯表情（Unicode emoji 或平台表情占位符）
    SYMBOL_ONLY = "symbol_only"     # 纯标点/符号
    DIGIT_ONLY = "digit_only"       # 纯数字
    URL_ONLY = "url_only"           # 纯链接
    TOO_SHORT = "too_short"         # 清洗后长度不足
    REPEAT_ONLY = "repeat_only"     # 单字符重复灌水
    PLACEHOLDER = "placeholder"     # 平台默认好评文案


# --- 正则表 -----------------------------------------------------------------

# Unicode emoji 主要码段 + 变体选择符 + 零宽连接符 + 肤色修饰符
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 各类 emoji 区块（含补充符号与象形文字）
    "\U00002600-\U000027BF"   # 杂项符号与装饰符号
    "\U0001F1E6-\U0001F1FF"   # 区域指示符（国旗）
    "\U00002190-\U000021FF"   # 箭头
    "\U00002B00-\U00002BFF"   # 杂项符号和箭头
    "\U0001F900-\U0001F9FF"   # 补充符号和象形文字
    "\U0000FE00-\U0000FE0F"   # 变体选择符
    "\U0000200D"              # 零宽连接符
    "\U0001F3FB-\U0001F3FF"   # 肤色修饰符
    "\U00003030\U0000303D\U00002049\U0000203C"
    "]+",
    flags=re.UNICODE,
)

# 平台表情占位符：[微笑R] [doge] [666] [憨笑]，以及颜文字 (^_^)
_PLATFORM_EMOJI = re.compile(r"\[[^\[\]]{1,12}\]")

# 常见颜文字骨架，字符集刻意收窄，避免误伤正常括号
_KAOMOJI = re.compile(r"[（(][\sa-zA-Z0-9_\-^~'\"`*/\\|:;=<>+.,∀ω・´｀°∇Д゜тт]{1,12}[)）]")

_URL = re.compile(r"https?://\S+|www\.\S+", flags=re.IGNORECASE)

# 话题标签与 @提及：内容本身不算主体评价，但要保留其中的中文词，故只去掉符号
_AT_MENTION = re.compile(r"@[\w一-鿿\-]{1,30}")

_WHITESPACE = re.compile(r"\s+")

# 中日韩统一表意文字 + 常用汉字扩展 + 英文字母 + 数字，视为「有信息量的字符」
_MEANINGFUL_CHAR = re.compile(r"[一-鿿㐀-䶿a-zA-Z0-9]")
_CJK_OR_LATIN = re.compile(r"[一-鿿㐀-䶿a-zA-Z]")

_DIGIT_ONLY = re.compile(r"^[\d\s\W]*\d[\d\s\W]*$")

# 各平台「默认好评」占位文案：用户没写内容，系统自动填的
_PLACEHOLDER_TEXTS = {
    "此用户没有填写评价", "此用户未填写评价", "用户未填写评价",
    "该用户没有填写评价", "系统默认好评", "默认好评", "此条评论已删除",
    "评论已被删除", "内容已删除", "商家未回复", "暂无评价", "无",
}


@dataclass(frozen=True)
class CleanResult:
    """清洗结果。

    Attributes:
        valid: 是否值得送进大模型。False 时调用方应直接按中性落库。
        text: 清洗后的正文（送模型用）。
        original: 原始正文（关键词溯源用）。
        reason: 无效原因码，valid=True 时为空串。
        emoji_stripped: 被剥离的表情数量，用于观察数据质量。
        truncated: 是否发生了截断。
    """

    valid: bool
    text: str
    original: str
    reason: str = ""
    emoji_stripped: int = 0
    truncated: bool = False


class ContentCleaner:
    """无状态清洗器，线程安全，可跨 worker 共享。"""

    def __init__(self, cfg: CleaningConfig) -> None:
        self._cfg = cfg

    # ------------------------------------------------------------------ 主流程
    def clean(self, content: Optional[str]) -> CleanResult:
        """清洗一条评论正文。

        Args:
            content: 原始 ``content`` 字段，允许 None。

        Returns:
            :class:`CleanResult`。``valid=False`` 时 ``reason`` 说明原因。
        """
        original = content or ""
        if not original.strip():
            return CleanResult(False, "", original, InvalidReason.EMPTY)

        # 1. Unicode 规范化：把全角字母数字、兼容字符折叠成标准形式
        text = unicodedata.normalize("NFKC", original)

        # 2. 剥离链接（链接本身不表达情感，但要记住「原文只有链接」的情况）
        without_url = _URL.sub(" ", text)
        if not _MEANINGFUL_CHAR.search(without_url) and _URL.search(text):
            return CleanResult(False, "", original, InvalidReason.URL_ONLY)
        text = without_url

        # 3. 剥离表情：Unicode emoji + 平台方括号占位符 + 颜文字
        emoji_hits = len(_EMOJI_PATTERN.findall(text)) + len(_PLATFORM_EMOJI.findall(text))
        text = _EMOJI_PATTERN.sub(" ", text)
        text = _PLATFORM_EMOJI.sub(" ", text)
        text = _KAOMOJI.sub(" ", text)

        # 4. @提及只去符号不去名字后面的正文
        text = _AT_MENTION.sub(" ", text)

        # 5. 压缩空白
        text = _WHITESPACE.sub(" ", text).strip()

        # 6. 有效性判定
        reason = self._invalid_reason(text, original, emoji_hits)
        if reason:
            return CleanResult(False, text, original, reason, emoji_hits)

        # 7. 截断（保留头部，评论的主要观点通常在前面）
        truncated = False
        limit = self._cfg.max_content_length
        if limit and len(text) > limit:
            text = text[:limit]
            truncated = True

        return CleanResult(True, text, original, "", emoji_hits, truncated)

    # ------------------------------------------------------------------ 判定
    def _invalid_reason(self, text: str, original: str, emoji_hits: int) -> str:
        """返回无效原因码；有效则返回空串。"""
        stripped = text.strip()
        if not stripped:
            # 清洗后什么都不剩：原文要么全是表情，要么全是符号
            return InvalidReason.EMOJI_ONLY if emoji_hits else InvalidReason.SYMBOL_ONLY

        if stripped in _PLACEHOLDER_TEXTS:
            return InvalidReason.PLACEHOLDER

        if not _MEANINGFUL_CHAR.search(stripped):
            return InvalidReason.SYMBOL_ONLY

        meaningful = "".join(_MEANINGFUL_CHAR.findall(stripped))
        if not meaningful:
            return InvalidReason.SYMBOL_ONLY

        # 单字符重复灌水："哈哈哈哈哈" / "666666"。
        # 必须排在纯数字判定之前——"666666" 两种原因都成立，报"灌水"更贴近实情。
        if len(set(meaningful)) == 1 and len(meaningful) >= 2:
            return InvalidReason.REPEAT_ONLY

        # 纯数字（含带符号的 "12 34!!!"）：没有语义
        if not _CJK_OR_LATIN.search(stripped) and _DIGIT_ONLY.match(stripped):
            return InvalidReason.DIGIT_ONLY

        # 长度门槛按「有信息量的字符数」算，而不是原始长度
        if len(meaningful) < max(1, self._cfg.min_valid_length):
            return InvalidReason.TOO_SHORT

        return ""

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def strip_emoji(text: str) -> str:
        """仅剥离表情，不做有效性判定。供关键词溯源比对使用。"""
        if not text:
            return ""
        cleaned = _EMOJI_PATTERN.sub("", text)
        cleaned = _PLATFORM_EMOJI.sub("", cleaned)
        return _WHITESPACE.sub(" ", cleaned).strip()
