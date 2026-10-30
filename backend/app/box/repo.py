# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主这边：景区盒子的登记。

令牌只在新建 / 重置时给一次明文，库里只存哈希（和登录会话一个做法）。
认盒子的结果在进程里缓存几秒：盒子上报和页面查数据都很频繁，不用每次查库。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core.db import Database
from ..db import tables as T

CACHE_SECONDS = 10
#: 距上次记「最近连上」超过这么久才写一次库
TOUCH_SECONDS = 30

_PUBLIC = ("id, box_id, box_name, scenic_ids, enabled, last_seen_at, last_ip, "
           "uploaded_works, uploaded_comments, create_time")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return "box_" + secrets.token_urlsafe(32)


def _shape(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    item = dict(row)
    try:
        ids = json.loads(item.get("scenic_ids") or "[]")
    except (TypeError, ValueError):
        ids = []
    item["scenic_ids"] = [str(i) for i in ids if str(i).strip()]
    item["enabled"] = bool(item.get("enabled"))
    return item


class BoxRepository:
    def __init__(self, db: Database):
        self.db = db
        self._cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._touched: Dict[str, float] = {}

    # ------------------------------------------------------------------ 管理
    async def list(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(f"SELECT {_PUBLIC} FROM `{T.BOX}` ORDER BY id ASC")
        return [_shape(r) for r in rows]

    async def get(self, box_id: str) -> Optional[Dict[str, Any]]:
        return _shape(await self.db.fetch_one(
            f"SELECT {_PUBLIC} FROM `{T.BOX}` WHERE box_id = %s", [box_id]))

    async def create(self, box_name: str, scenic_ids: List[str]) -> Tuple[Dict[str, Any], str]:
        token = new_token()
        box_id = "box-" + secrets.token_hex(4)
        await self.db.execute(
            f"INSERT INTO `{T.BOX}` (box_id, box_name, token_hash, scenic_ids) VALUES (%s, %s, %s, %s)",
            [box_id, box_name, token_hash(token), json.dumps(list(scenic_ids), ensure_ascii=False)])
        return await self.get(box_id), token

    async def update(self, box_id: str, fields: Dict[str, Any]) -> None:
        sets, args = [], []
        if "box_name" in fields:
            sets.append("box_name = %s")
            args.append(fields["box_name"])
        if "scenic_ids" in fields:
            sets.append("scenic_ids = %s")
            args.append(json.dumps(list(fields["scenic_ids"]), ensure_ascii=False))
        if "enabled" in fields:
            sets.append("enabled = %s")
            args.append(1 if fields["enabled"] else 0)
        if sets:
            await self.db.execute(
                f"UPDATE `{T.BOX}` SET {', '.join(sets)} WHERE box_id = %s", [*args, box_id])
        self.forget(box_id)

    async def reset_token(self, box_id: str) -> str:
        token = new_token()
        await self.db.execute(
            f"UPDATE `{T.BOX}` SET token_hash = %s WHERE box_id = %s", [token_hash(token), box_id])
        self.forget(box_id)
        return token

    async def delete(self, box_id: str) -> None:
        await self.db.execute(f"DELETE FROM `{T.BOX}` WHERE box_id = %s", [box_id])
        self.forget(box_id)

    def forget(self, box_id: str) -> None:
        """改了名字 / 景区 / 停用 / 换令牌：缓存里这个盒子马上作废。"""
        for key, (box, _) in list(self._cache.items()):
            if box.get("box_id") == box_id:
                self._cache.pop(key, None)

    # ------------------------------------------------------------------ 认盒子
    async def authenticate(self, token: str, ip: str = "") -> Optional[Dict[str, Any]]:
        if not token:
            return None
        key = token_hash(token)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[1] < CACHE_SECONDS:
            box = hit[0]
        else:
            box = _shape(await self.db.fetch_one(
                f"SELECT {_PUBLIC} FROM `{T.BOX}` WHERE token_hash = %s AND enabled = 1", [key]))
            if not box:
                self._cache.pop(key, None)
                return None
            self._cache[key] = (box, now)
        if now - self._touched.get(key, 0) > TOUCH_SECONDS:
            self._touched[key] = now
            await self.db.execute(
                f"UPDATE `{T.BOX}` SET last_seen_at = NOW(), last_ip = %s WHERE box_id = %s",
                [ip[:64], box["box_id"]])
        return box

    async def count_upload(self, box_id: str, works: int = 0, comments: int = 0) -> None:
        if works or comments:
            await self.db.execute(
                f"UPDATE `{T.BOX}` SET uploaded_works = uploaded_works + %s, "
                f"uploaded_comments = uploaded_comments + %s WHERE box_id = %s",
                [int(works), int(comments), box_id])


_repo: Optional[BoxRepository] = None


def set_box_repo(repo: Optional[BoxRepository]) -> None:
    global _repo
    _repo = repo


def get_box_repo() -> Optional[BoxRepository]:
    return _repo
