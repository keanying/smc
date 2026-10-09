"""标签体系：加载 taxonomy.yaml，提供白名单校验与提示词素材。

这个类是「维度 / 实体 / 情感字面值」的唯一真源：
    - 提示词里给模型看什么，来自这里
    - 模型吐回来的东西合不合法，也判在这里
两边共用一份数据，杜绝「提示词里写了、白名单里没有」这种最常见的错配。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import yaml

from ..config import ConfigError
from .enums import Sentiment
from .models import DimensionTag, EntityTag

__all__ = ["Taxonomy", "DimensionNode"]

# 维度路径：(dim1, dim2, dim3)，dim3 可为空串
DimPath = Tuple[str, str, str]


class DimensionNode:
    """一条三级维度路径及其示例关键词。"""

    __slots__ = ("dim1", "dim2", "dim3", "positive", "negative")

    def __init__(self, dim1: str, dim2: str, dim3: str,
                 positive: Sequence[str], negative: Sequence[str]) -> None:
        self.dim1 = dim1
        self.dim2 = dim2
        self.dim3 = dim3
        self.positive = list(positive)
        self.negative = list(negative)

    @property
    def path(self) -> DimPath:
        return (self.dim1, self.dim2, self.dim3)

    @property
    def label(self) -> str:
        return " / ".join(p for p in self.path if p)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<DimensionNode {self.label}>"


class Taxonomy:
    """标签体系（不可变，进程内单例即可）。"""

    def __init__(self, raw: Dict) -> None:
        self._raw = raw
        self.version: str = str(raw.get("version", "0"))

        sentiment_cfg = raw.get("sentiment") or {}
        self._sentiment_aliases: Dict[str, str] = self._build_sentiment_aliases(sentiment_cfg)

        self.nodes: List[DimensionNode] = []
        self._dim_paths: Set[DimPath] = set()
        self._dim1_set: Set[str] = set()
        self._dim2_set: Set[Tuple[str, str]] = set()
        self._parse_dimensions(raw.get("dimensions") or [])

        self.entity_types: Dict[str, Set[str]] = {}   # type -> 合法 value 集合
        self.open_entity_types: Set[str] = set()      # 只校验 type、不校验 value
        self.entity_keywords: Dict[Tuple[str, str], List[str]] = {}
        self._parse_entities(raw.get("entities") or [])

        self.keyword_blacklist: Set[str] = {
            str(w).strip().lower() for w in (raw.get("keyword_blacklist") or []) if str(w).strip()
        }

        self._validate()

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: Path | str) -> "Taxonomy":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"标签体系文件不存在：{p}")
        with p.open("r", encoding="utf-8-sig") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"taxonomy 根节点必须是字典：{p}")
        return cls(raw)

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _build_sentiment_aliases(cfg: Dict) -> Dict[str, str]:
        """构造「模型可能吐出的任意写法 -> 标准字面值」映射。"""
        canonical = {
            str(cfg.get("positive", Sentiment.POSITIVE.value)): Sentiment.POSITIVE.value,
            str(cfg.get("neutral", Sentiment.NEUTRAL.value)): Sentiment.NEUTRAL.value,
            str(cfg.get("negative", Sentiment.NEGATIVE.value)): Sentiment.NEGATIVE.value,
        }
        aliases = {k.strip().lower(): v for k, v in canonical.items()}
        for src, dst in (cfg.get("aliases") or {}).items():
            aliases[str(src).strip().lower()] = str(dst)
        # 枚举自身的字面值必须能被识别
        for s in Sentiment:
            aliases.setdefault(s.value.lower(), s.value)
        return aliases

    def _parse_dimensions(self, items: Iterable[Dict]) -> None:
        for d1 in items:
            if not isinstance(d1, dict) or d1.get("enabled") is False:
                continue
            dim1 = str(d1.get("dim1", "")).strip()
            if not dim1:
                raise ConfigError("taxonomy.dimensions 存在缺少 dim1 的节点")
            self._dim1_set.add(dim1)
            for d2 in d1.get("children") or []:
                if not isinstance(d2, dict) or d2.get("enabled") is False:
                    continue
                dim2 = str(d2.get("dim2", "")).strip()
                self._dim2_set.add((dim1, dim2))
                children = d2.get("children") or [{"dim3": None}]
                for d3 in children:
                    if not isinstance(d3, dict) or d3.get("enabled") is False:
                        continue
                    raw_dim3 = d3.get("dim3")
                    dim3 = "" if raw_dim3 in (None, "", "null") else str(raw_dim3).strip()
                    node = DimensionNode(
                        dim1, dim2, dim3,
                        d3.get("positive") or [],
                        d3.get("negative") or [],
                    )
                    self.nodes.append(node)
                    self._dim_paths.add(node.path)

    def _parse_entities(self, items: Iterable[Dict]) -> None:
        for group in items:
            if not isinstance(group, dict) or group.get("enabled") is False:
                continue
            etype = str(group.get("type", "")).strip()
            if not etype:
                raise ConfigError("taxonomy.entities 存在缺少 type 的节点")
            values: Set[str] = set()
            for item in group.get("values") or []:
                if not isinstance(item, dict) or item.get("enabled") is False:
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                values.add(name)
                kws = [str(k).strip() for k in (item.get("keywords") or []) if str(k).strip()]
                self.entity_keywords[(etype, name)] = kws
            self.entity_types[etype] = values
            if group.get("open_value") is True:
                self.open_entity_types.add(etype)

    def _validate(self) -> None:
        if not self.nodes:
            raise ConfigError("taxonomy.dimensions 为空，标注引擎无法工作")
        if not self.entity_types:
            raise ConfigError("taxonomy.entities 为空")
        for etype, values in self.entity_types.items():
            if not values and etype not in self.open_entity_types:
                raise ConfigError(f"实体类型 {etype!r} 没有任何启用的取值，且未声明 open_value")

    # ------------------------------------------------------------- 情感归一
    def normalize_sentiment(self, value) -> Optional[Sentiment]:
        """把模型输出的情感（中文/英文/数字）归一到 :class:`Sentiment`。

        无法识别时返回 ``None``，由调用方决定兜底策略——
        这里刻意不默认返回中性，否则解析错误会被静默吞掉。
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            score = int(value)
            if score in (-1, 0, 1):
                return Sentiment.from_score(score)
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        mapped = self._sentiment_aliases.get(text)
        if mapped:
            return Sentiment(mapped)
        # 容忍「正向情感」「负面评价」这类带修饰的写法
        for alias, canonical in self._sentiment_aliases.items():
            if alias and len(alias) >= 2 and alias in text:
                return Sentiment(canonical)
        return None

    # ------------------------------------------------------------- 白名单
    def is_valid_dimension(self, dim1: str, dim2: str, dim3: str) -> bool:
        return (dim1, dim2, dim3) in self._dim_paths

    def resolve_dimension(self, dim1: str, dim2: str, dim3: str) -> Optional[DimPath]:
        """尽最大努力把模型给的维度对齐到合法路径。

        依次尝试：
            1. 完整路径命中
            2. dim3 写错/多写 —— 若 (dim1,dim2) 下只有一条无三级的路径，则退化到它
            3. dim3 漏写 —— 若 (dim1,dim2) 下只有唯一一条三级路径，则补全
        仍然对不上返回 ``None``，由后处理丢弃。这样做的意义是：
        模型把「设施环境/基础设施/卫生间」写成「厕所」的近义偏差能被救回来，
        而彻底编造的维度不会混进结果。
        """
        dim1 = (dim1 or "").strip()
        dim2 = (dim2 or "").strip()
        dim3 = (dim3 or "").strip()

        if self.is_valid_dimension(dim1, dim2, dim3):
            return (dim1, dim2, dim3)

        siblings = [n for n in self.nodes if n.dim1 == dim1 and n.dim2 == dim2]
        if not siblings:
            return None
        if len(siblings) == 1:
            return siblings[0].path
        # 多个三级维度时，用字符串包含做一次弱匹配（忽略大小写，"wifi信号" → "WiFi"）
        if dim3:
            needle = dim3.casefold()
            for node in siblings:
                if not node.dim3:
                    continue
                target = node.dim3.casefold()
                if needle in target or target in needle:
                    return node.path
        return None

    def is_valid_entity(self, etype: str, value: str) -> bool:
        etype = (etype or "").strip()
        value = (value or "").strip()
        if not etype or not value:
            return False
        if etype in self.open_entity_types:
            return True
        return value in self.entity_types.get(etype, set())

    def resolve_entity(self, etype: str, value: str) -> Optional[EntityTag]:
        """校验并规整实体标签，非法返回 ``None``。"""
        etype = (etype or "").strip()
        value = (value or "").strip()
        if not etype or not value:
            return None
        if etype in self.open_entity_types:
            return EntityTag(type=etype, value=value)
        allowed = self.entity_types.get(etype)
        if allowed is None:
            return None
        if value in allowed:
            return EntityTag(type=etype, value=value)
        # 「情侣/夫妻」这类写法与配置里的「情侣夫妻」做一次去分隔符匹配
        squeezed = value.replace("/", "").replace("、", "").replace(" ", "")
        for name in allowed:
            if name.replace("/", "") == squeezed:
                return EntityTag(type=etype, value=name)
        return None

    # ------------------------------------------------------- 提示词素材
    def prompt_dimension_block(self, *, with_keywords: bool = True, max_kw: int = 4) -> str:
        """渲染成提示词里的维度清单。

        格式刻意做成「dim1 > dim2 > dim3｜正面示例｜负面示例」的紧凑单行，
        既让模型看清层级，又不至于把上下文撑爆。
        """
        lines: List[str] = []
        current_d1 = None
        for node in self.nodes:
            if node.dim1 != current_d1:
                current_d1 = node.dim1
                lines.append(f"\n【{node.dim1}】")
            path = f"  - {node.dim2}" + (f" > {node.dim3}" if node.dim3 else " > (无三级)")
            if with_keywords:
                pos = "、".join(node.positive[:max_kw])
                neg = "、".join(node.negative[:max_kw])
                path += f"　正面例：{pos}；负面例：{neg}"
            lines.append(path)
        return "\n".join(lines).strip()

    def prompt_entity_block(self, *, max_kw: int = 6) -> str:
        lines: List[str] = []
        for etype, values in self.entity_types.items():
            if etype in self.open_entity_types:
                lines.append(f"- {etype}：取值开放，直接抽取原文中出现的专有名称（如具体景点名、山名、湖名）")
                continue
            rendered = []
            for name in sorted(values):
                kws = self.entity_keywords.get((etype, name), [])[:max_kw]
                rendered.append(f"{name}（{('、'.join(kws)) or '—'}）" if kws else name)
            lines.append(f"- {etype}：" + "；".join(rendered))
        return "\n".join(lines)

    def dimension_paths(self) -> List[DimPath]:
        return sorted(self._dim_paths)

    def to_dimension_tag(self, path: DimPath, sentiment: Sentiment) -> DimensionTag:
        return DimensionTag(dim1=path[0], dim2=path[1], dim3=path[2], sentiment=sentiment.score)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Taxonomy v{self.version} dims={len(self.nodes)} "
            f"entity_types={len(self.entity_types)}>"
        )
