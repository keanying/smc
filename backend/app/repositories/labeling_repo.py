"""标注审核页面的数据访问。

只读评论表的标注字段 + 写人工复核结果。**不碰标注引擎的写回路径**——
引擎按 (channel, scenic_id, comment_id) UPDATE 那五列，
这里写的是 label_review_* 三列，两边互不干扰。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core.db import Database
from ..core.logging import get_logger
from ..db import tables

logger = get_logger(__name__)

#: 审核列表要展示的列。**显式列出**而不是 SELECT *：
#: 评论表里有 LONGTEXT（content/extra_content），全查会把内存和带宽打满。
REVIEW_COLUMNS = (
    "id", "channel", "scenic_id", "scenic_name", "work_id", "comment_id",
    "commenter_name", "content", "likes", "publish_time", "comment_level",
    "sentiment_label", "sentiment_score", "dimension_tags", "entity_tags",
    "keyword_tags", "label_review_flag", "label_review_by", "label_review_time",
)

# ---------------------------------------------------------------------------
# label_review_flag 的七档取值。**这一列是引擎和采集侧共用的**：
#   1/2/3 由审核页面写，4/5/6 由标注引擎写（见引擎 storage.review_flags）。
# 别在这里自己发明新值——引擎那边的 pending_flags 会按这套语义取数。
# ---------------------------------------------------------------------------
FLAG_UNLABELED = 0        # 未标注
FLAG_HUMAN_RIGHT = 1      # 人工复核：AI 标对了
FLAG_HUMAN_WRONG = 2      # 人工复核：AI 标错了（但还没改）
FLAG_HUMAN_FIXED = 3      # 复核成功（人工改过并确认）
FLAG_AI_OK = 4            # AI 标注成功
FLAG_AI_FAILED = 5        # AI 标注错误（模型调用/解析失败）
FLAG_AI_LOW_CONF = 6      # AI 标了但置信度低，待人工复核

FLAG_LABELS = {
    FLAG_UNLABELED: "未标注",
    FLAG_HUMAN_RIGHT: "人工复核正确",
    FLAG_HUMAN_WRONG: "人工复核错误",
    FLAG_HUMAN_FIXED: "复核成功",
    FLAG_AI_OK: "AI标注成功",
    FLAG_AI_FAILED: "AI标注错误",
    FLAG_AI_LOW_CONF: "未人工复核",
}

#: 由人工写的那几档。审核页面只允许写这些，不允许人工写 4/5/6——
#: 那是引擎的状态，人工覆盖了就分不清"AI 标的"还是"人改的"。
HUMAN_FLAGS = (FLAG_HUMAN_RIGHT, FLAG_HUMAN_WRONG, FLAG_HUMAN_FIXED)
#: 还需要人看一眼的：AI 标了但没人复核过的
NEEDS_REVIEW_FLAGS = (FLAG_AI_OK, FLAG_AI_FAILED, FLAG_AI_LOW_CONF)

# 兼容旧名字（v6 之前用的三档语义），避免外部引用一改就炸
FLAG_PENDING = FLAG_UNLABELED
FLAG_CONFIRMED = FLAG_HUMAN_RIGHT
FLAG_EDITED = FLAG_HUMAN_FIXED


def _loads(value: Any, fallback):
    """标注字段在库里是 JSON 文本。坏了就退回默认值，不能让整页打不开。"""
    if value in (None, ""):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        logger.debug("标注字段不是合法 JSON，按原样返回：%r", str(value)[:80])
        return fallback


class LabelingRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ 统计
    async def stats(self, scenic_id: str = "", channel: str = "") -> Dict[str, Any]:
        """某景区的标注进度。审核页面顶部那排数字。"""
        where, params = self._scope(scenic_id, channel)
        row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS total,"
            f" SUM(`sentiment_label` IS NULL OR `sentiment_label` = '') AS unlabeled,"
            f" SUM(`sentiment_score` = 1) AS positive,"
            f" SUM(`sentiment_score` = 0 AND `sentiment_label` IS NOT NULL"
            f"     AND `sentiment_label` <> '') AS neutral,"
            f" SUM(`sentiment_score` = -1) AS negative,"
            f" SUM(`label_review_flag` = 4) AS ai_ok,"
            f" SUM(`label_review_flag` = 5) AS ai_failed,"
            f" SUM(`label_review_flag` = 6) AS ai_low_conf,"
            f" SUM(`label_review_flag` = 1) AS human_right,"
            f" SUM(`label_review_flag` = 2) AS human_wrong,"
            f" SUM(`label_review_flag` = 3) AS human_fixed"
            f" FROM `{tables.COMMENTS}` {where}",
            params,
        ) or {}
        out = {k: int(row.get(k) or 0) for k in
               ("total", "unlabeled", "positive", "neutral", "negative",
                "ai_ok", "ai_failed", "ai_low_conf",
                "human_right", "human_wrong", "human_fixed")}
        out["labeled"] = out["total"] - out["unlabeled"]
        # 人看过的 = 1/2/3；还等着人看的 = AI 写过标记但没人复核（4/5/6）
        out["reviewed"] = out["human_right"] + out["human_wrong"] + out["human_fixed"]
        out["pending_review"] = out["ai_ok"] + out["ai_failed"] + out["ai_low_conf"]
        return out

    # ------------------------------------------------------------------ 列表
    async def list_comments(
        self, *, scenic_id: str = "", channel: str = "", label_state: str = "",
        sentiment: str = "", review_flag: Optional[int] = None,
        keyword: str = "", page: int = 1, page_size: int = 20,
    ) -> Dict[str, Any]:
        """审核列表。按景区收口，其余都是可选筛选。"""
        where, params = self._scope(scenic_id, channel)
        conds = [where[6:]] if where else []      # 去掉前缀 "WHERE "

        if label_state == "unlabeled":
            conds.append("(`sentiment_label` IS NULL OR `sentiment_label` = '')")
        elif label_state == "labeled":
            conds.append("(`sentiment_label` IS NOT NULL AND `sentiment_label` <> '')")
        if sentiment:
            conds.append("`sentiment_label` = %s")
            params.append(sentiment)
        if review_flag is not None:
            conds.append("`label_review_flag` = %s")
            params.append(int(review_flag))
        if keyword:
            # 只在正文里找。LIKE %x% 用不上索引，所以**必须**配合景区/平台收口，
            # 否则是全表扫描——上面的 scope 保证了这一点。
            conds.append("`content` LIKE %s")
            params.append(f"%{keyword}%")

        clause = ("WHERE " + " AND ".join(c for c in conds if c)) if conds else ""
        total_row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS n FROM `{tables.COMMENTS}` {clause}", list(params))
        total = int((total_row or {}).get("n") or 0)

        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        cols = ", ".join(f"`{c}`" for c in REVIEW_COLUMNS)
        rows = await self.db.fetch_all(
            f"SELECT {cols} FROM `{tables.COMMENTS}` {clause} "
            f"ORDER BY `publish_time` DESC, `id` DESC LIMIT %s OFFSET %s",
            list(params) + [page_size, (page - 1) * page_size],
        )
        return {
            "total": total, "page": page, "page_size": page_size,
            "items": [self._shape(r) for r in rows],
        }

    async def get_one(self, row_id: int) -> Optional[Dict[str, Any]]:
        cols = ", ".join(f"`{c}`" for c in REVIEW_COLUMNS)
        row = await self.db.fetch_one(
            f"SELECT {cols} FROM `{tables.COMMENTS}` WHERE `id` = %s", [int(row_id)])
        return self._shape(row) if row else None

    # ------------------------------------------------------------------ 写回
    async def save_review(
        self, row_id: int, *, sentiment_label: Optional[str] = None,
        sentiment_score: Optional[int] = None,
        dimension_tags: Any = None, entity_tags: Any = None,
        keyword_tags: Any = None, flag: int = FLAG_HUMAN_RIGHT,
        reviewer: str = "",
    ) -> bool:
        """保存人工复核。

        flag 只能是 1/2/3（人工那三档）。4/5/6 是引擎的状态，
        人工写进去就分不清"AI 标的"还是"人改的"了。
        两种情况都要留下**是谁、什么时候**改的，否则出了问题无从追溯。
        """
        if flag not in HUMAN_FLAGS:
            raise ValueError(
                f"人工复核只能写 {HUMAN_FLAGS}（1正确 2错误 3复核成功），"
                f"收到 {flag}。4/5/6 是标注引擎写的状态，不能由人工覆盖")
        sets, params = [], []
        for column, value in (
            ("sentiment_label", sentiment_label),
            ("sentiment_score", sentiment_score),
        ):
            if value is not None:
                sets.append(f"`{column}` = %s")
                params.append(value)
        for column, value in (
            ("dimension_tags", dimension_tags),
            ("entity_tags", entity_tags),
            ("keyword_tags", keyword_tags),
        ):
            if value is not None:
                sets.append(f"`{column}` = %s")
                # 和引擎写回保持一致：中文明文，不转义成 \uXXXX
                params.append(json.dumps(value, ensure_ascii=False))
        sets.append("`label_review_flag` = %s")
        params.append(int(flag))
        sets.append("`label_review_by` = %s")
        params.append(reviewer or "")
        sets.append("`label_review_time` = %s")
        params.append(datetime.now())
        params.append(int(row_id))

        affected = await self.db.execute(
            f"UPDATE `{tables.COMMENTS}` SET {', '.join(sets)} WHERE `id` = %s", params)
        return bool(affected)

    async def reset_review(self, row_id: int) -> bool:
        """撤销复核：标记回到 0（未标注），AI 会重新标它。

        ⚠️ 回到 0 而不是 4，是**故意**的：引擎跑批只取 pending_flags（默认 [0]），
        回到 0 才会被下一轮补标捞到。回到 4 的话这条就再也进不了跑批了。
        """
        return bool(await self.db.execute(
            f"UPDATE `{tables.COMMENTS}` SET `label_review_flag` = %s,"
            f" `label_review_by` = '', `label_review_time` = NULL WHERE `id` = %s",
            [FLAG_UNLABELED, int(row_id)]))

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _scope(scenic_id: str, channel: str) -> Tuple[str, List[Any]]:
        conds, params = [], []
        if scenic_id:
            conds.append("`scenic_id` = %s")
            params.append(scenic_id)
        if channel:
            conds.append("`channel` = %s")
            params.append(channel)
        return (("WHERE " + " AND ".join(conds)) if conds else ""), params

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["dimension_tags"] = _loads(row.get("dimension_tags"), [])
        item["entity_tags"] = _loads(row.get("entity_tags"), [])
        item["keyword_tags"] = _loads(row.get("keyword_tags"), [])
        item["labeled"] = bool(row.get("sentiment_label"))
        flag = int(row.get("label_review_flag") or 0)
        item["label_review_label"] = FLAG_LABELS.get(flag, f"未知({flag})")
        item["reviewed_by_human"] = flag in HUMAN_FLAGS
        return item
