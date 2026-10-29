"""Shared fixtures.

Nothing here touches Amazon or a real MySQL server.  SP-API is replaced by a
scripted HTTP session that the real :class:`~app.sp_api.ReportsClient` talks
to, and the database tests run against in-memory SQLite using the very same
SQLAlchemy table definition, so transaction behaviour is exercised for real.
"""

from __future__ import annotations

import copy
import gzip
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_ROOT, Settings, load_settings
from app.database import PromotionRepository
from app.schema import build_bootstrap_table
from app.sp_api import ReportsClient

TABLE = "api_promotion_data"
KEY_COLUMN = "promotionId"
SAMPLE_REPORT = PROJECT_ROOT / "data" / "samples" / "promotion_report_sample.json"

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
DOCUMENT_URL = "https://tortuga-prod-na.s3.amazonaws.com/doc-1?X-Amz-Signature=s3cr3tsig"

#: Minimum environment a valid Settings needs.
BASE_ENV = {
    "SP_API_CLIENT_ID": "amzn1.application-oa2-client.test",
    "SP_API_CLIENT_SECRET": "amzn1.oa2-cs.v1.testsecret",
    "SP_API_REFRESH_TOKEN": "Atzr|testrefresh",
    "DB_HOST": "localhost",
    "DB_PORT": "3306",
    "DB_NAME": "promoflow_test",
    "DB_USER": "tester",
    "DB_PASSWORD": "s3cret",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from the developer's real environment.

    Both halves matter: PromoFlow variables already exported are removed, and
    ``.env`` loading is disabled outright -- otherwise a populated project
    ``.env`` would silently satisfy the values these tests assert are missing.
    """
    monkeypatch.setattr("app.config.load_dotenv", lambda *_args, **_kwargs: False)
    prefixes = ("SP_API_", "MARKETPLACE_", "PROMOTION_", "REPORT_", "DB_", "DOWNLOAD_",
                "PROCESSED_", "FAILED_", "LOG_", "LOCK_", "MAX_RETRIES", "RETRY_", "CREATE_TABLES",
                "FILE_RETENTION", "KEEP_PROCESSED")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Populate the minimum valid environment and return it for mutation."""
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(BASE_ENV)


@pytest.fixture
def settings(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fully valid settings pointed at a temporary directory tree, never sleeping."""
    for key, name in (
        ("DOWNLOAD_DIR", "downloads"),
        ("PROCESSED_DIR", "processed"),
        ("FAILED_DIR", "failed"),
        ("LOG_DIR", "logs"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "promoflow.lock"))
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("REPORT_POLL_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("REPORT_POLL_TIMEOUT_SECONDS", "300")
    loaded = load_settings(env_file=None)
    loaded.ensure_directories()
    return loaded


@pytest.fixture
def repository() -> Iterator[PromotionRepository]:
    """Repository on in-memory SQLite, with the bootstrap table.

    Reflection, transactions and the DELETE/INSERT sequence are exercised for
    real; only the server behind them differs from production.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    table = build_bootstrap_table(TABLE, key_column=KEY_COLUMN)
    table.metadata.create_all(engine)
    repo = PromotionRepository(engine, table=TABLE, key_column=KEY_COLUMN)
    repo.reflect()
    try:
        yield repo
    finally:
        repo.dispose()


@pytest.fixture
def report() -> dict[str, Any]:
    """The shipped sample report: 3 promotions, 3 product rows, one promotion empty."""
    return json.loads(SAMPLE_REPORT.read_text(encoding="utf-8"))


def write_report(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def report_with(*promotions: dict[str, Any]) -> dict[str, Any]:
    return {"promotions": [copy.deepcopy(p) for p in promotions]}


# -- scripted SP-API -------------------------------------------------------


class FakeResponse:
    """The parts of ``requests.Response`` the client uses."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        content: bytes | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        if content is None:
            content = json.dumps(payload).encode() if payload is not None else b""
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if self._payload is None:
            return json.loads(self.content)
        return self._payload


class FakeHttp:
    """Scripted stand-in for ``requests.Session``.

    Each route is a method plus a URL substring with a queue of responses (or
    exceptions to raise).  The last response repeats once the queue drains.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, list[Any]]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def add(self, method: str, fragment: str, *responses: Any) -> FakeHttp:
        self.routes.insert(0, (method, fragment, list(responses)))
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for route_method, fragment, responses in self.routes:
            if route_method == method and fragment in url and responses:
                item = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(item, BaseException):
                    raise item
                return item
        raise AssertionError(f"unexpected request: {method} {url}")

    def calls_to(self, method: str, fragment: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [c for c in self.calls if c[0] == method and fragment in c[1]]

    def close(self) -> None:
        self.closed = True


def script_amazon(
    http: FakeHttp,
    document: dict[str, Any] | None,
    *,
    statuses: tuple[str, ...] = ("IN_QUEUE", "IN_PROGRESS", "DONE"),
    compression: str | None = "GZIP",
    report_id: str = "R1",
    document_id: str = "D1",
) -> FakeHttp:
    """Script a complete, successful report round trip."""
    body = json.dumps(document or {}).encode()
    http.add("POST", TOKEN_URL, FakeResponse(200, {"access_token": "Atza|access", "expires_in": 3600}))
    http.add("POST", "/reports/2021-06-30/reports", FakeResponse(202, {"reportId": report_id}))
    http.add(
        "GET",
        f"/reports/2021-06-30/reports/{report_id}",
        *[
            FakeResponse(
                200,
                {
                    "reportId": report_id,
                    "processingStatus": status,
                    **({"reportDocumentId": document_id} if status in ("DONE", "FATAL") else {}),
                },
            )
            for status in statuses
        ],
    )
    http.add(
        "GET",
        f"/reports/2021-06-30/documents/{document_id}",
        FakeResponse(
            200,
            {
                "reportDocumentId": document_id,
                "url": DOCUMENT_URL,
                **({"compressionAlgorithm": compression} if compression else {}),
            },
        ),
    )
    http.add(
        "GET",
        "s3.amazonaws.com",
        FakeResponse(200, content=gzip.compress(body) if compression else body),
    )
    return http


class FakeClock:
    """A monotonic clock that advances only when slept on."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(
    settings: Settings, http: FakeHttp, clock: FakeClock | None = None
) -> ReportsClient:
    clock = clock or FakeClock()
    return ReportsClient(settings, http=http, sleep=clock.sleep, clock=clock)


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()
