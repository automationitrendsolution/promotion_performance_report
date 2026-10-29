"""Exception hierarchy for PromoFlow.

Every failure mode the pipeline can hit has its own type so that callers can
decide independently whether to retry, alert, or abort.  ``transient`` marks the
errors that are safe to retry; a refused credential is never transient and is
never retried automatically.
"""

from __future__ import annotations


class PromoFlowError(Exception):
    """Base class for every PromoFlow failure."""

    #: Whether retrying the same operation could plausibly succeed.
    transient: bool = False
    #: Process exit code used when this error reaches the top level.
    exit_code: int = 1

    def __init__(self, message: str = "", *, transient: bool | None = None) -> None:
        super().__init__(message)
        if transient is not None:
            self.transient = transient


class ConfigurationError(PromoFlowError):
    """Missing or invalid environment configuration."""

    exit_code = 2


class AuthenticationError(PromoFlowError):
    """Amazon refused the LWA credentials, or SP-API denied the call.

    Never retried: a revoked refresh token or an app that is not authorised for
    the report stays that way until a person fixes it in Seller Central.
    """

    exit_code = 3


class ReportRequestError(PromoFlowError):
    """An SP-API call (createReport, getReport, getReportDocument) failed.

    ``transient`` is set per instance by :mod:`app.sp_api`: throttling (429),
    a 5xx or a dropped connection is retried, a rejected request is not.
    """

    exit_code = 4


class ReportProcessingError(PromoFlowError):
    """Amazon could not build the report (``FATAL``) or did not finish in time."""

    exit_code = 5


class DownloadError(PromoFlowError):
    """The report document could not be downloaded or decompressed."""

    transient = True
    exit_code = 6


class ReportParseError(PromoFlowError):
    """The downloaded document is not a usable promotion performance report."""

    exit_code = 7


class DataTransformationError(PromoFlowError):
    """Report rows could not be mapped onto the target table."""

    exit_code = 8


class DatabaseError(PromoFlowError):
    """MySQL connectivity or transaction failure.

    ``transient`` is set per instance by :mod:`app.database`: a dropped
    connection or a lost lock race is retried, a schema or data error is not.
    """

    exit_code = 9


class EmptyReportError(ReportParseError):
    """The report holds no promoted products for the requested window.

    A hard stop for the load, so that an empty report can never delete rows:
    nothing is deleted and nothing is inserted.
    """

    exit_code = 10


class LockError(PromoFlowError):
    """Another PromoFlow run already holds the execution lock."""

    exit_code = 11
