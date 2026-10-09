"""输出提取单测：模型输出再脏也要能抠出 JSON，抠不出必须显式报错。"""

from __future__ import annotations

import json

import pytest

from opinion_labeling_engine.llm.client import extract_output_text
from opinion_labeling_engine.llm.parser import ParseError, extract_json_object, parse_label_output

GOOD = {
    "relevant": True,
    "sentiment": "负向",
    "confidence": 0.9,
    "dimensions": [{"dim1": "游玩体验", "dim2": "排队时长", "dim3": "", "sentiment": "负向"}],
    "entities": [{"type": "票务类型", "value": "门票"}],
    "keywords": [{"word": "排队久", "polarity": "负向"}],
    "reason": "抱怨排队",
}
GOOD_TEXT = json.dumps(GOOD, ensure_ascii=False)


def test_plain_json():
    assert extract_json_object(GOOD_TEXT)["sentiment"] == "负向"


def test_markdown_fence():
    wrapped = f"```json\n{GOOD_TEXT}\n```"
    assert extract_json_object(wrapped)["sentiment"] == "负向"


def test_leading_explanation():
    noisy = f"好的，我的标注结果如下：\n{GOOD_TEXT}\n希望有帮助。"
    assert extract_json_object(noisy)["sentiment"] == "负向"


def test_braces_inside_string_do_not_break_scanning():
    payload = '前言 {"sentiment": "正向", "reason": "他说{很好}啊"} 后记'
    assert extract_json_object(payload)["reason"] == "他说{很好}啊"


def test_trailing_comma_and_smart_quotes_are_repaired():
    broken = '{"sentiment": "正向", "confidence": 0.8,}'
    assert extract_json_object(broken)["sentiment"] == "正向"


def test_single_element_array_is_unwrapped():
    assert extract_json_object(f"[{GOOD_TEXT}]")["sentiment"] == "负向"


def test_wrapped_in_result_key():
    payload = json.dumps({"result": GOOD}, ensure_ascii=False)
    parsed = parse_label_output(payload)
    assert parsed.sentiment == "负向"
    assert parsed.dimensions[0]["dim2"] == "排队时长"


def test_empty_output_raises():
    with pytest.raises(ParseError):
        extract_json_object("")


def test_no_json_raises():
    with pytest.raises(ParseError):
        extract_json_object("我觉得这条评论挺负面的。")


def test_missing_sentiment_raises():
    with pytest.raises(ParseError):
        parse_label_output('{"dimensions": []}')


def test_keywords_as_plain_string_list():
    payload = '{"sentiment": "正向", "keywords": ["风景优美", "服务好"]}'
    parsed = parse_label_output(payload)
    assert [k["word"] for k in parsed.keywords] == ["风景优美", "服务好"]


def test_confidence_is_clamped():
    parsed = parse_label_output('{"sentiment": "正向", "confidence": 3.5}')
    assert parsed.confidence == 1.0
    parsed = parse_label_output('{"sentiment": "正向", "confidence": "高"}')
    assert parsed.confidence == 0.0


def test_relevant_accepts_chinese():
    assert parse_label_output('{"sentiment": "中性", "relevant": "否"}').relevant is False
    assert parse_label_output('{"sentiment": "中性", "relevant": "是"}').relevant is True


# --------------------------------------------------------------------- 响应体
def test_extract_output_text_responses_protocol():
    body = {
        "output": [
            {"type": "reasoning", "content": [{"type": "text", "text": "先想一想"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": GOOD_TEXT}]},
        ]
    }
    assert extract_output_text(body) == GOOD_TEXT


def test_extract_output_text_top_level_field():
    assert extract_output_text({"output_text": GOOD_TEXT}) == GOOD_TEXT


def test_extract_output_text_chat_completions_fallback():
    body = {"choices": [{"message": {"role": "assistant", "content": GOOD_TEXT}}]}
    assert extract_output_text(body) == GOOD_TEXT


def test_extract_output_text_empty():
    assert extract_output_text({"output": []}) == ""
    assert extract_output_text({}) == ""
