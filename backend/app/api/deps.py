"""应用级依赖容器：所有单例集中在 AppState 上，路由通过 get_state() 拿。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

from ..browser.manager import BrowserSessionManager
from ..core.config import Config
from ..core.db import Database
from ..core.redis_client import RedisClient
from ..proxy.manager import ProxyManager
from ..repositories.account_repo import AccountRepository
from ..repositories.data_repo import DataRepository
from ..repositories.scenic_repo import ScenicRepository
from ..repositories.setting_repo import SettingRepository
from ..repositories.task_repo import TaskRepository
from ..scheduler.runner import TaskRunner
from ..scheduler.scheduler import TaskScheduler
from ..utils.exporter import CsvExporter


@dataclass
class AppState:
    config: Config
    db: Database
    redis: RedisClient
    proxy_manager: ProxyManager
    browser_manager: BrowserSessionManager
    scenics: ScenicRepository
    accounts: AccountRepository
    tasks: TaskRepository
    data: DataRepository
    settings: SettingRepository
    runner: TaskRunner
    scheduler: TaskScheduler
    exporter: CsvExporter


_state: Optional[AppState] = None


def set_state(state: AppState) -> None:
    global _state
    _state = state


def get_state() -> AppState:
    if _state is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化完成，请稍后重试")
    return _state
