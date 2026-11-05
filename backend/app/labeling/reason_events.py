# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""原因关键事件：从一条评论里摘出「因为什么好 / 不好不坏 / 不好」。

和现有 AI 标注是**两条独立的路**：单独的提示词、单独调一次模型，
不读也不改 sentiment_label / keyword_tags 这些列，现有标注逻辑一行不动。

每条原因三样东西：
    reason     原文里连续的一段（逐字照抄，拿它去搜评论正文一定搜得到）
    target     被评价的东西，可以从前文补全（「非常正宗实惠」→ 螺蛳粉），按对象汇总用
    sentiment  好评 / 中评 / 差评，每条原因自己判，不要求和整条评论同向

这个模块只管「提示词 + 解析 + 校验」，不碰数据库和网络，方便单测和以后接进系统。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SENTIMENTS", "ReasonItem", "ReasonResult",
    "build_system_prompt", "build_user_content", "build_messages",
    "parse_output", "validate_reasons", "extract",
]

GOOD, NEUTRAL, BAD = "好评", "中评", "差评"
SENTIMENTS = (GOOD, NEUTRAL, BAD)

#: 模型可能写成别的说法，统一归到三个值
_SENTIMENT_ALIASES = {
    "好评": GOOD, "好": GOOD, "正向": GOOD, "正面": GOOD, "积极": GOOD, "positive": GOOD,
    "中评": NEUTRAL, "中": NEUTRAL, "中性": NEUTRAL, "一般": NEUTRAL, "中立": NEUTRAL,
    "不好不坏": NEUTRAL, "neutral": NEUTRAL,
    "差评": BAD, "差": BAD, "坏": BAD, "负向": BAD, "负面": BAD, "消极": BAD, "negative": BAD,
}

DEFAULT_MAX_LEN = 20
DEFAULT_MAX_COUNT = 8
MIN_LEN = 2

#: 原因里不许有这些标点：有了就说明跨了分句，成了整句照抄
_SPLIT_PUNCT = re.compile(r"[。，、；：！？…,.;:!?\n\r\t]")
#: 首尾可以去掉的符号（模型常把引号、句号一起抄进来）
_EDGE_CHARS = " \t\r\n。，、；：！？…,.;:!?\"'“”‘’「」『』()（）[]【】~～—-"
_WHITESPACE = re.compile(r"\s+")


# ============================================================================ 提示词
_SYSTEM_TEMPLATE = """你是景区游客评论分析员。任务：从一条评论里，把让游客觉得「好」「不好不坏」「不好」的原因逐条摘出来。
摘出来的原因要让人一眼看懂「是什么怎么样」，并且能拿去原文里搜到。

================ 规则 ================
1. 原因必须是评论原文里【连续的一段】：逐字照抄，不改字、不补字、不跳字、不调换顺序，不跨越逗号句号等标点。
2. 片段要说清「什么 + 怎么样」：向前带上被评价的东西（景点、建筑、项目、菜品、商品、人员、设施、票价、交通、排队等），向后带上评价。
   - 被评价的东西和评价在同一个分句里，就一起摘："大殿的壁画没什么看头"，不要只摘"没什么看头"。
   - 被评价的东西在别的分句里、隔着标点接不上时，只摘评价那部分，不要补字；在 target 里写清它评价的是什么。
   - 原文确实没有具体对象（"完美的一天""太值了"），照原文摘，target 写"景区整体"。
3. 原因、条件、对比要保留："因为下雨玻璃栈道没开""比上次好""要是不排队就好了"。
4. 不要摘：
   - 没有评价的客观描述："周末去的""从南门进""带娃去的""导游是本地人"（原文没写"因为"的隐含理由不算）
   - 只有对象没有评价：单独的"大殿""烤肠"
   - 语气词、表情、网络口头禅、单个字
   - 广告、带货、招聘、与景区无关的闲聊
5. 每条原因单独判 sentiment，只能是：好评 / 中评 / 差评
   - 好评：明确肯定、满意、推荐
   - 中评：有评价但态度平淡或有保留（"一般""还行""中规中矩"），或者是建议（"希望多放点座椅"）
   - 差评：明确否定、抱怨，或者陈述了一个问题事实（"排队排了快两小时""厕所没纸""停车场满了"）
   - 按上下文的真实意思判：反讽按反面判；同一对象后面紧跟更明确的否定时（"还行吧，其实没什么意思"），按差评
   - 不要求和整条评论的态度一致：一条评论里好的、坏的都要摘
6. 同一个意思只摘一次。每条 {min_len}~{max_len} 个字，最多 {max_count} 条，按重要性排。
7. 评论与景区无关、或者没有任何评价时，reasons 输出空数组。

================ 输出 ================
只输出一行 JSON，不要解释、不要 markdown、不要思考过程：
{{"reasons":[{{"reason":"原文连续片段","target":"被评价的东西","sentiment":"好评|中评|差评"}}]}}

================ 示例 ================
{fewshot}
"""

