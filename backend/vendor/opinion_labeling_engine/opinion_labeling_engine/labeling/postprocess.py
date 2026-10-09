"""标注后处理：把模型的原始输出收敛成可以落库的结果。

模型再听话也会越界，所以提示词里写过的约束，这里必须用代码再执行一遍。
需求里的四条硬约束全部在本文件落地，每条都有对应的方法与单测：

    要求 1  keyword_tags 只能包含与整体情感同方向的关键词  → :meth:`_filter_keywords`
    要求 2  dimension_tags 必须合理（合法路径 + 去重 + 限量）→ :meth:`_filter_dimensions`
    要求 3  与景区主体无关的内容统一中性                    → :meth:`_apply_relevance`
    要求 4  keyword_tags 不得出现单字与幻觉词               → :meth:`_filter_keywords`

所有被丢弃的内容都记进 ``LabelResult.dropped``，便于抽样复核时定位模型的系统性偏差。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..config import LabelingConfig
from ..domain.enums import LabelSource, Sentiment
from ..domain.models import CommentRecord, DimensionTag, EntityTag, LabelResult
from ..domain.taxonomy import Taxonomy
from ..llm.parser import RawLabel
from ..logging_conf import get_logger

__all__ = ["LabelPostProcessor", "KeywordMatch"]

logger = get_logger(__name__)

# 关键词里不允许出现的字符：句读、成对括号、常见分隔符（全角半角都要覆盖）。
# 命中这些字符说明模型输出的是句子而不是词，直接丢弃。
_FORBIDDEN_IN_KEYWORD = re.compile(
    r"[。，、；：！？…—～·,.;:!?\n\r\t\"'“”‘’（）()\[\]【】｛｝{}《》<>|]"
)
# 只由标点/空白构成
_NON_INFORMATIVE = re.compile(r"^[\W_]+$", re.UNICODE)


class KeywordMatch:
    """关键词与原文的匹配级别，越小越可信。"""

    EXACT = "exact"              # 原文包含该词
    SUBSEQUENCE = "subseq"       # 字符按序出现在原文中（模型做了轻微省略）
    CHARSET = "charset"          # 字符全部出现在原文中但顺序不同（同义改写）
    MISS = "miss"                # 原文里找不到 —— 判为幻觉


class LabelPostProcessor:
    """无状态后处理器，线程安全。"""

    def __init__(self, taxonomy: Taxonomy, cfg: LabelingConfig) -> None:
        self._tax = taxonomy
        self._cfg = cfg

    # ================================================================== 主入口
    def build(
        self,
        record: CommentRecord,
        raw: RawLabel,
        *,
        source_text: str,
        model: str = "",
        latency_ms: int = 0,
    ) -> LabelResult:
        """把 :class:`RawLabel` 收敛成 :class:`LabelResult`。

        Args:
            record: 原始评论。
            raw: 模型输出的解析结果。
            source_text: 用于关键词溯源的文本（用清洗后的正文，不用原文——
                原文里的表情不可能成为关键词，拿它比对只会放宽校验）。
            model: 实际服务的模型名，落监控。
            latency_ms: 本次模型调用耗时。

        Returns:
            可直接落库的 :class:`LabelResult`。
        """
        dropped: Dict[str, List[str]] = {}

        # 1) 整体情感归一。识别不了就退到中性并把置信度清零——
        #    宁可标中性也不能猜，中性在下游指标里是"不加分不减分"的安全值。
        sentiment = self._tax.normalize_sentiment(raw.sentiment)
        confidence = float(raw.confidence or 0.0)
        if sentiment is None:
            dropped.setdefault("sentiment", []).append(str(raw.sentiment))
            sentiment = Sentiment.NEUTRAL
            confidence = 0.0

        # 2) 维度与实体
        dimensions = self._filter_dimensions(raw.dimensions, dropped)
        entities = self._filter_entities(raw.entities, dropped)

        # 3) 整体情感仲裁（可配置为按维度多数票复核）
        if self._cfg.overall_sentiment_arbiter == "majority" and dimensions:
            sentiment = self._majority_sentiment(dimensions, fallback=sentiment)

        # 4) 关键词：先按整体情感方向过滤，再做幻觉校验
        keywords = self._filter_keywords(raw.keywords, sentiment, source_text, dropped)

        result = LabelResult(
            comment_id=record.comment_id,
            sentiment=sentiment,
            dimension_tags=dimensions,
            entity_tags=entities,
            keyword_tags=keywords,
            confidence=round(confidence, 2),
            source=LabelSource.LLM,
            relevant=bool(raw.relevant),
            reason=raw.reason,
            model=model,
            latency_ms=latency_ms,
            dropped=dropped,
        )

        # 5) 相关性兜底（要求 3）：放在最后，确保它能覆盖前面所有结论
        return self._apply_relevance(result)

    # ============================================================ 要求 2：维度
    def _filter_dimensions(
        self,
        items: Sequence[Dict],
        dropped: Dict[str, List[str]],
    ) -> List[DimensionTag]:
        """校验维度路径合法性、归一情感、去重、限量。

        丢弃而不是"猜一个最近的维度"，是因为维度得分会直接进景区口碑看板，
        一个瞎标的维度比一个缺失的维度伤害大得多。
        """
        out: List[DimensionTag] = []
        seen: Set[Tuple[str, str, str]] = set()

        for item in items:
            dim1 = str(item.get("dim1") or "").strip()
            dim2 = str(item.get("dim2") or "").strip()
            dim3 = str(item.get("dim3") or "").strip()
            if dim3.lower() in {"null", "none", "无", "(无三级)"}:
                dim3 = ""

            if not dim1:
                dropped.setdefault("dimension", []).append(str(item))
                continue

            if self._cfg.strict_dimension_whitelist:
                path = self._tax.resolve_dimension(dim1, dim2, dim3)
                if path is None:
                    dropped.setdefault("dimension", []).append(f"{dim1}/{dim2}/{dim3}")
                    continue
            else:
                path = (dim1, dim2, dim3)

            sentiment = self._tax.normalize_sentiment(item.get("sentiment"))
            if sentiment is None:
                # 维度情感识别不了就丢这一条，不要用整体情感顶替：
                # 「风景好但厕所脏」里用整体负向顶替景色维度会直接标反。
                dropped.setdefault("dimension_sentiment", []).append(f"{dim1}/{dim2}/{dim3}")
                continue

            if path in seen:
                continue
            seen.add(path)
            out.append(self._tax.to_dimension_tag(path, sentiment))

            if len(out) >= self._cfg.dimension_max_count:
                break

        return out

    # ============================================================ 实体
    def _filter_entities(
        self,
        items: Sequence[Dict],
        dropped: Dict[str, List[str]],
    ) -> List[EntityTag]:
        """校验实体 type/value 是否在体系内；开放类型只校验 type。"""
        out: List[EntityTag] = []
        seen: Set[Tuple[str, str]] = set()

        for item in items:
            etype = str(item.get("type") or "").strip()
            value = str(item.get("value") or item.get("name") or "").strip()
            if not etype or not value:
                dropped.setdefault("entity", []).append(str(item))
                continue

            if self._cfg.strict_entity_whitelist:
                tag = self._tax.resolve_entity(etype, value)
                if tag is None:
                    dropped.setdefault("entity", []).append(f"{etype}:{value}")
                    continue
            else:
                tag = EntityTag(type=etype, value=value)

            key = (tag.type, tag.value)
            if key in seen:
                continue
            seen.add(key)
            out.append(tag)

            if len(out) >= self._cfg.entity_max_count:
                break

        return out

    # ==================================================== 要求 1 + 要求 4：关键词
    def _filter_keywords(
        self,
        items: Sequence[Dict],
        overall: Sentiment,
        source_text: str,
        dropped: Dict[str, List[str]],
    ) -> List[str]:
        """按整体情感方向筛关键词，并逐个做幻觉校验。

        过滤顺序（先便宜后昂贵）：
            方向不符 → 长度越界 → 含标点 → 黑名单 → 原文溯源 → 去重 → 包含关系 → 限量
        """
        kept: List[str] = []
        seen: Set[str] = set()

        for item in items:
            word = str(item.get("word") or item.get("keyword") or "").strip()
            if not word:
                continue

            # 要求 1：方向必须与整体情感一致。
            # polarity 缺失时按"模型已按提示词只输出同向词"处理，不因缺字段误杀。
            polarity_raw = item.get("polarity", item.get("sentiment"))
            if polarity_raw is not None:
                polarity = self._tax.normalize_sentiment(polarity_raw)
                if polarity is not None and polarity is not overall:
                    dropped.setdefault("keyword_polarity", []).append(f"{word}({polarity.value})")
                    continue

            reason = self._keyword_reject_reason(word, source_text)
            if reason:
                dropped.setdefault(f"keyword_{reason}", []).append(word)
                continue

            normalized = word.strip()
            if normalized in seen:
                continue
            seen.add(normalized)
            kept.append(normalized)

        kept = self._drop_contained(kept)
        return kept[: self._cfg.keyword_max_count]

    def _keyword_reject_reason(self, word: str, source_text: str) -> str:
        """返回拒绝原因码；通过校验返回空串。"""
        cfg = self._cfg

        # 要求 4：禁止单字，禁止整句
        if len(word) < cfg.keyword_min_length:
            return "too_short"
        if len(word) > cfg.keyword_max_length:
            return "too_long"
        if _FORBIDDEN_IN_KEYWORD.search(word):
            return "punctuation"
        if _NON_INFORMATIVE.match(word):
            return "non_informative"
        if word.lower() in self._tax.keyword_blacklist:
            return "blacklist"

        # 要求 4：幻觉校验——关键词必须能在原文中找到依据
        if cfg.keyword_must_appear_in_content:
            level = self._match_level(word, source_text)
            if level == KeywordMatch.MISS:
                return "hallucination"
            if level != KeywordMatch.EXACT and not cfg.keyword_allow_subsequence_match:
                return "hallucination"
        return ""

    @staticmethod
    def _match_level(word: str, source_text: str) -> str:
        """判断关键词在原文中的匹配级别。

        三级由严到宽：
            EXACT       原文直接包含 —— 最可信
            SUBSEQUENCE 字符按序出现 —— 模型省略了中间的虚词（"排队很久"→"排队久"）
            CHARSET     字符全部出现但顺序不同 —— 模型做了同义重组（"排了很久队"→"排队久"）
        三级都不满足即判定为幻觉。
        """
        if not source_text:
            return KeywordMatch.MISS
        if word in source_text:
            return KeywordMatch.EXACT

        # 按序子序列
        pos = 0
        ordered = True
        for ch in word:
            idx = source_text.find(ch, pos)
            if idx < 0:
                ordered = False
                break
            pos = idx + 1
        if ordered:
            return KeywordMatch.SUBSEQUENCE

        # 字符集覆盖
        if all(ch in source_text for ch in word):
            return KeywordMatch.CHARSET
        return KeywordMatch.MISS

    @staticmethod
    def _drop_contained(words: List[str]) -> List[str]:
        """同时出现"排队"和"排队久"时只保留信息量更大的长词。"""
        if len(words) < 2:
            return words
        ordered = sorted(words, key=len, reverse=True)
        kept: List[str] = []
        for word in ordered:
            if any(word in longer for longer in kept):
                continue
            kept.append(word)
        # 还原成模型给出的原始顺序（模型按重要性排序，这个顺序有价值）
        return [w for w in words if w in set(kept)]

    # ============================================================ 要求 3：相关性
    def _apply_relevance(self, result: LabelResult) -> LabelResult:
        """与景区主体无关 → 强制中性，并清空维度与关键词。

        实体标签保留：它们是无情感色彩的事实抽取，留着对下游没有污染。
        """
        if not self._cfg.force_neutral_when_irrelevant or result.relevant:
            return result

        result.sentiment = Sentiment.NEUTRAL
        result.dimension_tags = []
        result.keyword_tags = []
        result.source = LabelSource.RULE_IRRELEVANT
        if not result.reason:
            result.reason = "与景区主体无关"
        return result

    # ============================================================ 仲裁
    @staticmethod
    def _majority_sentiment(dimensions: List[DimensionTag], *, fallback: Sentiment) -> Sentiment:
        """按维度情感多数票裁决整体情感；平票时负向优先（负面舆情不能被稀释）。"""
        counter = Counter(d.sentiment for d in dimensions)
        if not counter:
            return fallback
        top = max(counter.values())
        winners = [score for score, count in counter.items() if count == top]
        if len(winners) == 1:
            return Sentiment.from_score(winners[0])
        if -1 in winners:
            return Sentiment.NEGATIVE
        if 1 in winners:
            return Sentiment.POSITIVE
        return Sentiment.NEUTRAL

    # ============================================================ 兜底结果
    def neutral_result(
        self,
        record: CommentRecord,
        *,
        source: LabelSource,
        reason: str,
    ) -> LabelResult:
        """构造一个"中性 + 全空"的兜底结果。

        用于两种场景：
            - 清洗判定为无效内容（纯表情/纯符号），根本不调模型
            - 模型多次失败，不能让这条评论卡在队列里无限重试
        """
        return LabelResult(
            comment_id=record.comment_id,
            sentiment=Sentiment.NEUTRAL,
            dimension_tags=[],
            entity_tags=[],
            keyword_tags=[],
            confidence=0.0,
            source=source,
            relevant=False,
            reason=reason,
        )
