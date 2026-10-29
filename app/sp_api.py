"""Selling Partner API client: LWA tokens and the Reports API (2021-06-30).

    createReport -> getReport (poll) -> getReportDocument -> download

Calls are authorised with an LWA access token in ``x-amz-access-token``.  SP-API
stopped requiring AWS Signature Version 4 in October 2023, so no IAM user,
access key or role ARN is involved.

Each HTTP call is retried on its own, so a dropped connection while polling
never costs the report itself: the run keeps polling the same report ID instead
of requesting a new one.
"""

from __future__ import annotations

import gzip
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import requests

from . import __version__
from .config import Settings, SpApiConfig
from .exceptions import (
    AuthenticationError,
    DownloadError,
    EmptyReportError,
    PromoFlowError,
    ReportProcessingError,
    ReportRequestError,
)
from .logging_config import get_logger
from .retry import retry_call
from .timeutils import ReportWindow, timestamp_label

logger = get_logger("sp_api")

T = TypeVar("T")

REPORT_TYPE = "GET_PROMOTION_PERFORMANCE_REPORT"
REPORTS_PATH = "/reports/2021-06-30"
USER_AGENT = f"PromoFlow/{__version__} (Language=Python)"

#: HTTP statuses a second attempt can plausibly get past.
TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

#: LWA tokens live for an hour; refresh this long before Amazon would refuse one.
_TOKEN_EXPIRY_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class ReportStatus:
    """One ``getReport`` response."""

    report_id: str
    processing_status: str
    document_id: str | None


@dataclass(frozen=True)
class ReportDocument:
    """One ``getReportDocument`` response.  ``url`` is pre-signed and short-lived."""

    document_id: str
    url: str
    compression: str | None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int

    @property
    def size_kb(self) -> float:
        return self.size_bytes / 1024


def _strip_query(url: str) -> str:
    """A loggable URL: pre-signed query strings carry live credentials."""
    return url.split("?", 1)[0]


