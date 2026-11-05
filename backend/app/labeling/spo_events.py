# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主谓宾方案：把评论里每一处评价拆成「主语 / 谓语 / 宾语 / 补充」，另提一组关键字。

和「原因关键事件」（reason_events.py）是同一个问题的另一种做法，用来对比效果。
同样和现有 AI 标注互不相干：单独的提示词、单独调一次模型，不读也不改任何标注列。

每个三元组：
    subject    主语：被评价的东西（不是「我」「我们」），省略了可以从上下文补，没有对象写「景区整体」
    predicate  谓语：评价动作或评价词（好吃、贵、没有、讲）
    object     宾语：没有就空（「烤鸭好吃」没有宾语）
    adjunct    补充：状语 / 补语里有信息的部分——程度、时长、数量、原因、条件、对比
    sentiment  好评 / 中评 / 差评
另外 keywords：实体名和评价词，原子化的短词。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .reason_events import (BAD, GOOD, NEUTRAL, SENTIMENTS, _SPLIT_PUNCT, _squash,
                            build_messages, normalize_sentiment)

__all__ = ["SENTIMENTS", "Triple", "SpoResult", "build_system_prompt", "build_messages",
           "parse_output", "validate", "extract"]

WHOLE = "景区整体"
SLOT_MAX_LEN = 20
KEYWORD_MIN, KEYWORD_MAX = 2, 10
DEFAULT_MAX_TRIPLES = 10
DEFAULT_MAX_KEYWORDS = 10
_EDGE = " \t\r\n。，、；：！？…,.;:!?\"'“”‘’「」『』()（）[]【】~～—-"


_SYSTEM_TEMPLATE = """你是景区游客评论分析员。任务：把评论里每一处「评价」拆成主谓宾结构，并提取关键字。

================ 规则 ================
1. 每个三元组对应原文里的一处评价：
   - subject 主语：被评价的东西（景点、建筑、项目、菜品、商品、人员、设施、票价、排队等）。
     注意主语是「被评价的东西」，不是「我」「我们」「游客」。
     这一分句省略了主语时，按上下文补上（用原文里出现过的词）；原文确实没有对象（"太值了""完美的一天"）写"景区整体"。
   - predicate 谓语：评价动作或评价词，如"好吃""贵""没有""讲""排队""值回"。不能为空。
   - object 宾语：谓语带的宾语，没有就填空字符串（"烤鸭好吃"没有宾语）。
   - adjunct 补充：状语、补语里有信息的部分——程度、时长、数量、原因、条件、对比，
     如"快两小时""挺细""有点""因为下雨""比上次"；没有就填空字符串。"很""也""就"这类没信息的不要。
   - 谓语、宾语、补充都必须用原文里的字，不改写、不同义替换；每一项不跨越逗号句号等标点。
   - sentiment 只能是：好评 / 中评 / 差评
     好评：明确肯定、满意、推荐；
     中评：有评价但态度平淡或有保留（"一般""还行""中规中矩"），或者是建议（"希望多放点座椅"）；
     差评：明确否定、抱怨，或者陈述了一个问题事实（"排队排了快两小时""厕所没纸"）。
     按上下文真实意思判，反讽按反面判；每个三元组自己判，不要求和整条评论一致，好的坏的都要拆。
2. 不要拆：没有评价的客观描述（"周末去的""从南门进""导游是本地人"）、只有对象没有评价、语气词表情、广告和与景区无关的内容。
3. 同一处评价只拆一次，最多 {max_triples} 个，按重要性排。
4. keywords：评论里的关键字——实体名（景点、店名、菜品、人物、作品名）和评价词。
   每个 {kw_min}~{kw_max} 个字，原文里原样出现，不要整句，不要"景区""感觉""非常"这类没信息的词，最多 {max_keywords} 个。
5. 评论与景区无关、或者没有任何评价时，triples 输出空数组。

================ 输出 ================
只输出一行 JSON，不要解释、不要 markdown、不要思考过程：
{{"triples":[{{"subject":"","predicate":"","object":"","adjunct":"","sentiment":"好评|中评|差评"}}],"keywords":[""]}}

================ 示例 ================
{fewshot}
"""


