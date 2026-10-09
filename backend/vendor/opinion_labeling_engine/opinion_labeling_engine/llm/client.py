"""火山方舟 Ark（responses 协议）客户端。

只做四件事，不含任何标注语义：
    1. 发请求（连接复用 + 超时）
    2. 限流（进程内令牌桶，按 qps）
    3. 重试（区分可重试与不可重试错误，指数退避 + 抖动）
    4. 从响应体里把模型输出的纯文本抠出来

任何标注逻辑都不在这里，方便换模型供应商时只替换本文件。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from ..config import LLMConfig
from ..logging_conf import get_logger

__all__ = ["ArkClient", "LLMResponse", "LLMError", "RetryableLLMError",
           "TokenBucket", "is_truncated", "extract_output_text"]

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """模型调用失败（不可重试）。"""


class RetryableLLMError(LLMError):
    """可重试的失败：超时、5xx、429、连接错误。"""


@dataclass(frozen=True)
class LLMResponse:
    """一次成功调用的结果。"""

    text: str
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_id: str = ""
    raw: Optional[Dict[str, Any]] = None


class TokenBucket:
    """线程安全令牌桶，控制全局 QPS。

    ``qps <= 0`` 表示不限流，``acquire`` 直接返回。
    """

    def __init__(self, qps: float, *, capacity: Optional[float] = None) -> None:
        self._qps = float(qps or 0)
        self._capacity = float(capacity if capacity is not None else max(qps, 1.0))
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """取令牌，不足则睡到够为止。返回实际等待秒数。"""
        if self._qps <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._qps)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self._qps
            time.sleep(min(sleep_for, 1.0))
            waited += min(sleep_for, 1.0)


class ArkClient:
    """Ark ``/responses`` 接口客户端。线程安全（requests.Session 复用连接）。"""

    #: 这些 HTTP 状态码值得重试
    RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509})

    def __init__(self, cfg: LLMConfig, *, session: Optional[requests.Session] = None) -> None:
        self._cfg = cfg
        self._session = session or requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        })
        self._bucket = TokenBucket(cfg.rate_limit_qps)
        # 连接池大小跟随并发，避免 urllib3 的 "Connection pool is full" 警告
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=64)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # ------------------------------------------------------------------ 调用
    def complete(self, messages: List[Dict[str, Any]], *,
                 max_output_tokens: Optional[int] = None) -> LLMResponse:
        """发起一次补全，内部自动重试。

        Args:
            messages: responses 协议的 ``input`` 数组。
            max_output_tokens: 覆盖配置里的输出上限。

        Returns:
            :class:`LLMResponse`。

        Raises:
            LLMError: 重试耗尽或遇到不可重试错误（如 401 鉴权失败）。
        """
        budget = int(max_output_tokens or self._cfg.max_output_tokens)
        payload = {
            "model": self._cfg.model,
            "stream": False,
            "tools": [],
            "input": messages,
            "temperature": self._cfg.temperature,
            "top_p": self._cfg.top_p,
            "max_output_tokens": budget,
        }
        # 透传厂商私有参数（如关闭思考链），默认为空，不动就不发
        if self._cfg.extra_body:
            payload.update(self._cfg.extra_body)

        last_error: Optional[Exception] = None
        for attempt in range(1, self._cfg.max_retries + 2):   # 首次 + max_retries 次重试
            waited = self._bucket.acquire()
            if waited > 0.5:
                logger.debug("限流等待 %.2fs", waited)
            started = time.monotonic()
            try:
                resp = self._session.post(
                    self._cfg.url,
                    json=payload,
                    timeout=(self._cfg.connect_timeout, self._cfg.read_timeout),
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = RetryableLLMError(f"网络异常：{exc}")
                self._sleep_backoff(attempt, last_error)
                continue
            except requests.RequestException as exc:            # pragma: no cover
                raise LLMError(f"请求构造失败：{exc}") from exc

            latency_ms = int((time.monotonic() - started) * 1000)

            if resp.status_code in self.RETRYABLE_STATUS:
                last_error = RetryableLLMError(
                    f"HTTP {resp.status_code}：{resp.text[:300]}"
                )
                self._sleep_backoff(attempt, last_error, resp=resp)
                continue
            if resp.status_code >= 400:
                # 4xx（除上面几个）多为鉴权/参数错误，重试无意义，快速失败
                raise LLMError(f"HTTP {resp.status_code}：{resp.text[:500]}")

            try:
                body = resp.json()
            except ValueError as exc:
                last_error = RetryableLLMError(f"响应不是合法 JSON：{resp.text[:300]}")
                self._sleep_backoff(attempt, last_error)
                continue

            usage = body.get("usage") or {}
            text = extract_output_text(body)

            # 输出被 max_output_tokens 截断：正文可能一个字都没出来（推理模型的
            # 思考过程也吃这份预算），也可能出到一半断成半截 JSON。
            # 原样重试没有意义——同样的预算还会断在同一个地方，必须把预算加上去再试。
            if is_truncated(body) and (not text or attempt <= self._cfg.max_retries):
                grown = min(budget * 2, self._cfg.max_output_tokens_cap)
                if grown > budget:
                    logger.warning(
                        "输出被 max_output_tokens=%d 截断（已用 %s tokens），"
                        "提到 %d 后重试；长期方案是调大 llm.max_output_tokens",
                        budget, usage.get("output_tokens", "?"), grown,
                    )
                    budget = grown
                    payload["max_output_tokens"] = budget
                    last_error = RetryableLLMError(f"输出被截断（预算 {budget}）")
                    continue                      # 立刻重试，不必退避——不是服务端过载
                if not text:
                    raise LLMError(
                        f"输出被截断且预算已达上限 {self._cfg.max_output_tokens_cap}，"
                        f"请调大 llm.max_output_tokens_cap 或缩短提示词"
                    )
                logger.warning("输出被截断且预算已达上限，按已拿到的部分继续解析")

            if not text:
                last_error = RetryableLLMError(
                    f"响应中没有可用文本：status={body.get('status')} "
                    f"incomplete={body.get('incomplete_details')} "
                    f"usage={usage}"
                )
                self._sleep_backoff(attempt, last_error)
                continue

            return LLMResponse(
                text=text,
                model=str(body.get("model") or self._cfg.model),
                latency_ms=latency_ms,
                prompt_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
                request_id=str(body.get("id") or resp.headers.get("x-request-id") or ""),
                raw=body,
            )

        raise LLMError(f"模型调用重试 {self._cfg.max_retries} 次仍失败：{last_error}")

    # ------------------------------------------------------------------ 退避
    def _sleep_backoff(self, attempt: int, error: Exception,
                       resp: Optional[requests.Response] = None) -> None:
        """指数退避 + 抖动；若服务端给了 Retry-After 则优先听它的。"""
        if attempt > self._cfg.max_retries:
            return
        delay = min(
            self._cfg.retry_backoff_base * (2 ** (attempt - 1)),
            self._cfg.retry_backoff_max,
        )
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = min(float(retry_after), self._cfg.retry_backoff_max)
                except ValueError:
                    pass
        delay += random.uniform(0, delay * 0.25)   # 抖动，避免多 worker 同步重试
        logger.warning("第 %d 次调用失败（%s），%.1fs 后重试", attempt, error, delay)
        time.sleep(delay)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "ArkClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 响应体判读
# ---------------------------------------------------------------------------
def is_truncated(body: Dict[str, Any]) -> bool:
    """判断本次响应是不是因为触到 ``max_output_tokens`` 而中断。

    responses 协议用 ``status="incomplete"`` + ``incomplete_details.reason="length"``
    表达；chat/completions 兼容模式用 ``finish_reason="length"``。两种都认。
    """
    if not isinstance(body, dict):
        return False
    details = body.get("incomplete_details") or {}
    if isinstance(details, dict) and details.get("reason") == "length":
        return True
    if body.get("status") == "incomplete":
        return True
    for choice in body.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason") == "length":
            return True
    return False


# ---------------------------------------------------------------------------
# 响应体文本提取
# ---------------------------------------------------------------------------
def extract_output_text(body: Dict[str, Any]) -> str:
    """从 Ark/OpenAI 系响应体中抠出模型输出的纯文本。

    需要兼容三种形态，因为不同模型、不同网关的返回结构并不统一：
        1. responses 协议：``output`` 数组里 ``type=message`` 项的
           ``content[*].text``（``type=reasoning`` 的思考项必须跳过）
        2. 部分网关直接给 ``output_text`` 顶层字段
        3. chat/completions 兼容模式：``choices[0].message.content``

    Returns:
        提取到的文本；一个都没有时返回空串（由调用方判失败并重试）。
    """
    if not isinstance(body, dict):
        return ""

    # 形态 2：最省事，优先
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if isinstance(direct, list):
        joined = "".join(x for x in direct if isinstance(x, str))
        if joined.strip():
            return joined.strip()

    # 形态 1
    chunks: List[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            continue                      # 思维链不是答案，跳过
        content = item.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("output_text", "text", "input_text"):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    if chunks:
        merged = "".join(chunks).strip()
        if merged:
            return merged

    # 形态 3
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            merged = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            ).strip()
            if merged:
                return merged

    return ""
