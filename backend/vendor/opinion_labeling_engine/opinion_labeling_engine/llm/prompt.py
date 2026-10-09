"""提示词工程。

本期不做模型微调，标注质量完全由提示词决定，所以这个文件是准确率的主战场。
提示词分四段，各自解决一个明确的失败模式：

    1. 角色与任务        —— 防止模型跑去写点评、写总结
    2. 标签体系清单      —— 防止模型自创维度（配合白名单双保险）
    3. 判定规则与边界    —— 落实需求里的 4 条硬约束
    4. 输出契约 + 少样本 —— 保证输出可被稳定解析

维度/实体清单从 taxonomy.yaml 渲染而来，改标签体系不需要动这个文件。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..domain.models import CommentRecord
from ..domain.taxonomy import Taxonomy
from ..preprocess.cleaner import CleanResult

__all__ = ["PromptBuilder", "OUTPUT_SCHEMA_HINT"]


OUTPUT_SCHEMA_HINT = """{
  "relevant": true,
  "sentiment": "正向|中性|负向",
  "confidence": 0.0,
  "dimensions": [{"dim1": "", "dim2": "", "dim3": "", "sentiment": "正向|中性|负向"}],
  "entities": [{"type": "", "value": ""}],
  "keywords": [{"word": "", "polarity": "正向|中性|负向"}],
  "reason": ""
}"""


_SYSTEM_TEMPLATE = """你是景区舆情标注专家，负责对景区在社交媒体/OTA 平台上的游客评论做结构化标注。
你的输出会直接写入数据仓库并驱动景区口碑看板，因此必须严格、克制、可复现，宁可少标也不能瞎标。

================ 一、评价维度标签体系（唯一合法取值） ================
只允许从下面这棵三级维度树中选择。dim1/dim2/dim3 必须逐字复制，不得改写、简写、合并或自创。
标注为"(无三级)"的二级维度，其 dim3 必须输出空字符串 ""。
{dimension_block}

================ 二、实体标签体系（唯一合法取值） ================
type 与 value 必须逐字复制下表；只有"景区地名"的 value 是开放的，从原文中抽取专有名称。
{entity_block}

================ 三、情感与置信度 ================
- sentiment 只能是：正向 / 中性 / 负向
- confidence 是你对本条整体情感判定的把握程度，取值 0~1，保留两位小数。
  证据充分、措辞明确时给 0.85 以上；文本短、含糊、反讽、褒贬参半时给 0.6 以下，不要虚高。

================ 四、判定规则（硬约束，违反即为错误标注） ================
R1. 与景区主体无关的内容，一律 relevant=false、sentiment="中性"，且 dimensions、keywords 必须为空数组。
    属于无关的典型情况：广告导流、招聘、代刷代购、纯打招呼、与该景区无关的闲聊、
    只谈论天气/心情/自拍而不涉及景区本身、对其他网友的回复且不含景区评价。
    注意：谈论景区的门票、交通、餐饮、服务、设施、景色、排队，都算与景区相关。

R2. dimensions 只标"评论中真实提到并带有评价色彩"的维度。
    - 一条评论可以命中多个维度；每个维度带它自己的 sentiment，可以与整体情感不同。
      例："风景很美但厕所太差" → 游玩体验/景色观赏/自然风光=正向，设施环境/基础设施/厕所=负向。
    - 只是提到了某事物但没有评价（如"我们从南门进"），不产生维度标签。
    - 找不到任何可靠维度时，dimensions 输出空数组，不要硬凑。
    - 最多输出 {dimension_max} 个维度，按证据强度从高到低排列。

R3. keywords 是"支撑本条整体情感判定"的关键词，必须与整体 sentiment 同方向：
    - 整体正向 → 只放正向词（如"风景优美""服务热情"），不得混入任何负向词
    - 整体负向 → 只放负向词（如"排队久""态度差"），不得混入任何正向词
    - 整体中性 → 只放客观中性的描述词（如"人较多""周末去的"），不得放褒贬词
    每个 keyword 都要标出它自己的 polarity，且 polarity 必须等于整体 sentiment。

