import asyncio
import os
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 测试连接信息从 backend/.env.test 读，不依赖 config.yaml 里写死的明文密码
_env_file = BACKEND_ROOT / ".env.test"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _value = _line.split("=", 1)
        os.environ.setdefault(_key.strip(), _value.strip())

os.environ.setdefault("SMC_REDIS_ENABLED", "false")  # 测试默认走内存退化实现


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def config():
    from app.core.config import load_config
    return load_config(use_cache=False)


@pytest.fixture()
def redis_client(config):
    from app.core.redis_client import RedisClient
    return RedisClient(config)
