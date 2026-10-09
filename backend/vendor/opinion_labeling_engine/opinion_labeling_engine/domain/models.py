"""领域模型：进出这套引擎的数据结构。

设计原则：
    - ``CommentRecord`` 只装「调用方给我的原始字段」，不含任何标注结果字段。
    - ``LabelResult`` 只装「我要写回去的五个字段 + 溯源信息」。
    - 两者都可 JSON 序列化，因为它们要在队列里穿行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .enums import LabelSource, Sentiment

__all__ = ["CommentRecord", "DimensionTag", "EntityTag", "LabelResult"]


def _as_str(value: Any) -> str:
    """把任意输入安全地转成字符串，None 转空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


@dataclass
class CommentRecord:
    """一条待标注的评论。

    **调用方入参（六个字段）**::

        {
            "channel":     "ctrip",              # 渠道       ★ 写回主键
            "work_id":     "76471",              # 作品ID       随记录带上，便于下钻回溯
            "scenic_id":   "PFTSCA01009835",     # 景区ID     ★ 写回主键
            "scenic_name": "天山天池",            # 景区名称     判定"是否与景区相关"的依据
            "comment_id":  "805353431",          # 评论ID     ★ 写回主键
            "content":     "完美的一天",          # 评论内容     标注对象
        }

    带 ★ 的三个字段构成写回主键（对应 ``config.yaml`` 的 ``storage.key_columns``），
    任一为空都无法定位到目标行，引擎在**入队时**就会拒绝，
    而不是等到 UPDATE 影响 0 行才发现。

    ``scenic_name`` 与 ``work_id`` 不是主键，缺失只告警不拦截：
    前者缺了模型判"是否与景区相关"会明显变差（"天池的水真清"会被误判成无关），
    后者只影响日志与后续下钻定位，不影响标注结果。

    其余字段（likes / extra_content / publish_time / commenter_name 等）全部可选，
    给了会作为辅助上下文提高准确率，不给也不影响标注。
    """

    comment_id: str
    content: str = ""
    scenic_id: str = ""
    scenic_name: str = ""
    channel: str = ""
    work_id: str = ""
    comment_level: str = ""
    comment_parent_id: str = ""
    commenter_id: str = ""
    commenter_name: str = ""
    image_list: str = ""
    video_list: str = ""
    location: str = ""
    likes: int = 0
    extra_content: str = ""
    publish_time: str = ""
    crawl_time: str = ""
    root_comment_id: str = ""
    sub_comment_count: int = 0
    task_id: str = ""

    #: 这条评论已经因为标注失败被重投过几轮。**不是表字段**，只在队列与失败池里流转，
    #: 用来让"累计失败次数"跨重标轮次存活——否则每轮重标都从 1 开始数，
    #: 永远看不出哪条是怎么标都标不出来的钉子户。
    retry_count: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommentRecord":
        """从 dict（CSV 行 / DB 行 / HTTP body）构造，未知字段直接忽略。

        Raises:
            ValueError: ``comment_id`` 缺失——没有主键就无法写回，属于调用方错误。
        """
        comment_id = _as_str(data.get("comment_id")).strip()
        if not comment_id:
            raise ValueError("CommentRecord 缺少 comment_id，无法定位写回目标行")

        def _int(key: str) -> int:
            raw = data.get(key)
            try:
                return int(raw) if raw not in (None, "") else 0
            except (TypeError, ValueError):
                return 0

        return cls(
            comment_id=comment_id,
            content=_as_str(data.get("content")),
            scenic_id=_as_str(data.get("scenic_id")).strip(),
            scenic_name=_as_str(data.get("scenic_name")).strip(),
            channel=_as_str(data.get("channel")).strip(),
            work_id=_as_str(data.get("work_id")),
            comment_level=_as_str(data.get("comment_level")),
            comment_parent_id=_as_str(data.get("comment_parent_id")),
            commenter_id=_as_str(data.get("commenter_id")),
            commenter_name=_as_str(data.get("commenter_name")),
            image_list=_as_str(data.get("image_list")),
            video_list=_as_str(data.get("video_list")),
            location=_as_str(data.get("location")),
            likes=_int("likes"),
            extra_content=_as_str(data.get("extra_content")),
            publish_time=_as_str(data.get("publish_time")),
            crawl_time=_as_str(data.get("crawl_time")),
            root_comment_id=_as_str(data.get("root_comment_id")),
            sub_comment_count=_int("sub_comment_count"),
            task_id=_as_str(data.get("task_id")),
            retry_count=_int("retry_count"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # ---- 送给模型的辅助上下文 ------------------------------------------------
    def extra_hints(self) -> Dict[str, Any]:
        """从 ``extra_content`` JSON 里挑出对标注有价值的结构化信息。

        携程的 extra_content 里有 score / landscape_score / tourist_type 等，
        它们能显著提高短评（如「完美的一天」）的情感判定准确率。
        解析失败一律返回空 dict，绝不因为脏数据阻断主流程。
        """
        if not self.extra_content:
            return {}
        try:
            payload = json.loads(self.extra_content)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}

        wanted = ("score", "landscape_score", "fun_score", "price_quality_score", "tourist_type")
        hints: Dict[str, Any] = {}
        for key in wanted:
            value = payload.get(key)
            if value not in (None, "", 0):
                hints[key] = value
        return hints

    def has_media(self) -> bool:
        """是否带图/带视频——纯表情评论若带图，仍属于有效互动，但不影响标注口径。"""
        return bool(self.image_list) or bool(self.video_list)


@dataclass
class DimensionTag:
    """一个维度标签，对应 ``dimension_tags`` 数组的一个元素。"""

    dim1: str
    dim2: str = ""
    dim3: str = ""
    sentiment: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """落库结构与线上样例一致：``{"dim1","dim2","dim3","sentiment"}``。"""
        return {"dim1": self.dim1, "dim2": self.dim2, "dim3": self.dim3, "sentiment": self.sentiment}

    @property
    def path(self) -> tuple:
        return (self.dim1, self.dim2, self.dim3)


@dataclass
class EntityTag:
    """一个实体标签，对应 ``entity_tags`` 数组的一个元素。"""

    type: str
    value: str

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "value": self.value}


