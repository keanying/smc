"""Redis 客户端构造：兼容老版本服务端。

为什么需要这个文件：

    新版 redis-py（>=5，尤其 6/8）默认会在建连时做 RESP3 协议握手，发 ``HELLO``；
    连上之后还会发 ``CLIENT SETINFO`` 上报客户端库名。这两个命令分别是
    Redis 6.0 和 7.2 才有的。

    Windows 上常见的 Redis 是微软那个停更在 3.0.504 的移植版，
    以及一些老的 Memurai/内网镜像，它们直接回：

        unknown command 'HELLO'

    我们一个 RESP3 特性都没用到（就是 LPUSH / BRPOPLPUSH / HSET / ZADD），
    所以显式退回 RESP2、并关掉客户端信息上报，对新老服务端都能连上。

参数按已安装 redis-py 的签名过滤，所以 4.x 到 8.x 都能跑——
硬写 ``protocol=2`` 在 redis-py 4.x 上会直接 TypeError。
"""

from __future__ import annotations

import inspect
from typing import Any, Dict

from .config import RedisConfig
from .logging_conf import get_logger

__all__ = ["create_redis_client", "explain_redis_error"]

logger = get_logger(__name__)

#: 关掉客户端信息上报的参数。两代 redis-py 写法不同，按签名二选一：
#:   redis-py >= 8：driver_info=None（lib_name/lib_version 已标记废弃，传了会告警）
#:   redis-py 5~7：lib_name=None + lib_version=None
_NO_CLIENT_INFO_MODERN: Dict[str, Any] = {"driver_info": None}
_NO_CLIENT_INFO_LEGACY: Dict[str, Any] = {"lib_name": None, "lib_version": None}


def create_redis_client(cfg: RedisConfig, **overrides: Any):
    """按配置建一个 Redis 客户端，自动适配 redis-py 版本与老服务端。

    Args:
        cfg: Redis 连接配置。
        **overrides: 覆盖或追加的 ``redis.Redis`` 参数。

    Raises:
        RuntimeError: 没装 redis 包。
    """
    try:
        import redis
    except ImportError as exc:      # pragma: no cover
        raise RuntimeError("需要安装 redis 包：pip install redis") from exc

    kwargs: Dict[str, Any] = {
        "host": cfg.host,
        "port": cfg.port,
        "db": cfg.db,
        "password": cfg.password or None,
        "socket_timeout": cfg.socket_timeout,
        "socket_connect_timeout": cfg.socket_timeout,
        "decode_responses": True,
        "health_check_interval": 30,
    }

    supported = set(inspect.signature(redis.Redis.__init__).parameters)

    # 1) 退回 RESP2：不发 HELLO（Redis 6.0 才有的命令）
    if "protocol" in supported:
        kwargs["protocol"] = 2

    # 2) 关掉客户端信息上报：不发 CLIENT SETINFO（Redis 7.2 才有）。
    #    只做第 1 步是不够的——实测 redis-py 8 在 RESP2 下仍然会发 CLIENT SETINFO。
    if "driver_info" in supported:
        kwargs.update(_NO_CLIENT_INFO_MODERN)
    else:
        kwargs.update({k: v for k, v in _NO_CLIENT_INFO_LEGACY.items() if k in supported})

    kwargs.update(overrides)
    return redis.Redis(**kwargs)


def explain_redis_error(exc: Exception, cfg: RedisConfig) -> str:
    """把 Redis 连接异常翻译成能照着做的提示。

    连不上 Redis 时最没用的日志就是原样打一句 ``unknown command 'HELLO'``——
    看到的人得自己去查那是什么。这里把常见几种直接说清楚。
    """
    text = str(exc)
    where = f"{cfg.host}:{cfg.port}/{cfg.db}"

    if "HELLO" in text or "CLIENT SETINFO" in text or "unknown command" in text.lower():
        return (
            f"{where} 的 Redis 版本过老，不认新版客户端的握手命令（{text}）。"
            f"本引擎已经显式退回 RESP2，如果还报这个错，说明装的 redis-py 太新、"
            f"没有 protocol 参数可退——降到 redis==5.0.* 即可；"
            f"或者把服务端换成 Redis 6+（Windows 上可用 Memurai 或 WSL）"
        )
    if "Connection refused" in text or "10061" in text:
        return (f"{where} 拒绝连接：Redis 没起来，或者端口/db 配错了。"
                f"先用 redis-cli -h {cfg.host} -p {cfg.port} ping 确认")
    if "AUTH" in text or "NOAUTH" in text or "invalid password" in text.lower():
        return f"{where} 鉴权失败：检查 failure_store.redis.password 或 REDIS_PASSWORD"
    if "timed out" in text.lower():
        return (f"{where} 连接超时：检查网络与防火墙，"
                f"或调大 failure_store.redis.socket_timeout（当前 {cfg.socket_timeout}s）")
    return f"{where} 连接失败：{text}"
