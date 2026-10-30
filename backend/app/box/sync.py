# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""盒子这边：定时从主拉「分到的景区」。

启动时拉一次，之后每分钟一次。内容没变就什么都不做；变了就按分配包导进本地库
（景区、关键字、词表、采集目标都和主一致，不再分配的停用，见 edition.import_bundle）。
本地库里留景区，是因为建任务、采集时要用；采到的数据不留。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from .. import edition as E
from ..core.logging import get_logger
from .client import MasterClient, MasterUnavailable

logger = get_logger(__name__)

INTERVAL_SECONDS = 60


class BoxSync:
    def __init__(self, client: MasterClient, db, scenics, settings,
                 interval: float = INTERVAL_SECONDS):
        self.client = client
        self.db = db
        self.scenics = scenics
        self.settings = settings
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self.status: Dict[str, Any] = {"ok": None, "last_ok_at": "", "last_error": "", "box_id": ""}

    async def start(self) -> None:
        record = await E.assignment(self.settings) or {}
        E.set_synced_box_name(record.get("box_name") or "")
        self._task = asyncio.create_task(self._loop(), name="box-sync")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await self.sync_once()
            await asyncio.sleep(self.interval)

    async def sync_once(self) -> Dict[str, Any]:
        """拉一次。返回状态（页面上「和主同步」那一栏显示它）。"""
        try:
            data = await self.client.request("GET", "/api/box/assignment", retry=False)
            bundle = data["bundle"]
            digest = hashlib.sha256(json.dumps(
                [data.get("box_name"), bundle.get("scenics")], sort_keys=True, ensure_ascii=False,
                default=str).encode("utf-8")).hexdigest()
            record = await E.assignment(self.settings) or {}
            if record.get("digest") != digest:
                result = await E.import_bundle(self.db, self.scenics, self.settings, bundle,
                                               imported_by="自动同步", allow_empty=True, digest=digest)
                logger.info("从主同步了分到的景区：%s 个%s", len(result["scenic_ids"]),
                            f"，停用 {len(result['dropped'])} 个" if result["dropped"] else "")
            E.set_synced_box_name(data.get("box_name") or "")
            self.status.update(ok=True, last_error="", box_id=data.get("box_id") or "",
                               last_ok_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except MasterUnavailable as exc:
            self.status.update(ok=False, last_error=str(exc))
            logger.warning("同步景区分配失败：%s", exc)
        except Exception as exc:  # noqa: BLE001 —— 同步出错不能把循环弄停
            self.status.update(ok=False, last_error=f"{type(exc).__name__}: {exc}")
            logger.exception("同步景区分配出错")
        return dict(self.status)


_client: Optional[MasterClient] = None
_sync: Optional[BoxSync] = None


def set_box(client: Optional[MasterClient], sync: Optional[BoxSync]) -> None:
    global _client, _sync
    _client, _sync = client, sync


def get_box_client() -> Optional[MasterClient]:
    return _client


def get_box_sync() -> Optional[BoxSync]:
    return _sync
