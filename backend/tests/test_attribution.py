# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""归因明细（attribution_details）：采集侧的对齐、导出、建列、写库映射。不连库。"""
from __future__ import annotations

import json

from app.core import attribution
from app.db import tables


def test_align_follows_keyword_tags():
    old = json.dumps([{"word": "一般", "reason": "态度平淡"}, {"word": "被删的", "reason": "x"}],
                     ensure_ascii=False)
    got = attribution.align(["一般", "人工加的", "海蓝星"], old,
                            default_reason=attribution.HUMAN_REASON, mark_words=["海蓝星"])
    assert got == [
        {"word": "一般", "reason": "态度平淡"},                       # 留下的词原因不变
        {"word": "人工加的", "reason": attribution.HUMAN_REASON},     # 新加的
        {"word": "海蓝星", "reason": attribution.MARK_WORD_REASON},   # 标注字
    ]                                                                # 删掉的词跟着没了


def test_loads_tolerates_dirty_values():
    assert attribution.loads(None) == []
    assert attribution.loads("") == []
    assert attribution.loads("不是json") == []
    assert attribution.loads('{"word":"a"}') == []
    assert attribution.loads('[1, {"word": ""}, {"word": "a"}]') == [{"word": "a", "reason": ""}]
    assert attribution.dumps([]) is None
    assert attribution.dumps([{"word": "一般", "reason": "平淡"}]) == '[{"word": "一般", "reason": "平淡"}]'


def test_mark_reason_matches_engine():
    from app.labeling import config_bridge
    config_bridge.ensure_on_path()
    from opinion_labeling_engine.labeling.scenic_rules import MARK_WORD_REASON
    assert attribution.MARK_WORD_REASON == MARK_WORD_REASON


def test_column_registered_and_mapped_for_engine():
    cols = {(t, c): d for t, c, d in tables.EXTRA_COLUMNS}
    definition = cols[(tables.COMMENTS, "attribution_details")]
    assert definition.startswith("LONGTEXT") and "COMMENT" in definition

    from app.core.config import load_config
    from app.labeling import config_bridge
    engine_cfg = config_bridge.build_engine_config(load_config())
    assert engine_cfg["storage"]["columns"]["attribution_details"] == "attribution_details"


def test_engine_writes_attribution_through_column_mapping():
    """引擎的 UPDATE 按 storage.columns 拼：配了映射就写这一列，值和 keyword_tags 对齐。"""
    from app.labeling import config_bridge
    config_bridge.ensure_on_path()
    from opinion_labeling_engine.config import StorageConfig
    from opinion_labeling_engine.domain.models import CommentRecord, LabelResult
    from opinion_labeling_engine.storage.repository import LabelRepository

    cfg = StorageConfig(table=tables.COMMENTS, columns={
        "sentiment_label": "sentiment_label", "sentiment_score": "sentiment_score",
        "dimension_tags": "dimension_tags", "entity_tags": "entity_tags",
        "keyword_tags": "keyword_tags", "attribution_details": "attribution_details"})
    sql, order = LabelRepository._build_update_sql(cfg)
    assert "`attribution_details` = %s" in sql
    repo = LabelRepository.__new__(LabelRepository)
    repo._cfg, repo._value_order = cfg, order
    record = CommentRecord.from_dict({"comment_id": "c1", "channel": "ctrip", "scenic_id": "S1"})
    result = LabelResult(comment_id="c1", keyword_tags=["一般"],
                         attribution_details=[{"word": "一般", "reason": "态度平淡"}])
    values = dict(zip(order, repo._params(record, result)))
    assert json.loads(values["attribution_details"]) == [{"word": "一般", "reason": "态度平淡"}]


def test_export_aligns_with_exported_keywords():
    from app.utils.exporter import COMMENTS_HEADERS, _export_value
    assert COMMENTS_HEADERS["attribution_details"] == "归因明细"
    row = {"keyword_tags": '["一般"]', "mark_word_tags": '["海蓝星"]',
           "attribution_details": '[{"word": "一般", "reason": "态度平淡"}]'}
    assert _export_value(tables.COMMENTS, row, "attribution_details") == [
        {"word": "一般", "reason": "态度平淡"},
        {"word": "海蓝星", "reason": attribution.MARK_WORD_REASON},
    ]
    assert _export_value(tables.COMMENTS, {"keyword_tags": None}, "attribution_details") is None


def test_result_to_dict_carries_attribution():
    from types import SimpleNamespace
    from app.labeling.manager import _result_to_dict
    res = SimpleNamespace(keyword_tags=["一般"], attribution_details=[{"word": "一般", "reason": "平淡"}])
    assert _result_to_dict(res)["attribution_details"] == [{"word": "一般", "reason": "平淡"}]
    old_engine = SimpleNamespace(keyword_tags=["一般"])          # 老引擎没有这个字段
    assert _result_to_dict(old_engine)["attribution_details"] == [{"word": "一般", "reason": ""}]
