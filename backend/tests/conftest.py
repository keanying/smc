# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

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
# 登录默认关掉：老用例直接调接口、起真实服务点页面，不登录。登录本身在 test_auth.py 里单独测
os.environ.setdefault("SMC_AUTH_ENABLED", "false")


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _current_event_loop(event_loop):
    """每条用例开始前把会话循环设回"当前循环"。

    不少同步用例里直接 `asyncio.run(...)`——它跑完会把当前事件循环置空，
    排在它后面的异步用例和 `asyncio.get_event_loop()` 就会报
    「There is no current event loop」，跟用例本身对不对没关系。
    """
    asyncio.set_event_loop(event_loop)
    yield


@pytest.fixture()
def config():
    from app.core.config import load_config
    return load_config(use_cache=False)


@pytest.fixture()
def redis_client(config):
    from app.core.redis_client import RedisClient
    return RedisClient(config)
