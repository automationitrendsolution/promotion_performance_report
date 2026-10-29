"""MySQL persistence.

The target table already exists in production, so PromoFlow **reflects** it and
writes what is really there.  It never issues DDL against an existing table;
``CREATE_TABLES=true`` only creates the table when it is entirely absent.

The load is DELETE-then-INSERT for the promotions in the report, committed as
one transaction: the table is never observed with a promotion's rows deleted
but not replaced, and a failed insert rolls the delete back with it.  A delete
is only ever issued alongside rows to insert, so an empty report cannot erase
anything.  Promotions that are not in the report are never touched.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Table, MetaData, create_engine, delete, func, insert, inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import DatabaseConfig, Settings
from .exceptions import DatabaseError
from .logging_config import get_logger
from .schema import TableSpec, build_bootstrap_table

logger = get_logger("database")

#: Rows per INSERT batch: fast, while staying well under a default MySQL
#: ``max_allowed_packet``.
INSERT_CHUNK_SIZE = 500
#: Keys per ``DELETE ... WHERE key IN (...)``.
DELETE_CHUNK_SIZE = 500

#: MySQL errors a second attempt can plausibly get past: the connection could
#: not be made or dropped mid-statement, or the transaction lost a lock race.
#: The driver files these under several unrelated exception classes, so they
#: are recognised by number.
TRANSIENT_MYSQL_ERRNOS = frozenset(
    {
        1205,  # lock wait timeout exceeded
        1213,  # deadlock found
        2003,  # can't connect to server
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection (I/O error)
    }
)


def _database_error(message: str, exc: SQLAlchemyError) -> DatabaseError:
    """Wrap a driver failure, marking it transient when a retry could succeed."""
    errno = getattr(getattr(exc, "orig", None), "errno", None)
    return DatabaseError(
        message,
        transient=bool(
            getattr(exc, "connection_invalidated", False) or errno in TRANSIENT_MYSQL_ERRNOS
        ),
    )


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one load."""

    table: str
    deleted: int
    inserted: int


def create_db_engine(config: DatabaseConfig, *, echo: bool = False) -> Engine:
    """Build a pooled engine.  The URL is never logged with its password."""
    try:
        engine = create_engine(
            config.url,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=config.connect_args(),
        )
    except SQLAlchemyError as exc:
        raise DatabaseError(
            f"Could not create database engine for {config.safe_url}: {exc}"
        ) from exc
    logger.debug("Engine created for %s", config.safe_url)
    return engine


