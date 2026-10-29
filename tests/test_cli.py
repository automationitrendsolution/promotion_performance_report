"""Argument parsing and execution-mode resolution."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.main import RunMode, RunOptions
from run import build_parser, resolve_mode, validate_args


def parse(*argv: str):
    parser = build_parser()
    args = parser.parse_args(list(argv))
    validate_args(parser, args)
    return args


class TestModeResolution:
    def test_no_flags_is_a_full_run(self) -> None:
        assert resolve_mode(parse()) is RunMode.FULL

    def test_download_only(self) -> None:
        assert resolve_mode(parse("--download-only")) is RunMode.DOWNLOAD_ONLY

    def test_process_file(self) -> None:
        assert resolve_mode(parse("--process-file", "a.json")) is RunMode.PROCESS_FILE

    def test_dry_run_keeps_the_full_workflow(self) -> None:
        args = parse("--dry-run")

        assert resolve_mode(args) is RunMode.FULL
        assert args.dry_run is True

    def test_report_id_is_still_a_full_run(self) -> None:
        args = parse("--report-id", "50123")

        assert resolve_mode(args) is RunMode.FULL
        assert args.report_id == "50123"


class TestFlags:
    def test_dates_are_parsed(self) -> None:
        args = parse("--date-from", "2026-09-01", "--date-to", "2026-09-30")

        assert (args.date_from, args.date_to) == (date(2026, 9, 1), date(2026, 9, 30))

    def test_bad_date_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse("--date-from", "01-09-2026")

    def test_reversed_dates_are_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse("--date-from", "2026-09-30", "--date-to", "2026-09-01")

    def test_conflicting_modes_are_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse("--download-only", "--process-file", "x.json")

    def test_process_file_with_report_id_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse("--process-file", "x.json", "--report-id", "1")

    def test_report_id_with_dates_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse("--report-id", "1", "--date-from", "2026-09-01")

    def test_dry_run_combines_with_process_file(self) -> None:
        args = parse("--dry-run", "--process-file", "x.json")

        assert args.dry_run is True
        assert resolve_mode(args) is RunMode.PROCESS_FILE


class TestRunOptions:
    def test_full_run_writes_to_the_database(self) -> None:
        assert RunOptions(mode=RunMode.FULL).writes_database is True

    def test_dry_run_never_writes(self) -> None:
        assert RunOptions(mode=RunMode.FULL, dry_run=True).writes_database is False

    def test_download_only_never_writes(self) -> None:
        assert RunOptions(mode=RunMode.DOWNLOAD_ONLY).writes_database is False

    def test_process_file_needs_no_amazon(self) -> None:
        assert RunOptions(mode=RunMode.PROCESS_FILE).needs_amazon is False

    def test_label_marks_dry_runs(self) -> None:
        assert RunOptions(mode=RunMode.FULL, dry_run=True).label == "full (dry-run)"


class TestEntryPoint:
    def test_missing_configuration_exits_with_the_config_code(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import run as cli
        from app.exceptions import ConfigurationError

        assert cli.main(["--download-only"]) == ConfigurationError.exit_code
        assert "Configuration error" in capsys.readouterr().err

    def test_process_file_rejects_a_missing_path(
        self, env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import run as cli

        monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("LOCK_FILE", str(tmp_path / "promoflow.lock"))

        assert cli.main(["--process-file", str(tmp_path / "absent.json")]) == 2
