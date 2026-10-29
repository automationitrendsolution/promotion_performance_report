"""Housekeeping for files PromoFlow itself created.

Deletion is deliberately narrow: only inside PromoFlow's own data directories,
and only files whose names match the patterns this application writes.
Nothing else on the server is ever touched.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Settings
from .logging_config import get_logger
from .timeutils import now_utc

logger = get_logger("maintenance")

#: Names PromoFlow creates: "<UTC timestamp>_<label>.<ext>", plus the
#: ".part" a download leaves behind if it is killed mid-write.
_OWNED_NAME = re.compile(r"^\d{8}T\d{6}Z_.+\.(json|csv|part)$", re.IGNORECASE)


def cleanup_old_files(settings: Settings) -> int:
    """Delete PromoFlow-created files older than ``FILE_RETENTION_DAYS``.

    Returns the number of files removed.
    """
    cutoff = now_utc().timestamp() - settings.retention_days * 86_400

    removed = 0
    for directory in (settings.download_dir, settings.processed_dir, settings.failed_dir):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not _is_owned_file(path):
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
                logger.debug("Removed expired file %s", path)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", path, exc)

    if removed:
        logger.info(
            "Housekeeping removed %d file(s) older than %d day(s)",
            removed,
            settings.retention_days,
        )
    return removed


def _is_owned_file(path: Path) -> bool:
    """Whether PromoFlow created this file and may therefore delete it."""
    return path.is_file() and bool(_OWNED_NAME.match(path.name))
