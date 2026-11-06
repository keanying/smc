# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""按景区的标注补充规则。

不同景区有自己的标注需求（比如 IP 景区要把剧名、角色名、演员都当关键词抽出来）。
采集系统在「景区管理 → 标注提示词」里给每个景区配：

    prompt          补充提示词，拼进这个景区每条评论的用户段（llm/prompt.py）
    broad_keywords  关键词放宽：不要求和整体情感同向、允许书名号全称、
                    长度和个数上限放大（labeling/postprocess.py）
    mark_words      标注字：评论原文里出现就补进 keyword_tags（labeling/service.py 出口统一补，
                    AI 标、规则判中性、重标都会补）

这里只是一个进程内的登记表，由采集系统定时刷新（app/labeling/scenic_rules_sync.py）。
worker 线程只读，刷新时整表替换，不需要加锁。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = ["ScenicRule", "set_rules", "rule_for", "match_marks", "merge_marks", "MARK_WORD_REASON",
           "BROAD_KEYWORD_MAX_LENGTH", "BROAD_KEYWORD_MAX_COUNT"]

#: 放宽后每个关键词最长多少字、每条最多多少个
BROAD_KEYWORD_MAX_LENGTH = 30
BROAD_KEYWORD_MAX_COUNT = 20


@dataclass(frozen=True)
class ScenicRule:
    prompt: str = ""
    broad_keywords: bool = False
    mark_words: Tuple[str, ...] = ()


_rules: Dict[str, ScenicRule] = {}


def set_rules(rules: Mapping[str, ScenicRule]) -> None:
    global _rules
    _rules = {k: v for k, v in rules.items() if v.prompt.strip() or v.broad_keywords or v.mark_words}


def rule_for(scenic_id: str) -> Optional[ScenicRule]:
    return _rules.get(scenic_id or "")


#: 匹配标注字时忽略的符号：「斑海豹“梦梦”」要能命中「海豹梦梦」
#: ⚠️ 和采集系统 app/core/mark_words.py 的同名规则保持一致
_IGNORED = re.compile(r"[\s“”\"'‘’「」『』《》〈〉()（）\[\]【】·・_\-—~～]")


def _norm(text: str) -> str:
    return _IGNORED.sub("", text or "").lower()


def match_marks(content: str, words: Sequence[str]) -> List[str]:
    """评论原文里出现了哪些标注字。按第一次出现的位置排，同一个词只出一次。"""
    text = _norm(content)
    if not text or not words:
        return []
    found = []
    for word in dict.fromkeys(w.strip() for w in words if w and w.strip()):
        key = _norm(word)
        pos = text.find(key) if key else -1
        if pos >= 0:
            found.append((pos, word))
    return [w for _, w in sorted(found, key=lambda x: x[0])]


#: 标注字补进关键词时，归因明细里写的原因（smc 的 app/core/mark_words.py 用同一句，有测试盯着）
MARK_WORD_REASON = "命中本景区标注字"


def merge_marks(keywords: Iterable[str], marks: Iterable[str]) -> List[str]:
    """AI 的关键词在前，标注字补在后；已经有的不重复补。"""
    out = list(keywords or [])
    seen = set(out)
    return out + [m for m in marks if m not in seen]
