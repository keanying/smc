# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""标注字：每个景区配的一组词（如 海蓝星、肖战、小城良方），评论**原文**里出现了就提取出来，
给 AI 标注的关键词做补充。

为什么不靠 AI：AI 的关键词是「支撑这条情感判断的词」（提示词规则 R3，如「太好看了」），
景区名、明星名、剧名这类主体词按规则就不会进去。标注字用确定的字面匹配，不花模型的钱、结果稳定。

命中的词**补进 keyword_tags**（AI 的关键词在前、标注字补在后，已有的不重复），另在 mark_word_tags 记一份：
- 新评论入库时补（data_repo.save_comments）；
- AI 标注时标注引擎在出口处再补一次（vendor labeling/service.py），AI 重标也冲不掉；
- 改了标注字点「重新识别」：keyword_tags 里上一次补的先拿掉、按新词表重新补（data_repo.apply_mark_words）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Sequence

#: 每个景区最多配多少个标注字
MAX_MARK_WORDS = 300


#: 匹配时忽略的符号：「斑海豹“梦梦”」要能命中「海豹梦梦」。
#: ⚠️ 和标注引擎 vendor/.../labeling/scenic_rules.py 的同名规则保持一致（测试里比对）
_IGNORED = re.compile(r"[\s“”\"'‘’「」『』《》〈〉()（）\[\]【】·・_\-—~～]")


def _norm(text: str) -> str:
    return _IGNORED.sub("", text or "").lower()


def match(content: str, words: Sequence[str]) -> List[str]:
    """评论原文里出现了哪些标注字。按在原文里第一次出现的位置排，同一个词只出一次；
    英文不分大小写，引号、书名号、空格等符号不影响匹配。"""
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
