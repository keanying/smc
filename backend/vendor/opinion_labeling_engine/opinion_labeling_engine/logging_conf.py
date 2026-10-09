"""日志初始化。全局只允许调用一次 :func:`setup_logging`。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingConfig

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(threadName)s | %(message)s"


def setup_logging(cfg: LoggingConfig, *, force: bool = False) -> None:
    """按配置装配 root logger（控制台 + 可选滚动文件）。

    重复调用会被忽略，除非 ``force=True``（单测里用）。
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(cfg.level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if cfg.dir:
        log_dir = Path(cfg.dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / cfg.file,
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
