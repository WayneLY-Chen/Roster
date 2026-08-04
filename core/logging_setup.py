"""Loguru configuration: one log file per category, plus a catch-all error log.

Modules never import ``loguru.logger`` directly. They call
:func:`get_logger` with a :class:`~core.constants.LogCategory`, which returns a
bound logger routed to that category's file.

    from core.logging_setup import get_logger
    from core.constants import LogCategory

    log = get_logger(LogCategory.CRAWL)
    log.info("fetched {}", url)
"""

from __future__ import annotations

import sys
from typing import Any

from loguru import logger

from core.constants import LogCategory
from core.config import AppConfig, get_config

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{extra[category]: <8}</cyan> | <level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[category]: <8} | "
    "{name}:{function}:{line} - {message}"
)

_configured = False


def _category_filter(category: LogCategory):
    """Route only records bound to ``category`` into that category's sink."""

    def _filter(record: dict[str, Any]) -> bool:
        return record["extra"].get("category") == category.value

    return _filter


def _error_filter(record: dict[str, Any]) -> bool:
    return record["level"].no >= logger.level("ERROR").no


def setup_logging(config: AppConfig | None = None, *, force: bool = False) -> None:
    """Install sinks. Idempotent unless ``force=True`` (used by tests)."""
    global _configured
    if _configured and not force:
        return

    config = config or get_config()
    log_dir = config.logging.resolved_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.configure(extra={"category": LogCategory.GUI.value})

    # sys.stderr 為 None 的情況真的存在：打包成不帶主控台的視窗程式時
    # （PyInstaller console=False），標準串流根本沒有被建立。把 None 交給
    # loguru 會直接 TypeError，讓程式在還沒開出視窗前就死掉——而且因為沒有
    # 主控台，使用者只會看到「點了沒反應」。
    if config.logging.console and sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=config.logging.level,
            format=_CONSOLE_FORMAT,
            colorize=True,
            backtrace=False,
            diagnose=False,
        )

    common = {
        "rotation": config.logging.rotation,
        "retention": config.logging.retention,
        "encoding": "utf-8",
        "format": _FILE_FORMAT,
        # 這裡曾經是 enqueue=True，註解寫「GUI 會在背景執行緒跑爬蟲」——那是
        # 誤解：enqueue 解決的是多「行程」寫同一個檔案，多執行緒 loguru 本來
        # 就自己上鎖，是安全的。代價則是每個輸出各配一條 writer 執行緒、用
        # multiprocessing 管線把每筆日誌序列化送過去；這個專案有八個日誌檔，
        # 就是八條管線與八條執行緒，全部都不需要。
        "enqueue": False,
    }

    for category in LogCategory:
        if category is LogCategory.ERROR:
            continue
        logger.add(
            log_dir / f"{category.value}.log",
            level=config.logging.level,
            filter=_category_filter(category),
            **common,
        )

    # Everything at ERROR or above lands here too, whatever its category.
    logger.add(
        log_dir / "error.log",
        level="ERROR",
        filter=_error_filter,
        backtrace=True,
        diagnose=False,
        **common,
    )

    _configured = True


def get_logger(category: LogCategory = LogCategory.GUI):
    """Return a logger bound to ``category``; configures sinks on first use."""
    setup_logging()
    return logger.bind(category=category.value)


def log_file_path(category: LogCategory, config: AppConfig | None = None):
    """Path of a category's log file (used by the GUI Logs page)."""
    config = config or get_config()
    return config.logging.resolved_dir / f"{category.value}.log"
