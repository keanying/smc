"""内容过滤：搜到的作品要不要留下。

平台的搜索结果里从来不只有你搜的那个词——夹着推荐、广告、蹭热度的内容。
搜「八大处」能搜出「点斑祛斑什么时候比较好」这种，采回来既占库
又要为它翻一遍评论（拟人模式下那是几十秒的真实点击）。

所以默认会做一层过滤：作品的标题/描述/标签里必须出现搜索关键字才留下。
但这条规则不是永远合适：

  · 有的景区别名很多，正文里写的是「西山八大处」「灵光寺」，
    卡着一个词会把真正相关的内容也丢掉；
  · 有时候就是想先把搜索结果全量存下来，回头再自己筛。

于是给出三种配置：

    关闭                    搜到什么存什么，评论也照采
    开启 + 按搜索关键字       只留标题/描述/标签里含搜索关键字的（默认，等同改造前的行为）
    开启 + 关键字 + 附关键字  上面那个词，再加上一组自己写的词，**命中任意一个**就留下

⚠️ 附关键字是"或"不是"且"。要求同时命中多个词，实际能留下的内容会少得离谱
（一条视频文案里同时出现「八大处」和「灵光寺」的概率并不高），
所以这里做成并集：把它当成"这些词都算相关"。

三类词的分工（配置在**景区**上，见 repositories/scenic_repo.py）：

    主关键字    拿去平台上搜        —— 决定"搜到什么"
    附关键字    命中任一就留存       —— 决定"相关不相关"（就是上面的 extra_keywords）
    过滤关键字  命中就丢，一票否决   —— 决定"要不要"（exclude_keywords）

顺序是固定的：先搜，再判相关，最后才轮到过滤词。过滤词放在最后是因为
它表达的是"这些东西我一概不要"——一条内容哪怕完美命中附关键字，
只要出现了过滤词，照样丢。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

#: 过滤方式
MODE_KEYWORD = "keyword"            # 只按搜索关键字
MODE_KEYWORD_PLUS = "keyword_plus"  # 搜索关键字 + 补充关键字
MODES = (MODE_KEYWORD, MODE_KEYWORD_PLUS)

#: 两类词各自的上限（用户定的）。**不一样**，别合成一个常量：
#: 附关键字是"景区的各种别名/相关说法"，一个大景区加上周边地名能堆到几百个；
#: 过滤关键字是"我一概不要的东西"，几十个就够用，写多了只会误伤。
MAX_AUX_KEYWORDS = 700
MAX_EXCLUDE_KEYWORDS = 200
#: 兼容旧名字。历史上只有一个上限，指的就是附关键字那份。
MAX_EXTRA_KEYWORDS = MAX_AUX_KEYWORDS
#: 单个词的长度上限
MAX_KEYWORD_LENGTH = 50


def split_keywords(raw: Any, limit: int = 0) -> List[str]:
    """把用户填的一串词拆成列表。

    中英文逗号、顿号、换行都当分隔符——用户不会记得该用哪个，
    而"填了没生效"这种问题查起来最费劲。

    limit：最多留几个，0 = 用默认上限（按附关键字那份，700）。
    ⚠️ 调用方必须按**自己那类词**的上限传：拿 200 去截附关键字的话，
    第 201 个之后的别名会被安静丢掉，现象是"我明明配了，怎么还是没采到"。
    """
    cap = int(limit) if limit and limit > 0 else MAX_AUX_KEYWORDS
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        text = str(raw or "")
        for separator in ("，", "、", "\n", "\r", ";", "；", "|"):
            text = text.replace(separator, ",")
        parts = text.split(",")

    seen = set()
    result: List[str] = []
    for part in parts:
        word = part.strip()
        if not word or len(word) > MAX_KEYWORD_LENGTH:
            continue
        lowered = word.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(word)
        if len(result) >= cap:
            break
    return result


@dataclass
class ContentFilter:
    """一次采集里"什么内容算相关"的规则。"""

    enabled: bool = True
    mode: str = MODE_KEYWORD
    extra_keywords: List[str] = field(default_factory=list)
    #: 「过滤关键字」：**命中就丢**，和上面两层是反向的。
    #:
    #: 判定顺序（三道，缺一不可）：
    #:   ① 主关键字拿去搜索        —— 决定搜到什么
    #:   ② 主/附关键字命中任一就留  —— 决定"相关不相关"
    #:   ③ 过滤关键字命中就丢       —— 决定"要不要"
    #:
    #: ⚠️ ③ 是**一票否决**，而且发生在 ② 之后：一条内容哪怕完美命中附关键字，
    #: 只要正文里出现了过滤词（"代运营""刷单""加微信"这类），照样丢。
    #: 没配就完全不起作用——不能因为列表是空的就把什么都拦下来。
    exclude_keywords: List[str] = field(default_factory=list)

    # ---------------- 构造 ----------------
    @classmethod
    def from_params(cls, params: Dict[str, Any]) -> "ContentFilter":
        """从任务 params 里解析。

        ⚠️ 缺省是"开启 + 按搜索关键字"——也就是改造前的行为。
        默认关掉的话，所有老任务会在某次升级后突然开始存一堆广告，
        而用户完全不知道发生了什么。
        """
        raw = (params or {}).get("content_filter")
        if not isinstance(raw, dict):
            return cls()
        mode = str(raw.get("mode") or MODE_KEYWORD).strip().lower()
        if mode not in MODES:
            mode = MODE_KEYWORD
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            mode=mode,
            extra_keywords=split_keywords(raw.get("extra_keywords"),
                                          MAX_AUX_KEYWORDS),
            exclude_keywords=split_keywords(raw.get("exclude_keywords"),
                                            MAX_EXCLUDE_KEYWORDS),
        )

    def to_params(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "extra_keywords": ",".join(self.extra_keywords),
            "exclude_keywords": ",".join(self.exclude_keywords),
        }

    # ---------------- 判定 ----------------
    def words_for(self, search_keyword: str) -> List[str]:
        """这次要拿哪些词去匹配。"""
        words = [search_keyword] if search_keyword else []
        if self.mode == MODE_KEYWORD_PLUS:
            words.extend(self.extra_keywords)
        return [w for w in words if w]

    def matches(self, text: str, search_keyword: str) -> bool:
        """这条内容留不留。

        text 是作品的标题 + 描述 + 标签拼起来的。
        """
        if not self.enabled:
            return True
        words = self.words_for(search_keyword)
        if not words:
            # 没有任何可匹配的词（比如主页采集没有搜索关键字），
            # 那就别拦——拦下来等于一条都采不到，比不过滤糟糕得多
            return True
        lowered = (text or "").lower()
        return any(word.lower() in lowered for word in words)

    def blocked_by(self, text: str) -> str:
        """命中了哪个过滤关键字（返回那个词）；没命中返回空串。

        ⚠️ 返回**命中的那个词**而不是 True/False，是为了日志能写清楚
        "因为『代运营』被丢的"。只报"被过滤词丢弃"的话，用户配了 200 个词，
        根本不知道是哪个词误伤了——然后只能一个个删着试。

        这一层**不看 enabled**：`enabled` 管的是"要不要按相关性筛"（②），
        而过滤词是"这些东西我一概不要"（③），两件事。用户把内容过滤关掉
        是想"搜到什么存什么"，不代表他想把明确排除掉的广告也存进来。
        """
        if not self.exclude_keywords:
            return ""
        lowered = (text or "").lower()
        for word in self.exclude_keywords:
            if word and word.lower() in lowered:
                return word
        return ""

    def describe(self) -> str:
        """写进任务日志的一行说明，用户能核对配置有没有生效。"""
        suffix = ""
        if self.exclude_keywords:
            suffix = (f"；过滤关键字 {len(self.exclude_keywords)} 个"
                      f"（{'、'.join(self.exclude_keywords[:8])}"
                      f"{'…' if len(self.exclude_keywords) > 8 else ''}），"
                      f"命中任意一个就丢弃")
        if not self.enabled:
            return "内容过滤：已关闭（搜到什么存什么）" + suffix
        if self.mode == MODE_KEYWORD_PLUS and self.extra_keywords:
            return (
                f"内容过滤：搜索关键字 + {len(self.extra_keywords)} 个附关键字"
                f"（{'、'.join(self.extra_keywords[:8])}"
                f"{'…' if len(self.extra_keywords) > 8 else ''}），命中任意一个就保留"
            ) + suffix
        if self.mode == MODE_KEYWORD_PLUS:
            return ("内容过滤：选了「关键字 + 附关键字」但附关键字是空的，"
                    "等同于只按搜索关键字") + suffix
        return "内容过滤：只保留标题/描述/标签里含搜索关键字的内容" + suffix


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    return default


def normalize(raw: Any) -> Dict[str, Any]:
    """存库前清洗成固定形状，编辑页读回来才不会看到一堆脏值。"""
    return ContentFilter.from_params({"content_filter": raw}).to_params()
