"""Logging setup.

Two sinks: a human-readable console stream and a rotating file under
``LOG_DIR``.  A redaction filter is installed on both so that a stray
``repr()`` of a DSN, an LWA token or a pre-signed download URL can never reach
disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

LOGGER_NAME = "promotion-performance-us"

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(process)d] %(module)s:%(lineno)d | %(message)s"

#: Patterns scrubbed from every log record.  Each must keep group 1 as the
#: literal prefix so the replacement stays readable.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"), r"\1***\2"),
    (
        re.compile(
            r"((?:password|passwd|pwd|secret|token|cookie|api[_-]?key|authorization)"
            r"\s*[=:]\s*)(?!\*)[^\s,;)'\"&]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    # LWA refresh ("Atzr|") and access ("Atza|") tokens, wherever they appear.
    (re.compile(r"(Atz[ar]\|)[^\s,;'\"&]+"), r"\1***"),
    # LWA client secrets.
    (re.compile(r"(amzn1\.oa2-cs\.v1\.)[0-9A-Za-z]+"), r"\1***"),
    # OAuth and pre-signed S3 query parameters on a report document URL.
    (
        re.compile(
            r"([?&](?:access_token|refresh_token|client_secret|code"
            r"|X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=)"
            r"[^&\s'\"]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
)


class RedactingFilter(logging.Filter):
    """Masks credential-shaped substrings in messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_redact(a) for a in record.args)
        return True


def _redact(value: object) -> object:
    if not isinstance(value, str):
        return value
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def configure_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    retention_days: int = 7,
    console: bool = True,
) -> logging.Logger:
    """Configure and return the ``promotion-performance-us`` logger.

    Safe to call more than once; existing handlers are replaced so repeated
    calls (tests, re-entry) do not duplicate output.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redactor = RedactingFilter()

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        stream.addFilter(redactor)
        logger.addHandler(stream)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "cron.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    file_handler.addFilter(redactor)
    logger.addHandler(file_handler)

    # Third-party libraries are noisy at DEBUG and can echo connection strings
    # or request URLs.
    for noisy in ("sqlalchemy.engine", "mysql.connector", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the ``promotion-performance-us`` logger."""
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")
