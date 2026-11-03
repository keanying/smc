# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""标注灌注 → 标注引擎。

景区管理里每个景区的「标注灌注」（补充提示词 + 关键词放宽开关）存在景区表里；
标注引擎在自己的 worker 线程里按评论的景区取（vendor 的 labeling/scenic_rules.py）。
这里负责把库里的配置推给引擎：启动时一次、之后每分钟一次、页面上保存时马上一次。
集群里每台机器各推各的（都读同一个库），哪台标注都用同一份。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..core.logging import get_logger
from .config_bridge import engine_importable, ensure_on_path

logger = get_logger(__name__)

INTERVAL_SECONDS = 60


async def refresh(scenics) -> int:
    """读库、推给引擎。返回配了灌注的景区数；引擎不在时返回 -1。"""
    if not engine_importable():
        return -1
    ensure_on_path()
    from opinion_labeling_engine.labeling.scenic_rules import ScenicRule, set_rules

    rules = await scenics.all_label_rules()
    set_rules({sid: ScenicRule(prompt=r["prompt"], broad_keywords=r["broad_keywords"],
                               mark_words=tuple(r.get("mark_words") or ()))
               for sid, r in rules.items()})
    return len(rules)


class ScenicRulesSync:
    def __init__(self, scenics, interval: float = INTERVAL_SECONDS):
        self.scenics = scenics
        self.interval = interval
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        try:
            count = await refresh(self.scenics)
            if count > 0:
                logger.info("标注灌注：%s 个景区配了补充标注提示词", count)
        except Exception as exc:  # noqa: BLE001 —— 推不上去不影响启动，下一轮再试
            logger.warning("标注灌注：读取失败：%s", exc)
        self._task = asyncio.create_task(self._loop(), name="scenic-label-rules")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await refresh(self.scenics)
            except Exception as exc:  # noqa: BLE001
                logger.debug("标注灌注：刷新失败：%s", exc)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
