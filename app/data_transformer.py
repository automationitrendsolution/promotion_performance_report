"""Align report rows with the live table.

The table is authoritative.  Report columns the table does not have are
dropped, table columns the report does not have are written as NULL, and each
value is coerced to its column's type -- numbers for numeric columns,
``datetime`` for DATETIME columns, text otherwise.  Both kinds of drift are
logged, so a schema change is visible rather than silent.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .exceptions import DataTransformationError, EmptyReportError
from .logging_config import get_logger
from .schema import TableSpec

logger = get_logger("transform")


@dataclass(frozen=True)
class TransformResult:
    """Rows ready to insert, plus what the alignment had to change."""

    frame: pd.DataFrame
    records: list[dict[str, Any]]
    #: Distinct key values in the report -- the rows the load replaces.
    promotion_ids: tuple[str, ...]
    dropped_columns: tuple[str, ...]
    null_columns: tuple[str, ...]

    @property
    def row_count(self) -> int:
        return len(self.records)


def transform(frame: pd.DataFrame, spec: TableSpec, *, key_column: str) -> TransformResult:
    """Shape ``frame`` to ``spec``.

    Raises:
        EmptyReportError: If there are no rows.
        DataTransformationError: If the key column is absent or has blanks --
            a row without a key could never be replaced by a later run.
    """
    if frame.empty:
        raise EmptyReportError("No rows to load; nothing will be deleted or inserted")
    if key_column not in frame.columns:
        raise DataTransformationError(
            f"Report has no {key_column!r} field to key the load on"
        )
    if key_column not in spec.columns:
        raise DataTransformationError(f"Table {spec.name!r} has no {key_column!r} column")

    keys = frame[key_column].map(_to_text)
    blank = int(keys.isna().sum() + (keys == "").sum())
    if blank:
        raise DataTransformationError(f"{blank} row(s) have no {key_column}")

    dropped = tuple(column for column in frame.columns if column not in spec.columns)
    null_columns = tuple(column for column in spec.columns if column not in frame.columns)
    if dropped:
        logger.warning(
            "%d report field(s) have no column in %s and will not be written: %s",
            len(dropped),
            spec.name,
            ", ".join(dropped),
        )
    if null_columns:
        logger.info(
            "%d column(s) of %s are not in the report and will be NULL: %s",
            len(null_columns),
            spec.name,
            ", ".join(null_columns),
        )

    aligned = pd.DataFrame(index=frame.index)
    for column in spec.columns:
        if column not in frame.columns:
            aligned[column] = None
            continue
        aligned[column] = _coerce(frame[column], column, spec)

    records = to_records(aligned)
    promotion_ids = tuple(sorted(set(keys)))
    logger.info(
        "Prepared %d row(s) across %d promotion(s) for %s",
        len(records),
        len(promotion_ids),
        spec.name,
    )
    return TransformResult(
        frame=aligned,
        records=records,
        promotion_ids=promotion_ids,
        dropped_columns=dropped,
        null_columns=null_columns,
    )


def _coerce(series: pd.Series, column: str, spec: TableSpec) -> pd.Series:
    if column in spec.integer or column in spec.numeric:
        numeric = pd.to_numeric(series, errors="coerce")
        _warn_on_lost_values(series, numeric, column)
        if column in spec.integer:
            return numeric.round().astype("Int64")
        return numeric.astype(float)
    if column in spec.datetime:
        # Amazon sends ISO 8601 in UTC ("2026-10-01T07:00:00Z"). MySQL DATETIME
        # has no zone, so store naive UTC.
        parsed = pd.to_datetime(series, errors="coerce", utc=True).dt.tz_convert(None)
        _warn_on_lost_values(series, parsed, column)
        return parsed
    return series.map(_to_text)


def _warn_on_lost_values(before: pd.Series, after: pd.Series, column: str) -> None:
    lost = int((before.notna() & after.isna()).sum())
    if lost:
        logger.warning("%d value(s) in %s could not be converted and will be NULL", lost, column)


def _to_text(value: Any) -> str | None:
    """Text as the column should hold it: whole floats lose their ``.0``."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Plain-Python dicts: the MySQL driver rejects numpy scalars and NaN."""
    columns = list(frame.columns)
    return [
        {column: _native(value) for column, value in zip(columns, row)}
        for row in frame.itertuples(index=False, name=None)
    ]


def _native(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value