class PromotionRepository:
    """All database access for the promotion table."""

    def __init__(self, engine: Engine, *, table: str, key_column: str) -> None:
        self.engine = engine
        self.table_name = table
        self.key_column = key_column
        self._metadata = MetaData()
        self._table: Table | None = None

    # -- connectivity ------------------------------------------------------

    def check_connection(self) -> None:
        """Fail fast if MySQL is unreachable."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {exc}", exc) from exc
        logger.info("Database connection verified")

    def create_missing_table(self) -> bool:
        """Create the table if it does not exist.  Returns whether it did.

        An existing table is left exactly as it is -- PromoFlow never alters a
        live schema.
        """
        try:
            if inspect(self.engine).has_table(self.table_name):
                return False
            table = build_bootstrap_table(self.table_name, key_column=self.key_column)
            table.metadata.create_all(self.engine, tables=[table])
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not create table {self.table_name!r}: {exc}") from exc
        logger.info("Created missing table %s", self.table_name)
        return True

    # -- reflection --------------------------------------------------------

    def reflect(self) -> None:
        """Load the live table definition.

        Raises:
            DatabaseError: If the table is missing, or lacks the key column the
                load replaces rows by.
        """
        try:
            table = Table(self.table_name, self._metadata, autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Could not reflect table {self.table_name!r}: {exc}. Create it with the DDL "
                "in docs/setup.md, or set CREATE_TABLES=true.",
                exc,
            ) from exc
        if self.key_column not in table.columns:
            raise DatabaseError(
                f"Table {self.table_name!r} has no column {self.key_column!r}. "
                "Set DB_KEY_COLUMN to the column that holds the promotion ID."
            )
        self._table = table
        logger.info("Reflected %s with %d column(s)", self.table_name, len(table.columns))

    @property
    def table(self) -> Table:
        if self._table is None:
            self.reflect()
        assert self._table is not None
        return self._table

    def spec(self) -> TableSpec:
        """Writable shape of the live table."""
        return TableSpec.from_table(self.table)

    # -- transactions ------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN / COMMIT, with ROLLBACK on any exception.

        The connection is closed on every path, including a COMMIT that fails
        because the server went away.
        """
        connection = self.engine.connect()
        try:
            transaction = connection.begin()
            logger.debug("BEGIN")
            try:
                yield connection
            except Exception as exc:
                try:
                    transaction.rollback()
                except SQLAlchemyError as rollback_exc:
                    # A dead connection cannot roll back, but the server drops
                    # the uncommitted work itself. Raise the error that caused it.
                    logger.warning("ROLLBACK failed: %s", rollback_exc)
                logger.error("ROLLBACK after error: %s: %s", type(exc).__name__, exc)
                raise
            transaction.commit()
            logger.debug("COMMIT")
        finally:
            connection.close()

    # -- writes ------------------------------------------------------------

    def replace_promotions(
        self, promotion_ids: Sequence[str], records: Sequence[dict[str, object]]
    ) -> WriteResult:
        """Replace every row of the given promotions, atomically.

        Raises:
            ValueError: If ``records`` is empty -- deleting without replacing is
                never a valid load.
            DatabaseError: On any database failure; the transaction is rolled
                back first, so the existing data survives intact.
        """
        if not records:
            raise ValueError(
                "replace_promotions called with zero records; refusing to delete existing data"
            )

        table = self.table
        key = table.c[self.key_column]
        try:
            with self.transaction() as connection:
                deleted = 0
                for start in range(0, len(promotion_ids), DELETE_CHUNK_SIZE):
                    chunk = list(promotion_ids[start : start + DELETE_CHUNK_SIZE])
                    result = connection.execute(delete(table).where(key.in_(chunk)))
                    deleted += max(result.rowcount, 0)
                logger.info(
                    "Deleted %d existing row(s) for %d promotion(s)", deleted, len(promotion_ids)
                )
                inserted = _chunked_insert(connection, table, records)
        except SQLAlchemyError as exc:
            raise _database_error(f"Load failed and was rolled back: {exc}", exc) from exc

        logger.info(
            "Load committed: %d deleted, %d inserted into %s",
            deleted,
            inserted,
            self.table_name,
        )
        return WriteResult(table=self.table_name, deleted=deleted, inserted=inserted)

    def count_rows(self, promotion_ids: Sequence[str] | None = None) -> int:
        """Row count, optionally for some promotions -- used by tests and checks."""
        statement = select(func.count()).select_from(self.table)
        if promotion_ids is not None:
            statement = statement.where(self.table.c[self.key_column].in_(list(promotion_ids)))
        try:
            with self.engine.connect() as connection:
                return int(connection.execute(statement).scalar_one())
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not count rows in {self.table_name}: {exc}") from exc

    def dispose(self) -> None:
        """Release pooled connections."""
        self.engine.dispose()
        logger.debug("Engine disposed")


def _chunked_insert(
    connection: Connection, table: Table, records: Sequence[dict[str, object]]
) -> int:
    """Insert records in batches; returns the number of rows sent."""
    total = 0
    for start in range(0, len(records), INSERT_CHUNK_SIZE):
        chunk = list(records[start : start + INSERT_CHUNK_SIZE])
        connection.execute(insert(table), chunk)
        total += len(chunk)
        logger.debug("Inserted chunk of %d row(s) into %s", len(chunk), table.name)
    return total


def build_repository(settings: Settings, *, echo: bool = False) -> PromotionRepository:
    """Create a repository, verify connectivity, and reflect the live table."""
    repository = PromotionRepository(
        create_db_engine(settings.database, echo=echo),
        table=settings.table,
        key_column=settings.key_column,
    )
    try:
        repository.check_connection()
        if settings.create_tables:
            repository.create_missing_table()
        repository.reflect()
    except Exception:
        repository.dispose()
        raise
    return repository
