"""平台账号的读写与挑选。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..core.constants import AccountStatus
from ..core.db import Database
from ..core.logging import get_logger

logger = get_logger(__name__)


#: "不在冷却中"的 SQL 片段。
#:
#: ⚠️ 挑账号的**每一个**入口都要带上它。漏掉一处的后果不是报错，
#: 是那条路径照样把冷却中的账号挑出来用——配额闸门等于不存在，
#: 而现象和没加过闸门一模一样（账号接着被用到封）。
#: tests/test_account_quota.py 里有一条结构性测试盯着这件事。
_NOT_COOLING = "(cooldown_until IS NULL OR cooldown_until <= NOW())"


class AccountRepository:
    def __init__(self, db: Database, rotation: Any = None):
        self.db = db
        #: 账号轮换锁（core/account_rotation.py）。为 None 时挑号退回原来的
        #: last_check_time 排序——测试里的半成品 repo 和老调用点都走这条路。
        self.rotation = rotation

    async def list(
        self, *, channel: str = "", status: str = "", group: str = "",
        page: int = 1, page_size: int = 50,
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        args: List[Any] = []
        if channel:
            clauses.append("channel = %s")
            args.append(channel)
        if status:
            clauses.append("status = %s")
            args.append(status)
        if group:
            clauses.append("account_group = %s")
            args.append(group)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `src_opinion_social_account` {where}", args, 0
        )
        rows = await self.db.fetch_all(
            f"SELECT id, channel, account_name, nickname, avatar, uid, login_type, "
            f"account_group, status, cooldown_until, cooldown_reason, "
            f"rotate_lock_hours, "
            f"profile_dir, cookie_updated_at, last_check_time, last_error, enabled, "
            f"create_time, update_time FROM `src_opinion_social_account` {where} "
            f"ORDER BY channel ASC, id DESC LIMIT %s OFFSET %s",
            [*args, int(page_size), max(0, (page - 1) * page_size)],
        )
        return {"total": int(total or 0), "page": page, "page_size": page_size, "items": rows}

    async def list_groups(self) -> List[Dict[str, Any]]:
        """所有账号分组及组内账号数，账号管理页的分组筛选用。"""
        return await self.db.fetch_all(
            "SELECT account_group AS name, COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'active' AND enabled = 1 THEN 1 ELSE 0 END) AS active "
            "FROM `src_opinion_social_account` GROUP BY account_group ORDER BY account_group"
        )

    async def get(self, channel: str, account_name: str) -> Optional[Dict]:
        return await self.db.fetch_one(
            "SELECT * FROM `src_opinion_social_account` WHERE channel = %s AND account_name = %s",
            [channel, account_name],
        )

    async def get_by_id(self, account_id: int) -> Optional[Dict]:
        return await self.db.fetch_one(
            "SELECT * FROM `src_opinion_social_account` WHERE id = %s", [account_id]
        )

    async def create(self, data: Dict[str, Any]) -> int:
        return await self.db.execute_returning_id(
            """
            INSERT INTO `src_opinion_social_account`
                (channel, account_name, nickname, login_type, account_group,
                 status, profile_dir, enabled, rotate_lock_hours)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                nickname = VALUES(nickname), login_type = VALUES(login_type),
                account_group = VALUES(account_group),
                enabled = VALUES(enabled),
                rotate_lock_hours = VALUES(rotate_lock_hours)
            """,
            [
                data["channel"], data["account_name"], data.get("nickname"),
                data.get("login_type", "qrcode"),
                (data.get("account_group") or "default")[:100],
                data.get("status", AccountStatus.NEVER_LOGIN.value),
                data.get("profile_dir"), int(data.get("enabled", 1)),
                max(0, int(data.get("rotate_lock_hours") or 0)),
            ],
        )

    async def update(self, channel: str, account_name: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        sets = [f"`{k}` = %s" for k in fields]
        args = list(fields.values()) + [channel, account_name]
        await self.db.execute(
            f"UPDATE `src_opinion_social_account` SET {', '.join(sets)} "
            f"WHERE channel = %s AND account_name = %s",
            args,
        )

    async def delete(self, channel: str, account_name: str) -> None:
        await self.db.execute(
            "DELETE FROM `src_opinion_social_account` WHERE channel = %s AND account_name = %s",
            [channel, account_name],
        )

    async def save_session(
        self, channel: str, account_name: str, *, cookies: List[Dict],
        profile_dir: str = "", fingerprint: Optional[Dict] = None,
        nickname: str = "", avatar: str = "", uid: str = "",
    ) -> None:
        """登录成功或静默刷新后写回登录态。"""
        fields: Dict[str, Any] = {
            "cookies": json.dumps(cookies, ensure_ascii=False),
            "cookie_updated_at": datetime.now(),
            "last_check_time": datetime.now(),
            "status": AccountStatus.ACTIVE.value,
            "last_error": None,
        }
        if profile_dir:
            fields["profile_dir"] = profile_dir
        if fingerprint:
            fields["fingerprint"] = json.dumps(fingerprint, ensure_ascii=False)
        if nickname:
            fields["nickname"] = nickname
        if avatar:
            fields["avatar"] = avatar
        if uid:
            fields["uid"] = uid
        await self.update(channel, account_name, fields)

    async def mark_expired(self, channel: str, account_name: str, reason: str = "") -> None:
        await self.update(channel, account_name, {
            "status": AccountStatus.EXPIRED.value,
            "last_check_time": datetime.now(),
            "last_error": (reason or "登录态已失效")[:500],
        })

    async def pick_active(
        self, channel: str, *, preferred: str = "", group: str = "",
        max_age_seconds: int = 0,
    ) -> Optional[Dict]:
        """挑一个可用账号。

        preferred 指定了就优先用它；否则按"最近校验过的排前面"轮换，
        让多个账号自然分摊请求量，而不是把一个账号用到被封。
        group 给了就只在该分组里挑——任务可以绑定一个账号组，
        不同业务线各用各的账号，互不挤占。
        """
        if preferred:
            row = await self.get(channel, preferred)
            if row and row["enabled"] and row["status"] == AccountStatus.ACTIVE.value:
                return row
            return row  # 交给上层决定是否触发重新登录

        clauses = ["channel = %s", "enabled = 1", "status = %s",
                   _NOT_COOLING]
        args: List[Any] = [channel, AccountStatus.ACTIVE.value]
        if group:
            clauses.append("account_group = %s")
            args.append(group)
        if max_age_seconds > 0:
            clauses.append("cookie_updated_at >= %s")
            args.append(datetime.now() - timedelta(seconds=max_age_seconds))
        where = " AND ".join(clauses)

        # ⚠️ 这里刻意**不**一步 `SELECT * ... LIMIT 1`。
        #    轮换锁要看过全部候选才知道该挑谁（见 core/account_rotation.py），
        #    但这张表的 cookies 是 LONGTEXT——把所有号的整串 Cookie 都拉
        #    回来纯属浪费。所以先只取排序要用的三列，定下人选再取整行。
        rows = await self.db.fetch_all(
            f"SELECT account_name, rotate_lock_hours FROM `src_opinion_social_account` "
            f"WHERE {where} ORDER BY last_check_time ASC, id ASC", args)
        if not rows:
            return None
        rows = self._rotate(channel, rows)
        return await self.get(channel, rows[0]["account_name"])

    def _rotate(self, channel: str, rows: List[Dict]) -> List[Dict]:
        """按轮换锁重排候选。没装轮换锁、或者重排出了岔子，就用原顺序。

        排完之后**条数必须没变**——轮换是"换个人先上"，不是"把人踢掉"。
        真要少了就说明实现写错了，这时候宁可用原顺序，也不能让候选变空
        导致采集停摆。
        """
        if self.rotation is None or len(rows) < 2:
            return rows
        try:
            ordered = self.rotation.order(channel, rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[轮换] 重排 %s 的候选账号失败，按原顺序来：%s",
                           channel, exc)
            return rows
        return ordered if len(ordered) == len(rows) else rows

    async def active_names(
        self, channel: str, *, preferred: str = "", group: str = ""
    ) -> List[str]:
        """这个平台当前能用的账号名，按"最久没校验的排前面"。

        给浏览器占用排队用（见 browser/slots.py）：拿到候选名单后，
        调度层会挑其中第一个**没被占用**的开浏览器；全被占就排队等。

        preferred 指定了账号就只返回它——用户点名要用哪个账号时，
        换一个账号采出来的数据归属就不对了，宁可等。
        只查 account_name 一列：这张表里 cookies 是 LONGTEXT，
        SELECT * 会把每个账号的整串 Cookie 都拉回来，纯属浪费。
        """
        if preferred:
            return [preferred]
        clauses = ["channel = %s", "enabled = 1", "status = %s", _NOT_COOLING]
        args: List[Any] = [channel, AccountStatus.ACTIVE.value]
        if group:
            clauses.append("account_group = %s")
            args.append(group)
        rows = await self.db.fetch_all(
            f"SELECT account_name, rotate_lock_hours FROM `src_opinion_social_account` "
            f"WHERE {' AND '.join(clauses)} ORDER BY last_check_time ASC, id ASC",
            args,
        )
        return [row["account_name"] for row in self._rotate(channel, rows)]

    async def mark_rotation_used(self, channel: str, account_name: str) -> None:
        """记一笔"这个号刚被派去采集了"，给轮换锁上锁。

        只查 `rotate_lock_hours` 一列：这张表的 cookies 是 LONGTEXT，
        为了拿一个小时数把整串 Cookie 拉回来不值当。
        """
        if self.rotation is None or not account_name:
            return
        try:
            row = await self.db.fetch_one(
                "SELECT rotate_lock_hours FROM `src_opinion_social_account` "
                "WHERE channel = %s AND account_name = %s", [channel, account_name])
            self.rotation.mark_used(channel, account_name, row)
        except Exception as exc:  # noqa: BLE001
            # 上锁失败最坏的结果是"下一轮又挑到它"，不该让采集停摆。
            logger.warning("[轮换] 给 %s/%s 上锁失败，本次不影响采集：%s",
                           channel, account_name, exc)

    async def list_active(self, channel: str) -> List[Dict]:
        return await self.db.fetch_all(
            f"SELECT * FROM `src_opinion_social_account` "
            f"WHERE channel = %s AND enabled = 1 AND status = %s AND {_NOT_COOLING} "
            f"ORDER BY last_check_time ASC",
            [channel, AccountStatus.ACTIVE.value],
        )

    @staticmethod
    def cookies_of(account: Dict[str, Any]) -> List[Dict]:
        raw = account.get("cookies")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @staticmethod
    def cookie_header(account: Dict[str, Any], host: str = "") -> str:
        """拼 Cookie 请求头。给了 host 就只发该站点收得到的那些。

        ⚠️ host 对微博是**必须**的：`.weibo.com` 和 `.weibo.cn` 各有一个
        叫 SUB 的 Cookie，值完全不同。不按域过滤就会把两个都发出去，
        去重后留下的可能正好是错的那份。
        """
        from ..browser import cookie_import

        cookies = AccountRepository.cookies_of(account)
        if host:
            return cookie_import.header_for_host(cookies, host)
        return cookie_import.to_header(cookies)
