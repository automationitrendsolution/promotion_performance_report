"""Read-only checks against the configured SP-API app and MySQL server.

Nothing here requests a report, writes, deletes or creates.  The SP-API check
only exchanges the refresh token for an access token.
"""

from __future__ import annotations

import pytest
import requests

from app.config import load_settings
from app.database import PromotionRepository, create_db_engine
from app.sp_api import LwaTokenProvider

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings():
    return load_settings()


@pytest.fixture(scope="module")
def repository(settings):
    repo = PromotionRepository(
        create_db_engine(settings.database),
        table=settings.table,
        key_column=settings.key_column,
    )
    repo.check_connection()
    repo.reflect()
    yield repo
    repo.dispose()


def test_lwa_accepts_the_refresh_token(settings) -> None:
    with requests.Session() as http:
        token = LwaTokenProvider(settings.sp_api, http).token()

    assert token.startswith("Atza|")


def test_server_is_reachable(repository: PromotionRepository) -> None:
    repository.check_connection()


def test_table_has_the_key_column(repository: PromotionRepository, settings) -> None:
    assert settings.key_column in repository.spec().columns


def test_every_report_field_has_a_column(repository: PromotionRepository) -> None:
    """A failure here means report data would silently not be written."""
    from app.schema import PRODUCT_FIELDS, PROMOTION_FIELDS

    missing = sorted(set(PRODUCT_FIELDS + PROMOTION_FIELDS) - set(repository.spec().columns))

    assert missing == [], f"report fields absent from the live table: {missing}"
