# PromoFlow server setup

These are manual setup steps for a fresh Linux server (Ubuntu 22.04+ or Amazon
Linux 2023). You run every command by hand. Nothing here is scripted.

The examples assume:

| What | Value |
|---|---|
| Service user | `promoflow` |
| Project directory | `/opt/promoflow` |
| uv binary | `/usr/local/bin/uv` |
| Environment file | `/opt/promoflow/.env` |

You can change these to suit the host, but then change them in every step.

---

## 1. System packages

**Ubuntu / Debian**

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git curl ca-certificates
```

**Amazon Linux / RHEL**

```bash
sudo dnf install -y python3.11 git tar gzip
```

Sizing: there is no browser, so the smallest instance will do (t3.micro).
Network: the host needs outbound 443 to `api.amazon.com`,
`sellingpartnerapi-na.amazon.com` and Amazon S3, and outbound 3306 to MySQL. It
needs no inbound ports.

## 2. Service user, project directory, dependencies

```bash
sudo useradd --system --create-home --home-dir /home/promoflow \
    --shell /usr/sbin/nologin promoflow

sudo mkdir -p /opt/promoflow
sudo git clone <repo-url> /opt/promoflow      # or copy the project in
sudo chown -R promoflow:promoflow /opt/promoflow

# uv (skip if `uv --version` already works)
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

