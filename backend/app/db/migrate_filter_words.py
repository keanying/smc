"""一次性搬迁：把任务里配的「补充关键字」挪到景区的「附关键字」。

背景
----
改造前，补充关键字配在**每个任务**的 params.content_filter.extra_keywords 里。
同一个景区跑十个任务就要填十遍，改一次要改十处，漏改一处就出现
"两个任务采出来的东西不一样"。改造后它挂在**景区**上，配一次全景区通用。

这个搬迁在服务启动时跑一次（幂等，跑多少遍结果都一样）：
把每个任务的 extra_keywords 并进它所属景区的附关键字，并逐条打日志说明
搬了什么——不打日志的话，用户下次跑任务发现过滤行为变了，
在任务页上又找不到那些词，会以为配置丢了。

⚠️ 只**增**不删：任务 params 里的老配置原样留着。
   一是万一搬错了还能对照，二是老任务在新代码里仍然读得到它
   （runner 会把两边取并集）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ..core.logging import get_logger
from . import tables

logger = get_logger(__name__)

#: 搬完之后在设置表里留个记号，别每次启动都重跑一遍、刷一屏日志
DONE_KEY = "_migrated_filter_words_v1"


def _extra_of(params: Any) -> List[str]:
    """从任务 params 里抠出补充关键字。"""
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (ValueError, TypeError):
            return []
    if not isinstance(params, dict):
        return []
    raw = ((params.get("content_filter") or {}) if isinstance(
        params.get("content_filter"), dict) else {}).get("extra_keywords")
    if not raw:
        return []
    from ..core.content_filter import split_keywords
    return split_keywords(raw)


async def run(db, scenics) -> Dict[str, int]:
    """执行搬迁。返回 {tasks, scenics, words}。"""
    stat = {"tasks": 0, "scenics": 0, "words": 0}
    try:
        done = await db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{tables.SETTING}` WHERE setting_key = %s",
            [DONE_KEY], 0)
        if int(done or 0):
            return stat
    except Exception as exc:      # noqa: BLE001
        logger.debug("[词表搬迁] 读记号失败，按未搬迁处理：%s", exc)

    try:
        rows = await db.fetch_all(
            f"SELECT task_id, task_name, scenic_id, params FROM `{tables.TASK}`")
    except Exception as exc:      # noqa: BLE001
        logger.warning("[词表搬迁] 读任务失败，跳过：%s", exc)
        return stat

    by_scenic: Dict[str, List[str]] = {}
    for row in rows:
        words = _extra_of(row.get("params"))
        if not words or not row.get("scenic_id"):
            continue
        stat["tasks"] += 1
        bucket = by_scenic.setdefault(row["scenic_id"], [])
        for word in words:
            if word not in bucket:
                bucket.append(word)
        logger.info("[词表搬迁] 任务《%s》的补充关键字 %s → 景区 %s 的附关键字",
                    row.get("task_name") or row.get("task_id"),
                    "、".join(words[:8]) + ("…" if len(words) > 8 else ""),
                    row["scenic_id"])

    for scenic_id, words in by_scenic.items():
        try:
            result = await scenics.add_filter_words(scenic_id, "aux", words)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[词表搬迁] 景区 %s 写入失败：%s", scenic_id, exc)
            continue
        stat["scenics"] += 1
        stat["words"] += int(result.get("added") or 0)

    if stat["tasks"]:
        logger.info("[词表搬迁] 完成：%d 个任务的补充关键字并入 %d 个景区，"
                    "新增 %d 个附关键字。任务里的老配置**没有删**，"
                    "两边取并集，随时可以对照",
                    stat["tasks"], stat["scenics"], stat["words"])

    try:
        await db.execute(
            f"INSERT INTO `{tables.SETTING}` (setting_key, setting_value) "
            f"VALUES (%s, %s) ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)",
            [DONE_KEY, json.dumps(stat, ensure_ascii=False)])
    except Exception as exc:      # noqa: BLE001
        logger.debug("[词表搬迁] 写记号失败（下次启动会再跑一遍，幂等无害）：%s", exc)
    return stat