def _t(s: str, p: str, o: str, a: str, senti: str) -> Dict[str, str]:
    return {"subject": s, "predicate": p, "object": o, "adjunct": a, "sentiment": senti}


#: 和原因方案用同一批示例，对比才公平；同样不放验收用的那几条评论
_FEWSHOT: List[Tuple[str, Dict[str, Any]]] = [
    ("景区名称：黄山\n评论正文：风景真的绝了，云海太震撼，但是索道排队排了快两小时，厕所也脏。",
     {"triples": [_t("风景", "绝了", "", "真的", GOOD), _t("云海", "震撼", "", "太", GOOD),
                  _t("索道", "排队", "", "快两小时", BAD), _t("厕所", "脏", "", "", BAD)],
      "keywords": ["风景", "云海", "震撼", "索道", "排队", "厕所"]}),
    ("景区名称：瑶琳仙境\n评论正文：门票100有点小贵，不过讲解员讲得挺细的，值回票价。我们周末带娃去的。",
     {"triples": [_t("门票", "贵", "", "有点小", BAD), _t("讲解员", "讲", "", "挺细", GOOD),
                  _t("门票", "值回", "票价", "", GOOD)],
      "keywords": ["门票", "讲解员", "值回票价", "周末", "带娃"]}),
    ("景区名称：某古镇\n评论正文：景区门口的烤肠味道不错，就是有点贵。古街一般般吧，希望多放点座椅。",
     {"triples": [_t("烤肠味道", "不错", "", "", GOOD), _t("烤肠", "贵", "", "有点", BAD),
                  _t("古街", "一般般", "", "", NEUTRAL), _t("景区整体", "放", "座椅", "希望多", NEUTRAL)],
      "keywords": ["烤肠", "味道不错", "古街", "一般般", "座椅"]}),
    ("景区名称：玻璃栈道景区\n评论正文：因为下雨玻璃栈道没开，白跑一趟，太坑了",
     {"triples": [_t("玻璃栈道", "没开", "", "因为下雨", BAD), _t("景区整体", "白跑", "一趟", "", BAD),
                  _t("景区整体", "坑", "", "太", BAD)],
      "keywords": ["下雨", "玻璃栈道", "白跑一趟", "坑"]}),
    ("景区名称：西湖\n评论正文：家人们谁懂啊，我这条裙子才89，链接放评论区了",
     {"triples": [], "keywords": []}),
]


def build_system_prompt(*, max_triples: int = DEFAULT_MAX_TRIPLES,
                        max_keywords: int = DEFAULT_MAX_KEYWORDS) -> str:
    shots = [f"--- 示例 {i} ---\n输入：\n{inp}\n输出：\n{json.dumps(out, ensure_ascii=False)}"
             for i, (inp, out) in enumerate(_FEWSHOT, start=1)]
    return _SYSTEM_TEMPLATE.format(max_triples=max_triples, max_keywords=max_keywords,
                                   kw_min=KEYWORD_MIN, kw_max=KEYWORD_MAX,
                                   fewshot="\n\n".join(shots))


# ============================================================================ 解析与校验
@dataclass
class Triple:
    subject: str
    predicate: str
    object: str
    adjunct: str
    sentiment: str

    def to_dict(self) -> Dict[str, str]:
        return {"subject": self.subject, "predicate": self.predicate, "object": self.object,
                "adjunct": self.adjunct, "sentiment": self.sentiment}

    def display(self) -> str:
        """给人看的写法：主语·谓语·宾语（补充）"""
        text = "·".join(x for x in (self.subject, self.predicate, self.object) if x)
        return f"{text}（{self.adjunct}）" if self.adjunct else text

    def joined(self) -> str:
        """主谓宾直接拼起来（不含补充），用来看拼回去在原文里能不能搜到。"""
        subject = "" if self.subject == WHOLE else self.subject
        return f"{subject}{self.predicate}{self.object}"


