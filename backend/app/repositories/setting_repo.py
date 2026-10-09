"""系统设置：页面上改的配置存 DB，优先级高于 config.yaml。

生效顺序：DEFAULT_CONFIG < config.yaml < 环境变量 < 本表。
放在最后是因为需求明确要求"在系统设置里设置代理"——
页面上改完必须立刻生效，不该被文件里的旧值盖回去。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..core.config import Config
from ..core.db import Database
from ..core.logging import get_logger

logger = get_logger(__name__)

# 允许在页面上覆盖的配置段白名单，避免误改 mysql 连接把服务改挂
OVERRIDABLE_SECTIONS = (
    "proxy", "crawl", "browser", "export", "scheduler", "platforms",
    # 标注：「边采边标」开关和模型配置都要能在页面上改
    "labeling",
    # 通知：飞书机器人地址、远程登录信息、开关
    "notify",
)


class SettingRepository:
    def __init__(self, db: Database):
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self.db.fetch_one(
            "SELECT setting_value FROM `src_opinion_sys_setting` WHERE setting_key = %s", [key]
        )
        if not row or row["setting_value"] is None:
            return default
        try:
            return json.loads(row["setting_value"])
        except (ValueError, TypeError):
            return row["setting_value"]

    async def set(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO `src_opinion_sys_setting` (setting_key, setting_value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)",
            [key, json.dumps(value, ensure_ascii=False)],
        )

    async def all(self) -> Dict[str, Any]:
        rows = await self.db.fetch_all("SELECT setting_key, setting_value FROM `src_opinion_sys_setting`")
        result: Dict[str, Any] = {}
        for row in rows:
            try:
                result[row["setting_key"]] = json.loads(row["setting_value"] or "null")
            except (ValueError, TypeError):
                result[row["setting_key"]] = row["setting_value"]
        return result

    async def apply_overrides(self, config: Config) -> Config:
        """把 DB 里的设置合并进内存配置对象。服务启动和每次改设置后调用。"""
        stored = await self.all()
        applied = []
        for section in OVERRIDABLE_SECTIONS:
            override = stored.get(section)
            if not isinstance(override, dict):
                continue
            target = config.get(section)
            if isinstance(target, dict):
                _deep_update(target, override)
                applied.append(section)
        if applied:
            logger.info("已应用系统设置覆盖：%s", "、".join(applied))
        return config

    async def save_section(self, section: str, values: Dict[str, Any], config: Config) -> None:
        if section not in OVERRIDABLE_SECTIONS:
            raise ValueError(
                f"不允许在页面上修改配置段 {section}；可改的有：{'、'.join(OVERRIDABLE_SECTIONS)}"
            )
        current = await self.get(section, {}) or {}
        if not isinstance(current, dict):
            current = {}
        _deep_update(current, values)
        await self.set(section, current)
        target = config.get(section)
        if isinstance(target, dict):
            _deep_update(target, values)


def _deep_update(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
