"""单测公共夹具。

所有测试都不连真实的 Redis / MySQL / 大模型：
    - 队列用 memory 后端
    - 模型用 FakeArkClient（按评论内容返回预置 JSON）
    - 数据库用 RecordingRepository（把写回内容记在内存里）
这样单测可以在 CI 里裸跑。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opinion_labeling_engine.config import DEFAULT_TAXONOMY_PATH, load_config  # noqa: E402
from opinion_labeling_engine.domain.models import CommentRecord, LabelResult   # noqa: E402
from opinion_labeling_engine.domain.taxonomy import Taxonomy                   # noqa: E402
from opinion_labeling_engine.llm.client import LLMResponse                     # noqa: E402
from opinion_labeling_engine.storage.failure_store import MemoryFailureStore   # noqa: E402


@pytest.fixture(scope="session")
def taxonomy() -> Taxonomy:
    return Taxonomy.load(DEFAULT_TAXONOMY_PATH)


@pytest.fixture()
def cfg():
    """不校验数据库的配置对象。"""
    return load_config(require_db=False)


class FakeArkClient:
    """假的模型客户端：按注册的「内容片段 → 输出文本」返回。

    未命中任何规则时返回 ``default``，用来测试解析失败与兜底路径。
    """

    def __init__(self, rules: Sequence[Tuple[str, str]] | None = None,
                 default: str = '{"relevant": true, "sentiment": "中性", "confidence": 0.5,'
                                ' "dimensions": [], "entities": [], "keywords": [], "reason": ""}'):
        self.rules: List[Tuple[str, str]] = list(rules or [])
        self.default = default
        self.calls: List[List[Dict[str, Any]]] = []

    def complete(self, messages, **_kwargs) -> LLMResponse:
        self.calls.append(messages)
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_text = msg["content"][0]["text"]
                break
        for needle, output in self.rules:
            if needle in user_text:
                return LLMResponse(text=output, model="fake-model", latency_ms=1)
        return LLMResponse(text=self.default, model="fake-model", latency_ms=1)

    def close(self) -> None:
        pass


class RecordingRepository:
    """把写回内容记在内存里的假仓储。"""

    def __init__(self) -> None:
        self.rows: List[Tuple[CommentRecord, LabelResult]] = []
        self.failed_marks: List[str] = []
        self.fail_times = 0

    def update_many(self, pairs, review_flags=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟写库失败")
        self.rows.extend(pairs)
        return None

    def update_one(self, record, result, review_flag: int = 0):
        self.rows.append((record, result))
        return True

    def mark_failed(self, records):
        """标注失败只置标记位，不写标注字段——测试里记下被标记的评论。"""
        self.failed_marks.extend(r.comment_id for r in records)
        return None

    def ensure_schema(self) -> None:
        pass

    def by_id(self, comment_id: str):
        for record, result in self.rows:
            if record.comment_id == comment_id:
                return result
        return None


@pytest.fixture()
def fake_client_factory():
    return FakeArkClient


@pytest.fixture()
def recording_repo():
    return RecordingRepository()


@pytest.fixture()
def failure_store(cfg):
    """单测一律用内存失败池——默认配置是 redis，跑单测时不该去连它。"""
    return MemoryFailureStore(cfg.failure_store)


def make_record(comment_id: str = "c1", content: str = "", **kwargs) -> CommentRecord:
    payload = {"comment_id": comment_id, "content": content}
    payload.update(kwargs)
    return CommentRecord.from_dict(payload)
