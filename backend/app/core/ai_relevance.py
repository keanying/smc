# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""AI 过滤：采集时让大模型判断一条作品 / 笔记 / 博文是不是在讲本景区。

任务上开了「AI 过滤」才走（params.ai_filter），放在关键字两道关卡之后：
关键字挡得住「点斑祛斑」这种完全不沾边的，挡不住「山西运城舜帝陵」「九嶷山上白云飞（诗词）」
这种字面命中、内容却不是本景区的——这一层交给模型看语义。

- 用的是标注同一套模型配置（系统设置 → 标注里的方舟 Key 和模型），不用另配；
- **判不了就留**：没配模型、模型挂了、输出看不懂，一律按相关留下并在日志里说明——
  宁可多留几条让人工删，也不能因为模型抽风把真数据丢了；
- 同一条作品判过一次就记住（换关键字又搜到它不再问模型）；
- 景区盒子不跑模型，转给主判（box/client.py 的 RemoteRelevanceJudge）。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from .logging import get_logger

logger = get_logger(__name__)

#: 送给模型的正文最多多少字（标题、标签另算）。太长费 token，开头几百字足够判断讲的是什么
MAX_TEXT = 800
CACHE_SIZE = 5000


@dataclass
class Verdict:
    #: True 相关 / False 不相关 / None 判不了（按相关留下）
    relevant: Optional[bool]
    reason: str = ""

    @property
    def keep(self) -> bool:
        return self.relevant is not False


def build_prompt(scenic: Dict[str, Any], *, channel: str, title: str, description: str,
                 label: str) -> str:
    place = "".join(str(scenic.get(k) or "") for k in ("province", "city"))
    names = [w for w in scenic.get("words") or [] if w][:30]
    body = (description or "").strip()
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT] + "…"
    return f"""你在帮景区舆情系统筛选采集到的社交媒体内容：判断下面这条内容是不是在讲这个景区。

景区：{scenic.get("scenic_name") or scenic.get("scenic_id")}{f"（{place}）" if place else ""}
这个景区的其他叫法、景点：{"、".join(names) if names else "（无）"}

算「相关」：游记攻略、打卡分享、景点介绍、门票交通住宿餐饮、在这个景区的经历和评价、这个景区的活动和新闻。
算「不相关」：
- 同名或相近、但在别的地方的景点（比如别的省市的同名景区）；
- 只是诗词、歌词、成语、神话故事里出现了这个地名，内容不是在讲去这个景区；
- 带货广告、招聘、与景区无关的新闻或闲聊；
- 只在话题标签里蹭了景区名，正文讲的是别的事。
拿不准的，算相关。

只输出一行 JSON，不要任何别的文字：{{"relevant": true 或 false, "reason": "不超过20字的理由"}}

内容（平台：{channel}）：
标题：{(title or "").strip()[:200]}
正文：{body}
标签：{(label or "").strip()[:200]}"""


def parse_verdict(text: str) -> Verdict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return Verdict(None, "模型输出看不懂")
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return Verdict(None, "模型输出看不懂")
    relevant = data.get("relevant")
    if isinstance(relevant, str):
        relevant = {"true": True, "false": False}.get(relevant.strip().lower())
    if not isinstance(relevant, bool):
        return Verdict(None, "模型没给出相关与否")
    return Verdict(relevant, str(data.get("reason") or "")[:40])


class _Cache:
    def __init__(self, size: int = CACHE_SIZE):
        self.size = size
        self._data: "OrderedDict[str, Verdict]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Verdict]:
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                self._data.move_to_end(key)
            return hit

    def put(self, key: str, verdict: Verdict) -> None:
        if verdict.relevant is None:
            return                     # 判不了的不记，下次再问
        with self._lock:
            self._data[key] = verdict
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)


class RelevanceJudge:
    """本机直接问模型（主、总部版用）。"""

    def __init__(self, config, *, client=None):
        self.config = config
        self._client = client            # 测试里换成假的
        self._client_error = ""
        self._cache = _Cache()
        self._lock = threading.Lock()

    def _llm(self):
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None and not self._client_error:
                try:
                    from ..labeling.config_bridge import (engine_importable, ensure_on_path,
                                                          write_runtime_config)
                    if not engine_importable():
                        raise RuntimeError("标注引擎目录不完整")
                    ensure_on_path()
                    from opinion_labeling_engine.config import load_config
                    from opinion_labeling_engine.llm.client import ArkClient
                    cfg = load_config(str(write_runtime_config(self.config)), require_db=False)
                    if not cfg.llm.api_key:
                        raise RuntimeError("没配模型 Key（系统设置 → 标注）")
                    self._client = ArkClient(cfg.llm)
                except Exception as exc:  # noqa: BLE001
                    self._client_error = str(exc)
                    logger.warning("AI 过滤用不了，按相关留下：%s", exc)
        return self._client

    def reset(self) -> None:
        """改了模型配置：下次重新建客户端。"""
        with self._lock:
            self._client, self._client_error = None, ""

    async def judge(self, scenic: Dict[str, Any], *, channel: str, work_id: str, title: str = "",
                    description: str = "", label: str = "") -> Verdict:
        key = f"{scenic.get('scenic_id')}|{channel}|{work_id}"
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        verdict = await asyncio.to_thread(self._judge_sync, scenic, channel, title, description, label)
        self._cache.put(key, verdict)
        return verdict

    def _judge_sync(self, scenic, channel, title, description, label) -> Verdict:
        client = self._llm()
        if client is None:
            return Verdict(None, f"AI 过滤用不了：{self._client_error}")
        prompt = build_prompt(scenic, channel=channel, title=title, description=description, label=label)
        try:
            response = client.complete(
                [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                max_output_tokens=200)
        except Exception as exc:  # noqa: BLE001
            return Verdict(None, f"模型调用失败：{type(exc).__name__}")
        return parse_verdict(getattr(response, "text", ""))


async def scenic_context(scenics, scenic_id: str) -> Dict[str, Any]:
    """给模型看的景区信息：名字、省市、启用的主关键字和附关键字。"""
    scenic = await scenics.get_scenic(scenic_id) or {"scenic_id": scenic_id}
    words: Sequence[str] = [k["keyword"] for k in await scenics.list_keywords(scenic_id, enabled_only=True)]
    words = list(words) + (await scenics.filter_words_for(scenic_id)).get("aux", [])
    return {"scenic_id": scenic_id, "scenic_name": scenic.get("scenic_name") or scenic_id,
            "province": scenic.get("province") or "", "city": scenic.get("city") or "",
            "words": list(dict.fromkeys(words))}
