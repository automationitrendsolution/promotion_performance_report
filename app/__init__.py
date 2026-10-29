"""PromoFlow -- Amazon promotion performance reports into MySQL.

Requests ``GET_PROMOTION_PERFORMANCE_REPORT`` from the Selling Partner API,
waits for Amazon to build it, downloads and flattens the JSON document with
pandas, and replaces the matching promotions in MySQL.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
