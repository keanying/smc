"""情感枚举与处理状态枚举。

情感字面值以线上样例数据为准（正向 / 中性 / 负向），
需求文档里的「正面 / 负面」写法通过 taxonomy.yaml 的 aliases 归一。
"""

from __future__ import annotations

from enum import Enum


class Sentiment(str, Enum):
    """三级情感。``value`` 是落库的 ``sentiment_label``。"""

    POSITIVE = "正向"
    NEUTRAL = "中性"
    NEGATIVE = "负向"

    @property
    def score(self) -> int:
        """落库的 ``sentiment_score``：1 / 0 / -1。"""
        return {"正向": 1, "中性": 0, "负向": -1}[self.value]

    @classmethod
    def from_score(cls, score: int) -> "Sentiment":
        return {1: cls.POSITIVE, 0: cls.NEUTRAL, -1: cls.NEGATIVE}[int(score)]


class LabelSource(str, Enum):
    """标注结果的来源，用于监控与问题回溯。"""

    LLM = "llm"                 # 正常走大模型
    RULE_INVALID = "invalid"    # 纯表情/纯符号等无效内容，规则直接判中性
    RULE_IRRELEVANT = "irrelevant"  # 模型判定与景区主体无关，强制中性
    FALLBACK = "fallback"       # 模型多次失败后的兜底中性


class TaskStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    DEAD = "dead"