#: 示例故意不用验收要看的那几条评论，免得模型背答案、验证失真
_FEWSHOT: List[Tuple[str, List[Dict[str, str]]]] = [
    ("景区名称：黄山\n评论正文：风景真的绝了，云海太震撼，但是索道排队排了快两小时，厕所也脏。",
     [{"reason": "风景真的绝了", "target": "风景", "sentiment": GOOD},
      {"reason": "云海太震撼", "target": "云海", "sentiment": GOOD},
      {"reason": "索道排队排了快两小时", "target": "索道排队", "sentiment": BAD},
      {"reason": "厕所也脏", "target": "厕所", "sentiment": BAD}]),
    ("景区名称：瑶琳仙境\n评论正文：门票100有点小贵，不过讲解员讲得挺细的，值回票价。我们周末带娃去的。",
     [{"reason": "门票100有点小贵", "target": "门票", "sentiment": BAD},
      {"reason": "讲解员讲得挺细的", "target": "讲解员", "sentiment": GOOD},
      {"reason": "值回票价", "target": "门票", "sentiment": GOOD}]),
    ("景区名称：某古镇\n评论正文：景区门口的烤肠味道不错，就是有点贵。古街一般般吧，希望多放点座椅。",
     [{"reason": "烤肠味道不错", "target": "烤肠", "sentiment": GOOD},
      {"reason": "就是有点贵", "target": "烤肠", "sentiment": BAD},
      {"reason": "古街一般般", "target": "古街", "sentiment": NEUTRAL},
      {"reason": "希望多放点座椅", "target": "座椅", "sentiment": NEUTRAL}]),
    ("景区名称：玻璃栈道景区\n评论正文：因为下雨玻璃栈道没开，白跑一趟，太坑了",
     [{"reason": "因为下雨玻璃栈道没开", "target": "玻璃栈道", "sentiment": BAD},
      {"reason": "白跑一趟", "target": "景区整体", "sentiment": BAD},
      {"reason": "太坑了", "target": "景区整体", "sentiment": BAD}]),
    ("景区名称：西湖\n评论正文：家人们谁懂啊，我这条裙子才89，链接放评论区了",
     []),
]


def build_system_prompt(*, max_len: int = DEFAULT_MAX_LEN, max_count: int = DEFAULT_MAX_COUNT) -> str:
    shots = []
    for idx, (inp, out) in enumerate(_FEWSHOT, start=1):
        shots.append(f"--- 示例 {idx} ---\n输入：\n{inp}\n输出：\n"
                     f"{json.dumps({'reasons': out}, ensure_ascii=False)}")
    return _SYSTEM_TEMPLATE.format(min_len=MIN_LEN, max_len=max_len, max_count=max_count,
                                   fewshot="\n\n".join(shots))


def build_user_content(text: str, scenic_name: str = "") -> str:
    lines = []
    if scenic_name:
        lines.append(f"景区名称：{scenic_name}")
    lines.append(f"评论正文：{text}")
    lines.append("")
    lines.append("请按规则输出 JSON。")
    return "\n".join(lines)


def _msg(role: str, text: str) -> Dict[str, Any]:
    """方舟 responses 协议的 input item（和标注引擎的写法一致）。"""
    return {"role": role, "content": [{"type": "input_text", "text": text}]}


def build_messages(text: str, scenic_name: str = "", *, system_prompt: str,
                   system_role: str = "system") -> List[Dict[str, Any]]:
    user = build_user_content(text, scenic_name)
    if system_role == "user":            # 和引擎的 llm.system_role 保持一致
        return [_msg("user", f"{system_prompt}\n\n{user}")]
    return [_msg("system", system_prompt), _msg("user", user)]


