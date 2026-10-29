"""The SP-API client against a scripted HTTP session."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

from app.config import Settings
from app.exceptions import (
    AuthenticationError,
    DownloadError,
    EmptyReportError,
    ReportProcessingError,
    ReportRequestError,
)
from app.sp_api import REPORT_TYPE
from app.timeutils import ReportWindow
from tests.conftest import (
    DOCUMENT_URL,
    TOKEN_URL,
    FakeClock,
    FakeHttp,
    FakeResponse,
    make_client,
    script_amazon,
)

WINDOW = ReportWindow(
    start=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc),
    end=datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc),
)


class TestToken:
    def test_refresh_token_grant_is_sent(self, settings: Settings, http: FakeHttp) -> None:
        script_amazon(http, {"promotions": []})

        make_client(settings, http).create_report(WINDOW)

        (_method, _url, kwargs), = http.calls_to("POST", TOKEN_URL)
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert kwargs["data"]["refresh_token"] == "Atzr|testrefresh"
        assert kwargs["data"]["client_id"] == "amzn1.application-oa2-client.test"

    def test_access_token_is_cached_across_calls(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, {"promotions": []}, statuses=("IN_QUEUE", "DONE"))
        client = make_client(settings, http)

        client.create_report(WINDOW)
        client.wait_for_report("R1")

        assert len(http.calls_to("POST", TOKEN_URL)) == 1

    def test_token_goes_in_the_access_token_header(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, {"promotions": []})

        make_client(settings, http).create_report(WINDOW)

        (_m, _u, kwargs), = http.calls_to("POST", "/reports/2021-06-30/reports")
        assert kwargs["headers"]["x-amz-access-token"] == "Atza|access"

    def test_rejected_credentials_are_an_authentication_error(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        http.add(
            "POST",
            TOKEN_URL,
            FakeResponse(400, {"error": "invalid_grant", "error_description": "revoked"}),
        )

        with pytest.raises(AuthenticationError, match="invalid_grant"):
            make_client(settings, http).create_report(WINDOW)
        assert len(http.calls_to("POST", TOKEN_URL)) == 1  # never retried

    def test_lwa_outage_is_retried(self, settings: Settings, http: FakeHttp) -> None:
        script_amazon(http, {"promotions": []})
        http.add(
            "POST",
            TOKEN_URL,
            FakeResponse(503, {"error": "unavailable"}),
            FakeResponse(200, {"access_token": "Atza|access", "expires_in": 3600}),
        )

        assert make_client(settings, http).create_report(WINDOW) == "R1"


class TestCreateReport:
    def test_requests_the_promotion_report_for_the_window(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, {"promotions": []})

        assert make_client(settings, http).create_report(WINDOW) == "R1"

        (_m, url, kwargs), = http.calls_to("POST", "/reports/2021-06-30/reports")
        assert url == "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"
        assert kwargs["json"] == {
            "reportType": REPORT_TYPE,
            "marketplaceIds": ["ATVPDKIKX0DER"],
            "reportOptions": {
                "promotionStartDateFrom": "2026-09-28T10:00:00Z",
                "promotionStartDateTo": "2026-10-28T10:00:00Z",
            },
        }

    def test_throttling_is_retried(self, settings: Settings, http: FakeHttp) -> None:
        script_amazon(http, {"promotions": []})
        http.add(
            "POST",
            "/reports/2021-06-30/reports",
            FakeResponse(429, {"errors": [{"code": "QuotaExceeded", "message": "slow down"}]}),
            FakeResponse(202, {"reportId": "R9"}),
        )

        assert make_client(settings, http).create_report(WINDOW) == "R9"

    def test_a_rejected_request_is_not_retried(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, {"promotions": []})
        http.add(
            "POST",
            "/reports/2021-06-30/reports",
            FakeResponse(400, {"errors": [{"code": "InvalidInput", "message": "bad date"}]}),
        )

        with pytest.raises(ReportRequestError, match="InvalidInput: bad date") as excinfo:
            make_client(settings, http).create_report(WINDOW)
        assert excinfo.value.transient is False
        assert len(http.calls_to("POST", "/reports/2021-06-30/reports")) == 1

    def test_access_denied_is_an_authentication_error(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, {"promotions": []})
        http.add(
            "POST",
            "/reports/2021-06-30/reports",
            FakeResponse(403, {"errors": [{"code": "Unauthorized", "message": "denied"}]}),
        )

        with pytest.raises(AuthenticationError, match="Unauthorized"):
            make_client(settings, http).create_report(WINDOW)

    def test_dropped_connection_is_retried(self, settings: Settings, http: FakeHttp) -> None:
        script_amazon(http, {"promotions": []})
        http.add(
            "POST",
            "/reports/2021-06-30/reports",
            requests.ConnectionError("reset by peer"),
            FakeResponse(202, {"reportId": "R2"}),
        )

        assert make_client(settings, http).create_report(WINDOW) == "R2"


class TestWaitForReport:
    def test_polls_until_done(self, settings: Settings, http: FakeHttp) -> None:
        script_amazon(http, {"promotions": []})
        clock = FakeClock()

        status = make_client(settings, http, clock).wait_for_report("R1")

        assert status.processing_status == "DONE"
        assert status.document_id == "D1"
        assert clock.slept == [30.0, 30.0]

    def test_cancelled_means_no_data(self, settings: Settings, http: FakeHttp) -> None:
        script_amazon(http, None, statuses=("IN_QUEUE", "CANCELLED"))

        with pytest.raises(EmptyReportError, match="cancelled report R1"):
            make_client(settings, http).wait_for_report("R1")

    def test_fatal_stops_and_reports_amazons_reason(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, {"errorDetails": "Date range too large"}, statuses=("FATAL",))

        with pytest.raises(ReportProcessingError, match="Date range too large"):
            make_client(settings, http).wait_for_report("R1")

    def test_fatal_without_a_document_still_fails_cleanly(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, None, statuses=("FATAL",))
        http.add(
            "GET",
            "/reports/2021-06-30/reports/R1",
            FakeResponse(200, {"reportId": "R1", "processingStatus": "FATAL"}),
        )

        with pytest.raises(ReportProcessingError, match=r"\(FATAL\)$"):
            make_client(settings, http).wait_for_report("R1")

    def test_gives_up_after_the_timeout_with_a_resume_hint(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, None, statuses=("IN_PROGRESS",))

        with pytest.raises(ReportProcessingError, match="--report-id R1"):
            make_client(settings, http).wait_for_report("R1")

    def test_a_failed_poll_is_retried_without_losing_the_report(
        self, settings: Settings, http: FakeHttp
    ) -> None:
        script_amazon(http, None)
        http.add(
            "GET",
            "/reports/2021-06-30/reports/R1",
            FakeResponse(500, {"errors": [{"code": "InternalFailure", "message": ""}]}),
            FakeResponse(200, {"reportId": "R1", "processingStatus": "DONE", "reportDocumentId": "D1"}),
        )

        assert make_client(settings, http).wait_for_report("R1").document_id == "D1"
        assert http.calls_to("POST", "/reports/2021-06-30/reports") == []


class TestDownload:
    def test_gzip_document_is_decompressed(
        self, settings: Settings, http: FakeHttp, tmp_path: Path
    ) -> None:
        document = {"promotions": [{"promotionId": "P1"}]}
        script_amazon(http, document, compression="GZIP")

        result = make_client(settings, http).download_document("D1", tmp_path)

        assert json.loads(result.path.read_text()) == document
        assert result.size_bytes == result.path.stat().st_size

    def test_uncompressed_document_is_saved_as_is(
        self, settings: Settings, http: FakeHttp, tmp_path: Path
    ) -> None:
        document = {"promotions": []}
        script_amazon(http, document, compression=None)

        result = make_client(settings, http).download_document("D1", tmp_path)

        assert json.loads(result.path.read_text()) == document

    def test_file_is_named_for_housekeeping(
        self, settings: Settings, http: FakeHttp, tmp_path: Path
    ) -> None:
        script_amazon(http, {"promotions": []})

        result = make_client(settings, http).download_document("D1", tmp_path)

        assert result.path.name.endswith("Z_promotion_report.json")
        assert list(tmp_path.glob("*.part")) == []

    def test_presigned_download_carries_no_api_token(
        self, settings: Settings, http: FakeHttp, tmp_path: Path
    ) -> None:
        script_amazon(http, {"promotions": []})

        make_client(settings, http).download_document("D1", tmp_path)

        (_m, url, kwargs), = http.calls_to("GET", "s3.amazonaws.com")
        assert url == DOCUMENT_URL
        assert "headers" not in kwargs

    def test_expired_url_is_retried_with_a_fresh_one(
        self, settings: Settings, http: FakeHttp, tmp_path: Path
    ) -> None:
        body = json.dumps({"promotions": []}).encode()
        script_amazon(http, None)
        http.add(
            "GET",
            "s3.amazonaws.com",
            FakeResponse(403, content=b"<Error>Request has expired</Error>"),
            FakeResponse(200, content=gzip.compress(body)),
        )

        result = make_client(settings, http).download_document("D1", tmp_path)

        assert json.loads(result.path.read_text()) == {"promotions": []}
        assert len(http.calls_to("GET", "/documents/D1")) == 2

    def test_corrupt_gzip_fails_after_retries(
        self, settings: Settings, http: FakeHttp, tmp_path: Path
    ) -> None:
        script_amazon(http, None)
        http.add("GET", "s3.amazonaws.com", FakeResponse(200, content=b"not gzip at all"))

        with pytest.raises(DownloadError, match="not valid GZIP"):
            make_client(settings, http).download_document("D1", tmp_path)
        assert len(http.calls_to("GET", "s3.amazonaws.com")) == settings.max_retries

    def test_unknown_compression_is_refused(
        self, settings: Settings, http: FakeHttp, tmp_path: Path
    ) -> None:
        script_amazon(http, {"promotions": []}, compression="ZSTD")

        with pytest.raises(DownloadError, match="Unsupported compressionAlgorithm"):
            make_client(settings, http).download_document("D1", tmp_path)


class TestCustomEndpoint:
    def test_endpoint_and_marketplaces_come_from_settings(
        self, env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http: FakeHttp
    ) -> None:
        from app.config import load_settings

        monkeypatch.setenv("SP_API_ENDPOINT", "https://sellingpartnerapi-eu.amazon.com/")
        monkeypatch.setenv("MARKETPLACE_IDS", "A1F83G8C2ARO7P, A1PA6795UKMFR9")
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        settings = load_settings()
        script_amazon(http, {"promotions": []})

        make_client(settings, http).create_report(WINDOW)

        (_m, url, kwargs), = http.calls_to("POST", "/reports/2021-06-30/reports")
        assert url.startswith("https://sellingpartnerapi-eu.amazon.com/reports/")
        assert kwargs["json"]["marketplaceIds"] == ["A1F83G8C2ARO7P", "A1PA6795UKMFR9"]
