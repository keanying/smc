"""关键词聚合：把同义的关键词归并到归类关键词。

为什么是确定性字典而不是让模型直接给归类词：

    这件事的全部价值就在「一致」。模型每次可能给「人多」「人流量大」「拥挤」，
    那等于没归并。字典的性质正好相反——同样的输入永远得到同样的输出，
    改了词典重跑一遍历史数据也跟着变，而且**一次模型调用都不用**。

匹配是「宽进严出」：一个原始关键词可以同时命中多个归类词
（「排队两小时人还特别多」→ 排队久 + 人多拥挤），但归类词本身是封闭集合，
不在 aggregation.yaml 里的词永远不会出现在结果中。

**否定词守卫**是这里最要紧的一条规则。词典里正负两组词天然互相包含：

    不好吃  含「好吃」  →  会同时命中「东西好吃」和「东西难吃」
    不拥挤  含「拥挤」  →  会同时命中「人多拥挤」和「不用排队」
    态度不好 含「态度好」→  会同时命中「服务热情」和「服务态度差」

一条好评因此在负面榜上留了一笔，词典越大这种对撞越多。所以包含匹配与正则
匹配都要看命中位置的**前几个字**：被「不/没/无/免/别」否定掉的，这一组不算命中。
同时保留一份"假否定"白名单（无敌、不错、不虚此行……），它们带否定字却是褒义。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import yaml

from ..config import ConfigError
from ..logging_conf import get_logger

__all__ = ["AggregationGroup", "KeywordAggregator"]

logger = get_logger(__name__)


@dataclass
class AggregationGroup:
    """一个归类关键词及其同义词。"""

    canonical: str
    polarity: str = ""
    #: 这个归类词对应 taxonomy 里的哪条维度路径，如 "游玩体验/排队时长"。
    #: 纯元数据，不参与匹配——它的用处是让 aggregation_keyword_tags
    #: 能和 dimension_tags 对上：看板上点「排队久」可以直接下钻到那个维度。
    dim: str = ""
    synonyms: List[str] = field(default_factory=list)
    patterns: List[re.Pattern] = field(default_factory=list)
    #: 一票否决的正则：命中任意一条，这一组直接不算命中。
    #: 用来处理否定词守卫兜不住的反例（如「人少」不该进「人多拥挤」）。
    exclude: List[re.Pattern] = field(default_factory=list)

    #: 归一化后的同义词，匹配时用
    normalized: Set[str] = field(default_factory=set)

    def vetoed(self, norm: str, raw: str) -> bool:
        return any(p.search(norm) or p.search(raw) for p in self.exclude)

    def __repr__(self) -> str:      # pragma: no cover - 调试用
        return f"<AggregationGroup {self.canonical} ({len(self.synonyms)} 个同义词)>"


# --------------------------------------------------------------------------- 否定守卫
#: 否定语素。命中位置往前看 3 个字，出现其中之一就认为这一组被否定了。
#: 故意**不**收「别」和「莫」：它们做否定只出现在句首祈使（"别来了"），
#: 而在词中间几乎全是「特别 / 区别 / 个别 / 莫名」这类构词——
#: 收了它们，「人还特别多」会被当成"人不多"，这正是踩过的坑。
#: "别来了"这类说法已经被「不推荐」组的同义词直接收了，不靠守卫。
_NEGATORS = "不没无未勿甭免非"

#: 「假否定」：带否定字但整体是褒义或中性固定搭配的词，不能当否定处理。
#: 少了这份白名单，「无敌好看」会被判成"不好看"，「不虚此行」会被判成负面。
_FALSE_NEGATION = (
    "无敌", "无与伦比", "无可挑剔", "无可替代", "无数", "无论", "无意中",
    "不错", "不愧", "不虚", "不容", "不但", "不仅", "不只", "不失为",
    "不得不", "不由得", "不知不觉", "没想到", "没得说", "别有", "别提",
    "非常", "非去不可", "莫名",
)

#: 往命中位置前面看几个字。2 个太短（"不是很好吃"看不到"不"），
#: 4 个太长会把不相干的否定拉进来。
_NEG_WINDOW = 3


def _is_negator_at(text: str, pos: int) -> bool:
    """``text[pos]`` 是不是一个真的否定语素（排掉"无敌""不虚此行"这类假否定）。"""
    if text[pos] not in _NEGATORS:
        return False
    ahead = text[pos:pos + 4]
    return not any(ahead.startswith(phrase) for phrase in _FALSE_NEGATION)


def _negated_before(norm: str, idx: int) -> bool:
    """``norm[idx:]`` 这次命中，是不是被前面的否定词否掉了。"""
    if idx <= 0:
        return False
    return any(_is_negator_at(norm, pos)
               for pos in range(max(0, idx - _NEG_WINDOW), idx))


def _negated_inside(span: str) -> bool:
    """正则命中的那一段里**夹着**否定词。

    这是正则特有的坑：``(风景|景色).{0,4}(美|好)`` 会整段吃掉「风景不美」，
    命中位置在"风"上，前面什么都没有，_negated_before 看不见那个"不"。
    所以还要往命中段**内部**看一眼——首字不算（「不虚此行」这类词本身以否定字开头）。
    """
    return any(_is_negator_at(span, pos) for pos in range(1, len(span)))


def _normalize(text: str) -> str:
    """归一化：全角转半角、去空白与常见标点、转小写。

    做这一步是因为原始关键词来自各个平台的用户输入，
    「人 太 多」「人太多！」「ＮＩＣＥ」应该和「人太多」「nice」算同一个词。
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s、，,。.!！?？~～\-—_/\\()（）\[\]【】\"'“”‘’]+", "", out)


