"""Environment parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, load_settings
from app.exceptions import ConfigurationError


class TestRequired:
    @pytest.mark.parametrize(
        "name",
        [
            "SP_API_CLIENT_ID",
            "SP_API_CLIENT_SECRET",
            "SP_API_REFRESH_TOKEN",
            "DB_HOST",
            "DB_NAME",
            "DB_USER",
            "DB_PASSWORD",
        ],
    )
    def test_each_required_variable(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.delenv(name)

        with pytest.raises(ConfigurationError, match=name):
            load_settings()

    def test_blank_counts_as_missing(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_HOST", "   ")

        with pytest.raises(ConfigurationError, match="DB_HOST"):
            load_settings()


class TestDefaults:
    def test_defaults_match_the_original_script(self, env: dict[str, str]) -> None:
        settings = load_settings()

        assert settings.sp_api.endpoint == "https://sellingpartnerapi-na.amazon.com"
        assert settings.marketplace_ids == ("ATVPDKIKX0DER",)
        assert settings.lookback_days == 30
        assert settings.poll_interval_seconds == 30.0
        assert settings.table == "api_promotion_data"
        assert settings.key_column == "promotionId"

    def test_relative_paths_resolve_against_the_project(self, env: dict[str, str]) -> None:
        settings = load_settings()

        assert settings.download_dir == PROJECT_ROOT / "data" / "downloads"
        assert settings.log_dir == PROJECT_ROOT / "logs"


class TestParsing:
    def test_marketplace_list(self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MARKETPLACE_IDS", "A, B ,A,,")

        assert load_settings().marketplace_ids == ("A", "B")

    def test_empty_marketplace_list(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MARKETPLACE_IDS", ",,")

        with pytest.raises(ConfigurationError, match="MARKETPLACE_IDS"):
            load_settings()

    def test_bad_integer(self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROMOTION_LOOKBACK_DAYS", "thirty")

        with pytest.raises(ConfigurationError, match="PROMOTION_LOOKBACK_DAYS"):
            load_settings()

    def test_bad_boolean(self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CREATE_TABLES", "maybe")

        with pytest.raises(ConfigurationError, match="CREATE_TABLES"):
            load_settings()

    def test_endpoint_must_be_https(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SP_API_ENDPOINT", "http://sellingpartnerapi-na.amazon.com")

        with pytest.raises(ConfigurationError, match="https"):
            load_settings()

    def test_timeout_shorter_than_interval(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REPORT_POLL_INTERVAL_SECONDS", "60")
        monkeypatch.setenv("REPORT_POLL_TIMEOUT_SECONDS", "30")

        with pytest.raises(ConfigurationError, match="REPORT_POLL_TIMEOUT_SECONDS"):
            load_settings()

    def test_missing_ssl_ca(self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_SSL_CA", "certs/absent.pem")

        with pytest.raises(ConfigurationError, match="DB_SSL_CA"):
            load_settings()

    def test_ssl_ca_is_passed_to_the_driver(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ca = tmp_path / "ca.pem"
        ca.write_text("pem")
        monkeypatch.setenv("DB_SSL_CA", str(ca))

        assert load_settings().database.connect_args()["ssl_ca"] == str(ca)

    def test_explicit_env_file_must_exist(self, env: dict[str, str], tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="env file not found"):
            load_settings(tmp_path / "absent.env")


class TestSecrecy:
    def test_secrets_are_not_in_repr(self, env: dict[str, str]) -> None:
        text = repr(load_settings())

        assert "s3cret" not in text
        assert "testsecret" not in text
        assert "testrefresh" not in text

    def test_describe_contains_no_secrets(self, env: dict[str, str]) -> None:
        text = str(load_settings().describe())

        assert "s3cret" not in text
        assert "testsecret" not in text
        assert "testrefresh" not in text

    def test_database_password_is_url_encoded(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_PASSWORD", "Team12345!@:/")

        database = load_settings().database

        assert "Team12345%21%40%3A%2F@" in database.url
        assert "Team" not in database.safe_url
