"""Aligning report rows with the live table."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import BigInteger, Column, DateTime, Double, Integer, MetaData, String, Table, Text

from app.data_transformer import to_records, transform
from app.exceptions import DataTransformationError, EmptyReportError
from app.report_processor import flatten
from app.schema import TableSpec, build_bootstrap_table
from tests.conftest import KEY_COLUMN

BOOTSTRAP = TableSpec.from_table(build_bootstrap_table())


def spec_of(*columns: Column) -> TableSpec:
    return TableSpec.from_table(Table("t", MetaData(), *columns))


class TestAgainstTheBootstrapTable:
    def test_every_report_field_is_written(self, report: dict[str, Any]) -> None:
        result = transform(flatten(report).frame, BOOTSTRAP, key_column=KEY_COLUMN)

        assert result.dropped_columns == ()
        assert result.null_columns == ()
        assert result.row_count == 3

    def test_promotion_ids_are_distinct(self, report: dict[str, Any]) -> None:
        result = transform(flatten(report).frame, BOOTSTRAP, key_column=KEY_COLUMN)

        assert result.promotion_ids == ("PROMO-0001", "PROMO-0002")

    def test_records_are_plain_python(self, report: dict[str, Any]) -> None:
        record = transform(flatten(report).frame, BOOTSTRAP, key_column=KEY_COLUMN).records[0]

        assert record["productUnitsSold"] == 64 and type(record["productUnitsSold"]) is int
        assert record["productRevenue"] == 1599.36 and type(record["productRevenue"]) is float
        assert record["startDateTime"] == "2026-10-01T07:00:00Z"


class TestAlignment:
    def test_fields_without_a_column_are_dropped(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1"], "extra": ["x"]})

        result = transform(frame, spec_of(Column("promotionId", String(64))), key_column=KEY_COLUMN)

        assert result.dropped_columns == ("extra",)
        assert result.records == [{"promotionId": "P1"}]

    def test_columns_without_a_field_are_null(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1"]})
        spec = spec_of(Column("promotionId", String(64)), Column("note", Text))

        result = transform(frame, spec, key_column=KEY_COLUMN)

        assert result.null_columns == ("note",)
        assert result.records == [{"promotionId": "P1", "note": None}]

    def test_generated_key_is_left_out(self) -> None:
        spec = spec_of(
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("promotionId", String(64)),
        )

        assert spec.columns == ("promotionId",)


class TestCoercion:
    def test_integer_column_from_text(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1", "P2"], "units": ["5", "bad"]})
        spec = spec_of(Column("promotionId", String(64)), Column("units", BigInteger))

        records = transform(frame, spec, key_column=KEY_COLUMN).records

        assert [r["units"] for r in records] == [5, None]

    def test_integer_column_rounds(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1"], "units": [4.6]})
        spec = spec_of(Column("promotionId", String(64)), Column("units", Integer))

        assert transform(frame, spec, key_column=KEY_COLUMN).records[0]["units"] == 5

    def test_float_column(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1"], "revenue": ["12.5"]})
        spec = spec_of(Column("promotionId", String(64)), Column("revenue", Double))

        assert transform(frame, spec, key_column=KEY_COLUMN).records[0]["revenue"] == 12.5

    def test_text_column_keeps_whole_numbers_whole(self) -> None:
        """A count stored in a TEXT column reads "64", not "64.0"."""
        frame = pd.DataFrame({"promotionId": ["P1", "P2"], "units": [64, np.nan]})
        spec = spec_of(Column("promotionId", String(64)), Column("units", Text))

        records = transform(frame, spec, key_column=KEY_COLUMN).records

        assert [r["units"] for r in records] == ["64", None]

    def test_datetime_column_is_parsed_as_naive_utc(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1"], "startDateTime": ["2026-10-01T07:00:00Z"]})
        spec = spec_of(Column("promotionId", String(64)), Column("startDateTime", DateTime))

        value = transform(frame, spec, key_column=KEY_COLUMN).records[0]["startDateTime"]

        assert value == datetime(2026, 10, 1, 7, 0, 0)
        assert value.tzinfo is None

    def test_nested_values_become_json_text(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1"], "tags": [["a", "b"]]})
        spec = spec_of(Column("promotionId", String(64)), Column("tags", Text))

        assert transform(frame, spec, key_column=KEY_COLUMN).records[0]["tags"] == '["a", "b"]'


class TestRefusal:
    def test_empty_frame(self) -> None:
        with pytest.raises(EmptyReportError):
            transform(pd.DataFrame(), BOOTSTRAP, key_column=KEY_COLUMN)

    def test_missing_key_field(self) -> None:
        with pytest.raises(DataTransformationError, match="no 'promotionId' field"):
            transform(pd.DataFrame({"asin": ["A"]}), BOOTSTRAP, key_column=KEY_COLUMN)

    def test_key_column_missing_from_table(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1"]})

        with pytest.raises(DataTransformationError, match="has no 'promotionId' column"):
            transform(frame, spec_of(Column("asin", String(16))), key_column=KEY_COLUMN)

    def test_blank_keys(self) -> None:
        frame = pd.DataFrame({"promotionId": ["P1", None, ""]})

        with pytest.raises(DataTransformationError, match="2 row"):
            transform(frame, BOOTSTRAP, key_column=KEY_COLUMN)


class TestToRecords:
    def test_nan_and_na_become_none(self) -> None:
        frame = pd.DataFrame(
            {"a": pd.array([1, None], dtype="Int64"), "b": [1.5, np.nan], "c": ["x", None]}
        )

        assert to_records(frame) == [
            {"a": 1, "b": 1.5, "c": "x"},
            {"a": None, "b": None, "c": None},
        ]

    def test_timestamps_become_datetimes(self) -> None:
        frame = pd.DataFrame({"t": pd.to_datetime(["2026-10-01 07:00:00", None])})

        records = to_records(frame)

        assert records[0]["t"] == datetime(2026, 10, 1, 7) and type(records[0]["t"]) is datetime
        assert records[1]["t"] is None
