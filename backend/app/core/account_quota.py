"""单账号配额与冷却：别把一个账号用到被封。

要解决的问题
------------------------------------------------------------------
平台判定"机器号"看的不只是**每次请求间隔**（那个 `core/pacing.py`
已经管了，小红书是 2~10 秒一条，并不激进），还看**一个账号的总量和
连续在线时长**。而这套系统在此之前对后者没有任何约束：

  · 没有"每天最多采多少"
  · 没有"连续工作多久该歇了"
  · 一个账号可以被连着用到看门狗超时（默认 6 小时）

真人不会连刷 6 小时。所以这里加两道闸：**日配额**和**单次时长**，
任一超了就让账号进入**冷却**，挑账号时自动跳过，到期自动恢复。

存在哪儿，为什么是两份
------------------------------------------------------------------
    Redis   热计数。判配额每条作品都要读一次，走 MySQL 太吵。
    MySQL   事实来源 + 页面要展示的历史用量；冷却状态也在库里，
            因为它必须跨重启、跨进程都算数。

Redis 会退化成进程内缓存（见 core/redis_client.py），**退化之后计数
就不共享了**。所以判定时以 MySQL 的累计值为准、Redis 只当缓存：
Redis 读不到就回 MySQL 取并回填。宁可慢一点，也不能因为 Redis 断了
就把配额闸门悄悄放开——那正是这个模块要防的事。

并发
------------------------------------------------------------------
计数用的是 get→+1→set，不是原子操作。这里成立是因为
`browser/slots.py` 保证**同一个 (渠道, 账号) 同时只有一个采集在跑**，
计数天然没有竞争。多机部署下这条不成立（见 docs/多机部署设计.md），
那时要换成 Redis INCR。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from ..db import tables
from .logging import get_logger

logger = get_logger(__name__)

#: 出厂默认。0 表示这一项不限制。
DEFAULT_LIMITS: Dict[str, int] = {
    "daily_works": 0,           # 每账号每天最多采多少条作品
    "session_minutes": 0,       # 单次连续工作上限（分钟）
    "cooldown_minutes": 120,    # 触发后冷却多久
}

#: 各平台的建议默认值。小红书风控最紧，所以给得最保守。
#: 这些是**出厂值**，用户在系统设置里改的会覆盖。
CHANNEL_DEFAULTS: Dict[str, Dict[str, int]] = {
    "xiaohongshu": {"daily_works": 150, "session_minutes": 45, "cooldown_minutes": 180},
    "douyin":      {"daily_works": 300, "session_minutes": 90, "cooldown_minutes": 120},
    "kuaishou":    {"daily_works": 300, "session_minutes": 90, "cooldown_minutes": 120},
    "weibo":       {"daily_works": 500, "session_minutes": 120, "cooldown_minutes": 60},
}


@dataclass
class QuotaVerdict:
    """一次判定的结果。`ok=False` 时 `reason` 是给人看的一句话。"""
    ok: bool
    reason: str = ""
    #: 建议冷却到什么时候（触发配额时才有）
    cooldown_until: Optional[datetime] = None

    def __bool__(self) -> bool:
        return self.ok


def _today() -> str:
    return date.today().isoformat()


class AccountQuota:
    """配额闸门。挂在 AppState 上，全进程一个。"""

    def __init__(self, config, redis, db):
        self.config = config
        self.redis = redis
        self.db = db
        #: 本次会话的开始时刻，进程内就够——连续工作时长本来就是
        #: "这一轮从什么时候开始跑"，跨进程没有意义。
        self._session_start: Dict[str, float] = {}

    # ---------------- 配置 ----------------
    @property
    def enabled(self) -> bool:
        cfg = self._raw_config()
        return bool(cfg.get("enabled", True))

    def _raw_config(self) -> Dict[str, Any]:
        try:
            crawl = self.config.get("crawl") or {}
        except Exception:  # noqa: BLE001
            return {}
        cfg = crawl.get("account_quota")
        return cfg if isinstance(cfg, dict) else {}

    def limits_for(self, channel: str) -> Dict[str, int]:
        """某个渠道实际生效的三个上限。

        叠加顺序：出厂默认 → 渠道出厂默认 → 用户全局配置 → 用户渠道配置。
        后面的盖前面的。
        """
        merged = self.base_limits_for(channel)
        per_channel = self._raw_config().get("per_channel")
        if isinstance(per_channel, dict):
            _apply(merged, per_channel.get(channel))
        return merged

    def base_limits_for(self, channel: str) -> Dict[str, int]:
        """不算「单平台配置」时的上限：出厂默认 → 渠道出厂默认 → 用户全局配置。

        账号管理页的「配额与轮换」里，单平台那一格留空时就是这个值，
        拿来当输入框的占位提示（"默认 150"），用户才知道不填等于多少。
        """
        merged = dict(DEFAULT_LIMITS)
        merged.update(CHANNEL_DEFAULTS.get(channel, {}))
        _apply(merged, self._raw_config())
        return merged

    # ---------------- 计数 ----------------
    def _redis_key(self, channel: str, account: str) -> str:
        return self.redis.key("quota", channel, account, _today())

    async def used_today(self, channel: str, account: str) -> int:
        """今天这个账号已经采了多少条作品。

        Redis 优先；读不到就回 MySQL 取并回填——**不能因为 Redis 断了
        就当成 0**，那等于配额闸门被悄悄放开。
        """
        key = self._redis_key(channel, account)
        try:
            cached = self.redis.get(key)
        except Exception:  # noqa: BLE001
            cached = None
        if cached is not None:
            try:
                return int(cached)
            except (TypeError, ValueError):
                pass

        total = await self._db_used_today(channel, account)
        try:
            self.redis.set(key, str(total), ex=_seconds_to_midnight())
        except Exception:  # noqa: BLE001
            pass
        return total

    async def _db_used_today(self, channel: str, account: str) -> int:
        if self.db is None:
            return 0
        try:
            value = await self.db.fetch_value(
                f"SELECT works FROM `{tables.ACCOUNT_QUOTA}` "
                f"WHERE channel = %s AND account_name = %s AND stat_date = %s",
                [channel, account, _today()], 0)
        except Exception as exc:  # noqa: BLE001
            _shrug("读日用量", channel, account, exc)
            return 0
        return int(value or 0)

    async def record_works(self, channel: str, account: str, n: int = 1) -> None:
        """采到 n 条作品。Redis 和 MySQL 都记。"""
        if not account or n <= 0:
            return
        key = self._redis_key(channel, account)
        try:
            current = int(self.redis.get(key) or 0)
        except (TypeError, ValueError):
            current = 0
        except Exception:  # noqa: BLE001
            current = 0
        try:
            self.redis.set(key, str(current + n), ex=_seconds_to_midnight())
        except Exception:  # noqa: BLE001
            pass

        if self.db is None:
            return
        try:
            await self.db.execute(
                f"INSERT INTO `{tables.ACCOUNT_QUOTA}` "
                f"(channel, account_name, stat_date, works, last_active_at) "
                f"VALUES (%s, %s, %s, %s, %s) "
                f"ON DUPLICATE KEY UPDATE works = works + VALUES(works), "
                f"last_active_at = VALUES(last_active_at)",
                [channel, account, _today(), int(n), datetime.now()])
        except Exception as exc:  # noqa: BLE001
            _shrug("记用量", channel, account, exc)

    # ---------------- 会话时长 ----------------
    def start_session(self, channel: str, account: str) -> None:
        """账号开始干活。单次连续时长从这里算。"""
        if account:
            self._session_start[f"{channel}:{account}"] = time.monotonic()

    def end_session(self, channel: str, account: str) -> None:
        self._session_start.pop(f"{channel}:{account}", None)

    def session_minutes(self, channel: str, account: str) -> float:
        started = self._session_start.get(f"{channel}:{account}")
        return 0.0 if started is None else (time.monotonic() - started) / 60.0

    # ---------------- 判定 ----------------
    async def check(self, channel: str, account: str) -> QuotaVerdict:
        """这个账号现在还能不能接着用。

        判定顺序：冷却中？→ 日配额满了？→ 连续工作超时了？
        冷却放最前面是因为它最便宜（一次 SELECT），而且冷却中的账号
        连数都不用看。
        """
        if not self.enabled or not account:
            return QuotaVerdict(True)

        cooling = await self.cooling_until(channel, account)
        if cooling is not None:
            left = (cooling - datetime.now()).total_seconds() / 60
            return QuotaVerdict(
                False,
                f"账号 [{channel}/{account}] 正在冷却，还有 {max(1, int(left))} 分钟"
                f"（到 {cooling:%H:%M}）")

        limits = self.limits_for(channel)

        daily = limits.get("daily_works", 0)
        if daily > 0:
            used = await self.used_today(channel, account)
            if used >= daily:
                until = await self.cool_down(
                    channel, account,
                    f"今日配额用满（{used}/{daily} 条）", limits)
                return QuotaVerdict(
                    False,
                    f"账号 [{channel}/{account}] 今天已经采了 {used} 条，"
                    f"到达日配额 {daily}，进入冷却到 {until:%H:%M}",
                    until)

        session_cap = limits.get("session_minutes", 0)
        if session_cap > 0:
            minutes = self.session_minutes(channel, account)
            if minutes >= session_cap:
                until = await self.cool_down(
                    channel, account,
                    f"连续工作 {minutes:.0f} 分钟（上限 {session_cap}）", limits)
                return QuotaVerdict(
                    False,
                    f"账号 [{channel}/{account}] 已经连续工作 {minutes:.0f} 分钟，"
                    f"到达单次上限 {session_cap} 分钟，进入冷却到 {until:%H:%M}",
                    until)

        return QuotaVerdict(True)

    # ---------------- 冷却 ----------------
    async def cooling_until(self, channel: str, account: str) -> Optional[datetime]:
        """还在冷却就返回到期时间，否则 None。"""
        if self.db is None or not account:
            return None
        try:
            row = await self.db.fetch_one(
                f"SELECT cooldown_until FROM `{tables.ACCOUNT}` "
                f"WHERE channel = %s AND account_name = %s", [channel, account])
        except Exception as exc:  # noqa: BLE001
            _shrug("读冷却状态", channel, account, exc)
            return None
        if not row:
            return None
        until = row.get("cooldown_until")
        if not until:
            return None
        if isinstance(until, str):
            try:
                until = datetime.fromisoformat(until)
            except ValueError:
                return None
        return until if until > datetime.now() else None

    async def cool_down(self, channel: str, account: str, reason: str,
                        limits: Optional[Dict[str, int]] = None) -> datetime:
        """让账号进入冷却。返回到期时间。"""
        limits = limits or self.limits_for(channel)
        minutes = max(1, int(limits.get("cooldown_minutes", 120) or 120))
        until = datetime.now() + timedelta(minutes=minutes)
        if self.db is not None:
            try:
                await self.db.execute(
                    f"UPDATE `{tables.ACCOUNT}` SET cooldown_until = %s, "
                    f"cooldown_reason = %s WHERE channel = %s AND account_name = %s",
                    [until, reason[:200], channel, account])
            except Exception as exc:  # noqa: BLE001
                _shrug("写冷却状态", channel, account, exc)
        self.end_session(channel, account)
        logger.warning("[配额] 账号 %s/%s 进入冷却 %d 分钟：%s",
                       channel, account, minutes, reason)
        return until

    async def clear_cooldown(self, channel: str, account: str) -> None:
        """人工解除冷却。页面上给一个按钮，急着用的时候能放出来。"""
        if self.db is None:
            return
        await self.db.execute(
            f"UPDATE `{tables.ACCOUNT}` SET cooldown_until = NULL, "
            f"cooldown_reason = '' WHERE channel = %s AND account_name = %s",
            [channel, account])
        logger.info("[配额] 账号 %s/%s 的冷却已人工解除", channel, account)

    # ---------------- 页面展示 ----------------
    async def usage(self, channel: str = "", account: str = "",
                    days: int = 1) -> List[Dict[str, Any]]:
        """账号管理页要的用量。默认只看今天。"""
        if self.db is None:
            return []
        clauses = ["stat_date >= %s"]
        args: List[Any] = [(date.today() - timedelta(days=max(0, days - 1))).isoformat()]
        if channel:
            clauses.append("channel = %s"); args.append(channel)
        if account:
            clauses.append("account_name = %s"); args.append(account)
        try:
            return await self.db.fetch_all(
                f"SELECT channel, account_name, stat_date, works, last_active_at "
                f"FROM `{tables.ACCOUNT_QUOTA}` WHERE {' AND '.join(clauses)} "
                f"ORDER BY stat_date DESC, works DESC", args)
        except Exception as exc:  # noqa: BLE001
            _shrug("读用量列表", channel, account, exc)
            return []


def _shrug(what: str, channel: str, account: str, exc: Exception) -> None:
    """配额出错就算了，别把采集带下去。

    ⚠️ 这不是"偷懒吞异常"。这一整套配额是**闸门**，不是**数据**：
    老库里可能根本没有 src_opinion_account_quota 表、也没有
    cooldown_until 列（CREATE TABLE IF NOT EXISTS 会补表，但补不了
    别人家的列）。如果让这里的 SQL 错误冒上去，结果是"升级完之后
    采集直接不工作了"——为了一个防护功能，把主功能弄挂，不值当。

    代价是闸门在这种库上失效。所以打的是 warning 不是 debug：
    日志里必须看得见"配额没生效"，而不是悄悄放行。
    """
    logger.warning("[配额] %s 失败（%s/%s），本次跳过闸门：%s",
                   what, channel or "-", account or "-", exc)


def _apply(merged: Dict[str, int], override: Any) -> None:
    """把用户配置叠加上去。**0 表示"这一项不填"，不是"不限制"。**

    ⚠️ 这条语义必须和设置页面上写的一致。页面上三个数字框默认是 0，
    提示写的是"留 0 = 按平台出厂默认"——如果这里把 0 当成"不限制"，
    用户什么都不改保存一下，小红书的 150 条/天就被悄悄改成了无限制，
    而页面上还显示着"按平台默认"。那比没有这个功能更危险。

    要彻底关掉配额，用 `enabled: false`，不要靠填 0。
    """
    if not isinstance(override, dict):
        return
    for key in DEFAULT_LIMITS:
        if key not in override:
            continue
        value = _as_int(override[key], 0)
        if value > 0:
            merged[key] = value


def _as_int(value: Any, fallback: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _seconds_to_midnight() -> int:
    """到次日零点还有多少秒。日计数的 TTL 用它，自然跨天清零。"""
    now = datetime.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return max(60, int((tomorrow - now).total_seconds()))
