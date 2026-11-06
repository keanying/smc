# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""归因明细（评论表 attribution_details 列）：keyword_tags 里每个关键词「为什么提取它」。

    [{"word": "一般", "reason": "对景区整体的评价，态度平淡，是判为中性的依据"}, …]

**和 keyword_tags 一一对应、顺序一致**，以 keyword_tags 为准：
- AI 标注时由标注引擎写（模型给每个关键词写 reason，见引擎 llm/prompt.py 的 R8）
- 标注字补进 keyword_tags 时，原因写 :data:`MARK_WORD_REASON`
- 人工复核改了关键词：留下的词原因不变，新加的写 :data:`HUMAN_REASON`，删掉的词明细跟着删
- 历史数据没有这一列的值，读出来按 keyword_tags 对齐、原因为空

这个模块只做纯函数，不碰数据库；引擎那边有一份同名逻辑（domain/models.py 的 align_attribution）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = ["MARK_WORD_REASON", "HUMAN_REASON", "REASON_MAX", "loads", "dumps", "align"]

#: 标注字补进关键词时写的原因。⚠️ 和引擎 labeling/scenic_rules.py 的 MARK_WORD_REASON 一字不差（有测试盯着）
MARK_WORD_REASON = "命中本景区标注字"
#: 人工复核时新加的关键词
HUMAN_REASON = "人工复核添加"
REASON_MAX = 60


def loads(value: Any) -> List[Dict[str, str]]:
    """库里的值 → 明细列表。NULL、空串、脏数据都当成空列表。"""
    if isinstance(value, list):
        data = value
    else:
        try:
            data = json.loads(value) if value else []
        except (TypeError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and str(item.get("word") or "").strip():
            out.append({"word": str(item["word"]), "reason": str(item.get("reason") or "")})
    return out


def dumps(details: Sequence[Dict[str, str]]) -> Optional[str]:
    """明细列表 → 写库的值。空列表存 NULL（和 keyword_tags 的空值习惯一致，判空简单）。"""
    return json.dumps(list(details), ensure_ascii=False) if details else None


def align(words: Iterable[Any], details: Any, *, default_reason: str = "",
          mark_words: Iterable[str] = ()) -> List[Dict[str, str]]:
    """按 keyword_tags 生成明细：词的顺序跟 keyword_tags，原因从已有明细里找。

    找不到原因时：是标注字 → :data:`MARK_WORD_REASON`；否则用 ``default_reason``。
    """
    reasons: Dict[str, str] = {}
    for item in loads(details):
        word = item["word"].strip()
        if word not in reasons:
            reasons[word] = item["reason"][:REASON_MAX]
    marks = {str(m).strip() for m in mark_words or ()}
    out: List[Dict[str, str]] = []
    for word in words or []:
        w = str(word)
        reason = reasons.get(w.strip()) or (MARK_WORD_REASON if w.strip() in marks else default_reason)
        out.append({"word": w, "reason": reason})
    return out
