"""Parsing and flattening the promotion report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.exceptions import EmptyReportError, ReportParseError
from app.report_processor import flatten, load_report, read_promotion_report
from app.schema import PRODUCT_FIELDS, PROMOTION_FIELDS
from tests.conftest import SAMPLE_REPORT, report_with, write_report


class TestFlatten:
    def test_one_row_per_product(self, report: dict[str, Any]) -> None:
        result = flatten(report)

        assert result.row_count == 3
        assert result.promotion_count == 3
        assert list(result.frame["asin"]) == ["B0EXAMPLE01", "B0EXAMPLE02", "B0EXAMPLE03"]

    def test_promotion_fields_are_copied_onto_each_product(self, report: dict[str, Any]) -> None:
        frame = flatten(report).frame

        assert list(frame["promotionId"]) == ["PROMO-0001", "PROMO-0001", "PROMO-0002"]
        assert list(frame["promotionName"])[:2] == ["Fall Savings 20% Off"] * 2

    def test_columns_match_the_original_script(self, report: dict[str, Any]) -> None:
        """Product fields first, then the promotion meta -- json_normalize's order."""
        frame = flatten(report).frame

        assert list(frame.columns) == list(PRODUCT_FIELDS) + list(PROMOTION_FIELDS)

    def test_promotions_without_products_are_reported(self, report: dict[str, Any]) -> None:
        result = flatten(report)

        assert result.promotions_without_products == ("PROMO-0003",)

    def test_missing_included_products_key_is_tolerated(self, report: dict[str, Any]) -> None:
        del report["promotions"][2]["includedProducts"]

        assert flatten(report).promotions_without_products == ("PROMO-0003",)

    def test_missing_promotion_field_becomes_blank(self, report: dict[str, Any]) -> None:
        for promotion in report["promotions"]:
            promotion.pop("lastUpdatedDateTime")

        frame = flatten(report).frame

        assert frame["lastUpdatedDateTime"].isna().all()

    def test_extra_product_fields_are_kept(self, report: dict[str, Any]) -> None:
        report["promotions"][0]["includedProducts"][0]["newField"] = "x"

        assert "newField" in flatten(report).frame.columns


class TestRejection:
    def test_no_promotions_array(self) -> None:
        with pytest.raises(ReportParseError, match="not a promotion performance report"):
            flatten({"something": []})

    def test_promotions_not_an_array(self) -> None:
        with pytest.raises(ReportParseError, match="not an array"):
            flatten({"promotions": {}})

    def test_malformed_products(self) -> None:
        with pytest.raises(ReportParseError, match="malformed includedProducts"):
            flatten({"promotions": [{"promotionId": "P", "includedProducts": ["oops"]}]})

    def test_zero_promotions_is_empty(self) -> None:
        with pytest.raises(EmptyReportError):
            flatten({"promotions": []})

    def test_promotions_without_any_products_is_empty(self) -> None:
        with pytest.raises(EmptyReportError, match="none has any included products"):
            flatten(report_with({"promotionId": "P", "includedProducts": []}))

    def test_empty_report_is_still_a_parse_error(self) -> None:
        """Callers catching ReportParseError also see an empty report."""
        assert issubclass(EmptyReportError, ReportParseError)


class TestFiles:
    def test_reads_the_sample(self) -> None:
        assert read_promotion_report(SAMPLE_REPORT).row_count == 3

    def test_utf8_bom_is_accepted(self, tmp_path: Path, report: dict[str, Any]) -> None:
        path = write_report(tmp_path / "r.json", report)
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

        assert read_promotion_report(path).row_count == 3

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text("")

        with pytest.raises(ReportParseError, match="is empty"):
            load_report(path)

    def test_not_json(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text("<html>error</html>")

        with pytest.raises(ReportParseError, match="not valid JSON"):
            load_report(path)

    def test_json_array_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text("[]")

        with pytest.raises(ReportParseError, match="not a JSON object"):
            load_report(path)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ReportParseError, match="Could not read"):
            load_report(tmp_path / "absent.json")
