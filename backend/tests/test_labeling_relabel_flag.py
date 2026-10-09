"""「AI 再标注」必须把 label_review_flag 一起写回。

这是实跑抓到的一个安静的错：页面上点「AI 再标注」，五个标注字段
都更新了，复核标记却退回 **0（未标注）**——这条评论在审核页上又变成
"从没标过"，点多少次都一样，而接口返回一切正常。

根因在引擎里（不改它）：`label_sync(row, write=True)` 内部调的是
`repository.update_one(record, result)`，review_flag 用默认值 0。
只有队列那条路（worker/consumer.py）会先算好 flag 再写。
所以 smc 这一层必须自己算 flag 并显式传进去。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.labeling.manager import LabelingManager


class _FakeRepo:
    def __init__(self):
        self.calls = []

    def update_one(self, record, result, *, review_flag=0):
        self.calls.append({"record": record, "result": result, "review_flag": review_flag})
        return True


class _FakeService:
    def __init__(self, result):
        self._result = result

    def label(self, record):
        return self._result


def _manager_with(engine) -> LabelingManager:
    mgr = LabelingManager.__new__(LabelingManager)
    mgr._engine = engine
    return mgr


def _engine(*, flags=None, threshold=0.6, result=None):
    repo = _FakeRepo()
    cfg = SimpleNamespace(
        storage=SimpleNamespace(
            review_flags=flags if flags is not None
            else {"ai_success": 4, "ai_failed": 5, "ai_low_confidence": 6}),
        labeling=SimpleNamespace(low_confidence_threshold=threshold),
    )
    return SimpleNamespace(
        cfg=cfg, repository=repo, service=_FakeService(result),
        _to_record=lambda row: SimpleNamespace(comment_id=row.get("comment_id", "c1")),
    )


def _result(*, source, confidence):
    return SimpleNamespace(
        source=source, confidence=confidence,
        sentiment_label="负向", sentiment_score=-1,
        dimension_tags=[], entity_tags=[], keyword_tags=[],
    )


def _llm_source():
    from app.labeling import config_bridge

    config_bridge.ensure_on_path()
    from opinion_labeling_engine.domain.enums import LabelSource

    return LabelSource


def test_relabel_writes_ai_success_flag():
    """置信度达标 → 写 4（AI标注成功），**不是 0**。"""
    src = _llm_source()
    eng = _engine(result=_result(source=src.LLM, confidence=0.95))
    mgr = _manager_with(eng)

    out = mgr._relabel_sync({"comment_id": "c1"})

    assert eng.repository.calls, "根本没写库"
    flag = eng.repository.calls[0]["review_flag"]
    assert flag == 4, f"复核标记写成了 {flag}，应为 4"
    assert out["label_review_flag"] == 4, "返回给前端的也要带上 flag"


def test_relabel_writes_low_confidence_flag():
    """置信度低于阈值 → 写 6（未人工复核），交给人看一眼。"""
    src = _llm_source()
    eng = _engine(result=_result(source=src.LLM, confidence=0.2), threshold=0.6)

    _manager_with(eng)._relabel_sync({"comment_id": "c2"})

    assert eng.repository.calls[0]["review_flag"] == 6


def test_relabel_never_writes_zero():
    """⚠️ 兜底：无论规则怎么变，都**不能**写 0。

    0 的语义是"从没标过"。刚标完的评论写 0，在审核页上就查不出来，
    一键补标也会把它当未标注的再标一遍——无限循环。
    """
    src = _llm_source()
    for conf in (0.0, 0.01, 0.5, 0.6, 1.0):
        eng = _engine(result=_result(source=src.LLM, confidence=conf))
        _manager_with(eng)._relabel_sync({"comment_id": "c3"})
        flag = eng.repository.calls[0]["review_flag"]
        assert flag != 0, f"confidence={conf} 时写了 0"
        assert flag in (4, 5, 6), f"confidence={conf} 时写了 {flag}，引擎只该写 4/5/6"


def test_relabel_flag_follows_engine_rule_not_a_copy():
    """flag 的档位取值跟着**引擎配置**走，不是 smc 这边写死的常量。

    引擎以后把 ai_success 改成别的值，这里必须跟着变——
    两边各写一份迟早漂。
    """
    src = _llm_source()
    eng = _engine(flags={"ai_success": 41, "ai_failed": 51, "ai_low_confidence": 61},
                  result=_result(source=src.LLM, confidence=0.95))

    _manager_with(eng)._relabel_sync({"comment_id": "c4"})

    assert eng.repository.calls[0]["review_flag"] == 41


def test_relabel_survives_missing_repository():
    """引擎没开数据库（enable_db=False）时不该炸，只是不写库。"""
    src = _llm_source()
    eng = _engine(result=_result(source=src.LLM, confidence=0.9))
    eng.repository = None

    out = _manager_with(eng)._relabel_sync({"comment_id": "c5"})

    assert out["label_review_flag"] == 4
