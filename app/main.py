"""Pipeline orchestration and execution modes.

    SP-API createReport -> getReport (poll) -> getReportDocument -> JSON
        -> pandas (one row per promoted product) -> MySQL

Each stage raises its own exception type (see :mod:`app.exceptions`); this
module decides what that means for the run, what gets logged, and what the
process exits with.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar

from .config import Settings
from .data_transformer import TransformResult, transform
from .database import PromotionRepository, WriteResult, build_repository
from .exceptions import AuthenticationError, DatabaseError, EmptyReportError, PromoFlowError
from .logging_config import get_logger
from .maintenance import cleanup_old_files
from .report_processor import ReadResult, read_promotion_report
from .retry import retry_call
from .runlock import run_lock
from .schema import TableSpec, build_bootstrap_table
from .sp_api import DownloadResult, ReportsClient
from .timeutils import ReportWindow, now_utc, resolve_window, timestamp_label

logger = get_logger("pipeline")

T = TypeVar("T")


class RunMode(str, Enum):
    """What a single invocation is supposed to do."""

    FULL = "full"
    DOWNLOAD_ONLY = "download-only"
    PROCESS_FILE = "process-file"


@dataclass
class RunOptions:
    """Per-invocation switches, all settable from the CLI."""

    mode: RunMode = RunMode.FULL
    dry_run: bool = False
    process_file: Path | None = None
    #: Poll and download an already-requested report instead of creating one.
    report_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    use_lock: bool = True
    echo_sql: bool = False

    @property
    def writes_database(self) -> bool:
        """Whether this run is allowed to modify MySQL."""
        return not self.dry_run and self.mode in {RunMode.FULL, RunMode.PROCESS_FILE}

    @property
    def needs_amazon(self) -> bool:
        return self.mode in {RunMode.FULL, RunMode.DOWNLOAD_ONLY}

    @property
    def label(self) -> str:
        return f"{self.mode.value} (dry-run)" if self.dry_run else self.mode.value


@dataclass
class RunSummary:
    """Everything the run is expected to report on."""

    mode: str
    started_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    window: ReportWindow | None = None
    report_id: str | None = None
    document_id: str | None = None
    report_path: Path | None = None
    report_bytes: int = 0
    promotion_count: int = 0
    row_count: int = 0
    dropped_columns: list[str] = field(default_factory=list)
    null_columns: list[str] = field(default_factory=list)
    write_result: WriteResult | None = None
    error: str | None = None
    succeeded: bool = False

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or now_utc()
        return (end - self.started_at).total_seconds()

    def log(self) -> None:
        logger.info("=" * 72)
        logger.info("Run summary (%s)", self.mode)
        logger.info("  started (UTC)      : %s", self.started_at)
        logger.info("  finished (UTC)     : %s", self.finished_at or now_utc())
        logger.info("  duration           : %.1fs", self.duration_seconds)
        logger.info("  report window      : %s", self.window or "n/a")
        logger.info("  report id          : %s", self.report_id or "n/a")
        logger.info("  report file        : %s", self.report_path)
        logger.info(
            "  report size        : %s",
            f"{self.report_bytes / 1024:.1f} KB" if self.report_bytes else "n/a",
        )
        logger.info("  promotions         : %d", self.promotion_count)
        logger.info("  rows processed     : %d", self.row_count)
        logger.info(
            "  dropped fields     : %s",
            ", ".join(self.dropped_columns) if self.dropped_columns else "none",
        )
        logger.info(
            "  NULL columns       : %s",
            ", ".join(self.null_columns) if self.null_columns else "none",
        )
        if self.write_result:
            logger.info(
                "  %s: %d deleted, %d inserted",
                self.write_result.table,
                self.write_result.deleted,
                self.write_result.inserted,
            )
        logger.info("  status             : %s", "SUCCESS" if self.succeeded else "FAILED")
        if self.error:
            logger.info("  error              : %s", self.error)
        logger.info("=" * 72)


class PromotionPipeline:
    """Runs one PromoFlow execution end to end."""

    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        repository_factory: Callable[[], PromotionRepository] | None = None,
        client_factory: Callable[[], ReportsClient] | None = None,
    ) -> None:
        """
        Args:
            repository_factory: Overrides how the MySQL repository is built.
            client_factory: Overrides how the SP-API client is built.
                Both are used by tests to substitute local stand-ins;
                production always leaves them None.
        """
        self.settings = settings
        self.options = options
        self.summary = RunSummary(mode=options.label)
        # A caller that supplies its own repository owns its lifecycle, so the
        # pipeline must not dispose the engine out from under it.
        self._owns_repository = repository_factory is None
        self._repository_factory = repository_factory or (
            lambda: build_repository(settings, echo=options.echo_sql)
        )
        self._client_factory = client_factory or (lambda: ReportsClient(settings))

    # -- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        """Execute the configured mode, returning the summary either way."""
        self.settings.ensure_directories()
        self._log_start()
        try:
            with run_lock(self.settings.lock_file, enabled=self.options.use_lock):
                if self.options.mode is RunMode.DOWNLOAD_ONLY:
                    self._run_download_only()
                elif self.options.mode is RunMode.PROCESS_FILE:
                    self._run_process_file()
                else:
                    self._run_full()
            self.summary.succeeded = True
        except PromoFlowError as exc:
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.error("Run failed: %s", self.summary.error)
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced after summary
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Unexpected failure")
            raise
        finally:
            self.summary.finished_at = now_utc()
            self.summary.log()
        return self.summary

    def _log_start(self) -> None:
        logger.info("=" * 72)
        logger.info("PromoFlow starting | mode=%s", self.options.label)
        logger.info("  UTC              : %s", now_utc().isoformat())
        for key, value in self.settings.describe().items():
            logger.info("  %-16s : %s", key, value)
        logger.info("=" * 72)

    # -- modes -------------------------------------------------------------

    def _run_full(self) -> None:
        repository = self._open_repository()
        try:
            download = self._fetch_report()
            try:
                read_result = read_promotion_report(download.path)
                transformed = self._transform(read_result, repository)
                if repository is not None and self.options.writes_database:
                    self._write(repository, transformed)
            except Exception:
                # Set the report aside for inspection or a --process-file
                # retry, apart from the downloads that are still in flight.
                self._archive(download.path, success=False)
                raise
            if repository is not None and self.options.writes_database:
                # The rows are committed, so the report has nothing left to give.
                if self.settings.keep_processed_report:
                    self._archive(download.path, success=True)
                else:
                    self._discard(download.path)
            else:
                self._preview(transformed)
                self._archive(download.path, success=True)
        finally:
            # Housekeeping also has to run when the report was empty or the
            # download failed -- otherwise a week of failing runs prunes
            # nothing -- but it must never mask the error that got us here.
            try:
                cleanup_old_files(self.settings)
            except Exception:
                logger.warning("Housekeeping failed", exc_info=True)
            if repository is not None and self._owns_repository:
                repository.dispose()

    def _run_download_only(self) -> None:
        download = self._fetch_report()
        logger.info(
            "Download-only complete: %s (%.1f KB). No processing performed.",
            download.path,
            download.size_kb,
        )

    def _run_process_file(self) -> None:
        source = self.options.process_file
        assert source is not None  # guaranteed by the CLI
        source = Path(source).expanduser().resolve()
        logger.info("Processing existing file: %s", source)
        self.summary.report_path = source
        if source.is_file():
            self.summary.report_bytes = source.stat().st_size

        repository = self._open_repository()
        try:
            read_result = read_promotion_report(source)
            transformed = self._transform(read_result, repository)
            if repository is not None and self.options.writes_database:
                # The file belongs to whoever passed it in; it is never moved
                # or deleted.
                self._write(repository, transformed)
            else:
                self._preview(transformed)
        finally:
            if repository is not None and self._owns_repository:
                repository.dispose()

    # -- stages ------------------------------------------------------------

    def _open_repository(self) -> PromotionRepository | None:
        """Connect to MySQL, or return None when the run must not write."""
        if self.options.mode is RunMode.DOWNLOAD_ONLY:
            return None
        try:
            return self._retry_db("Database connection", self._repository_factory)
        except DatabaseError:
            if self.options.writes_database:
                raise
            logger.warning(
                "DRY RUN: MySQL is unreachable; continuing without database validation"
            )
            return None

    def _fetch_report(self) -> DownloadResult:
        """Request (or resume) the report, wait for Amazon, download it."""
        client = self._client_factory()
        try:
            if self.options.report_id:
                report_id = self.options.report_id
                logger.info("Resuming existing report %s; no new report requested", report_id)
            else:
                window = resolve_window(
                    self.settings.lookback_days,
                    date_from=self.options.date_from,
                    date_to=self.options.date_to,
                )
                self.summary.window = window
                report_id = client.create_report(window)
            self.summary.report_id = report_id

            status = client.wait_for_report(report_id)
            assert status.document_id is not None  # wait_for_report guarantees it
            self.summary.document_id = status.document_id
            result = client.download_document(status.document_id, self.settings.download_dir)
        finally:
            client.close()

        self.summary.report_path = result.path
        self.summary.report_bytes = result.size_bytes
        return result

    def _transform(
        self, read_result: ReadResult, repository: PromotionRepository | None
    ) -> TransformResult:
        """Align the report to the live table when available.

        Without a database connection (a dry run against an unreachable
        server), the bootstrap table stands in so the rest of the workflow can
        still be exercised.
        """
        self.summary.promotion_count = read_result.promotion_count
        if repository is not None:
            spec = repository.spec()
        else:
            spec = TableSpec.from_table(
                build_bootstrap_table(self.settings.table, key_column=self.settings.key_column)
            )
            logger.warning(
                "No database connection: aligning against the built-in column set "
                "instead of the live table"
            )

        transformed = transform(read_result.frame, spec, key_column=self.settings.key_column)
        self.summary.row_count = transformed.row_count
        self.summary.dropped_columns = list(transformed.dropped_columns)
        self.summary.null_columns = list(transformed.null_columns)
        return transformed

    def _write(self, repository: PromotionRepository, transformed: TransformResult) -> None:
        if transformed.row_count == 0:
            raise EmptyReportError(
                "Transformed report has zero rows; skipping the load so existing data "
                "is preserved"
            )
        self.summary.write_result = self._retry_db(
            "Promotion load",
            lambda: repository.replace_promotions(transformed.promotion_ids, transformed.records),
        )

    def _retry_db(self, description: str, operation: Callable[[], T]) -> T:
        """Run a database step, retrying the failures a second attempt can fix.

        Safe for every step it wraps: connecting and reflecting are read-only,
        and the load is one DELETE+INSERT transaction, so re-running it
        converges on the same rows.
        """
        return retry_call(
            operation,
            description=description,
            attempts=self.settings.max_retries,
            base_delay=self.settings.retry_backoff_seconds,
            max_delay=self.settings.retry_backoff_max_seconds,
        )

    def _preview(self, transformed: TransformResult) -> None:
        """Dry run: write the rows that would be loaded, as CSV, for inspection."""
        path = self.settings.processed_dir / f"{timestamp_label()}_promotion_rows.csv"
        transformed.frame.to_csv(path, index=False)
        logger.info(
            "DRY RUN: %d row(s) across %d promotion(s) prepared; nothing written to MySQL. "
            "Preview: %s",
            transformed.row_count,
            len(transformed.promotion_ids),
            path,
        )

    def _discard(self, path: Path) -> None:
        """Delete a report whose rows are already in MySQL."""
        try:
            path.unlink(missing_ok=True)
            logger.info("Deleted report %s now that it is loaded", path)
        except OSError as exc:
            # Not fatal: housekeeping retries it after FILE_RETENTION_DAYS.
            logger.warning("Could not delete %s: %s", path, exc)

    def _archive(self, path: Path, *, success: bool) -> None:
        """Move a report into ``processed/`` or ``failed/``."""
        destination_dir = self.settings.processed_dir if success else self.settings.failed_dir
        destination = destination_dir / path.name
        try:
            shutil.move(str(path), str(destination))
            logger.info("Archived report to %s", destination)
            if self.summary.report_path == path:
                self.summary.report_path = destination
        except OSError as exc:
            logger.warning("Could not archive %s to %s: %s", path, destination, exc)


def handle_failure(pipeline: PromotionPipeline, error: BaseException) -> None:
    """Emit the operator-facing guidance for a failed run."""
    if isinstance(error, AuthenticationError):
        logger.error(
            "Amazon refused the credentials and this cannot be retried automatically. "
            "A refresh token stops working when the seller de-authorises the app; "
            "re-authorise it in Seller Central and update SP_API_REFRESH_TOKEN."
        )
        return
    if isinstance(error, EmptyReportError):
        logger.error(
            "The report contained no promoted products. Nothing was deleted or "
            "inserted; %s is unchanged.",
            pipeline.settings.table,
        )
        return
    # Covers both a report moved to FAILED_DIR and one left in DOWNLOAD_DIR
    # because the move itself failed.
    report = pipeline.summary.report_path
    if (
        pipeline.options.mode is RunMode.FULL
        and report is not None
        and report.is_file()
    ):
        logger.error(
            "The report was downloaded before the failure. Load it without asking "
            "Amazon again: python run.py --process-file %s",
            report,
        )
