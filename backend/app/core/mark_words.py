# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""标注字：每个景区配的一组词（如 海蓝星、肖战、小城良方），评论**原文**里出现了就提取出来，
给 AI 标注的关键词做补充。

为什么不靠 AI：AI 的关键词是「支撑这条情感判断的词」（提示词规则 R3，如「太好看了」），
景区名、明星名、剧名这类主体词按规则就不会进去。标注字用确定的字面匹配，不花模型的钱、结果稳定。

存在评论表单独一列 mark_word_tags（JSON 数组）：AI 标注会整列覆盖 keyword_tags，
写在一起的话下一次 AI 一标就冲掉了。页面和导出里两者合并显示（AI 的在前、标注字补在后）。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, List, Sequence

#: 每个景区最多配多少个标注字
MAX_MARK_WORDS = 300


def match(content: str, words: Sequence[str]) -> List[str]:
    """评论原文里出现了哪些标注字。按在原文里第一次出现的位置排，同一个词只出一次；英文不分大小写。"""
    text = (content or "").lower()
    if not text or not words:
        return []
    found = []
    for word in dict.fromkeys(w.strip() for w in words if w and w.strip()):
        pos = text.find(word.lower())
        if pos >= 0:
            found.append((pos, word))
    return [w for _, w in sorted(found, key=lambda x: x[0])]


def dumps(words: Iterable[str]) -> str:
    return json.dumps(list(words), ensure_ascii=False)


def loads(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in data] if isinstance(data, list) else []


def merge_keywords(ai_keywords: Any, marks: Any) -> List[Any]:
    """AI 关键词在前，标注字补在后（AI 已经有的不重复补）。AI 关键词可能是字符串，也可能是 {word: ..} 对象。"""
    ai = list(ai_keywords or [])
    seen = {(k.get("word") if isinstance(k, dict) else str(k)) for k in ai}
    return ai + [m for m in loads(marks) if m not in seen]
