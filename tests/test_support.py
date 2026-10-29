"""Retry policy, run lock, log redaction, housekeeping and time handling."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.exceptions import (
    AuthenticationError,
    ConfigurationError,
    DownloadError,
    LockError,
    ReportRequestError,
)
from app.logging_config import RedactingFilter, _redact
from app.maintenance import cleanup_old_files
from app.retry import compute_delay, is_transient, retry_call
from app.runlock import run_lock
from app.timeutils import isoformat_z, resolve_window, timestamp_label

NOW = datetime(2026, 10, 28, 10, 15, 30, 123456, tzinfo=timezone.utc)


class TestRetry:
    def test_returns_the_first_success(self) -> None:
        calls = iter([ReportRequestError("429", transient=True), "ok"])

        def operation() -> str:
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return item

        assert retry_call(operation, description="t", attempts=3, base_delay=0, max_delay=0) == "ok"

    def test_gives_up_after_the_last_attempt(self) -> None:
        attempts: list[int] = []

        def operation() -> None:
            attempts.append(1)
            raise DownloadError("flaky")

        with pytest.raises(DownloadError):
            retry_call(operation, description="t", attempts=3, base_delay=0, max_delay=0)
        assert len(attempts) == 3

    def test_never_retries_refused_credentials(self) -> None:
        attempts: list[int] = []

        def operation() -> None:
            attempts.append(1)
            raise AuthenticationError("invalid_grant")

        with pytest.raises(AuthenticationError):
            retry_call(operation, description="t", attempts=5, base_delay=0, max_delay=0)
        assert attempts == [1]

    def test_transient_is_per_instance(self) -> None:
        assert is_transient(ReportRequestError("x", transient=True))
        assert not is_transient(ReportRequestError("x"))
        assert not is_transient(ConfigurationError("x"))
        assert is_transient(ConnectionError())

    def test_backoff_is_capped(self) -> None:
        assert all(compute_delay(10, 5, 120) <= 120 for _ in range(50))
        assert compute_delay(1, 0, 120) == 0


class TestRunLock:
    def test_acquires_and_releases(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "promoflow.lock"

        with run_lock(lock_file):
            assert lock_file.is_file()
        with run_lock(lock_file):  # re-acquirable once released
            pass

    def test_records_the_holding_pid(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "promoflow.lock"

        with run_lock(lock_file):
            assert lock_file.read_text().strip() == str(os.getpid())

    def test_disabled_lock_is_a_no_op(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "promoflow.lock"

        with run_lock(lock_file, enabled=False):
            assert not lock_file.exists()

    def test_second_process_is_refused(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "promoflow.lock"
        project_root = Path(__file__).resolve().parent.parent
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(project_root)!r})
            from pathlib import Path
            from app.exceptions import LockError
            from app.runlock import run_lock
            try:
                with run_lock(Path({str(lock_file)!r})):
                    print("ACQUIRED")
            except LockError as exc:
                print("REFUSED", exc)
            """
        )

        with run_lock(lock_file):
            completed = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )

        assert "REFUSED" in completed.stdout, completed.stderr
        assert f"holder pid {os.getpid()}" in completed.stdout

    def test_lock_is_released_even_when_the_body_raises(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "promoflow.lock"

        with pytest.raises(RuntimeError):
            with run_lock(lock_file):
                raise RuntimeError("boom")
        with run_lock(lock_file):  # would raise LockError if still held
            pass

    def test_lock_error_exit_code(self) -> None:
        assert LockError.exit_code == 11


class TestRedaction:
    @pytest.mark.parametrize(
        ("raw", "secret"),
        [
            ("mysql+mysqlconnector://user:Team12345!@host:3306/db", "Team12345!"),
            ("refresh_token=Atzr|IwEBIabc", "IwEBIabc"),
            ("token is Atzr|IwEBIIqpV8k8 in a sentence", "IwEBIIqpV8k8"),
            ("x-amz-access-token: Atza|IwEaccess", "IwEaccess"),
            ("client secret amzn1.oa2-cs.v1.59cefb51ecc", "59cefb51ecc"),
            ("https://s3.amazonaws.com/doc?X-Amz-Signature=abc123&X-Amz-Credential=AKIA", "abc123"),
            ("password=hunter2", "hunter2"),
        ],
    )
    def test_secret_is_masked(self, raw: str, secret: str) -> None:
        assert secret not in str(_redact(raw))

    def test_filter_redacts_arguments(self) -> None:
        record = logging.LogRecord("n", logging.INFO, "f", 1, "dsn %s", ("mysql://u:pw@h/d",), None)

        RedactingFilter().filter(record)

        assert "pw" not in record.getMessage()

    def test_ordinary_text_is_untouched(self) -> None:
        text = "Report R1 status: DONE (3 promotions)"

        assert _redact(text) == text


class TestHousekeeping:
    def test_removes_only_old_owned_files(self, settings: Settings) -> None:
        old_owned = settings.download_dir / "20260101T000000Z_promotion_report.json"
        new_owned = settings.download_dir / "20261028T000000Z_promotion_report.json"
        foreign = settings.download_dir / "notes.json"
        for path in (old_owned, new_owned, foreign):
            path.write_text("{}")
        ancient = NOW.timestamp() - 90 * 86_400
        os.utime(old_owned, (ancient, ancient))
        os.utime(foreign, (ancient, ancient))

        assert cleanup_old_files(settings) == 1
        assert not old_owned.exists()
        assert new_owned.exists() and foreign.exists()

    def test_leftover_part_files_are_cleaned(self, settings: Settings) -> None:
        part = settings.download_dir / "20260101T000000Z_promotion_report.json.part"
        part.write_text("{")
        ancient = NOW.timestamp() - 90 * 86_400
        os.utime(part, (ancient, ancient))

        assert cleanup_old_files(settings) == 1


class TestTime:
    def test_default_window_is_the_lookback(self) -> None:
        window = resolve_window(30, reference=NOW)

        assert isoformat_z(window.end) == "2026-10-28T10:15:30Z"
        assert isoformat_z(window.start) == "2026-09-28T10:15:30Z"

    def test_report_options(self) -> None:
        assert resolve_window(30, reference=NOW).report_options() == {
            "promotionStartDateFrom": "2026-09-28T10:15:30Z",
            "promotionStartDateTo": "2026-10-28T10:15:30Z",
        }

    def test_explicit_days_are_whole_utc_days(self) -> None:
        window = resolve_window(
            30, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), reference=NOW
        )

        assert str(window) == "2026-09-01T00:00:00Z .. 2026-09-30T23:59:59Z"

    def test_only_from_runs_until_now(self) -> None:
        window = resolve_window(30, date_from=date(2026, 10, 1), reference=NOW)

        assert isoformat_z(window.end) == "2026-10-28T10:15:30Z"

    def test_reversed_window_is_refused(self) -> None:
        with pytest.raises(ConfigurationError):
            resolve_window(30, date_from=date(2026, 11, 1), reference=NOW)

    def test_server_zone_is_irrelevant(self) -> None:
        from datetime import timedelta

        ist = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))

        assert resolve_window(30, reference=ist) == resolve_window(30, reference=NOW)

    def test_timestamp_label(self) -> None:
        assert timestamp_label(NOW) == "20261028T101530Z"
