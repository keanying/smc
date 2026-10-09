"""账号轮换锁：一个号今天用过了，下一轮先换别人。

要解决的问题
------------------------------------------------------------------
`account_quota.py` 管的是"一个号别用过头"（日配额、单次时长）。但在
撞到配额之前还有一段很长的灰色地带：三个号摆在那儿，调度却总是挑同
一个——因为 `pick_active` 的排序是 `last_check_time ASC`，而
**"最久没校验"不等于"最久没采集"**。校验（探活、抓 Cookie）跟采集是
两回事，一个号可能天天被采、却很久没被校验过，于是它永远排在最前面。

结果就是：号有三个，压力全压在一个上。平台看到的是"这个号天天在跑，
另外两个号像是摆设"——这本身就是一条很难看的曲线。

所以这里加一把**轮换锁**：一个号被派去采集之后，在锁定期内
（默认 12 小时）排到候选名单的**最后面**，让没干过活的号先上。

为什么是"排到后面"而不是"排除掉"
------------------------------------------------------------------
因为用户明确说了：**除非只有一个账号**。

如果做成硬过滤，那么"只有一个号"、"所有号都在锁定期内"这两种情况下
候选名单会变空，采集直接停摆——为了轮换把主功能弄停，方向就错了。
排序则天然满足这条：能换就换，换不了就还用它（挑锁最早到期的那个，
也就是歇得最久的）。

这跟**冷却**（`account_quota.cool_down`）不是一回事，别搞混：

    冷却    硬闸门。这个号已经出问题了/用满了，**不许**再用。
            存 MySQL，`pick_active` 一条 SQL 直接排掉。
    轮换锁  软偏好。这个号没毛病，只是**刚干过活**，有别人就让别人上。
            存 Redis，到点自动过期，不需要任何清理逻辑。

存 Redis 的代价
------------------------------------------------------------------
Redis 会静默退化成进程内缓存（见 `core/redis_client.py`）。退化之后
锁不跨进程、也不跨重启，轮换就退回成原来的 `last_check_time` 排序。

这个代价可以接受，因为轮换锁是**优化**不是**约束**：丢了锁最坏的结果
是"又挑回了老样子"，不会造成超采——真正的上限由配额和冷却把着，那两个
是以 MySQL 为准的。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from .logging import get_logger

logger = get_logger(__name__)

#: 出厂默认：一个号采过之后，12 小时内先让别人上。
DEFAULT_LOCK_HOURS = 12.0

#: 上限。写太大等于把号永久雪藏，多半是填错了。
MAX_LOCK_HOURS = 24 * 14


class AccountRotation:
    """账号轮换锁。只依赖 Redis，读不到就当"没锁"。"""

    def __init__(self, config, redis):
        self.config = config
        self.redis = redis

    # ---------------- 配置 ----------------
    def _raw_config(self) -> Dict[str, Any]:
        try:
            crawl = self.config.get("crawl") or {}
        except Exception:  # noqa: BLE001
            return {}
        cfg = crawl.get("account_quota")
        return cfg if isinstance(cfg, dict) else {}

    @property
    def enabled(self) -> bool:
        return bool(self._raw_config().get("rotate_lock_enabled", True))

    def global_hours(self) -> float:
        """全局默认锁定时长。0 或填错都退回出厂 12 小时。"""
        return _hours(self._raw_config().get("rotate_lock_hours"), DEFAULT_LOCK_HOURS)

    def hours_for(self, account: Optional[Dict[str, Any]] = None) -> float:
        """某个号实际生效的锁定时长。

        账号自己填了就用账号的（**这就是"在账号侧配置"**），
        填 0 或没填就用全局默认。
        """
        if account:
            own = account.get("rotate_lock_hours")
            if own not in (None, "", 0, "0"):
                return _hours(own, self.global_hours())
        return self.global_hours()

    # ---------------- 锁 ----------------
    def _key(self, channel: str, account: str) -> str:
        return self.redis.key("rotate", channel, account)

    def mark_used(self, channel: str, account: str,
                  account_row: Optional[Dict[str, Any]] = None) -> None:
        """这个号刚被派去采集了，按它自己的时长上锁。"""
        if not account or not self.enabled:
            return
        hours = self.hours_for(account_row)
        if hours <= 0:
            return
        try:
            self.redis.set(self._key(channel, account), str(int(time.time())),
                           ex=int(hours * 3600))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[轮换] 给 %s/%s 上锁失败，本次不影响采集：%s",
                           channel, account, exc)

    def locked_seconds(self, channel: str, account: str) -> int:
        """还要锁多久（秒）。没锁或读不到都返回 0。"""
        if not account or not self.enabled:
            return 0
        try:
            ttl = self.redis.ttl(self._key(channel, account))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[轮换] 读 %s/%s 的锁失败：%s", channel, account, exc)
            return 0
        # -1 = 没设过期，-2 = key 不存在；两种都当"没锁"
        return int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else 0

    def release(self, channel: str, account: str) -> None:
        """人工解锁。页面上"立刻用这个号"之类的入口会用到。"""
        try:
            self.redis.delete(self._key(channel, account))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[轮换] 解锁 %s/%s 失败：%s", channel, account, exc)

    # ---------------- 排序 ----------------
    def order(self, channel: str,
              accounts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把候选名单重排：没锁的按原顺序在前，锁着的按"最早解锁"在后。

        ⚠️ **只重排，不删**。所有号都锁着的时候（包括"就一个号"这种
        最常见的情况）照样要返回完整名单，否则采集就停了。
        """
        rows = list(accounts)
        if not self.enabled or len(rows) < 2:
            return rows
        free: List[Dict[str, Any]] = []
        locked: List[tuple] = []
        for row in rows:
            left = self.locked_seconds(channel, row.get("account_name") or "")
            (free.append(row) if left <= 0
             else locked.append((left, len(locked), row)))
        if locked and free:
            logger.debug("[轮换] %s：%d 个号在锁定期内，先用另外 %d 个",
                         channel, len(locked), len(free))
        locked.sort(key=lambda item: (item[0], item[1]))
        return free + [item[2] for item in locked]


def _hours(value: Any, fallback: float) -> float:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return fallback
    if hours <= 0:
        return fallback
    return min(hours, MAX_LOCK_HOURS)
