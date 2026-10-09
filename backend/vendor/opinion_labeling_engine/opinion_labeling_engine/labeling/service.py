"""标注服务：单条评论的完整标注流程编排。

    clean → (无效则直接中性返回) → build prompt → call LLM
          → parse → (解析失败则修复轮重试) → postprocess → LabelResult

这一层是纯业务编排，不碰队列、不碰数据库，因此可以被单测直接调用，
也可以被 HTTP 接口、离线脚本等任意入口复用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from ..config import AppConfig
from ..domain.enums import LabelSource
from ..domain.models import CommentRecord, LabelResult
from ..domain.taxonomy import Taxonomy
from ..llm.client import ArkClient, LLMError
from ..llm.parser import ParseError, parse_label_output
from ..llm.prompt import PromptBuilder
from ..logging_conf import get_logger
from ..preprocess.cleaner import ContentCleaner
from .postprocess import LabelPostProcessor

__all__ = ["LabelingService", "LabelingStats", "LabelingFailed"]

logger = get_logger(__name__)


class LabelingFailed(RuntimeError):
    """模型没能给出可用的标注结果。

    抛出它意味着**这条评论这次没标成**，不能写库：
    写个假中性会把行占掉，以后既认不出它是失败的，下游指标还会把它
    当成一条真实的中性评价。调用方应该把它转入失败池，等后续重标。

    注意区分——下面这两种**不是**失败，它们是有效的标注结论，照常写库：
        - 纯表情/纯符号等无效内容  → 中性（source=invalid）
        - 与景区主体无关            → 中性（source=irrelevant）
    """

    def __init__(self, comment_id: str, reason: str) -> None:
        super().__init__(f"评论 {comment_id} 标注失败：{reason}")
        self.comment_id = comment_id
        self.reason = reason


@dataclass
class LabelingStats:
    """进程内累计统计，线程安全自增，供日志与监控使用。"""

    total: int = 0
    labeled: int = 0
    skipped_invalid: int = 0
    irrelevant: int = 0
    parse_repaired: int = 0
    failed: int = 0
    low_confidence: int = 0
    failed_stored: int = 0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def incr(self, field: str, delta: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + delta)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total": self.total,
                "labeled": self.labeled,
                "skipped_invalid": self.skipped_invalid,
                "irrelevant": self.irrelevant,
                "parse_repaired": self.parse_repaired,
                "failed": self.failed,
                "low_confidence": self.low_confidence,
                "failed_stored": self.failed_stored,
            }


class LabelingService:
    """单条评论标注。线程安全，可被多个 worker 共享。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        taxonomy: Optional[Taxonomy] = None,
        client: Optional[ArkClient] = None,
    ) -> None:
        self._cfg = cfg
        self.taxonomy = taxonomy or Taxonomy.load(cfg.taxonomy_path)
        self._cleaner = ContentCleaner(cfg.cleaning)
        self._prompt = PromptBuilder(
            self.taxonomy,
            keyword_min=cfg.labeling.keyword_min_length,
            keyword_max=cfg.labeling.keyword_max_length,
            keyword_max_count=cfg.labeling.keyword_max_count,
            dimension_max=cfg.labeling.dimension_max_count,
            with_fewshot=cfg.llm.with_fewshot,
            fewshot_style=cfg.llm.fewshot_style,
            system_role=cfg.llm.system_role,
        )
        self._post = LabelPostProcessor(self.taxonomy, cfg.labeling)
        self._client = client or ArkClient(cfg.llm)
        self.stats = LabelingStats()

    # ------------------------------------------------------------------ 主流程
    def label(self, record: CommentRecord) -> LabelResult:
        """标注一条评论。

        Args:
            record: 待标注评论。

        Returns:
            :class:`LabelResult`，``source`` 字段说明结果是怎么来的。
            返回即表示这是一个**可以写库的结论**（含无效内容与景区无关两种中性）。

        Raises:
            LabelingFailed: 模型调用或输出解析失败，这条没标成，不该写库。
                配置 ``labeling.write_on_model_failure: true`` 可退回老行为
                （返回中性兜底而不抛）。
        """
        self.stats.incr("total")

        # --- 第一道闸：清洗 --------------------------------------------------
        cleaned = self._cleaner.clean(record.content)
        if not cleaned.valid:
            self.stats.incr("skipped_invalid")
            logger.debug("评论 %s 判定为无效内容（%s），直接中性落库",
                         record.comment_id, cleaned.reason)
            return self._post.neutral_result(
                record,
                source=LabelSource.RULE_INVALID,
                reason=f"无效内容:{cleaned.reason}",
            )

        # --- 第二道闸：大模型 ------------------------------------------------
        try:
            result = self._label_with_llm(record, cleaned)
        except (LLMError, ParseError) as exc:
            self.stats.incr("failed")
            if self._cfg.labeling.write_on_model_failure:
                logger.error("评论 %s 标注失败，按配置走中性兜底写库：%s",
                             record.comment_id, exc)
                return self._post.neutral_result(
                    record,
                    source=LabelSource.FALLBACK,
                    reason=f"模型失败:{type(exc).__name__}",
                )
            logger.error("评论 %s 标注失败，不写库，转入失败池：%s", record.comment_id, exc)
            raise LabelingFailed(record.comment_id, f"{type(exc).__name__}: {exc}") from exc

        # --- 统计 ------------------------------------------------------------
        if result.source is LabelSource.RULE_IRRELEVANT:
            self.stats.incr("irrelevant")
        else:
            self.stats.incr("labeled")
        if 0 < result.confidence < self._cfg.labeling.low_confidence_threshold:
            self.stats.incr("low_confidence")
            logger.info("评论 %s 置信度 %.2f 低于阈值，建议人工复核",
                        record.comment_id, result.confidence)

        if self._cfg.logging.log_content:
            logger.debug("原文：%s", cleaned.text)
        logger.debug(result.summary())
        return result

    # ------------------------------------------------------------------ 模型轮
    def _label_with_llm(self, record: CommentRecord, cleaned) -> LabelResult:
        """调用模型并解析；解析失败时追加一次"修复轮"。

        Raises:
            LLMError: 客户端重试耗尽。
            ParseError: 修复轮之后仍然无法解析。
        """
        messages = self._prompt.build_messages(record, cleaned)
        response = self._client.complete(messages)

        try:
            raw = parse_label_output(response.text)
        except ParseError as first_error:
            logger.warning("评论 %s 输出解析失败，进入修复轮：%s",
                           record.comment_id, first_error)
            repair_messages = self._prompt.build_repair_messages(
                record, cleaned, response.text, str(first_error)
            )
            repair = self._client.complete(repair_messages)
            raw = parse_label_output(repair.text)     # 再失败就抛出去，由 label() 兜底
            self.stats.incr("parse_repaired")
            response = repair

        return self._post.build(
            record,
            raw,
            source_text=cleaned.text,
            model=response.model,
            latency_ms=response.latency_ms,
        )

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LabelingService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
