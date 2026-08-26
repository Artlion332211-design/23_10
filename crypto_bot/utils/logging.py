"""Structured logging setup.

Produces four rotating log files (`app.log`, `trades.log`, `errors.log`,
`signals.log`) plus a human-readable console stream. File output is JSON
(one object per line) so it can be grepped/ingested later for strategy
analytics; the console stays plain text for interactive use.

Any secret registered via :func:`register_secret` (API keys, bot tokens) is
scrubbed from every log record before it is written anywhere, regardless of
which logger or handler emitted it.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

_REDACT_PLACEHOLDER = "***REDACTED***"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 10


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets: list[str] = [s for s in (secrets or []) if s]

    def add_secret(self, value: str | None) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            msg = record.getMessage()
            for secret in self._secrets:
                if secret in msg:
                    msg = msg.replace(secret, _REDACT_PLACEHOLDER)
            record.msg = msg
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


SECRET_FILTER = SecretRedactionFilter()


def register_secret(value: str | None) -> None:
    """Register a runtime secret so it is redacted from all future log lines."""
    SECRET_FILTER.add_secret(value)


def _file_handler(log_path: Path, filename: str, min_level: int) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        log_path / filename, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(min_level)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SECRET_FILTER)
    return handler


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    console.addFilter(SECRET_FILTER)
    root.addHandler(console)

    root.addHandler(_file_handler(log_path, "app.log", logging.INFO))
    root.addHandler(_file_handler(log_path, "errors.log", logging.WARNING))

    trades_logger = logging.getLogger("trades")
    trades_logger.setLevel(logging.INFO)
    trades_logger.propagate = True
    trades_logger.handlers.clear()
    trades_logger.addHandler(_file_handler(log_path, "trades.log", logging.INFO))

    signals_logger = logging.getLogger("signals")
    signals_logger.setLevel(logging.INFO)
    signals_logger.propagate = True
    signals_logger.handlers.clear()
    signals_logger.addHandler(_file_handler(log_path, "signals.log", logging.INFO))

    # Quiet down noisy third-party loggers.
    for noisy in ("binance", "telegram", "httpx", "urllib3", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_trade_logger() -> logging.Logger:
    return logging.getLogger("trades")


def get_signal_logger() -> logging.Logger:
    return logging.getLogger("signals")
