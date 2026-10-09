"""输出截断处理单测。

线上踩到的：``max_output_tokens: 1024`` 对推理模型太小，思考过程把额度吃光，
响应回来 ``incomplete_details: {"reason": "length"}``，正文一个字都没有；
或者出到一半断成半截 JSON。

原样重试是没用的——同样的预算还会断在同一个地方，所以客户端必须把预算加上去再试。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import requests

from opinion_labeling_engine.config import ConfigError, load_config
from opinion_labeling_engine.llm.client import (ArkClient, LLMError, is_truncated)

GOOD_JSON = json.dumps({
    "relevant": True, "sentiment": "正向", "confidence": 0.9,
    "dimensions": [], "entities": [], "keywords": [], "reason": "好评",
}, ensure_ascii=False)


# ===================================================================== 判定
def test_is_truncated_responses_protocol():
    assert is_truncated({"incomplete_details": {"reason": "length"}}) is True
    assert is_truncated({"status": "incomplete"}) is True


def test_is_truncated_chat_completions_fallback():
    assert is_truncated({"choices": [{"finish_reason": "length"}]}) is True
    assert is_truncated({"choices": [{"finish_reason": "stop"}]}) is False


def test_is_truncated_normal_response():
    assert is_truncated({"status": "completed", "output": []}) is False
    assert is_truncated({"incomplete_details": {"reason": "content_filter"}}) is False
    assert is_truncated({}) is False
    assert is_truncated(None) is False


# ===================================================================== 假 HTTP
class FakeResponse:
    def __init__(self, body, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class FakeSession:
    """按脚本依次返回预置响应，并记录每次请求的 payload。"""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.payloads = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):     # noqa: A002
        # 必须存副本：客户端在重试时是原地改 payload 的，存引用会让所有记录变成同一份
        self.payloads.append(dict(json))
        if not self._responses:
            raise AssertionError("假会话的预置响应用完了，说明重试次数超出预期")
        return FakeResponse(self._responses.pop(0))

    def mount(self, *_a, **_kw):
        pass

    def close(self):
        pass


def truncated_body(with_text: str = "") -> dict:
    """一个被 length 截断的响应；``with_text`` 非空表示"断成了半截正文"。"""
    output = [{"type": "reasoning", "content": [{"type": "text", "text": "思考中……"}]}]
    if with_text:
        output.append({"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": with_text}]})
    return {
        "id": "resp_1", "model": "deepseek-v4-flash-260425", "status": "incomplete",
        "incomplete_details": {"reason": "length"},
        "output": output, "usage": {"input_tokens": 1500, "output_tokens": 1024},
    }


def ok_body(text: str = GOOD_JSON) -> dict:
    return {
        "id": "resp_2", "model": "deepseek-v4-flash-260425", "status": "completed",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 1500, "output_tokens": 120},
    }


def make_client(responses, **cfg_kwargs) -> tuple[ArkClient, FakeSession]:
    cfg = load_config(require_db=False).llm
    cfg = replace(cfg, rate_limit_qps=0, retry_backoff_base=0.0, **cfg_kwargs)
    session = FakeSession(responses)
    return ArkClient(cfg, session=session), session


# ===================================================================== 行为
def test_truncated_with_no_text_doubles_budget_and_retries():
    client, session = make_client([truncated_body(), ok_body()],
                                  max_output_tokens=1024)
    resp = client.complete([{"role": "user", "content": []}])

    assert resp.text == GOOD_JSON
    assert [p["max_output_tokens"] for p in session.payloads] == [1024, 2048]


def test_budget_keeps_doubling_until_it_works():
    client, session = make_client(
        [truncated_body(), truncated_body(), truncated_body(), ok_body()],
        max_output_tokens=1024, max_retries=5)
    client.complete([{"role": "user", "content": []}])
    assert [p["max_output_tokens"] for p in session.payloads] == [1024, 2048, 4096, 8192]


def test_budget_never_exceeds_the_cap():
    """涨到上限就停手，不会无限翻倍空转。"""
    client, session = make_client(
        [truncated_body(), truncated_body(), truncated_body()],
        max_output_tokens=1024, max_output_tokens_cap=2048, max_retries=5)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": []}])
    assert [p["max_output_tokens"] for p in session.payloads] == [1024, 2048]


def test_truncated_at_cap_with_no_text_fails_loudly():
    """涨到上限还是一个字没有 —— 报清楚是预算问题，别让人去查网络。"""
    client, _ = make_client([truncated_body()],
                            max_output_tokens=2048, max_output_tokens_cap=2048)
    with pytest.raises(LLMError) as exc:
        client.complete([{"role": "user", "content": []}])
    assert "截断" in str(exc.value)
    assert "max_output_tokens_cap" in str(exc.value)


def test_half_written_json_also_triggers_a_bigger_retry():
    """断成半截 JSON 时也要加预算重试，而不是把半截喂给解析器。"""
    half = '{"relevant": true, "sentiment": "正向", "dimensions": [{"dim1": "游玩体验"'
    client, session = make_client([truncated_body(half), ok_body()],
                                  max_output_tokens=1024)
    resp = client.complete([{"role": "user", "content": []}])
    assert resp.text == GOOD_JSON
    assert len(session.payloads) == 2


def test_normal_response_does_not_touch_the_budget():
    client, session = make_client([ok_body()], max_output_tokens=4096)
    client.complete([{"role": "user", "content": []}])
    assert len(session.payloads) == 1
    assert session.payloads[0]["max_output_tokens"] == 4096


def test_empty_output_without_truncation_still_retries_normally():
    """不是截断、就是没内容 —— 走原来的退避重试，不加预算。"""
    empty = {"id": "r", "status": "completed", "output": [], "usage": {}}
    client, session = make_client([empty, ok_body()], max_output_tokens=4096)
    client.complete([{"role": "user", "content": []}])
    assert [p["max_output_tokens"] for p in session.payloads] == [4096, 4096]


# ===================================================================== 配置
def test_extra_body_is_merged_into_payload():
    client, session = make_client([ok_body()],
                                  extra_body={"thinking": {"type": "disabled"}})
    client.complete([{"role": "user", "content": []}])
    assert session.payloads[0]["thinking"] == {"type": "disabled"}


def test_extra_body_defaults_to_absent():
    client, session = make_client([ok_body()])
    client.complete([{"role": "user", "content": []}])
    assert "thinking" not in session.payloads[0]
    assert "reasoning" not in session.payloads[0]


def test_default_budget_is_generous_enough_for_a_reasoning_model():
    cfg = load_config(require_db=False)
    assert cfg.llm.max_output_tokens >= 4096
    assert cfg.llm.max_output_tokens_cap >= cfg.llm.max_output_tokens


def test_cap_below_budget_is_rejected():
    cfg = load_config(require_db=False).llm
    with pytest.raises(ConfigError):
        replace(cfg, max_output_tokens=8192, max_output_tokens_cap=4096).validate()


def test_extra_body_must_be_a_dict():
    cfg = load_config(require_db=False).llm
    with pytest.raises(ConfigError):
        replace(cfg, extra_body=["nope"]).validate()


# ===================================================================== 提示词
def test_prompt_asks_for_compact_json(taxonomy):
    """缩进过的 JSON 体积翻倍，提示词里要明确压成一行。"""
    from opinion_labeling_engine.llm.prompt import PromptBuilder

    system = PromptBuilder(taxonomy).system_prompt
    assert "压缩成一行" in system
    assert "不要输出思考过程" in system
