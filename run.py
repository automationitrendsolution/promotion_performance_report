#!/usr/bin/env python3
"""PromoFlow command-line entry point.

Requests the Amazon ``GET_PROMOTION_PERFORMANCE_REPORT`` through the Selling
Partner API, then loads it into MySQL.

Modes
-----
    python run.py                                # production run
    python run.py --dry-run                      # full workflow, no MySQL writes
    python run.py --download-only                # fetch the report JSON and stop
    python run.py --process-file report.json     # load a saved report, no Amazon
    python run.py --report-id 50123018000        # resume an already-requested report

Running under cron or systemd
-----------------------------
A scheduled run is just ``python run.py`` with no flags.  Two things about
cron's environment have to be handled, and the shipped schedules already do:

1. **cron starts in ``$HOME`` with a near-empty environment.**  PromoFlow reads
   ``.env`` from *the project directory*, so the job must ``cd`` there first,
   and must call ``uv`` by absolute path because cron's ``PATH`` is minimal::

       30 6 * * * promoflow cd /opt/promoflow && /usr/local/bin/uv run --no-sync \\
                  python run.py >> /opt/promoflow/logs/cron-stdout.log 2>&1

   Under systemd the equivalents are ``WorkingDirectory=`` and
   ``EnvironmentFile=`` -- see ``docs/setup.md``.

2. **Runs must not overlap.**  PromoFlow takes an exclusive lock on
   ``LOCK_FILE`` and exits 11 if another run holds it, so a job that overruns
   its slot is skipped rather than doubled up.  No ``flock`` wrapper needed.

Nothing here is interactive: there is no browser and no prompt, so a scheduled
run can never hang waiting on a human.  It can only wait on Amazon, and that
wait is capped by ``REPORT_POLL_TIMEOUT_SECONDS``.

Monitoring a scheduled run
--------------------------
Alert on the exit code; 3 is the only one that needs a human rather than a
retry::

    0   success
    2   configuration error
    3   authentication -- Amazon refused the LWA credentials
    5   Amazon could not build the report, or it timed out
    9   database error (transaction rolled back, existing data intact)
    10  empty report (nothing written, nothing deleted)
    11  another run holds the lock

Every run ends with a SUCCESS/FAILED summary block in ``LOG_DIR/cron.log``
listing the report window, report ID, row counts and insert/delete totals.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from app import __version__
from app.config import load_settings
from app.exceptions import ConfigurationError, PromoFlowError
from app.logging_config import configure_logging, get_logger
from app.main import PromotionPipeline, RunMode, RunOptions, handle_failure


#: Shown at the bottom of ``--help``.  Kept separate from the module docstring
#: so that `--help` stays short; the full operational notes live above.
_HELP_EPILOG = """
examples:
  python run.py                              production run (what cron calls)
  python run.py --dry-run                    full workflow, nothing written to MySQL
  python run.py --download-only              fetch the report JSON and stop
  python run.py --process-file report.json   load a saved report without Amazon
  python run.py --report-id 50123018000      resume a report requested earlier
  python run.py --date-from 2026-09-01 --date-to 2026-09-30
                                             backfill promotions that started then

scheduling:
  Cron calls `python run.py` with no flags, from the project directory so that
  .env is found. Overlapping runs are refused by an internal lock.
  See docs/setup.md.

exit codes:
  0 success · 2 config · 3 authentication (needs a human) · 4 SP-API request
  5 report failed/timed out · 6 download · 7 report parse · 8 transform
  9 database · 10 empty report (nothing written) · 11 locked by another run
"""


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"dates must be YYYY-MM-DD, got {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promoflow",
        description=(
            "Request the Amazon promotion performance report through SP-API and "
            "load it into MySQL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"PromoFlow {__version__}")

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the full request and processing workflow without writing to MySQL. "
            "Writes a CSV preview to PROCESSED_DIR. Combines with --process-file."
        ),
    )

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--download-only",
        action="store_true",
        help="Download the report JSON and stop (no parsing, no database).",
    )
    modes.add_argument(
        "--process-file",
        type=Path,
        metavar="PATH",
        help="Load a saved report JSON without contacting Amazon.",
    )

    parser.add_argument(
        "--report-id",
        metavar="ID",
        help=(
            "Poll and download a report that was already requested (e.g. one that "
            "timed out) instead of requesting a new one."
        ),
    )
    parser.add_argument(
        "--date-from",
        type=_parse_date,
        metavar="YYYY-MM-DD",
        help="Earliest promotion start date (UTC). Default: PROMOTION_LOOKBACK_DAYS ago.",
    )
    parser.add_argument(
        "--date-to",
        type=_parse_date,
        metavar="YYYY-MM-DD",
        help="Latest promotion start date (UTC, inclusive). Default: now.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="Path to an alternative .env file.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override LOG_LEVEL for this run.",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip the single-instance lock. Only for manual, supervised runs.",
    )
    parser.add_argument(
        "--echo-sql",
        action="store_true",
        help="Log SQL statements (verbose; never logs credentials).",
    )
    return parser


def resolve_mode(args: argparse.Namespace) -> RunMode:
    if args.process_file is not None:
        return RunMode.PROCESS_FILE
    if args.download_only:
        return RunMode.DOWNLOAD_ONLY
    return RunMode.FULL


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject combinations argparse cannot express."""
    has_window = args.date_from is not None or args.date_to is not None
    if args.process_file is not None and (args.report_id or has_window):
        parser.error("--process-file does not contact Amazon; drop --report-id/--date-*")
    if args.report_id and has_window:
        parser.error("--report-id reuses that report's window; drop --date-from/--date-to")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--date-from is after --date-to")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    mode = resolve_mode(args)

    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ConfigurationError.exit_code

    settings.ensure_directories()
    configure_logging(
        settings.log_dir,
        level=args.log_level or settings.log_level,
        retention_days=settings.log_retention_days,
    )
    logger = get_logger("cli")

    if args.process_file is not None and not Path(args.process_file).expanduser().is_file():
        logger.error("File not found: %s", args.process_file)
        return 2

    options = RunOptions(
        mode=mode,
        dry_run=args.dry_run,
        process_file=args.process_file,
        report_id=args.report_id,
        date_from=args.date_from,
        date_to=args.date_to,
        use_lock=not args.no_lock,
        echo_sql=args.echo_sql,
    )

    pipeline = PromotionPipeline(settings, options)
    try:
        pipeline.run()
    except PromoFlowError as exc:
        handle_failure(pipeline, exc)
        return exc.exit_code
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception:  # noqa: BLE001 - already logged with traceback
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
