"""End to end: scripted SP-API -> real client -> pandas -> SQLite."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select

from app import database
from app.config import Settings
from app.database import PromotionRepository
from app.exceptions import (
    AuthenticationError,
    DatabaseError,
    EmptyReportError,
    LockError,
    ReportParseError,
    ReportProcessingError,
)
from app.logging_config import LOGGER_NAME
from app.main import PromotionPipeline, RunMode, RunOptions, handle_failure
from app.runlock import run_lock
from tests.conftest import (
    SAMPLE_REPORT,
    TOKEN_URL,
    FakeHttp,
    FakeResponse,
    make_client,
    script_amazon,
    write_report,
)


def pipeline(
    settings: Settings,
    repository: PromotionRepository,
    http: FakeHttp,
    **options: Any,
) -> PromotionPipeline:
    return PromotionPipeline(
        settings,
        RunOptions(**options),
        repository_factory=lambda: repository,
        client_factory=lambda: make_client(settings, http),
    )


def rows(repository: PromotionRepository) -> list[tuple[str, str]]:
    table = repository.table
    with repository.engine.connect() as connection:
        return sorted(connection.execute(select(table.c.promotionId, table.c.asin)).all())


def seed(repository: PromotionRepository, *pairs: tuple[str, str]) -> None:
    repository.replace_promotions(
        sorted({p for p, _ in pairs}), [{"promotionId": p, "asin": a} for p, a in pairs]
    )


class TestFullRun:
    def test_loads_every_product_row(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        script_amazon(http, report)

        summary = pipeline(settings, repository, http).run()

        assert summary.succeeded
        assert summary.report_id == "R1"
        assert (summary.promotion_count, summary.row_count) == (3, 3)
        assert summary.write_result is not None and summary.write_result.inserted == 3
        assert rows(repository) == [
            ("PROMO-0001", "B0EXAMPLE01"),
            ("PROMO-0001", "B0EXAMPLE02"),
            ("PROMO-0002", "B0EXAMPLE03"),
        ]

    def test_replaces_reported_promotions_and_keeps_the_rest(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        seed(repository, ("PROMO-0001", "STALE"), ("OLD-PROMO", "KEEP"))
        script_amazon(http, report)

        summary = pipeline(settings, repository, http).run()

        assert summary.write_result is not None and summary.write_result.deleted == 1
        assert ("PROMO-0001", "STALE") not in rows(repository)
        assert ("OLD-PROMO", "KEEP") in rows(repository)

    def test_a_promotion_without_products_keeps_its_old_rows(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        seed(repository, ("PROMO-0003", "EARLIER"))
        script_amazon(http, report)

        pipeline(settings, repository, http).run()

        assert ("PROMO-0003", "EARLIER") in rows(repository)

    def test_running_twice_converges(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        script_amazon(http, report)

        pipeline(settings, repository, http).run()
        pipeline(settings, repository, http).run()

        assert repository.count_rows() == 3

    def test_the_report_file_is_deleted_once_loaded(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        script_amazon(http, report)

        pipeline(settings, repository, http).run()

        assert list(settings.download_dir.iterdir()) == []
        assert list(settings.processed_dir.iterdir()) == []

    def test_keep_processed_report_archives_it(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dataclasses import replace

        script_amazon(http, report)
        kept = replace(settings, keep_processed_report=True)

        PromotionPipeline(
            kept,
            RunOptions(),
            repository_factory=lambda: repository,
            client_factory=lambda: make_client(kept, http),
        ).run()

        assert [p.name.endswith("_promotion_report.json") for p in kept.processed_dir.iterdir()] == [True]

    def test_the_window_comes_from_the_lookback(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        script_amazon(http, report)

        summary = pipeline(settings, repository, http).run()

        assert summary.window is not None
        assert (summary.window.end - summary.window.start).days == settings.lookback_days

    def test_explicit_dates_set_the_window(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        from datetime import date

        script_amazon(http, report)

        pipeline(
            settings, repository, http, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
        ).run()

        (_m, _u, kwargs), = http.calls_to("POST", "/reports/2021-06-30/reports")
        assert kwargs["json"]["reportOptions"] == {
            "promotionStartDateFrom": "2026-09-01T00:00:00Z",
            "promotionStartDateTo": "2026-09-30T23:59:59Z",
        }

    def test_report_id_resumes_without_requesting_a_new_report(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        script_amazon(http, report, report_id="R77")

        summary = pipeline(settings, repository, http, report_id="R77").run()

        assert summary.report_id == "R77"
        assert http.calls_to("POST", "/reports/2021-06-30/reports") == []
        assert repository.count_rows() == 3


class TestDryRun:
    def test_writes_nothing(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        seed(repository, ("PROMO-0001", "STALE"))
        script_amazon(http, report)

        summary = pipeline(settings, repository, http, dry_run=True).run()

        assert summary.succeeded and summary.write_result is None
        assert rows(repository) == [("PROMO-0001", "STALE")]

    def test_leaves_a_csv_preview_and_the_report(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        script_amazon(http, report)

        pipeline(settings, repository, http, dry_run=True).run()

        (preview,) = settings.processed_dir.glob("*_promotion_rows.csv")
        assert len(pd.read_csv(preview)) == 3
        assert len(list(settings.processed_dir.glob("*_promotion_report.json"))) == 1

    def test_survives_an_unreachable_database(
        self, settings: Settings, http: FakeHttp, report: dict[str, Any]
    ) -> None:
        script_amazon(http, report)

        def unreachable() -> PromotionRepository:
            raise DatabaseError("down", transient=False)

        summary = PromotionPipeline(
            settings,
            RunOptions(dry_run=True),
            repository_factory=unreachable,
            client_factory=lambda: make_client(settings, http),
        ).run()

        assert summary.succeeded and summary.row_count == 3


class TestFailures:
    def test_cancelled_report_deletes_nothing(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
    ) -> None:
        seed(repository, ("PROMO-0001", "KEEP"))
        script_amazon(http, None, statuses=("IN_QUEUE", "CANCELLED"))

        with pytest.raises(EmptyReportError):
            pipeline(settings, repository, http).run()

        assert rows(repository) == [("PROMO-0001", "KEEP")]

    def test_empty_promotions_deletes_nothing(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
    ) -> None:
        seed(repository, ("PROMO-0001", "KEEP"))
        script_amazon(http, {"promotions": []})

        with pytest.raises(EmptyReportError):
            pipeline(settings, repository, http).run()

        assert rows(repository) == [("PROMO-0001", "KEEP")]

    def test_fatal_report(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
    ) -> None:
        script_amazon(http, {"error": "boom"}, statuses=("FATAL",))

        with pytest.raises(ReportProcessingError, match="FATAL"):
            pipeline(settings, repository, http).run()

    def test_refused_credentials(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
    ) -> None:
        http.add("POST", TOKEN_URL, FakeResponse(401, {"error": "invalid_client"}))

        with pytest.raises(AuthenticationError):
            pipeline(settings, repository, http).run()

    def test_failed_load_keeps_the_report_and_the_data(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # configure_logging() in an earlier test may have turned propagation
        # off; caplog listens on the root logger.
        monkeypatch.setattr(logging.getLogger(LOGGER_NAME), "propagate", True)
        seed(repository, ("PROMO-0001", "KEEP"))
        script_amazon(http, report)

        def broken_insert(*_args: Any, **_kwargs: Any) -> int:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("INSERT", {}, Exception("disk full"))

        monkeypatch.setattr(database, "_chunked_insert", broken_insert)
        run = pipeline(settings, repository, http)

        with pytest.raises(DatabaseError) as excinfo:
            run.run()
        handle_failure(run, excinfo.value)

        assert rows(repository) == [("PROMO-0001", "KEEP")]
        assert list(settings.download_dir.glob("*_promotion_report.json")) == []
        (kept,) = settings.failed_dir.glob("*_promotion_report.json")
        assert f"--process-file {kept}" in caplog.text

    def test_an_unparseable_report_is_moved_to_failed(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
    ) -> None:
        script_amazon(http, {"not": "a promotion report"})

        with pytest.raises(ReportParseError):
            pipeline(settings, repository, http).run()

        assert len(list(settings.failed_dir.glob("*_promotion_report.json"))) == 1
        assert list(settings.download_dir.glob("*.json")) == []

    def test_an_empty_report_is_moved_to_failed(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
    ) -> None:
        script_amazon(http, {"promotions": []})

        with pytest.raises(EmptyReportError):
            pipeline(settings, repository, http).run()

        assert len(list(settings.failed_dir.glob("*_promotion_report.json"))) == 1

    def test_a_successful_run_leaves_failed_empty(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
        report: dict[str, Any],
    ) -> None:
        script_amazon(http, report)

        pipeline(settings, repository, http).run()

        assert list(settings.failed_dir.iterdir()) == []

    def test_a_second_run_is_refused(
        self, settings: Settings, repository: PromotionRepository, http: FakeHttp,
    ) -> None:
        with run_lock(settings.lock_file):
            with pytest.raises(LockError):
                pipeline(settings, repository, http).run()
        assert http.calls == []


class TestOtherModes:
    def test_download_only_touches_no_database(
        self, settings: Settings, http: FakeHttp, report: dict[str, Any]
    ) -> None:
        script_amazon(http, report)

        def no_database() -> PromotionRepository:
            raise AssertionError("download-only must not connect")

        summary = PromotionPipeline(
            settings,
            RunOptions(mode=RunMode.DOWNLOAD_ONLY),
            repository_factory=no_database,
            client_factory=lambda: make_client(settings, http),
        ).run()

        assert summary.report_path is not None and summary.report_path.is_file()

    def test_process_file_loads_without_amazon(
        self, settings: Settings, repository: PromotionRepository, tmp_path: Path,
        report: dict[str, Any],
    ) -> None:
        path = write_report(tmp_path / "saved.json", report)

        summary = PromotionPipeline(
            settings,
            RunOptions(mode=RunMode.PROCESS_FILE, process_file=path),
            repository_factory=lambda: repository,
            client_factory=lambda: (_ for _ in ()).throw(AssertionError("no Amazon")),
        ).run()

        assert summary.row_count == 3 and repository.count_rows() == 3
        assert path.is_file()  # the caller's file is never moved

    def test_process_file_dry_run(
        self, settings: Settings, repository: PromotionRepository
    ) -> None:
        summary = PromotionPipeline(
            settings,
            RunOptions(mode=RunMode.PROCESS_FILE, process_file=SAMPLE_REPORT, dry_run=True),
            repository_factory=lambda: repository,
        ).run()

        assert summary.row_count == 3 and repository.count_rows() == 0
        assert SAMPLE_REPORT.is_file()


class TestLiveSchemaDrift:
    def test_columns_the_table_lacks_are_dropped_not_fatal(
        self, settings: Settings, http: FakeHttp, report: dict[str, Any]
    ) -> None:
        from sqlalchemy import Column, MetaData, String, Table, create_engine
        from sqlalchemy.pool import StaticPool

        engine = create_engine("sqlite://", poolclass=StaticPool)
        Table(
            settings.table, MetaData(),
            Column("promotionId", String(64)), Column("asin", String(16)),
        ).metadata.create_all(engine)
        repo = PromotionRepository(engine, table=settings.table, key_column=settings.key_column)
        repo.reflect()
        script_amazon(http, copy.deepcopy(report))

        summary = pipeline(settings, repo, http).run()

        assert "productRevenue" in summary.dropped_columns
        assert repo.count_rows() == 3