def _describe_errors(response: requests.Response) -> str:
    """Amazon's ``{"errors": [{"code", "message"}]}`` body, as one line."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "(empty body)"
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        return "; ".join(
            f"{e.get('code', '?')}: {e.get('message', '')}".strip()
            for e in errors
            if isinstance(e, dict)
        )
    if isinstance(body, dict) and "error" in body:
        # LWA's OAuth shape.
        return f"{body.get('error')}: {body.get('error_description', '')}".strip()
    return str(body)[:300]


class LwaTokenProvider:
    """Exchanges the refresh token for access tokens, caching each until near expiry."""

    def __init__(
        self,
        config: SpApiConfig,
        http: requests.Session,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token is None or self._clock() >= self._expires_at:
            self._refresh()
        assert self._token is not None
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def _refresh(self) -> None:
        try:
            response = self._http.request(
                "POST",
                self._config.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._config.refresh_token,
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                },
                headers={"user-agent": USER_AGENT},
                timeout=self._config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ReportRequestError(
                f"LWA access-token request failed: {exc}", transient=True
            ) from exc

        if response.status_code == 200:
            try:
                body = response.json()
                token = body["access_token"]
                expires_in = int(body.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError) as exc:
                raise ReportRequestError(
                    "LWA returned 200 without a usable access_token", transient=True
                ) from exc
            self._token = token
            self._expires_at = self._clock() + max(
                expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS, 0
            )
            logger.debug("Obtained LWA access token (valid %ds)", expires_in)
            return

        detail = _describe_errors(response)
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            raise ReportRequestError(
                f"LWA access-token request returned HTTP {response.status_code}: {detail}",
                transient=True,
            )
        raise AuthenticationError(
            f"LWA refused the credentials (HTTP {response.status_code}): {detail}. "
            "Check SP_API_CLIENT_ID, SP_API_CLIENT_SECRET and SP_API_REFRESH_TOKEN."
        )


class ReportsClient:
    """Everything PromoFlow needs from the SP-API Reports API."""

    def __init__(
        self,
        settings: Settings,
        *,
        http: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            http: Overrides the HTTP session.  Used by tests to script Amazon's
                responses; production always leaves this None.
            sleep, clock: Override time, so tests never actually wait.
        """
        self._settings = settings
        self._config = settings.sp_api
        self._http = http or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._tokens = LwaTokenProvider(self._config, self._http, clock=clock)

    # -- public API --------------------------------------------------------

    def create_report(self, window: ReportWindow) -> str:
        """Ask Amazon to build the report.  Returns the report ID."""
        body = {
            "reportType": REPORT_TYPE,
            "marketplaceIds": list(self._settings.marketplace_ids),
            "reportOptions": window.report_options(),
        }
        payload = self._call(
            "createReport",
            lambda: self._api("POST", f"{REPORTS_PATH}/reports", json=body, expected=(200, 202)),
        )
        report_id = payload.get("reportId")
        if not report_id:
            raise ReportRequestError(f"createReport returned no reportId: {payload}")
        logger.info(
            "Requested %s for %s (window %s). Report ID: %s",
            REPORT_TYPE,
            ", ".join(self._settings.marketplace_ids),
            window,
            report_id,
        )
        return str(report_id)

    def get_report(self, report_id: str) -> ReportStatus:
        payload = self._call(
            "getReport", lambda: self._api("GET", f"{REPORTS_PATH}/reports/{report_id}")
        )
        status = payload.get("processingStatus")
        if not status:
            raise ReportRequestError(f"getReport returned no processingStatus: {payload}")
        document_id = payload.get("reportDocumentId")
        return ReportStatus(
            report_id=report_id,
            processing_status=str(status),
            document_id=str(document_id) if document_id else None,
        )

    def wait_for_report(self, report_id: str) -> ReportStatus:
        """Poll until the report is ``DONE``.

        Raises:
            EmptyReportError: ``CANCELLED`` -- which Amazon does by itself when
                there is no data for the window.
            ReportProcessingError: ``FATAL``, or still not done after
                ``REPORT_POLL_TIMEOUT_SECONDS``.
        """
        interval = self._settings.poll_interval_seconds
        deadline = self._clock() + self._settings.poll_timeout_seconds
        while True:
            status = self.get_report(report_id)
            logger.info("Report %s status: %s", report_id, status.processing_status)

            if status.processing_status == "DONE":
                if not status.document_id:
                    raise ReportProcessingError(
                        f"Report {report_id} is DONE but has no reportDocumentId"
                    )
                return status
            if status.processing_status == "CANCELLED":
                raise EmptyReportError(
                    f"Amazon cancelled report {report_id}. It does this when there is no "
                    "promotion data for the requested window."
                )
            if status.processing_status == "FATAL":
                detail = self._fatal_detail(status)
                raise ReportProcessingError(
                    f"Amazon could not build report {report_id} (FATAL)"
                    + (f": {detail}" if detail else "")
                )

            if self._clock() + interval > deadline:
                raise ReportProcessingError(
                    f"Report {report_id} is still {status.processing_status} after "
                    f"{self._settings.poll_timeout_seconds:.0f}s. Resume it later with "
                    f"--report-id {report_id} instead of requesting a new one."
                )
            self._sleep(interval)

    def get_report_document(self, document_id: str) -> ReportDocument:
        return self._call("getReportDocument", lambda: self._get_report_document(document_id))

    def download_document(self, document_id: str, destination_dir: Path) -> DownloadResult:
        """Download and decompress the report into ``destination_dir``.

        Each attempt fetches a fresh pre-signed URL, so a retry after the URL
        expired still succeeds.  The file is written under a ``.part`` name and
        renamed once complete, so a killed run never leaves a truncated report
        that looks finished.
        """

        def attempt() -> bytes:
            return self._fetch(self._get_report_document(document_id))

        raw = self._call("Report document download", attempt)

        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / f"{timestamp_label()}_promotion_report.json"
        part = path.with_name(path.name + ".part")
        part.write_bytes(raw)
        part.replace(path)
        logger.info("Report saved to %s (%.1f KB)", path, len(raw) / 1024)
        return DownloadResult(path=path, size_bytes=len(raw))

    def close(self) -> None:
        self._http.close()

    # -- internals ---------------------------------------------------------

    def _call(self, description: str, operation: Callable[[], T]) -> T:
        return retry_call(
            operation,
            description=description,
            attempts=self._settings.max_retries,
            base_delay=self._settings.retry_backoff_seconds,
            max_delay=self._settings.retry_backoff_max_seconds,
            sleep=self._sleep,
        )

    def _api(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        """One authorised SP-API call, with its failure classified."""
        headers = {
            "x-amz-access-token": self._tokens.token(),
            "user-agent": USER_AGENT,
            "accept": "application/json",
        }
        try:
            response = self._http.request(
                method,
                f"{self._config.endpoint}{path}",
                json=json,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ReportRequestError(f"{method} {path} failed: {exc}", transient=True) from exc

        if response.status_code in expected:
            try:
                payload = response.json()
            except ValueError as exc:
                raise ReportRequestError(
                    f"{method} {path} returned a body that is not JSON", transient=True
                ) from exc
            if not isinstance(payload, dict):
                raise ReportRequestError(f"{method} {path} returned unexpected JSON: {payload!r}")
            return payload

        detail = _describe_errors(response)
        if response.status_code in (401, 403):
            self._tokens.invalidate()
            raise AuthenticationError(
                f"SP-API denied {method} {path} (HTTP {response.status_code}): {detail}. "
                "Check that the app is authorised for this seller and has the role "
                "that grants access to promotion reports."
            )
        raise ReportRequestError(
            f"{method} {path} returned HTTP {response.status_code}: {detail}",
            transient=response.status_code in TRANSIENT_HTTP_STATUSES,
        )

    def _get_report_document(self, document_id: str) -> ReportDocument:
        payload = self._api("GET", f"{REPORTS_PATH}/documents/{document_id}")
        url = payload.get("url")
        if not url:
            raise ReportRequestError(f"getReportDocument returned no url for {document_id}")
        compression = payload.get("compressionAlgorithm")
        return ReportDocument(
            document_id=document_id,
            url=str(url),
            compression=str(compression) if compression else None,
        )

    def _fetch(self, document: ReportDocument) -> bytes:
        """Download a document's bytes, decompressed."""
        logger.info(
            "Downloading report document %s (compression=%s) from %s",
            document.document_id,
            document.compression or "none",
            _strip_query(document.url),
        )
        try:
            # No SP-API headers: this is a pre-signed S3 URL, not an API call.
            response = self._http.request(
                "GET", document.url, timeout=self._config.timeout_seconds
            )
        except requests.RequestException as exc:
            raise DownloadError(f"Report document download failed: {exc}") from exc

        if response.status_code != 200:
            # A 403 is usually an expired pre-signed URL; the next attempt
            # fetches a new one, so it is retried too.
            raise DownloadError(
                f"Report document download returned HTTP {response.status_code}",
                transient=response.status_code in TRANSIENT_HTTP_STATUSES
                or response.status_code == 403,
            )

        content = response.content
        if document.compression is None:
            return content
        if document.compression.upper() != "GZIP":
            raise DownloadError(
                f"Unsupported compressionAlgorithm {document.compression!r}", transient=False
            )
        try:
            return gzip.decompress(content)
        except (OSError, EOFError, zlib.error) as exc:
            raise DownloadError(f"Report document is not valid GZIP: {exc}") from exc

    def _fatal_detail(self, status: ReportStatus) -> str:
        """Amazon's explanation of a FATAL report, when it left one."""
        if not status.document_id:
            return ""
        try:
            raw = self._fetch(self._get_report_document(status.document_id))
        except PromoFlowError as exc:
            logger.debug("Could not fetch the FATAL report's error document: %s", exc)
            return ""
        return raw.decode("utf-8", errors="replace").strip()[:1000]
