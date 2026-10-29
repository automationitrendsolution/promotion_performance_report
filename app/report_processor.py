"""Parse the promotion performance report document.

Amazon delivers JSON: a ``promotions`` array, each promotion carrying an
``includedProducts`` array.  The output is one row per promoted product, with
the promotion's own fields copied onto each -- the same shape the original
script's ``pandas.json_normalize`` call produced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .exceptions import EmptyReportError, ReportParseError
from .logging_config import get_logger
from .schema import PROMOTION_FIELDS

logger = get_logger("report")


@dataclass(frozen=True)
class ReadResult:
    """A parsed report."""

    frame: pd.DataFrame
    promotion_count: int
    #: Promotions in the report with no ``includedProducts``.  They produce no
    #: rows, so their existing rows in MySQL are left alone.
    promotions_without_products: tuple[str, ...] = field(default_factory=tuple)

    @property
    def row_count(self) -> int:
        return len(self.frame)


def load_report(path: Path) -> dict[str, Any]:
    """Read the report JSON from disk.

    Raises:
        ReportParseError: If the file is unreadable or is not a JSON object.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ReportParseError(f"Could not read report {path}: {exc}") from exc
    if not text.strip():
        raise ReportParseError(f"Report {path} is empty")
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ReportParseError(f"Report {path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ReportParseError(f"Report {path} is not a JSON object")
    return document


def read_promotion_report(path: Path) -> ReadResult:
    """Load and flatten the report at ``path``."""
    result = flatten(load_report(path))
    logger.info(
        "Parsed %s: %d promotion(s), %d product row(s)",
        path.name,
        result.promotion_count,
        result.row_count,
    )
    return result


def flatten(document: dict[str, Any]) -> ReadResult:
    """One row per promoted product.

    Raises:
        ReportParseError: If the document is not a promotion performance report.
        EmptyReportError: If it contains no promoted products at all.
    """
    promotions = document.get("promotions")
    if promotions is None:
        raise ReportParseError(
            "Report has no 'promotions' array; it is not a promotion performance report"
        )
    if not isinstance(promotions, list):
        raise ReportParseError("Report 'promotions' is not an array")
    if not promotions:
        raise EmptyReportError("Report contains no promotions for the requested window")

    with_products: list[dict[str, Any]] = []
    without_products: list[str] = []
    for index, promotion in enumerate(promotions):
        if not isinstance(promotion, dict):
            raise ReportParseError(f"Promotion #{index} is not an object")
        products = promotion.get("includedProducts") or []
        if not isinstance(products, list) or not all(isinstance(p, dict) for p in products):
            raise ReportParseError(
                f"Promotion {promotion.get('promotionId', f'#{index}')} has a malformed "
                "includedProducts array"
            )
        if products:
            with_products.append({**promotion, "includedProducts": products})
        else:
            without_products.append(str(promotion.get("promotionId", f"#{index}")))

    if without_products:
        logger.warning(
            "%d promotion(s) have no included products and produce no rows: %s",
            len(without_products),
            ", ".join(without_products),
        )
    if not with_products:
        raise EmptyReportError(
            f"Report lists {len(promotions)} promotion(s) but none has any included products"
        )

    try:
        frame = pd.json_normalize(
            with_products,
            record_path="includedProducts",
            meta=list(PROMOTION_FIELDS),
            errors="ignore",
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ReportParseError(f"Could not flatten the report: {exc}") from exc

    return ReadResult(
        frame=frame,
        promotion_count=len(promotions),
        promotions_without_products=tuple(without_products),
    )
