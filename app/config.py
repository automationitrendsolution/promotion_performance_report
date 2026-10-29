"""Environment-driven configuration.

Nothing here reads a credential from source; every secret arrives through the
process environment (populated from ``.env`` locally, or from systemd's
``EnvironmentFile=`` / AWS Secrets Manager in production).  Validation is strict
and fails fast: a run with a bad DSN should never get as far as asking Amazon
for a report.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

from .exceptions import ConfigurationError
from .schema import DEFAULT_KEY_COLUMN, DEFAULT_TABLE

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SP_API_ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
DEFAULT_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
#: Amazon.com (US).
DEFAULT_MARKETPLACE_IDS: tuple[str, ...] = ("ATVPDKIKX0DER",)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{name} must be a boolean (true/false), got {raw!r}"
    )


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_path(name: str, default: Path) -> Path:
    raw = _get(name)
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _get_https_url(name: str, default: str) -> str:
    url = (_get(name, default) or default).rstrip("/")
    if not url.startswith("https://"):
        raise ConfigurationError(f"{name} must be an https URL, got {url!r}")
    return url


def _get_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _get(name)
    if raw is None:
        return default
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not values:
        raise ConfigurationError(f"{name} must list at least one value")
    return values


@dataclass(frozen=True)
class SpApiConfig:
    """Login with Amazon (LWA) credentials and the regional SP-API endpoint.

    No AWS access key or IAM role is needed: SP-API stopped requiring AWS
    Signature Version 4 in October 2023.
    """

    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    endpoint: str = DEFAULT_SP_API_ENDPOINT
    token_url: str = DEFAULT_LWA_TOKEN_URL
    timeout_seconds: int = 60


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 30
    #: Path to a CA bundle when the server requires TLS (e.g. isrgrootx1.pem).
    ssl_ca: Path | None = None
    #: Verify the server certificate against the CA.  Leave off only when the
    #: server presents a certificate that cannot be validated.
    ssl_verify_cert: bool = False
    #: Disable TLS entirely.  Only for a server on a trusted private network.
    ssl_disabled: bool = False

    def connect_args(self) -> dict[str, object]:
        """Driver kwargs for mysql-connector-python."""
        args: dict[str, object] = {"connection_timeout": self.connect_timeout_seconds}
        if self.ssl_disabled:
            args["ssl_disabled"] = True
            return args
        if self.ssl_ca is not None:
            args["ssl_ca"] = str(self.ssl_ca)
            args["ssl_verify_cert"] = self.ssl_verify_cert
        return args

    @property
    def url(self) -> str:
        """SQLAlchemy URL.  Credentials are percent-encoded, never logged."""
        return (
            f"mysql+mysqlconnector://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def safe_url(self) -> str:
        """Same URL with the password redacted -- this is the loggable form."""
        return (
            f"mysql+mysqlconnector://{self.user}:***@{self.host}:{self.port}/{self.name}"
        )

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.safe_url


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    sp_api: SpApiConfig
    database: DatabaseConfig

    marketplace_ids: tuple[str, ...]
    #: Default report window: promotions that started in the last N days.
    lookback_days: int
    poll_interval_seconds: float
    poll_timeout_seconds: float

    table: str
    key_column: str
    create_tables: bool

    download_dir: Path
    processed_dir: Path
    #: Reports that downloaded but could not be loaded, kept for inspection.
    failed_dir: Path
    log_dir: Path
    lock_file: Path

    max_retries: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float

    retention_days: int
    log_level: str
    log_retention_days: int
    keep_processed_report: bool = False

    def ensure_directories(self) -> None:
        """Create every directory PromoFlow writes to.  Idempotent."""
        for path in (
            self.download_dir,
            self.processed_dir,
            self.failed_dir,
            self.log_dir,
            self.lock_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """Loggable snapshot of the configuration.  Contains no secrets."""
        return {
            "sp_api_endpoint": self.sp_api.endpoint,
            "lwa_client_id": self.sp_api.client_id,
            "credentials": "configured",
            "marketplace_ids": ", ".join(self.marketplace_ids),
            "lookback_days": self.lookback_days,
            "poll_interval_s": self.poll_interval_seconds,
            "poll_timeout_s": self.poll_timeout_seconds,
            "database": self.database.safe_url,
            "table": self.table,
            "key_column": self.key_column,
            "download_dir": str(self.download_dir),
            "failed_dir": str(self.failed_dir),
            "log_dir": str(self.log_dir),
            "log_retention_days": self.log_retention_days,
            "keep_processed": self.keep_processed_report,
            "max_retries": self.max_retries,
        }


def load_settings(env_file: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read, validate and return the runtime configuration.

    Args:
        env_file: Optional explicit ``.env`` path.  Defaults to ``.env`` beside
            the project root when it exists.
        override: Whether ``.env`` values replace already-exported variables.
            Off by default so that real environment variables (EC2/systemd) win.

    Raises:
        ConfigurationError: If a required value is missing or malformed.
    """
    candidate = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=override)
    elif env_file is not None:
        raise ConfigurationError(f"env file not found: {candidate}")

    data_dir = PROJECT_ROOT / "data"

    sp_api = SpApiConfig(
        client_id=_require("SP_API_CLIENT_ID"),
        client_secret=_require("SP_API_CLIENT_SECRET"),
        refresh_token=_require("SP_API_REFRESH_TOKEN"),
        endpoint=_get_https_url("SP_API_ENDPOINT", DEFAULT_SP_API_ENDPOINT),
        token_url=_get_https_url("SP_API_TOKEN_URL", DEFAULT_LWA_TOKEN_URL),
        timeout_seconds=_get_int("SP_API_TIMEOUT_SECONDS", 60, minimum=1),
    )

    ssl_ca_raw = _get("DB_SSL_CA")
    ssl_ca = None
    if ssl_ca_raw:
        ssl_ca = Path(ssl_ca_raw).expanduser()
        if not ssl_ca.is_absolute():
            ssl_ca = (PROJECT_ROOT / ssl_ca).resolve()
        if not ssl_ca.is_file():
            raise ConfigurationError(f"DB_SSL_CA file not found: {ssl_ca}")

    database = DatabaseConfig(
        host=_require("DB_HOST"),
        port=_get_int("DB_PORT", 3306, minimum=1),
        name=_require("DB_NAME"),
        user=_require("DB_USER"),
        password=_require("DB_PASSWORD"),
        pool_recycle_seconds=_get_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60),
        connect_timeout_seconds=_get_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        ssl_ca=ssl_ca,
        ssl_verify_cert=_get_bool("DB_SSL_VERIFY_CERT", False),
        ssl_disabled=_get_bool("DB_SSL_DISABLED", False),
    )

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL must be a standard level, got {log_level!r}")

    settings = Settings(
        sp_api=sp_api,
        database=database,
        marketplace_ids=_get_list("MARKETPLACE_IDS", DEFAULT_MARKETPLACE_IDS),
        lookback_days=_get_int("PROMOTION_LOOKBACK_DAYS", 30, minimum=1),
        poll_interval_seconds=_get_float("REPORT_POLL_INTERVAL_SECONDS", 30.0, minimum=1.0),
        poll_timeout_seconds=_get_float("REPORT_POLL_TIMEOUT_SECONDS", 1800.0, minimum=1.0),
        table=_get("DB_TABLE", DEFAULT_TABLE) or DEFAULT_TABLE,
        key_column=_get("DB_KEY_COLUMN", DEFAULT_KEY_COLUMN) or DEFAULT_KEY_COLUMN,
        create_tables=_get_bool("CREATE_TABLES", False),
        download_dir=_get_path("DOWNLOAD_DIR", data_dir / "downloads"),
        processed_dir=_get_path("PROCESSED_DIR", data_dir / "processed"),
        failed_dir=_get_path("FAILED_DIR", data_dir / "failed"),
        log_dir=_get_path("LOG_DIR", PROJECT_ROOT / "logs"),
        lock_file=_get_path("LOCK_FILE", data_dir / "promoflow.lock"),
        max_retries=_get_int("MAX_RETRIES", 3, minimum=1),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 5.0, minimum=0.0),
        retry_backoff_max_seconds=_get_float("RETRY_BACKOFF_MAX_SECONDS", 120.0, minimum=0.0),
        retention_days=_get_int("FILE_RETENTION_DAYS", 30, minimum=1),
        log_level=log_level,
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 5, minimum=1),
        keep_processed_report=_get_bool("KEEP_PROCESSED_REPORT", False),
    )

    if settings.poll_timeout_seconds < settings.poll_interval_seconds:
        raise ConfigurationError(
            "REPORT_POLL_TIMEOUT_SECONDS must be at least REPORT_POLL_INTERVAL_SECONDS"
        )
    return settings
