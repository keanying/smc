"""请求体形状单测。

方舟曾经因为 assistant item 缺 ``status`` 直接 400：

    MissingParameter: The request failed because it is missing `input.status` parameter

这类错误在跑批跑到一半才暴露，代价很高，所以把请求形状钉死在测试里。
基准是方舟文档给的最小可用请求：

    {"role": "user", "content": [{"type": "input_text", "text": "..."}]}
"""

from __future__ import annotations

import json

import pytest

from opinion_labeling_engine.config import load_config
from opinion_labeling_engine.domain.models import CommentRecord
from opinion_labeling_engine.llm.prompt import PromptBuilder
from opinion_labeling_engine.preprocess.cleaner import ContentCleaner

RECORD = CommentRecord(
    comment_id="805353431", channel="ctrip", work_id="76471",
    scenic_id="PFTSCA01009835", scenic_name="天山天池", content="完美的一天",
)


@pytest.fixture()
def cleaned(cfg):
    return ContentCleaner(cfg.cleaning).clean(RECORD.content)


def build(taxonomy, **kwargs) -> PromptBuilder:
    return PromptBuilder(taxonomy, **kwargs)


# =============================================================== 通用形状
@pytest.mark.parametrize("style", ["inline", "message"])
@pytest.mark.parametrize("system_role", ["system", "user"])
def test_every_item_is_json_serializable_and_well_formed(
        taxonomy, cleaned, style, system_role):
    msgs = build(taxonomy, fewshot_style=style, system_role=system_role) \
        .build_messages(RECORD, cleaned)
    json.dumps(msgs, ensure_ascii=False)          # 进不了 JSON 就别谈发请求了

    for item in msgs:
        assert item["role"] in {"system", "user", "assistant"}
        assert isinstance(item["content"], list) and item["content"]
        for part in item["content"]:
            assert set(part) == {"type", "text"}
            assert part["type"] in {"input_text", "output_text"}
            assert isinstance(part["text"], str) and part["text"]


@pytest.mark.parametrize("style", ["inline", "message"])
@pytest.mark.parametrize("system_role", ["system", "user"])
def test_last_item_is_the_user_comment(taxonomy, cleaned, style, system_role):
    msgs = build(taxonomy, fewshot_style=style, system_role=system_role) \
        .build_messages(RECORD, cleaned)
    last = msgs[-1]
    assert last["role"] == "user"
    assert "完美的一天" in last["content"][0]["text"]


# =============================================================== user / system
def test_user_item_matches_ark_minimal_sample_exactly(taxonomy, cleaned):
    """user item 必须与方舟最小样例逐字段一致，不多带任何字段。"""
    msgs = build(taxonomy).build_messages(RECORD, cleaned)
    user = msgs[-1]
    assert set(user) == {"role", "content"}          # 没有 type、没有 status
    assert user["role"] == "user"
    assert user["content"][0]["type"] == "input_text"


def test_system_item_has_no_extra_fields(taxonomy, cleaned):
    msgs = build(taxonomy, system_role="system").build_messages(RECORD, cleaned)
    system = msgs[0]
    assert system["role"] == "system"
    assert set(system) == {"role", "content"}
    assert system["content"][0]["type"] == "input_text"


# =============================================================== inline（默认）
def test_inline_style_emits_no_assistant_item(taxonomy, cleaned):
    """默认配置下 input 里一个 assistant item 都不该有。"""
    msgs = build(taxonomy, fewshot_style="inline").build_messages(RECORD, cleaned)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert all(p["type"] == "input_text" for m in msgs for p in m["content"])


def test_inline_style_still_carries_the_examples(taxonomy, cleaned):
    """示例折进 system 段，内容不能丢。"""
    builder = build(taxonomy, fewshot_style="inline")
    assert "标注示例" in builder.system_prompt
    assert "云海太震撼" in builder.system_prompt          # 示例 1 的输入
    assert "带货广告，与景区无关" in builder.system_prompt  # 示例 3 的输出

    without = build(taxonomy, with_fewshot=False, fewshot_style="inline")
    assert "标注示例" not in without.system_prompt


def test_system_role_user_collapses_to_a_single_user_item(taxonomy, cleaned):
    """最大兼容模式：整个请求就一个 user item，与 curl 样例形状完全相同。"""
    msgs = build(taxonomy, system_role="user").build_messages(RECORD, cleaned)
    assert len(msgs) == 1
    assert msgs[0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": msgs[0]["content"][0]["text"]}],
    }
    text = msgs[0]["content"][0]["text"]
    assert "景区舆情标注专家" in text and "完美的一天" in text


# =============================================================== message
def test_message_style_assistant_items_carry_type_and_status(taxonomy, cleaned):
    """这就是当初 400 的那个字段，必须有。"""
    msgs = build(taxonomy, fewshot_style="message").build_messages(RECORD, cleaned)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert assistants, "message 模式应该产生 assistant 轮"
    for item in assistants:
        assert item["type"] == "message"
        assert item["status"] == "completed"
        assert item["content"][0]["type"] == "output_text"


def test_message_style_alternates_user_assistant(taxonomy, cleaned):
    msgs = build(taxonomy, fewshot_style="message").build_messages(RECORD, cleaned)
    roles = [m["role"] for m in msgs]
    assert roles[0] == "system"
    assert roles[-1] == "user"
    assert roles.count("assistant") == roles.count("user") - 1   # 最后一轮没有回答


# =============================================================== 修复轮
@pytest.mark.parametrize("style,system_role,expect_assistant", [
    ("inline", "system", False),
    ("message", "system", True),
    ("inline", "user", False),
])
def test_repair_messages_shape(taxonomy, cleaned, style, system_role, expect_assistant):
    builder = build(taxonomy, fewshot_style=style, system_role=system_role)
    msgs = builder.build_repair_messages(RECORD, cleaned, "我不想输出 JSON", "无法解析")
    json.dumps(msgs, ensure_ascii=False)

    has_assistant = any(m["role"] == "assistant" for m in msgs)
    assert has_assistant is expect_assistant
    if has_assistant:
        for item in (m for m in msgs if m["role"] == "assistant"):
            assert item["status"] == "completed"

    # 无论哪种形态，坏输出和纠正要求都必须传达到
    blob = "\n".join(p["text"] for m in msgs for p in m["content"])
    assert "我不想输出 JSON" in blob
    assert "无法解析" in blob


def test_repair_with_system_role_user_is_single_item(taxonomy, cleaned):
    msgs = build(taxonomy, system_role="user") \
        .build_repair_messages(RECORD, cleaned, "坏输出", "格式错")
    assert len(msgs) == 1 and msgs[0]["role"] == "user"


# =============================================================== 配置校验
def test_invalid_style_and_role_are_rejected(taxonomy):
    with pytest.raises(ValueError):
        PromptBuilder(taxonomy, fewshot_style="whatever")
    with pytest.raises(ValueError):
        PromptBuilder(taxonomy, system_role="assistant")


def test_config_defaults_are_the_compatible_ones():
    cfg = load_config(require_db=False)
    assert cfg.llm.fewshot_style == "inline"
    assert cfg.llm.system_role == "system"


def test_config_rejects_bad_values():
    from dataclasses import replace

    from opinion_labeling_engine.config import ConfigError

    cfg = load_config(require_db=False)
    with pytest.raises(ConfigError):
        replace(cfg.llm, fewshot_style="nope").validate()
    with pytest.raises(ConfigError):
        replace(cfg.llm, system_role="nope").validate()