@dataclass
class SpoResult:
    triples: List[Triple] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    dropped: List[Tuple[str, str]] = field(default_factory=list)

    def by_sentiment(self, sentiment: str) -> List[Triple]:
        return [t for t in self.triples if t.sentiment == sentiment]


def parse_output(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"输出里没有 JSON：{raw[:200]}")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("输出不是 JSON 对象")
    triples = data.get("triples") or []
    keywords = data.get("keywords") or []
    if not isinstance(triples, list) or not isinstance(keywords, list):
        raise ValueError("triples / keywords 不是数组")
    return {"triples": [x for x in triples if isinstance(x, dict)],
            "keywords": [str(x) for x in keywords if isinstance(x, (str, int, float))]}


def _clean(v: Any) -> str:
    return str(v or "").strip().strip(_EDGE)


def validate(payload: Dict[str, Any], text: str, *,
             max_triples: int = DEFAULT_MAX_TRIPLES,
             max_keywords: int = DEFAULT_MAX_KEYWORDS) -> SpoResult:
    """校验：好坏取值 → 谓语不能空 → 各项不跨标点、不超长 → 谓语/宾语/补充原文出现 → 主语原文出现或是「景区整体」→ 去重 → 限量。"""
    res = SpoResult()
    hay = _squash(text)
    seen = set()
    for item in payload.get("triples") or []:
        s, p = _clean(item.get("subject")), _clean(item.get("predicate"))
        o, a = _clean(item.get("object")), _clean(item.get("adjunct"))
        label = "·".join(x for x in (s, p, o) if x) + (f"（{a}）" if a else "")
        senti = normalize_sentiment(item.get("sentiment"))
        if senti is None:
            res.dropped.append((label, "好坏取值不对"))
            continue
        if not p:
            res.dropped.append((label, "没有谓语"))
            continue
        s = s or WHOLE
        slots = [("主语", s), ("谓语", p), ("宾语", o), ("补充", a)]
        bad = ""
        for name, v in slots:
            if not v or (name == "主语" and v == WHOLE):
                continue
            if _SPLIT_PUNCT.search(v):
                bad = f"{name}跨了标点"
            elif len(_squash(v)) > SLOT_MAX_LEN:
                bad = f"{name}太长"
            elif _squash(v) not in hay:
                bad = f"{name}原文里没有"
            if bad:
                break
        if bad:
            res.dropped.append((label, bad))
            continue
        key = (s, p, o, a)
        if key in seen:
            continue
        seen.add(key)
        res.triples.append(Triple(s, p, o, a, senti))
    if len(res.triples) > max_triples:
        for t in res.triples[max_triples:]:
            res.dropped.append((t.display(), "超过条数上限"))
        res.triples = res.triples[:max_triples]

    kw_seen = set()
    for kw in payload.get("keywords") or []:
        w = _clean(kw)
        if not w:
            continue
        core = _squash(w)
        if len(core) < KEYWORD_MIN:
            res.dropped.append((w, "关键字太短"))
        elif len(core) > KEYWORD_MAX:
            res.dropped.append((w, "关键字太长"))
        elif _SPLIT_PUNCT.search(w):
            res.dropped.append((w, "关键字跨了标点"))
        elif core not in hay:
            res.dropped.append((w, "关键字原文里没有"))
        elif core not in kw_seen:
            kw_seen.add(core)
            res.keywords.append(w)
    res.keywords = res.keywords[:max_keywords]
    return res


def extract(model_text: str, cleaned_text: str, **kw: Any) -> SpoResult:
    return validate(parse_output(model_text), cleaned_text, **kw)


def joined_searchable(triple: Triple, raw_content: str) -> bool:
    """主谓宾拼回去能不能在原始评论里搜到（原因方案的片段天然能搜到，这里拿来对比）。"""
    return bool(triple.joined()) and _squash(triple.joined()) in _squash(raw_content)
