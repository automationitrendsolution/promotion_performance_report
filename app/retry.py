"""Retry with exponential backoff, restricted to transient failures.

Configuration errors and refused credentials are deliberately excluded:
repeating them cannot succeed, and hammering LWA with a revoked token is
hostile to the service.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from .exceptions import PromoFlowError
from .logging_config import get_logger

T = TypeVar("T")
logger = get_logger("retry")


def is_transient(error: BaseException) -> bool:
    """Whether ``error`` is worth retrying."""
    if isinstance(error, PromoFlowError):
        return error.transient
    return isinstance(error, (ConnectionError, TimeoutError, OSError))


def compute_delay(attempt: int, base: float, maximum: float) -> float:
    """Exponential backoff with full jitter, capped at ``maximum``."""
    if base <= 0:
        return 0.0
    exponential = min(base * (2 ** (attempt - 1)), maximum if maximum > 0 else base)
    return random.uniform(0, exponential)


def retry_call(
    operation: Callable[[], T],
    *,
    description: str,
    attempts: int,
    base_delay: float,
    max_delay: float,
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``, retrying transient failures up to ``attempts`` times.

    Raises:
        The last exception raised by ``operation`` once attempts are exhausted,
        or immediately if the failure is not transient.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error: BaseException
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            last_error = exc
            if not is_transient(exc):
                logger.error(
                    "%s failed with a non-retryable error: %s: %s",
                    description,
                    type(exc).__name__,
                    exc,
                )
                raise
            if attempt >= attempts:
                logger.error(
                    "%s failed after %d attempt(s): %s: %s",
                    description,
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                raise
            delay = compute_delay(attempt, base_delay, max_delay)
            logger.warning(
                "%s failed on attempt %d/%d (%s: %s); retrying in %.1fs",
                description,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            if on_retry is not None:
                on_retry(attempt, exc)
            if delay:
                sleep(delay)
    raise last_error  # pragma: no cover - loop always returns or raises
