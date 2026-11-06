# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""归因明细（attribution_details）：每个关键词「为什么提取它」，和 keyword_tags 一一对应。"""

from __future__ import annotations

import json

import pytest

from opinion_labeling_engine.config import LabelingConfig
from opinion_labeling_engine.domain.models import LabelResult, align_attribution
from opinion_labeling_engine.labeling import scenic_rules
from opinion_labeling_engine.labeling.postprocess import LabelPostProcessor
from opinion_labeling_engine.labeling.service import LabelingService
from opinion_labeling_engine.llm.parser import RawLabel, parse_label_output
from opinion_labeling_engine.llm.prompt import PromptBuilder

from conftest import FakeArkClient, make_record


@pytest.fixture()
def post(taxonomy) -> LabelPostProcessor:
    return LabelPostProcessor(taxonomy, LabelingConfig())


def test_reasons_follow_the_kept_keywords_only(post):
    record = make_record(content="风景很美，但是排队太久了，总体还是值得")
    raw = RawLabel(sentiment="正向", confidence=0.8, keywords=[
        {"word": "风景很美", "polarity": "正向", "reason": "夸景色美"},
        {"word": "排队太久", "polarity": "负向", "reason": "抱怨排队"},     # 方向不符被过滤
        {"word": "还是值得", "polarity": "正向"},                           # 模型漏写 reason
    ])
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["风景很美", "还是值得"]
    assert result.attribution_details == [
        {"word": "风景很美", "reason": "夸景色美"},
        {"word": "还是值得", "reason": ""},
    ]
    row = result.to_row()
    assert json.loads(row["attribution_details"]) == result.attribution_details
    assert [d["word"] for d in json.loads(row["attribution_details"])] == json.loads(row["keyword_tags"])


def test_irrelevant_clears_attribution(post):
    record = make_record(content="链接放评论区了")
    raw = RawLabel(relevant=False, sentiment="正向",
                   keywords=[{"word": "链接放评论区", "polarity": "正向", "reason": "x"}])
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == [] and result.attribution_details == []
    assert result.to_row()["attribution_details"] == "[]"


def test_align_is_driven_by_keyword_tags():
    # 关键词被人改过 / 补过：明细永远跟着 keyword_tags 走，缺原因的用默认值，多余的丢掉
    details = [{"word": "一般", "reason": "态度平淡"}, {"word": "多余", "reason": "?"}, "脏数据"]
    assert align_attribution(["一般", "新加的"], details, default_reason="人工") == [
        {"word": "一般", "reason": "态度平淡"}, {"word": "新加的", "reason": "人工"}]
    long = [{"word": "a", "reason": "很" * 200}]
    assert len(align_attribution(["a"], long)[0]["reason"]) == 60
    assert LabelResult(comment_id="x", keyword_tags=["a"]).attribution_json() == '[{"word": "a", "reason": ""}]'


def test_parser_keeps_reason_field():
    raw = parse_label_output('{"sentiment":"中性","keywords":[{"word":"一般","polarity":"中性","reason":"态度平淡"}]}')
    assert raw.keywords[0]["reason"] == "态度平淡"


def test_mark_words_get_their_own_reason(cfg):
    out = ('{"relevant":true,"sentiment":"正向","confidence":0.8,"dimensions":[],"entities":[],'
           '"keywords":[{"word":"海豹表演好看","polarity":"正向","reason":"夸表演"}],"reason":""}')
    service = LabelingService(cfg, client=FakeArkClient([("海豹表演", out)]))
    scenic_rules.set_rules({"S1": scenic_rules.ScenicRule(mark_words=("海蓝星", "海豹表演好看"))})
    try:
        result = service.label(make_record(content="海蓝星的海豹表演好看", scenic_id="S1"))
    finally:
        scenic_rules.set_rules({})
    assert result.keyword_tags == ["海豹表演好看", "海蓝星"]
    assert result.attribution_details == [
        {"word": "海豹表演好看", "reason": "夸表演"},                    # AI 已经提了的，原因不覆盖
        {"word": "海蓝星", "reason": scenic_rules.MARK_WORD_REASON},
    ]


def test_prompt_asks_for_reason_and_neutral_boundaries(taxonomy):
    prompt = PromptBuilder(taxonomy).system_prompt
    assert '"reason": ""}]' in prompt                  # 输出契约里关键词带 reason
    assert "R8" in prompt and "为什么提取这个词" in prompt
    assert "累死我了" in prompt and "一般，空气不错" in prompt
    # 少样本里每个关键词都写了 reason，模型才学得会
    from opinion_labeling_engine.llm.prompt import _FEWSHOT
    for shot in _FEWSHOT:
        for kw in shot["output"]["keywords"]:
            assert kw.get("reason"), kw
