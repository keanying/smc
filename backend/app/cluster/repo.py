# 景区社媒采集系统 · Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""集群的两张表：机器（心跳）和租约（谁是主机）。

时间一律用数据库的 NOW()：几台机器的系统时钟难免差几秒，
拿各自的本地时间比心跳，会把时钟慢的机器误判成掉线。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.db import Database
from ..db import tables as T
from .identity import NodeIdentity

MASTER_LEASE = "master"


class NodeRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ 机器
    async def get(self, node_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            f"SELECT *, TIMESTAMPDIFF(SECOND, heartbeat_at, NOW()) AS idle_seconds "
            f"FROM `{T.CLUSTER_NODE}` WHERE node_id = %s", [node_id])
        return _decode(row) if row else None

    async def heartbeat(self, me: NodeIdentity, *, version: str, max_running: int,
                        running_count: int, stats: Dict[str, Any],
                        started_at: datetime) -> None:
        """登记 / 刷新本机。accept_tasks 是页面上设的，这里不碰。"""
        await self.db.execute(
            f"INSERT INTO `{T.CLUSTER_NODE}` (node_id, node_name, role, advertise_url, host, pid, "
            f"version, max_running, running_count, stats, started_at, heartbeat_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
            f"ON DUPLICATE KEY UPDATE node_name = VALUES(node_name), role = VALUES(role), "
            f"advertise_url = VALUES(advertise_url), host = VALUES(host), pid = VALUES(pid), "
            f"version = VALUES(version), max_running = VALUES(max_running), "
            f"running_count = VALUES(running_count), stats = VALUES(stats), "
            f"started_at = VALUES(started_at), heartbeat_at = NOW()",
            [me.node_id, me.node_name, me.role, me.advertise_url, me.host, me.pid, version,
             int(max_running), int(running_count), json.dumps(stats, ensure_ascii=False, default=str),
             started_at],
        )

    async def mark_stopped(self, node_id: str) -> None:
        """正常关机：心跳清空，页面上立刻显示离线，主机也不会再往这台派任务。"""
        await self.db.execute(
            f"UPDATE `{T.CLUSTER_NODE}` SET heartbeat_at = NULL, running_count = 0 "
            f"WHERE node_id = %s", [node_id])

    async def list(self, offline_seconds: int) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"SELECT *, TIMESTAMPDIFF(SECOND, heartbeat_at, NOW()) AS idle_seconds "
            f"FROM `{T.CLUSTER_NODE}` ORDER BY create_time ASC, node_id ASC")
        out = []
        for row in rows:
            item = _decode(row)
            idle = item.get("idle_seconds")
            item["online"] = idle is not None and int(idle) <= offline_seconds
            out.append(item)
        return out

    async def set_accept(self, node_id: str, accept: bool) -> int:
        return await self.db.execute(
            f"UPDATE `{T.CLUSTER_NODE}` SET accept_tasks = %s WHERE node_id = %s",
            [int(accept), node_id])

    async def delete(self, node_id: str) -> int:
        return await self.db.execute(
            f"DELETE FROM `{T.CLUSTER_NODE}` WHERE node_id = %s", [node_id])

    # ------------------------------------------------------------------ 租约
    async def acquire_lease(self, holder: str, ttl_seconds: int,
                            name: str = MASTER_LEASE) -> bool:
        """抢 / 续租约。持有者是自己、或者上一个持有者过期了，才能拿到。

        ⚠️ 不能看 UPDATE 的受影响行数：同一秒里续两次，expires_at 值没变，
        MySQL 报 0 行受影响，会误以为租约丢了。所以更新完再读一次持有者。
        """
        ttl = max(5, int(ttl_seconds))
        # 不用 INSERT IGNORE：行已存在时它每次都报一条 Duplicate entry 警告，心跳一次一条
        await self.db.execute(
            f"INSERT INTO `{T.CLUSTER_LEASE}` (lease_name, holder, expires_at) "
            f"VALUES (%s, %s, NOW() + INTERVAL %s SECOND) "
            f"ON DUPLICATE KEY UPDATE lease_name = lease_name", [name, holder, ttl])
        await self.db.execute(
            f"UPDATE `{T.CLUSTER_LEASE}` SET holder = %s, expires_at = NOW() + INTERVAL %s SECOND "
            f"WHERE lease_name = %s AND (holder = %s OR expires_at < NOW())",
            [holder, ttl, name, holder])
        return await self.lease_holder(name) == holder

    async def lease_holder(self, name: str = MASTER_LEASE) -> str:
        row = await self.db.fetch_one(
            f"SELECT holder FROM `{T.CLUSTER_LEASE}` WHERE lease_name = %s AND expires_at >= NOW()",
            [name])
        return str(row["holder"]) if row else ""

    async def release_lease(self, holder: str, name: str = MASTER_LEASE) -> None:
        """正常关机时主动交出，别的主机不用等租约过期就能接手。"""
        await self.db.execute(
            f"UPDATE `{T.CLUSTER_LEASE}` SET expires_at = NOW() - INTERVAL 1 SECOND "
            f"WHERE lease_name = %s AND holder = %s", [name, holder])

    # ------------------------------------------------------------------ 设置同步
    async def settings_digest(self) -> Dict[str, str]:
        """各段设置的原文。别的机器在页面上改了设置，本机靠比对它发现。"""
        rows = await self.db.fetch_all(
            f"SELECT setting_key, setting_value FROM `{T.SETTING}`")
        return {str(r["setting_key"]): str(r["setting_value"] or "") for r in rows}


def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    raw = item.get("stats")
    try:
        item["stats"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        item["stats"] = {}
    return item
