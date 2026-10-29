"""Report fields and the shape of the target table.

The live MySQL table is authoritative: :class:`TableSpec` is built by
reflecting it, and the load writes the columns that are really there.  The
bootstrap table below is used only when ``CREATE_TABLES=true`` finds no table
at all, and as a stand-in for a dry run that cannot reach MySQL.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Double,
    Float,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)

DEFAULT_TABLE = "api_promotion_data"
#: Rows are replaced per promotion: every row of a promotion in the report is
#: deleted, then the report's rows for it are inserted.
DEFAULT_KEY_COLUMN = "promotionId"

#: Fields of each ``includedProducts`` entry -- one output row per product.
PRODUCT_FIELDS: tuple[str, ...] = (
    "asin",
    "productName",
    "productGlanceViews",
    "productUnitsSold",
    "productRevenue",
    "productRevenueCurrencyCode",
)

#: Promotion-level fields copied onto every one of its product rows.
PROMOTION_FIELDS: tuple[str, ...] = (
    "promotionId",
    "promotionName",
    "marketplaceId",
    "merchantId",
    "type",
    "status",
    "glanceViews",
    "unitsSold",
    "revenue",
    "revenueCurrencyCode",
    "startDateTime",
    "endDateTime",
    "createdDateTime",
    "lastUpdatedDateTime",
)


@dataclass(frozen=True)
class TableSpec:
    """The writable shape of a table, by column type."""

    name: str
    columns: tuple[str, ...]
    integer: frozenset[str]
    numeric: frozenset[str]
    datetime: frozenset[str]

    @classmethod
    def from_table(cls, table: Table) -> TableSpec:
        """Classify ``table``'s columns, leaving out a generated key."""
        columns: list[str] = []
        integer: set[str] = set()
        numeric: set[str] = set()
        datetime_columns: set[str] = set()
        for column in table.columns:
            if _is_generated_key(table, column):
                continue
            columns.append(column.name)
            if isinstance(column.type, Integer):
                integer.add(column.name)
            # Float/Double stopped subclassing Numeric in SQLAlchemy 2.1.
            elif isinstance(column.type, (Numeric, Float)):
                numeric.add(column.name)
            elif isinstance(column.type, DateTime):
                datetime_columns.add(column.name)
        return cls(
            name=table.name,
            columns=tuple(columns),
            integer=frozenset(integer),
            numeric=frozenset(numeric),
            datetime=frozenset(datetime_columns),
        )


def _is_generated_key(table: Table, column: Column) -> bool:
    """An AUTO_INCREMENT primary key, which the INSERT must leave out."""
    return (
        column.primary_key
        and len(table.primary_key.columns) == 1
        and isinstance(column.type, Integer)
        and column.autoincrement in (True, "auto")
    )


def build_bootstrap_table(
    name: str = DEFAULT_TABLE,
    *,
    key_column: str = DEFAULT_KEY_COLUMN,
    metadata: MetaData | None = None,
) -> Table:
    """The table PromoFlow creates when none exists.

    Column names and order match what the original script's
    ``pandas.to_sql`` produced, so either table works.  Dates stay ISO 8601
    strings exactly as Amazon sends them.
    """
    metadata = metadata if metadata is not None else MetaData()
    columns = [
        Column("asin", String(16)),
        Column("productName", Text),
        Column("productGlanceViews", BigInteger),
        Column("productUnitsSold", BigInteger),
        Column("productRevenue", Double),
        Column("productRevenueCurrencyCode", String(8)),
        Column("promotionId", String(64)),
        Column("promotionName", Text),
        Column("marketplaceId", String(32)),
        Column("merchantId", String(32)),
        Column("type", String(64)),
        Column("status", String(32)),
        Column("glanceViews", BigInteger),
        Column("unitsSold", BigInteger),
        Column("revenue", Double),
        Column("revenueCurrencyCode", String(8)),
        Column("startDateTime", String(32)),
        Column("endDateTime", String(32)),
        Column("createdDateTime", String(32)),
        Column("lastUpdatedDateTime", String(32)),
    ]
    for column in columns:
        if column.name == key_column:
            column.index = True
    return Table(name, metadata, *columns)
