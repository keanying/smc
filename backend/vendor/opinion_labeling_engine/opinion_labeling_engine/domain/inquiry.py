"""疑问句识别与数量词识别 —— 解决「咨询类评论被当成评价」的问题。

要解决的真实案例（用户从库里捞出来的）::

    最近人多吗              → 之前抽出 ["人多"]
    人多吗？我们4岁多可去么   → 之前抽出 ["人多", "4岁多"]

这两条**都不是评价**，是出行前的打听。但它们抽出的「人多」经聚合后变成
「人多拥挤」，直接混进景区的拥挤度统计里——一个准备去玩的人问了句
「人多吗」，在看板上变成了一条「嫌人多」的负面舆情。评论量越大，
这类噪声占比越高（问「人多吗」「要预约吗」的人远比抱怨的人多）。

两类要拦的东西
--------------
1. **疑问小句里的词**：「人多吗」里的「人多」是提问对象，不是断言。
   按小句判定而不是整条判定——「风景真不错，请问几点关门？」
   前半句的评价必须留住。

2. **纯数量词**：「4岁多」「两小时」「50块」是事实陈述，不带褒贬，
   放进 keyword_tags 只会稀释真正的评价词。
   注意只拦**整词都是数量**的，「排队两小时」必须留住——
   它的词头是「排队」，这才是评价。

为什么不能只靠提示词
--------------------
提示词里写了模型也会漂，尤其短文本。而且这是个会被反复回填的历史数据问题，
规则层能离线重算，提示词不能。所以两层都做：提示词降低发生率，
后处理保证下限。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

__all__ = ["Clause", "split_clauses", "is_interrogative", "is_pure_inquiry",
           "interrogative_spans", "declarative_text", "is_quantity_only",
           "occurrences_all_inside"]


# --------------------------------------------------------------------------- 小句切分
#: 小句边界。逗号也算——「人多吗，可以带娃吗」是两问，
#: 不切开就只能整条判定，会把「风景好，几点关门？」的前半句一起误杀。
_CLAUSE_DELIM = "。！？!?；;，,、…\n\r\t　"

_CLAUSE_RE = re.compile(rf"[^{re.escape(_CLAUSE_DELIM)}]+[{re.escape(_CLAUSE_DELIM)}]*")


@dataclass(frozen=True)
class Clause:
    """一个小句及其在原文中的位置。"""

    text: str
    start: int
    end: int

    @property
    def question(self) -> bool:
        return is_interrogative(self.text)


def split_clauses(text: str) -> List[Clause]:
    """按句读把文本切成小句，保留每句在原文中的下标区间。

    下标必须保留：关键词是否落在疑问句里，靠的是「关键词在原文中的位置
    是否落进某个疑问小句的区间」，丢了下标就只能退回整条判定。
    """
    if not text:
        return []
    return [Clause(m.group(0), m.start(), m.end()) for m in _CLAUSE_RE.finditer(text)
            if m.group(0).strip(_CLAUSE_DELIM + " ")]


# --------------------------------------------------------------------------- 疑问判定
#: 句尾疑问语气词。只收**单独出现在句尾就足以判定为疑问**的字：
#:   吗/嘛/么  —— 「人多吗」「可去么」，几乎不会有别的意思
#: 故意不收 吧/呢：「太美了吧」「真好看呢」是感叹不是提问，
#: 它们只有配上问号才算疑问，而问号本身已经被下面的规则覆盖了。
_TAIL_QUESTION_CHARS = "吗嘛么麽"

#: 疑问标点（半角全角）
_QUESTION_MARKS = "?？"

#: 疑问词。命中其一即判定为疑问句——这些词在陈述句里几乎不出现。
#: 「怎么这么多人」这类反问确实带情绪，会被一起拦掉；
#: 这是有意的取舍：宁可少标一条真抱怨，也不能让几千条打听污染拥挤度统计。
_QUESTION_WORDS = (
    "请问", "想问", "问一下", "咨询", "求问", "有没有", "有木有", "是不是",
    "能不能", "可不可以", "可以吗", "行不行", "好不好", "要不要", "需不需要",
    "值不值", "贵不贵", "多不多", "远不远", "难不难", "方不方便",
    "多少钱", "多少人", "几点", "几号", "几天", "几个小时", "什么时候", "哪一天",
    "怎么走", "怎么去", "怎么买", "怎么预约", "如何前往", "在哪", "哪里买",
    "求推荐", "求攻略", "有人知道",
)


def is_interrogative(text: str) -> bool:
    """这句是不是提问。

    判定顺序（从最强的信号到最弱的）：
        1. 含问号
        2. 句尾是 吗/嘛/么
        3. 含明确疑问词
    """
    stripped = text.strip().strip(" 　")
    if not stripped:
        return False

    if any(ch in stripped for ch in _QUESTION_MARKS):
        return True

    # 去掉句尾标点再看最后一个字
    tail = stripped.rstrip("。！!；;，,、…~～ 　")
    if tail and tail[-1] in _TAIL_QUESTION_CHARS:
        return True

    return any(word in stripped for word in _QUESTION_WORDS)


def interrogative_spans(text: str) -> List[Tuple[int, int]]:
    """返回所有疑问小句在原文中的区间。"""
    return [(c.start, c.end) for c in split_clauses(text) if c.question]


def declarative_text(text: str) -> str:
    """返回去掉所有疑问小句之后剩下的陈述部分。

    用来给"只在疑问句里出现过"的判定留一条活口：

        人多吗？反正我去的时候人是真的多

    「人多」这四个字只在前半句出现过，但后半句"人是真的多"说的就是这件事，
    只是换了个说法。只看字面出现位置会把这条真抱怨误杀，
    所以再拿陈述部分做一次宽松匹配。
    """
    return "".join(c.text for c in split_clauses(text) if not c.question)


def is_pure_inquiry(text: str) -> bool:
    """整条评论是不是纯咨询（每一个小句都是提问）。

    「最近人多吗」→ True；
    「风景很美，请问几点关门？」→ False（前半句是评价，必须留住）。
    """
    clauses = split_clauses(text)
    if not clauses:
        return False
    return all(c.question for c in clauses)


def occurrences_all_inside(word: str, text: str,
                           spans: Sequence[Tuple[int, int]]) -> bool:
    """``word`` 在 ``text`` 中的每一次出现是否都落在 ``spans`` 里。

    「人多吗？人是真的多」中的「人多」有两处出现，一处在疑问句、
    一处在陈述句，这时不能拦——只有**全部**出现都在疑问句里才判定为提问对象。

    Returns:
        True 表示这个词只在疑问句里出现过，应当丢弃。
        词在原文中一次都找不到（模型做了同义改写）时返回 False，
        交给幻觉校验那一层去管，这里不越权。
    """
    if not word or not text or not spans:
        return False

    found = False
    pos = 0
    while True:
        idx = text.find(word, pos)
        if idx < 0:
            break
        found = True
        end = idx + len(word)
        if not any(s <= idx and end <= e for s, e in spans):
            return False          # 有一次出现在陈述句里 → 放行
        pos = idx + 1
    return found


# --------------------------------------------------------------------------- 数量词
_NUMERALS = "0-9０-９一二三四五六七八九十百千万两半几俩仨壹贰叁肆伍陆柒捌玖拾"

#: 量词/单位。放在 (?:...)* 里可重复，才能吃掉「2个小时」这种量词叠量词的写法。
_UNITS = (
    "岁|个月|个|人|位|名|口|米|公里|千米|里|块|元|角|毛|钱|天|日|小时|钟头|"
    "分钟|秒|号|点|次|回|趟|年|月|周|星期|层|楼|度|斤|公斤|克|吨|台|辆|张|"
    "份|杯|条|只|头|件|种|类|排|档|星|级|折"
)

#: 整词都是「数字 + 量词 + 约数后缀」→ 是事实描述，不是评价。
#:     4岁多 / 两小时 / 50块 / 3个人 / 2026 / 五点半
#: 「排队两小时」词头是「排队」，匹配不上（正则从头锚定），会被放行——这是关键。
_QUANTITY_ONLY = re.compile(
    rf"^[{_NUMERALS}]+(?:{_UNITS})*(?:[{_NUMERALS}]+(?:{_UNITS})*)*"
    rf"(?:多|余|来|左右|上下|出头|以上|以下|不到|整)?$"
)


def is_quantity_only(word: str) -> bool:
    """整个词是不是纯数量表达（不带任何评价色彩）。

    >>> is_quantity_only("4岁多")
    True
    >>> is_quantity_only("排队两小时")      # 词头是评价对象，留住
    False
    >>> is_quantity_only("五星好评")
    False
    """
    w = word.strip()
    if not w:
        return False
    return bool(_QUANTITY_ONLY.match(w))