cd /opt/promoflow
sudo -u promoflow -H /usr/local/bin/uv sync --no-dev
```

## 3. Configuration (`.env`)

```bash
sudo -u promoflow cp /opt/promoflow/.env.example /opt/promoflow/.env
sudo chmod 600 /opt/promoflow/.env
sudo -u promoflow nano /opt/promoflow/.env
```

At minimum, set the three `SP_API_*` credentials and the database settings.
`.env.example` explains every variable.

If MySQL requires TLS, copy the CA bundle in and point `DB_SSL_CA` at it:

```bash
sudo -u promoflow mkdir -p /opt/promoflow/certs
sudo -u promoflow cp isrgrootx1.pem /opt/promoflow/certs/
# .env:  DB_SSL_CA=certs/isrgrootx1.pem
```

## 4. Database table

If the database already has `api_promotion_data`, for example one created by
the original script's `to_sql`, skip this step. PromoFlow reflects whatever
columns the table has.

For a fresh database, choose one option:

- set `CREATE_TABLES=true` in `.env` for the first run, then set it back to
  `false`, **or**
- create the table with the DDL in [Appendix: schema](#appendix-schema).

## 5. Verify the credentials (read-only)

This step exchanges the refresh token for an access token and reflects the
table. It requests no report and writes nothing:

```bash
cd /opt/promoflow
sudo -u promoflow -H /usr/local/bin/uv sync      # adds pytest
sudo -u promoflow -H /usr/local/bin/uv run pytest -m live
```

## 6. Verify without writing to MySQL

```bash
sudo -u promoflow -H /usr/local/bin/uv run python run.py --dry-run
```

This requests a real report and waits for it, which can take a few minutes. The
run should end with `status : SUCCESS` in the summary block. A preview of the
rows it would have written is left in `data/processed/*_promotion_rows.csv`.

## 7. Schedule it

Use **either** systemd (preferred) **or** cron, not both.

The recommended schedule is once a day. Each run re-reads the promotions that
started in the last `PROMOTION_LOOKBACK_DAYS` and replaces their rows, so a
missed day heals itself on the next run. A run normally takes a few minutes, and
most of that is waiting for Amazon. `REPORT_POLL_TIMEOUT_SECONDS` (30 minutes)
caps it.

### Option A: systemd timer

Create `/etc/systemd/system/promoflow.service`:

```ini
[Unit]
Description=PromoFlow - Amazon promotion performance report load
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=promoflow
Group=promoflow
WorkingDirectory=/opt/promoflow
ExecStart=/usr/local/bin/uv run --no-sync python run.py
TimeoutStartSec=3600
Nice=10
# Exit 10 (no promotions in the window) is not a failure.
SuccessExitStatus=10

# Hardening: PromoFlow only writes inside its own directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/opt/promoflow/data /opt/promoflow/logs
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=promoflow
```

Create `/etc/systemd/system/promoflow.timer`:

```ini
[Unit]
Description=Run PromoFlow daily

[Timer]
OnCalendar=*-*-* 06:30:00 UTC
RandomizedDelaySec=300
Persistent=true
Unit=promoflow.service

[Install]
WantedBy=timers.target
```

Enable and check it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now promoflow.timer

systemctl list-timers promoflow.timer
journalctl -u promoflow.service -f
```

### Option B: cron

Create `/etc/cron.d/promoflow` (mode 644):

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
HOME=/home/promoflow
CRON_TZ=UTC

30 6 * * * promoflow cd /opt/promoflow && /usr/local/bin/uv run --no-sync python run.py >> /opt/promoflow/logs/cron-stdout.log 2>&1
```

The `cd` is required, because PromoFlow reads `.env` from the project
directory. The report window is computed in UTC whatever cron's timezone is.

PromoFlow writes and rotates `logs/cron.log` itself. The redirect above
captures only what reaches stdout before logging starts, such as a
configuration error. To keep that file small, create
`/etc/logrotate.d/promoflow`:

```
/opt/promoflow/logs/cron-stdout.log {
    weekly
    rotate 4
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    su promoflow promoflow
}
```

Do not add `cron.log` to this file. PromoFlow rotates that one itself.

### Windows Task Scheduler (local machines)

```powershell
schtasks /Create /TN "PromoFlow" /SC DAILY /ST 12:00 `
  /TR "cmd /c cd /d C:\projects\pomotion_performance_report && uv run --no-sync python run.py"
```

## 8. Storage housekeeping (automatic)

Once the steps above are done, nothing needs manual cleanup:

- **Reports**: each one is deleted as soon as its rows are committed to MySQL. A
  dry run keeps its report in `data/processed/`, and a failed load moves it to
  `data/failed/`. Both are kept for inspection until `FILE_RETENTION_DAYS`.
- **Dry-run previews**: deleted after `FILE_RETENTION_DAYS`.
- **`logs/cron.log`**: rotated at UTC midnight. Files older than
  `LOG_RETENTION_DAYS` are deleted.

## Rebuilding a host

Repeat steps 1 to 3 and restore `.env`. Nothing else on the host is stateful.

---

## Appendix: schema

The column names and order match what the original script's `pandas.to_sql`
produced. PromoFlow reflects the real table at runtime, so treat this as a
reference, not a contract. An existing table with `TEXT`/`BIGINT`/`DOUBLE`
columns works as it is.

```sql
CREATE TABLE `api_promotion_data` (
  `asin` varchar(16) DEFAULT NULL,
  `productName` text,
  `productGlanceViews` bigint DEFAULT NULL,
  `productUnitsSold` bigint DEFAULT NULL,
  `productRevenue` double DEFAULT NULL,
  `productRevenueCurrencyCode` varchar(8) DEFAULT NULL,
  `promotionId` varchar(64) DEFAULT NULL,
  `promotionName` text,
  `marketplaceId` varchar(32) DEFAULT NULL,
  `merchantId` varchar(32) DEFAULT NULL,
  `type` varchar(64) DEFAULT NULL,
  `status` varchar(32) DEFAULT NULL,
  `glanceViews` bigint DEFAULT NULL,
  `unitsSold` bigint DEFAULT NULL,
  `revenue` double DEFAULT NULL,
  `revenueCurrencyCode` varchar(8) DEFAULT NULL,
  `startDateTime` varchar(32) DEFAULT NULL,
  `endDateTime` varchar(32) DEFAULT NULL,
  `createdDateTime` varchar(32) DEFAULT NULL,
  `lastUpdatedDateTime` varchar(32) DEFAULT NULL,
  KEY `ix_api_promotion_data_promotionId` (`promotionId`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

If the table was created by `to_sql`, `promotionId` is `TEXT` and has no index.
The load deletes by `promotionId`, so an index keeps it fast as the table grows:

```sql
ALTER TABLE api_promotion_data ADD INDEX ix_promotionId (promotionId(64));
```