@dataclass
class LabelResult:
    """一条评论的标注结果——即将写回源表的五个字段。"""

    comment_id: str
    sentiment: Sentiment = Sentiment.NEUTRAL
    dimension_tags: List[DimensionTag] = field(default_factory=list)
    entity_tags: List[EntityTag] = field(default_factory=list)
    keyword_tags: List[str] = field(default_factory=list)

    # ---- 以下字段不落源表，用于监控、复核与问题回溯 ----------------------------
    confidence: float = 0.0
    source: LabelSource = LabelSource.LLM
    relevant: bool = True
    reason: str = ""
    model: str = ""
    latency_ms: int = 0
    dropped: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def sentiment_label(self) -> str:
        return self.sentiment.value

    @property
    def sentiment_score(self) -> int:
        return self.sentiment.score

    @property
    def need_review(self) -> bool:
        """低置信度需人工复核——由调用方对比阈值后设置 ``confidence`` 即可判断。"""
        return self.source is LabelSource.LLM and self.confidence > 0

    def dimension_json(self, *, ensure_ascii: bool = False) -> str:
        return json.dumps([d.to_dict() for d in self.dimension_tags], ensure_ascii=ensure_ascii)

    def entity_json(self, *, ensure_ascii: bool = False) -> str:
        return json.dumps([e.to_dict() for e in self.entity_tags], ensure_ascii=ensure_ascii)

    def keyword_json(self, *, ensure_ascii: bool = False) -> str:
        return json.dumps(list(self.keyword_tags), ensure_ascii=ensure_ascii)

    def to_row(self, *, ensure_ascii: bool = False) -> Dict[str, Any]:
        """转成写库用的列值字典（键是逻辑名，由 repository 映射到物理列名）。"""
        return {
            "sentiment_label": self.sentiment_label,
            "sentiment_score": self.sentiment_score,
            "dimension_tags": self.dimension_json(ensure_ascii=ensure_ascii),
            "entity_tags": self.entity_json(ensure_ascii=ensure_ascii),
            "keyword_tags": self.keyword_json(ensure_ascii=ensure_ascii),
        }

    def summary(self) -> str:
        """一行日志摘要。"""
        dims = "/".join(f"{d.dim1}-{d.dim2}:{d.sentiment}" for d in self.dimension_tags) or "-"
        kws = ",".join(self.keyword_tags) or "-"
        return (
            f"[{self.comment_id}] {self.sentiment_label}({self.sentiment_score}) "
            f"conf={self.confidence:.2f} src={self.source.value} dims={dims} kw={kws}"
        )