R4. keywords 必须是评论原文中真实出现的连续片段，或原文词语的直接组合，严禁编造：
    - 长度 {kw_min}~{kw_max} 个字符，绝对不允许输出单个汉字
    - 不要输出"景区""地方""感觉""可以""非常"这类没有信息量的通用词
    - 不要输出整句话，只要词或短语
    - 最多 {kw_max_count} 个，按重要性排序；原文确实提不出合格关键词时输出空数组

R5. 反讽、先扬后抑、条件句要按真实语义判定整体情感：
    "本来挺期待的，结果排队三小时" → 负向；"虽然有点贵，但真的值" → 正向。
    整体情感看的是游客对本次游玩的总体态度，不是各维度的简单相加。

================ 五、输出契约 ================
只输出一个 JSON 对象，不要输出任何解释文字、不要输出 markdown 代码块以外的内容。
**输出压缩成一行**：不要换行、不要缩进、不要在冒号逗号后加多余空格。
（缩进过的 JSON 体积翻倍，容易顶到输出上限被截断成半截。）
不要输出思考过程，直接给结果。
JSON 结构固定如下（reason 用不超过 20 字的中文简述判定依据）：
{schema}
"""


_FEWSHOT: List[Dict[str, Any]] = [
    {
        "input": "景区名称：黄山\n评论正文：风景真的绝了，云海太震撼，但是索道排队排了快两小时，厕所也脏。",
        "output": {
            "relevant": True,
            "sentiment": "负向",
            "confidence": 0.82,
            "dimensions": [
                {"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光", "sentiment": "正向"},
                {"dim1": "游玩体验", "dim2": "排队时长", "dim3": "", "sentiment": "负向"},
                {"dim1": "设施环境", "dim2": "基础设施", "dim3": "厕所", "sentiment": "负向"},
            ],
            "entities": [
                {"type": "设施设备", "value": "游玩设施"},
                {"type": "设施设备", "value": "基础设施"},
            ],
            "keywords": [
                {"word": "排队排了快两小时", "polarity": "负向"},
                {"word": "厕所也脏", "polarity": "负向"},
            ],
            "reason": "景色好但排队久且厕所脏，抱怨占主导",
        },
    },
    {
        "input": "景区名称：天山天池\n补充信息：总评分 5.0，景色评分 5.0，游客类型 朋友出游\n评论正文：完美的一天",
        "output": {
            "relevant": True,
            "sentiment": "正向",
            "confidence": 0.72,
            "dimensions": [],
            "entities": [{"type": "游客类型", "value": "朋友结伴"}],
            "keywords": [{"word": "完美的一天", "polarity": "正向"}],
            "reason": "笼统好评，未指向具体维度",
        },
    },
    {
        "input": "景区名称：西湖\n评论正文：家人们谁懂啊，我这条裙子才 89，链接放评论区了",
        "output": {
            "relevant": False,
            "sentiment": "中性",
            "confidence": 0.95,
            "dimensions": [],
            "entities": [],
            "keywords": [],
            "reason": "带货广告，与景区无关",
        },
    },
    {
        "input": "景区名称：瑶琳仙境\n评论正文：门票 100 有点小贵，不过讲解员讲得挺细的，值回票价。带娃去的，推车能进。",
        "output": {
            "relevant": True,
            "sentiment": "正向",
            "confidence": 0.78,
            "dimensions": [
                {"dim1": "游玩体验", "dim2": "性价比", "dim3": "门票价格", "sentiment": "正向"},
                {"dim1": "服务质量", "dim2": "导游服务", "dim3": "讲解水平", "sentiment": "正向"},
            ],
            "entities": [
                {"type": "景区地名", "value": "瑶琳仙境"},
                {"type": "票务类型", "value": "门票"},
                {"type": "游客类型", "value": "亲子家庭"},
            ],
            "keywords": [
                {"word": "值回票价", "polarity": "正向"},
                {"word": "讲得挺细", "polarity": "正向"},
            ],
            "reason": "先抑后扬，最终肯定性价比与讲解",
        },
    },
]


class PromptBuilder:
    """把 taxonomy + 一条评论组装成模型请求的 messages。

    构造一次、复用多次：system 段是常量，只在初始化时渲染一遍，
    避免每条评论都重新拼几千字的维度清单。
    """

    #: 少样本示例的承载方式
    #:   inline  —— 示例作为文本折进 system 段，``input`` 里不出现任何 assistant item
    #:              （默认；方舟对 assistant item 的字段要求更严，绕开它最省事）
    #:   message —— 示例走独立的 user/assistant 轮，模型学得略准一点
    FEWSHOT_STYLES = ("message", "inline")

    #: system 段的承载方式
    #:   system —— 独立的 system item（标准做法）
    #:   user   —— 折进唯一的 user item，此时 input 里只有一个 user 元素，
    #:             与方舟文档给的最小请求样例形状完全一致（最大兼容）
    SYSTEM_ROLES = ("system", "user")

    def __init__(self, taxonomy: Taxonomy, *, keyword_min: int = 2, keyword_max: int = 12,
                 keyword_max_count: int = 8, dimension_max: int = 6,
                 with_fewshot: bool = True, fewshot_style: str = "inline",
                 system_role: str = "system") -> None:
        if fewshot_style not in self.FEWSHOT_STYLES:
            raise ValueError(f"fewshot_style 只支持 {self.FEWSHOT_STYLES}，"
                             f"当前 {fewshot_style!r}")
        if system_role not in self.SYSTEM_ROLES:
            raise ValueError(f"system_role 只支持 {self.SYSTEM_ROLES}，当前 {system_role!r}")
        self._system_role = system_role
        self._taxonomy = taxonomy
        self._with_fewshot = with_fewshot
        self._fewshot_style = fewshot_style
        self._system = _SYSTEM_TEMPLATE.format(
            dimension_block=taxonomy.prompt_dimension_block(),
            entity_block=taxonomy.prompt_entity_block(),
            dimension_max=dimension_max,
            kw_min=keyword_min,
            kw_max=keyword_max,
            kw_max_count=keyword_max_count,
            schema=OUTPUT_SCHEMA_HINT,
        )
        if with_fewshot and fewshot_style == "inline":
            self._system += "\n\n" + self._render_fewshot_text()

    @property
    def system_prompt(self) -> str:
        return self._system

    # ------------------------------------------------------------------ 用户段
    def build_user_content(self, record: CommentRecord, cleaned: CleanResult) -> str:
        """渲染单条评论的用户段。

        景区名称必须给——「天山天池的水很清」这种评论，模型知道景区名才能正确
        判定「相关」，否则容易误判成与景区无关。
        """
        lines: List[str] = []
        if record.scenic_name:
            lines.append(f"景区名称：{record.scenic_name}")
        if record.channel:
            lines.append(f"来源渠道：{record.channel}")

        hints = record.extra_hints()
        if hints:
            parts = []
            mapping = {
                "score": "总评分",
                "landscape_score": "景色评分",
                "fun_score": "趣味评分",
                "price_quality_score": "性价比评分",
                "tourist_type": "游客类型",
            }
            for key, label in mapping.items():
                if key in hints:
                    parts.append(f"{label} {hints[key]}")
            if parts:
                lines.append("补充信息：" + "，".join(parts))
                lines.append("（补充信息中的评分仅作参考，最终情感以评论正文为准；"
                             "正文与评分矛盾时以正文为准）")

        lines.append(f"评论正文：{cleaned.text}")
        lines.append("")
        lines.append("请按系统提示的 JSON 契约输出标注结果。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 组装
    @staticmethod
    def _render_fewshot_text() -> str:
        """把少样本示例渲染成 system 段里的一段文本（inline 模式用）。"""
        blocks = ["================ 六、标注示例 ================"]
        for idx, shot in enumerate(_FEWSHOT, start=1):
            blocks.append(f"\n--- 示例 {idx} ---\n输入：\n{shot['input']}\n"
                          f"输出：\n{json.dumps(shot['output'], ensure_ascii=False)}")
        return "\n".join(blocks)

    def build_messages(self, record: CommentRecord, cleaned: CleanResult) -> List[Dict[str, Any]]:
        """产出 Ark responses 协议的 ``input`` 数组。

        ``system_role="user"`` 时整个提示词压成**一个** user item，
        请求形状与方舟文档的最小样例逐字一致——网关再挑剔也挑不出毛病。
        """
        user_content = self.build_user_content(record, cleaned)

        if self._system_role == "user":
            return [self._msg("user", f"{self._system}\n\n{user_content}")]

        messages: List[Dict[str, Any]] = [self._msg("system", self._system)]

        if self._with_fewshot and self._fewshot_style == "message":
            for shot in _FEWSHOT:
                messages.append(self._msg("user", shot["input"]))
                messages.append(
                    self._msg("assistant", json.dumps(shot["output"], ensure_ascii=False))
                )

        messages.append(self._msg("user", user_content))
        return messages

    @staticmethod
    def _msg(role: str, text: str) -> Dict[str, Any]:
        """构造 responses 协议的一个 input item。

        协议对角色的要求不一样，少一个字段服务端就直接 400：

        - ``system`` / ``user``：内容块类型 ``input_text``，item 上不带其它字段。
          这就是方舟文档最小请求样例的形状，一个字都不多加。
        - ``assistant``：内容块类型 ``output_text``，且 item 上**必须**带
          ``type`` 与 ``status``。把 assistant 轮回传给模型时漏了它们，方舟会报
          ``MissingParameter: The request failed because it is missing
          input.status parameter``（只有 fewshot_style=message 会走到这里）。
        """
        if role != "assistant":
            return {"role": role, "content": [{"type": "input_text", "text": text}]}
        return {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}],
        }

    # ------------------------------------------------------------------ 重试提示
    def build_repair_messages(
        self,
        record: CommentRecord,
        cleaned: CleanResult,
        bad_output: str,
        error: str,
    ) -> List[Dict[str, Any]]:
        """输出解析失败时的修复轮：把错误回喂给模型，要求它只重出 JSON。

        比整轮重试省 token，且成功率更高——多数解析失败是格式问题而非理解问题。

        inline 模式下不产生 assistant item，改把坏输出塞进 user 段里描述，
        效果略差一点，但保证 ``input`` 里全是 ``input_text``。
        """
        bad = (bad_output or "")[:2000]
        retry_hint = (f"你上一次针对这条评论的输出是：\n{bad}\n\n"
                      f"它无法解析，错误：{error}。\n"
                      f"请只输出一个符合契约的 JSON 对象，不要任何额外文字：\n"
                      f"{OUTPUT_SCHEMA_HINT}")

        if self._system_role == "user":
            return [self._msg("user", f"{self._system}\n\n"
                                      f"{self.build_user_content(record, cleaned)}\n\n"
                                      f"{retry_hint}")]

        messages = [self._msg("system", self._system)]
        messages.append(self._msg("user", self.build_user_content(record, cleaned)))

        if self._fewshot_style == "message":
            messages.append(self._msg("assistant", bad))
            messages.append(self._msg(
                "user",
                f"你上一次的输出无法解析，错误：{error}。\n"
                f"请只输出一个符合契约的 JSON 对象，不要任何额外文字：\n{OUTPUT_SCHEMA_HINT}",
            ))
        else:
            messages.append(self._msg("user", retry_hint))
        return messages
