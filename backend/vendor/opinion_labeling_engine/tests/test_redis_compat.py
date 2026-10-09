"""Redis 兼容性单测。

线上踩到的：Windows 上的 Redis 是 3.0.504 老版本，新版 redis-py 建连时
发 RESP3 握手命令 ``HELLO``（Redis 6.0 才有），服务端直接回：

    unknown command 'HELLO'

我们一个 RESP3 特性都没用（LPUSH / BRPOPLPUSH / HSET / ZADD 而已），
所以显式退回 RESP2，并关掉 ``CLIENT SETINFO`` 客户端信息上报（Redis 7.2 才有）。

参数要按已安装 redis-py 的签名过滤：硬写 ``protocol=2`` 在 redis-py 4.x 上会 TypeError。
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace

import pytest

from opinion_labeling_engine.config import RedisConfig, load_config
from opinion_labeling_engine.redis_compat import create_redis_client, explain_redis_error

RCFG = RedisConfig(host="127.0.0.1", port=6378, db=1, password="",
                   key_prefix="ole", socket_timeout=5)


# ===================================================================== 假 redis 包
def install_fake_redis(monkeypatch, *, params):
    """装一个假的 redis 模块，其 Redis.__init__ 只接受 ``params`` 里的参数。"""
    captured = {}

    def make_init(accepted):
        arg_list = ", ".join(f"{name}=None" for name in accepted)
        src = (f"def __init__(self, {arg_list}):\n"
               f"    self.kwargs = {{{', '.join(f'{n!r}: {n}' for n in accepted)}}}\n")
        ns: dict = {}
        exec(src, ns)                                   # noqa: S102 - 测试里动态造签名
        return ns["__init__"]

    Redis = type("Redis", (), {"__init__": make_init(params)})
    module = types.ModuleType("redis")
    module.Redis = Redis
    monkeypatch.setitem(sys.modules, "redis", module)
    return captured


# redis-py 8：lib_name/lib_version 已废弃，改用 driver_info
MODERN = ["host", "port", "db", "password", "socket_timeout", "socket_connect_timeout",
          "decode_responses", "health_check_interval", "protocol", "driver_info",
          "lib_name", "lib_version", "client_name"]
# redis-py 5~7：有 protocol 和 lib_name，没有 driver_info
MID = ["host", "port", "db", "password", "socket_timeout", "socket_connect_timeout",
       "decode_responses", "health_check_interval", "protocol", "lib_name", "lib_version"]
# redis-py 4.x：两者都没有
LEGACY = ["host", "port", "db", "password", "socket_timeout", "socket_connect_timeout",
          "decode_responses", "health_check_interval"]


# ===================================================================== 构造
def test_modern_redis_py_uses_driver_info(monkeypatch):
    """redis-py 8：RESP2 + driver_info=None（传 lib_name 会触发废弃告警）。"""
    install_fake_redis(monkeypatch, params=MODERN)
    client = create_redis_client(RCFG)

    assert client.kwargs["protocol"] == 2, "必须退回 RESP2，否则老服务端会拒 HELLO"
    assert client.kwargs["driver_info"] is None, "关掉 CLIENT SETINFO 上报"
    assert client.kwargs["lib_name"] is None or "lib_name" not in client.kwargs


def test_mid_version_redis_py_uses_lib_name(monkeypatch):
    """redis-py 5~7：没有 driver_info，得用 lib_name/lib_version。"""
    install_fake_redis(monkeypatch, params=MID)
    client = create_redis_client(RCFG)
    assert client.kwargs["protocol"] == 2
    assert client.kwargs["lib_name"] is None
    assert client.kwargs["lib_version"] is None


def test_legacy_redis_py_does_not_get_unknown_kwargs(monkeypatch):
    """redis-py 4.x 没有 protocol 参数，硬传会 TypeError。"""
    install_fake_redis(monkeypatch, params=LEGACY)
    client = create_redis_client(RCFG)          # 不该抛
    assert "protocol" not in client.kwargs
    assert "lib_name" not in client.kwargs
    assert "driver_info" not in client.kwargs


def test_resp2_alone_is_not_enough(monkeypatch):
    """实测 redis-py 8 在 RESP2 下仍然会发 CLIENT SETINFO，
    所以关闭客户端信息上报是必须的第二步，不能只设 protocol。"""
    install_fake_redis(monkeypatch, params=MODERN)
    client = create_redis_client(RCFG)
    assert "driver_info" in client.kwargs


def test_connection_settings_are_passed_through(monkeypatch):
    install_fake_redis(monkeypatch, params=MODERN)
    client = create_redis_client(RCFG)
    assert client.kwargs["host"] == "127.0.0.1"
    assert client.kwargs["port"] == 6378
    assert client.kwargs["db"] == 1
    assert client.kwargs["decode_responses"] is True
    assert client.kwargs["socket_timeout"] == 5
    assert client.kwargs["socket_connect_timeout"] == 5


def test_empty_password_becomes_none(monkeypatch):
    """空字符串密码会被老 redis-py 当成"要鉴权"，必须转成 None。"""
    install_fake_redis(monkeypatch, params=MODERN)
    assert create_redis_client(RCFG).kwargs["password"] is None
    assert create_redis_client(replace(RCFG, password="pw")).kwargs["password"] == "pw"


def test_overrides_win(monkeypatch):
    install_fake_redis(monkeypatch, params=MODERN)
    client = create_redis_client(RCFG, decode_responses=False, db=9)
    assert client.kwargs["decode_responses"] is False
    assert client.kwargs["db"] == 9


# ===================================================================== 报错翻译
def test_hello_error_names_the_real_cause():
    msg = explain_redis_error(Exception("unknown command 'HELLO'"), RCFG)
    assert "版本过老" in msg
    assert "redis==5.0" in msg or "Redis 6+" in msg
    assert "127.0.0.1:6378/1" in msg


def test_client_setinfo_error_is_recognised():
    msg = explain_redis_error(Exception("unknown command 'CLIENT SETINFO'"), RCFG)
    assert "版本过老" in msg


def test_connection_refused_tells_you_what_to_check():
    msg = explain_redis_error(ConnectionError("Connection refused"), RCFG)
    assert "redis-cli" in msg
    assert "6378" in msg


def test_auth_error():
    assert "鉴权失败" in explain_redis_error(Exception("NOAUTH Authentication required"), RCFG)


def test_timeout_error_mentions_the_current_setting():
    msg = explain_redis_error(Exception("Connection timed out"), RCFG)
    assert "socket_timeout" in msg
    assert "5" in msg


def test_unknown_error_is_passed_through_verbatim():
    msg = explain_redis_error(Exception("某种没见过的错"), RCFG)
    assert "某种没见过的错" in msg
    assert "127.0.0.1:6378/1" in msg


# ===================================================================== 接线
def test_failure_store_uses_the_compat_builder(monkeypatch):
    """失败池必须走共用构造器，不能自己 new 一个不带兼容参数的客户端。"""
    install_fake_redis(monkeypatch, params=MODERN)
    from opinion_labeling_engine.storage.failure_store import RedisFailureStore

    cfg = replace(load_config(require_db=False).failure_store, redis=RCFG)
    store = RedisFailureStore(cfg)
    assert store._r.kwargs["protocol"] == 2


def test_queue_uses_the_compat_builder(monkeypatch):
    install_fake_redis(monkeypatch, params=MODERN)
    from opinion_labeling_engine.queues.redis_queue import RedisQueue

    cfg = replace(load_config(require_db=False).queue, redis=RCFG)
    queue = RedisQueue(cfg)
    assert queue._r.kwargs["protocol"] == 2


def test_startup_error_carries_the_explanation(monkeypatch):
    """连不上时抛出的异常要带可操作的说明，而不是干巴巴一句连接失败。"""
    from opinion_labeling_engine.storage import failure_store as fs

    class Boom:
        def ping(self):
            raise Exception("unknown command 'HELLO'")

        def close(self):
            pass

    monkeypatch.setattr(fs, "create_redis_client", lambda *_a, **_kw: Boom())
    cfg = replace(load_config(require_db=False).failure_store,
                  backend="redis", redis=RCFG)
    with pytest.raises(RuntimeError) as exc:
        fs.create_failure_store(cfg)
    assert "版本过老" in str(exc.value)
