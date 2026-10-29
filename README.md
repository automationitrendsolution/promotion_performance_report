# PromoFlow: Amazon promotion data into MySQL

PromoFlow collects the performance of your Amazon promotions (deals, coupons,
discounts) every day and saves it in the MySQL database that Power BI reads
from. It replaces the old manual Python script.

| Part | For | Read it to learn |
| --- | --- | --- |
| [**1. Overview**](#part-1--overview-for-everyone) | Everyone | What it does, how to tell it worked, what to do when it didn't |
| [**2. Running it**](#part-2--running-it) | Whoever operates it | Setup, commands, scheduling |
| [**3. Technical reference**](#part-3--technical-reference) | Developers | Settings, design, tests, exit codes |

---

# Part 1 — Overview (for everyone)

## What it does

Once a day PromoFlow does five things:

```
 1. Asks Amazon     "Send me the performance of every promotion
                     that started in the last 30 days."
 2. Waits           Amazon prepares the report (usually under a minute).
 3. Downloads it    One file with all the promotions and their products.
 4. Tidies it up    One row per product in each promotion.
 5. Saves it        Into the api_promotion_data table in MySQL.
```

**It never creates duplicates.** For each promotion in the report, PromoFlow
removes that promotion's old rows and saves the fresh ones. Promotions that
aren't in the report are left alone. Running it twice gives the same result as
running it once.

**It never loses data.** The remove and the save happen as one step. If the
save fails, nothing is removed, and the table stays exactly as it was.

## What gets saved

Each row is **one product in one promotion**. A promotion with 40 products has
40 rows, and the promotion's details are repeated on each of them.

| About the product | About the promotion |
| --- | --- |
| ASIN, product name | Promotion ID and name |
| Views, units sold, revenue (with currency) | Type (e.g. Lightning Deal) and status (e.g. Active, Canceled) |
| | Views, units sold, revenue for the whole promotion |
| | Start, end, created and last-updated dates |

Dates are Amazon's, in UTC. `lastUpdatedDateTime` is when Amazon last changed
the promotion, **not** when PromoFlow saved it.

## How to tell it worked

**Option 1: the log.** Open `logs\cron.log` and scroll to the bottom. A good
run ends like this:

```
  promotions         : 439
  rows processed     : 19319
  api_promotion_data: 19319 deleted, 19319 inserted
  status             : SUCCESS
```

"Deleted" and "inserted" are normally the same or close. That is the old copy
being swapped for the new one.

**Option 2: the database.** In any MySQL tool:

```sql
SELECT COUNT(*) AS total_rows, COUNT(DISTINCT promotionId) AS promotions
FROM api_promotion_data;
```

## When something goes wrong

The last lines of `logs\cron.log` say what happened. **In every case below,
the data already in MySQL is untouched.**

| The log says | What it means | What to do |
| --- | --- | --- |
| `status : SUCCESS` | All good. | Nothing. |
| `EmptyReportError` | No promotions started in the last 30 days. | Nothing. This is normal. |
| `AuthenticationError` | Amazon no longer accepts the login. Usually the app was disconnected in Seller Central. | Reconnect the app in Seller Central and put the new refresh token in `.env`. |
| `DatabaseError` | MySQL was unreachable or refused the save. | Check that the database server is up, then run again. The downloaded report is in `data\failed\`. |
| `still IN_PROGRESS` | Amazon took more than 30 minutes. | Run again later. The message shows a command that picks up the same report. |
| `FATAL` | Amazon could not build the report. | Run again. If it keeps happening, see [Troubleshooting](#troubleshooting). |
| `LockError` | It was already running. | Nothing. The second copy stopped on purpose. |
| `Configuration error` | A setting in `.env` is missing or wrong. | The message names the setting. Fix it in `.env`. |

## The folders

| Folder | What's in it |
| --- | --- |
| `logs\` | `cron.log`: the diary of every run. Old logs are deleted after 5 days. |
| `data\failed\` | Reports that downloaded but couldn't be saved. Kept for 30 days. |
| `data\processed\` | Previews from test runs (`--dry-run`). Kept for 30 days. |
| `data\downloads\` | Reports while they are being processed. Normally empty. |
| `data\samples\` | A small example report, used for testing. |

PromoFlow cleans these folders itself. Nobody needs to delete anything.

## Words used in this document

| Word | Meaning |
| --- | --- |
| **SP-API** | Amazon's Selling Partner API, the official way programs get seller data from Amazon. |
| **Refresh token** | A long-lived password Amazon issues when a seller connects an app. Keep it secret. |
| **Dry run** | A test run that does everything except save to MySQL. |
| **Cron / Task Scheduler** | The tools that start PromoFlow automatically every day (Linux / Windows). |

---

# Part 2 — Running it

## One-time setup

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```powershell
cd C:\projects\pomotion_performance_report
uv sync                             # install everything
copy .env.example .env              # then fill in .env (see below)
```

Open `.env` and fill in at least:

| Setting | Where to get it |
| --- | --- |
| `SP_API_CLIENT_ID`, `SP_API_CLIENT_SECRET` | Seller Central → Apps and Services → Develop Apps → your app |
| `SP_API_REFRESH_TOKEN` | Issued when the seller authorises the app |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Your MySQL server |

`.env` holds passwords. Never email it, share it, or commit it to git. Git
already ignores it.

## Check the setup (safe, changes nothing)

```powershell
uv run pytest -m live           # can we log in to Amazon and MySQL?
uv run python run.py --dry-run  # full test run, saves nothing
```

Both should pass or end with `status : SUCCESS`. The dry run leaves a preview
of the rows it would have saved in `data\processed\*_promotion_rows.csv`.

## Everyday commands

| Command | What it does |
| --- | --- |
| `uv run python run.py` | **The normal run.** Asks Amazon, downloads, saves to MySQL. |
| `uv run python run.py --dry-run` | Test run. Everything except saving. |
| `uv run python run.py --process-file data\failed\<file>.json` | Saves a report that failed earlier, without asking Amazon again. |
| `uv run python run.py --date-from 2026-08-01 --date-to 2026-08-31` | Fetches promotions that started in a past period. |
| `uv run python run.py --report-id <ID>` | Picks up a report Amazon was still preparing when a run gave up. |
| `uv run python run.py --download-only` | Downloads the report and stops. |
| `uv run python run.py --help` | Lists every option. |

## Run it automatically every day

Once a day is enough. Each run re-reads the last 30 days, so a missed day is
caught up the next time.

**On Windows**, run this once:

```powershell
schtasks /Create /TN "PromoFlow" /SC DAILY /ST 12:00 /TR "cmd /c cd /d C:\projects\pomotion_performance_report && uv run --no-sync python run.py"
schtasks /Run /TN "PromoFlow"     # run it now to check it works
```

The task only runs while the PC is on and someone is logged in.

**On a Linux server**, which runs whether or not anyone is logged in, follow
[docs/setup.md](docs/setup.md). It covers installation, a systemd timer or cron
line, and the database table.

## Fixing a failed run

1. Read the end of `logs\cron.log`, then use the table in
   [When something goes wrong](#when-something-goes-wrong).
2. If the report downloaded, it is in `data\failed\`. Once the cause is fixed,
   save it:
   ```powershell
   dir data\failed
   uv run python run.py --process-file data\failed\20261029T060306Z_promotion_report.json
   ```
3. Otherwise just run `uv run python run.py` again. Repeating a run is always
   safe.

---

# Part 3 — Technical reference

## How it works

```
LWA refresh token → access token
        ↓
createReport  GET_PROMOTION_PERFORMANCE_REPORT (Reports API 2021-06-30)
        ↓
getReport     poll every 30s until DONE / CANCELLED / FATAL
        ↓
getReportDocument → download → gunzip (if compressionAlgorithm=GZIP) → JSON
        ↓  pandas.json_normalize
one row per includedProducts entry, promotion fields as meta
        ↓  align to the reflected live table, coerce types
one transaction: DELETE WHERE promotionId IN (…) + chunked INSERT
```

| Module | Responsibility |
| --- | --- |
| [`run.py`](run.py) | CLI: flags, modes, exit codes. |
| [`app/config.py`](app/config.py) | Reads and validates every setting from the environment. Fails before contacting Amazon if anything is wrong. |
| [`app/sp_api.py`](app/sp_api.py) | LWA tokens, createReport, polling, getReportDocument, download. Each HTTP call is retried on its own. |
| [`app/report_processor.py`](app/report_processor.py) | Parses and flattens the report JSON. Rejects non-reports and empty reports. |
| [`app/data_transformer.py`](app/data_transformer.py) | Aligns rows to the live table and coerces values to each column's type. |
| [`app/database.py`](app/database.py) | Reflects the table; DELETE+INSERT in one transaction; ROLLBACK on failure. |
| [`app/main.py`](app/main.py) | Orchestration, run modes, the run summary, moving reports to processed/failed. |
| [`app/schema.py`](app/schema.py) | Report field lists and the table created when none exists. |
| `app/retry.py`, `runlock.py`, `maintenance.py`, `logging_config.py`, `timeutils.py` | Retries, single-instance lock, cleanup, logging with secret redaction, UTC handling. |

**Design decisions**

- **The live table is authoritative.** The table is reflected at startup.
  Report fields with no column are dropped, columns with no field are written
  as NULL, and an auto-increment `id` is left out. All of it is logged. No DDL
  is ever run against an existing table.
- **Replace per promotion.** Only the promotions in the report are deleted and
  re-inserted. A promotion listed with no products keeps its existing rows.
- **No AWS signing.** SP-API has not required SigV4 since October 2023, so there
  are no AWS keys, no IAM role and no `python-amazon-sp-api`.
- **Retry only what can succeed.** Throttling (429), 5xx responses, dropped
  connections and MySQL lost-connection or deadlock errors are retried with
  backoff. Refused credentials and bad requests fail immediately.

## Settings (`.env`)

Exported environment variables override `.env`. All paths are relative to the
project folder. The full annotated list is in [`.env.example`](.env.example).

| Variable | Default | Notes |
| --- | --- | --- |
| `SP_API_CLIENT_ID` / `SP_API_CLIENT_SECRET` / `SP_API_REFRESH_TOKEN` | — | **Required.** |
| `SP_API_ENDPOINT` | `https://sellingpartnerapi-na.amazon.com` | `-eu` / `-fe` for other regions. |
| `MARKETPLACE_IDS` | `ATVPDKIKX0DER` (US) | Comma-separated. |
| `PROMOTION_LOOKBACK_DAYS` | `30` | Window of promotion **start** dates. |
| `REPORT_POLL_INTERVAL_SECONDS` / `REPORT_POLL_TIMEOUT_SECONDS` | `30` / `1800` | |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | — / `3306` | **Required** (except the port). |
| `DB_TABLE` / `DB_KEY_COLUMN` | `api_promotion_data` / `promotionId` | |
| `CREATE_TABLES` | `false` | Creates the table only if it doesn't exist. |
| `DB_SSL_CA` | — | CA bundle, if MySQL requires TLS. |
| `MAX_RETRIES` / `RETRY_BACKOFF_SECONDS` | `3` / `5` | |
| `DOWNLOAD_DIR` / `PROCESSED_DIR` / `FAILED_DIR` / `LOG_DIR` | `data/…` / `logs` | |
| `KEEP_PROCESSED_REPORT` | `false` | `true` keeps loaded reports in `PROCESSED_DIR`. |
| `FILE_RETENTION_DAYS` / `LOG_RETENTION_DAYS` | `30` / `5` | Cleanup ages. |

## The table

Column names and order match what the original script's `pandas.to_sql`
created, so the existing table works unchanged. To create it from scratch, set
`CREATE_TABLES=true` for one run, or use the DDL in
[docs/setup.md](docs/setup.md#appendix-schema). If a column is `DATETIME`, dates
are stored as naive UTC. Otherwise they are kept as Amazon's ISO 8601 strings.

## Exit codes

For monitoring. Only **3** needs a person. The others are temporary, or normal
(10).

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Configuration error |
| 3 | Authentication: Amazon refused the credentials |
| 4 | SP-API request error |
| 5 | Report `FATAL` or poll timeout |
| 6 | Download error |
| 7 | Report could not be parsed |
| 8 | Data transformation error |
| 9 | Database error (rolled back) |
| 10 | Empty report: nothing written |
| 11 | Another run holds the lock |
| 130 | Interrupted |

## Troubleshooting

- **`invalid_grant`**: the refresh token was revoked, usually because the app
  was de-authorised. Re-authorise it and update `SP_API_REFRESH_TOKEN`.
- **`invalid_client`**: the client ID or secret is wrong, or the secret was
  rotated.
- **`SP-API denied … (HTTP 403)`**: the login is valid, but the app lacks the
  role for this report, or was authorised by a different seller.
- **`FATAL`**: Amazon's reason is appended to the message. If it is about the
  date range, shorten it with `PROMOTION_LOOKBACK_DAYS` or
  `--date-from/--date-to`.
- **`dropped fields` / `NULL columns` in the summary**: the report and the
  table disagree on a column name. This is harmless. Add or rename the column if
  you want that data.

## Tests

```powershell
uv run pytest            # unit tests: no network, no database
uv run pytest -m live    # read-only: Amazon login + MySQL table check
```

The unit tests run the real SP-API client against scripted Amazon responses,
and the real database code against in-memory SQLite. They cover throttling,
retries, cancelled, failed and timed-out reports, compression, flattening, type
alignment, rollback, the lock and log redaction.

## Security

- `.env`, reports, logs and `*.pem` files are git-ignored.
- Secrets are masked in the logs: passwords, Amazon tokens, client secrets and
  download-link signatures.
- Anyone with the refresh token, client ID and secret can read the seller's
  data. Keep `.env` private (on Linux, `chmod 600 .env`).
- Cleanup deletes only files PromoFlow created, inside its own `data\` folders.

## Changes from the original script

| Original script | PromoFlow |
| --- | --- |
| Passwords and keys written in the code | Kept in `.env`, never in code |
| Waited for a `FAILED` status Amazon never sends, so a failed report looped forever | Handles every status, with a time limit |
| Delete was never committed, and was invalid SQL for a single promotion | Delete and insert in one committed transaction |
| A failed insert left promotions deleted | A failed insert undoes the delete |
| Needed AWS keys and an IAM role | Not needed |
| JSON → CSV → table (numeric-looking IDs lost leading zeros) | JSON → table directly |
#   p r o m o t i o n _ p e r f o r m a n c e _ r e p o r t -  
 