# ============================================================================ 解析与校验
@dataclass
class ReasonItem:
    reason: str
    target: str
    sentiment: str

    def to_dict(self) -> Dict[str, str]:
        return {"reason": self.reason, "target": self.target, "sentiment": self.sentiment}


@dataclass
class ReasonResult:
    items: List[ReasonItem] = field(default_factory=list)
    #: 没通过校验被丢掉的：[(片段, 原因码)]
    dropped: List[Tuple[str, str]] = field(default_factory=list)

    def by_sentiment(self, sentiment: str) -> List[str]:
        return [i.reason for i in self.items if i.sentiment == sentiment]


def parse_output(text: str) -> List[Dict[str, Any]]:
    """从模型输出里取出 reasons 数组。容忍 markdown 代码块、前后多余文字。

    Raises:
        ValueError: 找不到合法 JSON。
    """
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        # 有的模型直接吐数组
        s2, e2 = raw.find("["), raw.rfind("]")
        if s2 >= 0 and e2 > s2:
            data = json.loads(raw[s2:e2 + 1])
            return [x for x in data if isinstance(x, dict)]
        raise ValueError(f"输出里没有 JSON：{raw[:200]}")
    data = json.loads(raw[start:end + 1])
    items = data.get("reasons") if isinstance(data, dict) else None
    if items is None:
        items = data.get("reason_tags") or data.get("items") or []
    if not isinstance(items, list):
        raise ValueError("reasons 不是数组")
    return [x for x in items if isinstance(x, dict)]


def _squash(s: str) -> str:
    """比对时忽略空白：清洗后的正文是「门票 100 有点小贵」，模型常写成「门票100有点小贵」。"""
    return _WHITESPACE.sub("", s or "")


def normalize_sentiment(value: Any) -> Optional[str]:
    key = str(value or "").strip().lower()
    return _SENTIMENT_ALIASES.get(key)


def validate_reasons(items: List[Dict[str, Any]], text: str, *,
                     max_len: int = DEFAULT_MAX_LEN,
                     max_count: int = DEFAULT_MAX_COUNT) -> ReasonResult:
    """逐条校验模型给的原因。

    过滤顺序：好坏取值 → 去首尾符号 → 长度 → 跨标点 → 原文逐字连续出现 → 去重 → 被更长的包含 → 限量
    """
    result = ReasonResult()
    haystack = _squash(text)
    kept: List[ReasonItem] = []
    seen = set()

    for item in items:
        reason = str(item.get("reason") or item.get("word") or "").strip().strip(_EDGE_CHARS)
        if not reason:
            continue
        sentiment = normalize_sentiment(item.get("sentiment") or item.get("polarity"))
        if sentiment is None:
            result.dropped.append((reason, "好坏取值不对"))
            continue
        core = _squash(reason)
        if len(core) < MIN_LEN:
            result.dropped.append((reason, "太短"))
            continue
        if len(core) > max_len:
            result.dropped.append((reason, "太长"))
            continue
        if _SPLIT_PUNCT.search(reason):
            result.dropped.append((reason, "跨了标点"))
            continue
        if core not in haystack:
            result.dropped.append((reason, "原文里不连续"))
            continue
        if core in seen:
            continue
        seen.add(core)
        target = str(item.get("target") or "").strip() or "景区整体"
        kept.append(ReasonItem(reason=reason, target=target[:30], sentiment=sentiment))

    # 被另一条更长的原因完整包含的短原因去掉（「没有意义」⊂「池神庙建筑没有意义」）
    final: List[ReasonItem] = []
    for it in kept:
        core = _squash(it.reason)
        if any(core != _squash(o.reason) and core in _squash(o.reason) for o in kept):
            result.dropped.append((it.reason, "被更长的包含"))
            continue
        final.append(it)

    if len(final) > max_count:
        for it in final[max_count:]:
            result.dropped.append((it.reason, "超过条数上限"))
        final = final[:max_count]
    result.items = final
    return result


def searchable_in(reason: str, raw_content: str) -> bool:
    """这条原因拿去搜**原始**评论正文（未清洗）能不能搜到。忽略空白。"""
    return _squash(reason) in _squash(raw_content)


def extract(model_text: str, cleaned_text: str, **kw: Any) -> ReasonResult:
    """解析 + 校验一步到位。"""
    return validate_reasons(parse_output(model_text), cleaned_text, **kw)
