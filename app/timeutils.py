"""Timezone-aware time handling.

Every timestamp in PromoFlow is UTC and says so.  The server's local time is
never used, so the same code requests the same report window on an EC2 box in
UTC, a laptop in IST, and a container with no ``/etc/localtime`` at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .exceptions import ConfigurationError

UTC = timezone.utc

__all__ = [
    "UTC",
    "ReportWindow",
    "isoformat_z",
    "now_utc",
    "resolve_window",
    "timestamp_label",
]


def now_utc() -> datetime:
    """Current instant, in UTC."""
    return datetime.now(tz=UTC)


def isoformat_z(value: datetime) -> str:
    """ISO 8601 in UTC with a ``Z`` suffix and whole seconds, as SP-API expects."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp_label(reference: datetime | None = None) -> str:
    """The ``<UTC timestamp>`` prefix of every file PromoFlow writes."""
    return (reference or now_utc()).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class ReportWindow:
    """The range of promotion *start* dates a report covers."""

    start: datetime
    end: datetime

    def report_options(self) -> dict[str, str]:
        """``reportOptions`` for ``GET_PROMOTION_PERFORMANCE_REPORT``."""
        return {
            "promotionStartDateFrom": isoformat_z(self.start),
            "promotionStartDateTo": isoformat_z(self.end),
        }

    def __str__(self) -> str:
        return f"{isoformat_z(self.start)} .. {isoformat_z(self.end)}"


def resolve_window(
    lookback_days: int,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    reference: datetime | None = None,
) -> ReportWindow:
    """Return the promotion-start window to request.

    By default the window ends now and starts ``lookback_days`` earlier.
    ``date_from`` / ``date_to`` override either end for backfills; they are
    whole UTC days, so ``date_to`` includes everything up to 23:59:59.

    Raises:
        ConfigurationError: If the window would end before it starts.
    """
    now = (reference or now_utc()).astimezone(UTC).replace(microsecond=0)
    end = datetime.combine(date_to, time(23, 59, 59), tzinfo=UTC) if date_to else now
    start = (
        datetime.combine(date_from, time.min, tzinfo=UTC)
        if date_from
        else end - timedelta(days=lookback_days)
    )
    if start > end:
        raise ConfigurationError(
            f"Report window starts after it ends ({isoformat_z(start)} > {isoformat_z(end)})"
        )
    return ReportWindow(start=start, end=end)
