"""Transactions, reflection, and the no-delete-without-insert guarantee.

Runs against in-memory SQLite using the bootstrap table, so the reflection,
DELETE/INSERT sequencing and ROLLBACK behaviour under test are the production
code paths.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app import database
from app.database import PromotionRepository, _database_error
from app.exceptions import DatabaseError
from tests.conftest import KEY_COLUMN, TABLE


def record(promotion_id: str, asin: str = "B0", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "promotionId": promotion_id,
        "asin": asin,
        "productUnitsSold": 1,
        "productRevenue": 9.99,
    }
    row.update(overrides)
    return row


def bare_repository(**kwargs: Any) -> PromotionRepository:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    defaults: dict[str, Any] = {"table": TABLE, "key_column": KEY_COLUMN}
    defaults.update(kwargs)
    return PromotionRepository(engine, **defaults)


def asins(repository: PromotionRepository) -> list[tuple[str, str]]:
    table = repository.table
    with repository.engine.connect() as connection:
        return sorted(
            connection.execute(select(table.c.promotionId, table.c.asin)).all()
        )


class TestReflection:
    def test_reflects_the_live_column_set(self, repository: PromotionRepository) -> None:
        columns = repository.spec().columns

        assert KEY_COLUMN in columns
        assert "asin" in columns

    def test_missing_table_is_reported_clearly(self) -> None:
        with pytest.raises(DatabaseError, match="Could not reflect"):
            bare_repository().reflect()

    def test_missing_key_column_is_rejected(self) -> None:
        repo = bare_repository()
        Table(TABLE, MetaData(), Column("asin", String(16))).metadata.create_all(repo.engine)

        with pytest.raises(DatabaseError, match="DB_KEY_COLUMN"):
            repo.reflect()

    def test_auto_increment_id_is_not_written(self) -> None:
        repo = bare_repository()
        Table(
            TABLE,
            MetaData(),
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("promotionId", String(64)),
        ).metadata.create_all(repo.engine)
        repo.reflect()

        assert repo.spec().columns == ("promotionId",)


class TestCreateMissingTable:
    def test_creates_an_absent_table(self) -> None:
        repo = bare_repository()

        assert repo.create_missing_table() is True
        repo.reflect()
        assert "productRevenue" in repo.spec().columns

    def test_leaves_an_existing_table_alone(self, repository: PromotionRepository) -> None:
        assert repository.create_missing_table() is False


class TestReplacePromotions:
    def test_inserts_into_an_empty_table(self, repository: PromotionRepository) -> None:
        result = repository.replace_promotions(["P1"], [record("P1", "A"), record("P1", "B")])

        assert (result.deleted, result.inserted) == (0, 2)
        assert repository.count_rows() == 2

    def test_replaces_the_promotions_in_the_report(self, repository: PromotionRepository) -> None:
        repository.replace_promotions(["P1"], [record("P1", "OLD1"), record("P1", "OLD2")])

        result = repository.replace_promotions(["P1"], [record("P1", "NEW")])

        assert (result.deleted, result.inserted) == (2, 1)
        assert asins(repository) == [("P1", "NEW")]

    def test_other_promotions_are_untouched(self, repository: PromotionRepository) -> None:
        repository.replace_promotions(["P1", "P2"], [record("P1", "A"), record("P2", "B")])

        repository.replace_promotions(["P1"], [record("P1", "A2")])

        assert asins(repository) == [("P1", "A2"), ("P2", "B")]

    def test_a_single_promotion_works(self, repository: PromotionRepository) -> None:
        """The original script built ``IN ('X',)`` here, which is invalid SQL."""
        repository.replace_promotions(["P1"], [record("P1")])

        assert repository.replace_promotions(["P1"], [record("P1")]).deleted == 1

    def test_is_idempotent(self, repository: PromotionRepository) -> None:
        rows = [record("P1", "A"), record("P2", "B")]

        repository.replace_promotions(["P1", "P2"], rows)
        repository.replace_promotions(["P1", "P2"], rows)

        assert repository.count_rows() == 2

    def test_many_promotions_are_deleted_in_chunks(
        self, repository: PromotionRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(database, "DELETE_CHUNK_SIZE", 2)
        monkeypatch.setattr(database, "INSERT_CHUNK_SIZE", 2)
        ids = [f"P{i}" for i in range(5)]
        repository.replace_promotions(ids, [record(i) for i in ids])

        result = repository.replace_promotions(ids, [record(i) for i in ids])

        assert (result.deleted, result.inserted) == (5, 5)

    def test_refuses_to_delete_without_inserting(self, repository: PromotionRepository) -> None:
        repository.replace_promotions(["P1"], [record("P1")])

        with pytest.raises(ValueError, match="zero records"):
            repository.replace_promotions(["P1"], [])
        assert repository.count_rows() == 1

    def test_failed_insert_rolls_the_delete_back(self, repository: PromotionRepository) -> None:
        repository.replace_promotions(["P1"], [record("P1", "KEEP")])

        with pytest.raises(DatabaseError, match="rolled back"):
            # The driver cannot bind an arbitrary object, so the INSERT fails
            # after the DELETE has already run inside the transaction.
            repository.replace_promotions(["P1"], [record("P1", "NEW", productUnitsSold=object())])

        assert asins(repository) == [("P1", "KEEP")]

    def test_count_rows_for_some_promotions(self, repository: PromotionRepository) -> None:
        repository.replace_promotions(["P1", "P2"], [record("P1"), record("P1"), record("P2")])

        assert repository.count_rows(["P1"]) == 2


class TestErrorClassification:
    @staticmethod
    def driver_error(errno: int | None) -> OperationalError:
        orig = Exception("driver")
        orig.errno = errno  # type: ignore[attr-defined]
        return OperationalError("SELECT 1", {}, orig)

    def test_lost_connection_is_transient(self) -> None:
        assert _database_error("x", self.driver_error(2013)).transient is True

    def test_deadlock_is_transient(self) -> None:
        assert _database_error("x", self.driver_error(1213)).transient is True

    def test_unknown_column_is_not(self) -> None:
        assert _database_error("x", self.driver_error(1054)).transient is False

    def test_unreachable_server_is_transient(self) -> None:
        repo = bare_repository()
        repo.engine = create_engine("mysql+mysqlconnector://u:p@127.0.0.1:1/db", connect_args={"connection_timeout": 1})

        with pytest.raises(DatabaseError, match="connection failed") as excinfo:
            repo.check_connection()
        assert excinfo.value.transient is True