class KeywordAggregator:
    """关键词 → 归类关键词。无状态（除了统计计数），线程安全。"""

    def __init__(self, groups: Sequence[AggregationGroup]) -> None:
        self._groups = list(groups)
        self._exact: Dict[str, List[str]] = {}
        # 包含匹配按同义词长度倒序：先试长的，
        # 否则「人多」会把「人山人海」里的判断抢走，归类粒度就散了
        self._contains: List[Tuple[str, str]] = []

        for group in self._groups:
            for norm in group.normalized:
                self._exact.setdefault(norm, [])
                if group.canonical not in self._exact[norm]:
                    self._exact[norm].append(group.canonical)
                self._contains.append((norm, group.canonical))
        self._contains.sort(key=lambda item: len(item[0]), reverse=True)

        #: 没能归类的原始关键词计数，供 `keywords` 命令统计出来扩词典
        self.unmatched: Dict[str, int] = {}

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: Path | str) -> "KeywordAggregator":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"聚合词典不存在：{p}")
        with p.open("r", encoding="utf-8-sig") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"聚合词典根节点必须是字典：{p}")
        return cls.from_raw(raw)

    @classmethod
    def from_raw(cls, raw: Dict) -> "KeywordAggregator":
        groups: List[AggregationGroup] = []
        seen: Set[str] = set()

        for item in raw.get("groups") or []:
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            canonical = str(item.get("canonical", "")).strip()
            if not canonical:
                raise ConfigError("aggregation.groups 存在缺少 canonical 的分组")
            if canonical in seen:
                raise ConfigError(f"归类关键词重复：{canonical!r}")
            seen.add(canonical)

            synonyms = [str(s).strip() for s in (item.get("synonyms") or []) if str(s).strip()]
            # 归类词自己也算自己的同义词，否则「人多拥挤」这个词本身匹配不上
            synonyms.append(canonical)

            def _compile(key: str) -> List[re.Pattern]:
                out = []
                for expr in item.get(key) or []:
                    try:
                        out.append(re.compile(str(expr)))
                    except re.error as exc:
                        raise ConfigError(
                            f"归类词 {canonical!r} 的 {key} 正则 {expr!r} "
                            f"无法编译：{exc}") from exc
                return out

            groups.append(AggregationGroup(
                canonical=canonical,
                polarity=str(item.get("polarity", "")).strip(),
                dim=str(item.get("dim", "")).strip(),
                synonyms=synonyms,
                patterns=_compile("patterns"),
                exclude=_compile("exclude"),
                normalized={n for n in (_normalize(s) for s in synonyms) if n},
            ))

        if not groups:
            raise ConfigError("聚合词典里没有任何启用的分组")
        return cls(groups)

    # ------------------------------------------------------------------ 匹配
    def match(self, keyword: str) -> List[str]:
        """把一个原始关键词映射成归类关键词列表（可能为空，也可能多个）。"""
        norm = _normalize(keyword)
        if not norm:
            return []

        hits: List[str] = []
        vetoed = {g.canonical for g in self._groups if g.exclude and g.vetoed(norm, keyword)}

        # 1) 精确命中——整词相等，不存在"被前面的字否定"的可能，直接用
        for canonical in self._exact.get(norm, []):
            if canonical not in hits and canonical not in vetoed:
                hits.append(canonical)

        # 2) 包含匹配，长同义词优先；被否定词挡住的那次命中不算，
        #    但同一个同义词在别处还有一次干净的出现就仍然算（"不拥挤但确实拥挤"）
        for syn, canonical in self._contains:
            if canonical in hits or canonical in vetoed or not syn:
                continue
            idx = norm.find(syn)
            while idx >= 0:
                if not _negated_before(norm, idx):
                    hits.append(canonical)
                    break
                idx = norm.find(syn, idx + 1)

        # 3) 正则——处理「排队.*小时」这类构造出来的说法，同样要过否定守卫
        for group in self._groups:
            if group.canonical in hits or group.canonical in vetoed:
                continue
            if self._pattern_hit(group, norm, keyword):
                hits.append(group.canonical)

        return hits

    @staticmethod
    def _pattern_hit(group: AggregationGroup, norm: str, raw: str) -> bool:
        for pattern in group.patterns:
            for text in (norm, raw):
                for m in pattern.finditer(text):
                    if _negated_before(text, m.start()):
                        continue
                    if _negated_inside(m.group(0)):
                        continue
                    return True
        return False

    def aggregate(self, keywords: Iterable[str], *,
                  track_unmatched: bool = True) -> List[str]:
        """把一组关键词聚合成去重后的归类关键词列表。

        顺序按归类词在词典里的定义顺序，保证同一批关键词无论传入顺序如何，
        产出的数组都一样——落库之后做等值比较、算 diff 才有意义。
        """
        hit: Set[str] = set()
        for keyword in keywords:
            matched = self.match(keyword)
            if matched:
                hit.update(matched)
            elif track_unmatched:
                key = str(keyword).strip()
                if key:
                    self.unmatched[key] = self.unmatched.get(key, 0) + 1

        return [g.canonical for g in self._groups if g.canonical in hit]

    # ------------------------------------------------------------------ 观测
    def top_unmatched(self, limit: int = 30) -> List[Tuple[str, int]]:
        """没归类上的关键词按频次排序。频次高的就是该补进词典的。"""
        return sorted(self.unmatched.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    def reset_unmatched(self) -> None:
        self.unmatched.clear()

    @property
    def canonicals(self) -> List[str]:
        return [g.canonical for g in self._groups]

    def polarity_of(self, canonical: str) -> str:
        for group in self._groups:
            if group.canonical == canonical:
                return group.polarity
        return ""

    def dim_of(self, canonical: str) -> str:
        """归类词对应的维度路径，用于看板从归类词下钻到维度。"""
        for group in self._groups:
            if group.canonical == canonical:
                return group.dim
        return ""

    def describe(self) -> List[Tuple[str, str, str, int]]:
        """(归类词, 情感, 维度路径, 同义词数) —— `check` 命令用它打词典总览。"""
        return [(g.canonical, g.polarity, g.dim, len(g.synonyms)) for g in self._groups]

    def __len__(self) -> int:
        return len(self._groups)

    def __repr__(self) -> str:      # pragma: no cover
        return f"<KeywordAggregator {len(self._groups)} 组>"
