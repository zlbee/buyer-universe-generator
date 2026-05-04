"""Shared logging setup for API and CLI entrypoints."""

from __future__ import annotations

import logging


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(log_level: str | int = "INFO") -> None:
    """Configure application logs once so module loggers reach the console."""

    level = _coerce_log_level(log_level)
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(console_handler)

    for handler in root_logger.handlers:
        # Existing handlers can come from Uvicorn or pytest; lower their threshold only when needed.
        if handler.level > level:
            handler.setLevel(level)

    logging.getLogger("src").setLevel(level)


def _coerce_log_level(log_level: str | int) -> int:
    if isinstance(log_level, int):
        return log_level
    normalized = log_level.strip().upper()
    resolved = logging.getLevelName(normalized)
    if isinstance(resolved, int):
        return resolved
    raise ValueError(f"Unknown log level: {log_level}")
