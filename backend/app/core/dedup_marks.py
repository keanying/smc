"""采集水印：关键字级 + 作品级，都落 Redis。

要解决什么
----------
用户的原话：

  「采集关键字各加一个水印到 redis，避免重新开启已经采集过的关键字重复采集，
    默认过期时间 4 小时。其实我也再想 笔记/作品/博文也是一样的，
    采过的直接加一个水印，默认过期时间 3 天，采过的直接跳过。」

两级水印解决两件不同的事：

* **关键字水印（默认 4 小时）** —— 任务被反复重启时，别把刚采完的关键字再跑一遍。
  拟人模式一个关键字十几分钟，重复跑的代价是实打实的时间和风控额度。
* **作品水印（默认 3 天）** —— 同一条作品被多个关键字命中是常态（实测重合率三成），
  而且天天跑的话前天采过的今天基本没变化。命中就跳过，**连评论都不用翻**。

为什么用 Redis 而不是查库
-------------------------
查库能得到"采过没"，但得不到"多久之前采的"，而且每条作品一次查询。
Redis 的 TTL 天然表达"这个结论只在这段时间内有效"，SETNX 还能一次调用
完成"检查 + 打标"，不用担心并发下重复。

⚠️ Redis 连不上时 RedisClient 会退化成**进程内缓存**。那时候水印只在
   本进程有效，多进程部署会各跑各的——这不是 bug，是有意的降级：
   宁可重复采一点，也不能因为 Redis 挂了就不采了。
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .logging import get_logger

logger = get_logger(__name__)

#: 出厂默认。用户可在 crawl.dedup 里改。
DEFAULT_KEYWORD_TTL = 4 * 3600        # 关键字：4 小时
DEFAULT_WORK_TTL = 3 * 24 * 3600      # 作品/笔记/博文：3 天


def _short(text: str, limit: int = 80) -> str:
    """关键字可能很长、还带各种符号，超长就用哈希兜底。

    Redis 的 key 本身没长度问题，但太长的 key 在 `KEYS`/日志里没法看，
    而且中文关键字拼出来的 key 容易踩到各种编码坑。
    """
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class DedupMarks:
    """两级水印的读写。"""

    def __init__(self, redis, config=None):
        self.redis = redis
        cfg = ((config.get("crawl") or {}).get("dedup") or {}) if config else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.keyword_ttl = int(cfg.get("keyword_ttl_seconds", DEFAULT_KEYWORD_TTL))
        self.work_ttl = int(cfg.get("work_ttl_seconds", DEFAULT_WORK_TTL))
        #: 关键字水印可以单独关（有人想每次都重跑关键字，只靠作品级去重）
        self.keyword_enabled = bool(cfg.get("keyword_enabled", True))
        self.work_enabled = bool(cfg.get("work_enabled", True))

    # ------------------------------------------------------------- 关键字
    def _keyword_key(self, scenic_id: str, channel: str, keyword: str) -> str:
        return self.redis.key("done", "kw", scenic_id or "-", channel,
                              _short(keyword))

    def keyword_done(self, scenic_id: str, channel: str, keyword: str) -> bool:
        """这个关键字在水印有效期内采过没。"""
        if not (self.enabled and self.keyword_enabled):
            return False
        try:
            return bool(self.redis.get(self._keyword_key(scenic_id, channel, keyword)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[水印] 读关键字水印失败（当作没采过）：%s", exc)
            return False

    def keyword_ttl_left(self, scenic_id: str, channel: str, keyword: str) -> int:
        """还剩多久过期。日志里写出来，用户才知道"还要等多久才会再采"。"""
        try:
            return int(self.redis.ttl(self._keyword_key(scenic_id, channel, keyword)))
        except Exception:  # noqa: BLE001
            return -1

    def mark_keyword(self, scenic_id: str, channel: str, keyword: str) -> None:
        """关键字采完了，打上水印。"""
        if not (self.enabled and self.keyword_enabled):
            return
        try:
            self.redis.set(self._keyword_key(scenic_id, channel, keyword),
                           "1", ex=self.keyword_ttl)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[水印] 写关键字水印失败（不影响采集）：%s", exc)

    def clear_keyword(self, scenic_id: str, channel: str, keyword: str) -> None:
        """手动清掉一个关键字的水印（页面上"强制重采"用）。"""
        try:
            self.redis.delete(self._keyword_key(scenic_id, channel, keyword))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- 作品
    def _work_key(self, channel: str, work_id: str) -> str:
        # ⚠️ 作品水印**不带 scenic_id**：同一条作品可能被多个景区的关键字命中，
        # 但它就是同一条作品，采过一次就够了。带上景区会让去重失效。
        return self.redis.key("done", "work", channel, _short(work_id, 120))

    def work_done(self, channel: str, work_id: str) -> bool:
        if not (self.enabled and self.work_enabled) or not work_id:
            return False
        try:
            return bool(self.redis.get(self._work_key(channel, work_id)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[水印] 读作品水印失败（当作没采过）：%s", exc)
            return False

    def mark_work(self, channel: str, work_id: str) -> None:
        if not (self.enabled and self.work_enabled) or not work_id:
            return
        try:
            self.redis.set(self._work_key(channel, work_id), "1", ex=self.work_ttl)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[水印] 写作品水印失败（不影响采集）：%s", exc)

    def claim_work(self, channel: str, work_id: str) -> bool:
        """**原子**地"检查 + 占位"：拿到 True 表示这条归你采。

        用 SETNX 而不是先 get 再 set：两个任务同时跑同一个景区时，
        先查后写会双双认为"没人采过"，然后都去采一遍。
        """
        if not (self.enabled and self.work_enabled) or not work_id:
            return True
        try:
            got = self.redis.set(self._work_key(channel, work_id), "1",
                                 ex=self.work_ttl, nx=True)
            return bool(got)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[水印] 占位失败（放行）：%s", exc)
            return True

    def describe(self) -> str:
        """任务开始时打一行，用户能确认水印是不是按预期生效。"""
        if not self.enabled:
            return "采集水印：已关闭（每次都会重新采）"
        parts = []
        parts.append(f"关键字 {self.keyword_ttl // 3600} 小时"
                     if self.keyword_enabled else "关键字 关闭")
        parts.append(f"作品 {self.work_ttl // 86400} 天"
                     if self.work_enabled else "作品 关闭")
        return "采集水印：" + "，".join(parts) + "（采过的直接跳过）"
