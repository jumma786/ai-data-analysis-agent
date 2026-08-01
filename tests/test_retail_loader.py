"""Unit tests for the Online Retail cleaning logic.

These use tiny hand-built frames to pin down the *transformation rules* (column
aliasing, null handling, cancellation filtering, revenue derivation). They say
nothing about the real dataset -- verifying that requires actually loading the
Excel file, which the integration suite covers.
"""
from __future__ import annotations

import pandas as pd
import pytest

from scripts.load_online_retail import (
    clean_retail, is_cancellation, normalize_columns, to_snake_case)


def _retail_ii_frame() -> pd.DataFrame:
    """Column names as they appear in online_retail_II.xlsx."""
    return pd.DataFrame({
        "Invoice": ["489434", "C489449", "489435", None, "489436"],
        "StockCode": ["85048", "21733", "22178", "22179", None],
        "Description": ["glass", "heart", "candle", "orphan", "orphan"],
        "Quantity": [12, -6, 4, 1, 1],
        "InvoiceDate": ["2009-12-01 07:45:00"] * 5,
        "Price": [6.95, 2.55, 1.25, 1.0, 1.0],
        "Customer ID": [13085.0, 13085.0, 13078.0, None, None],
        "Country": ["United Kingdom", "United Kingdom", "France", "UK", "UK"],
    })


def test_to_snake_case_handles_both_naming_styles():
    assert to_snake_case("Customer ID") == "customer_id"
    assert to_snake_case("InvoiceNo") == "invoice_no"
    assert to_snake_case("StockCode") == "stock_code"


def test_normalize_columns_maps_retail_ii_names():
    out = normalize_columns(_retail_ii_frame())
    assert {"invoice", "stock_code", "price", "customer_id"} <= set(out.columns)


def test_normalize_columns_maps_legacy_online_retail_names():
    legacy = pd.DataFrame({
        "InvoiceNo": ["536365"], "StockCode": ["85123A"], "Description": ["x"],
        "Quantity": [6], "InvoiceDate": ["2010-12-01 08:26:00"],
        "UnitPrice": [2.55], "CustomerID": [17850.0], "Country": ["United Kingdom"],
    })
    out = normalize_columns(legacy)
    assert "invoice" in out.columns          # from InvoiceNo
    assert "price" in out.columns            # from UnitPrice
    assert "customer_id" in out.columns      # from CustomerID


def test_normalize_columns_rejects_unrelated_input():
    with pytest.raises(ValueError, match="missing expected column"):
        normalize_columns(pd.DataFrame({"foo": [1], "bar": [2]}))


def test_is_cancellation_flags_c_prefixed_invoices():
    mask = is_cancellation(pd.Series(["489434", "C489449", "c489450"]))
    assert list(mask) == [False, True, True]


def test_clean_retail_drops_nulls_and_cancellations():
    cleaned = clean_retail(_retail_ii_frame())
    # 5 rows in: 1 cancelled, 1 null invoice, 1 null stock_code -> 2 remain.
    assert list(cleaned["invoice"]) == ["489434", "489435"]


def test_clean_retail_derives_revenue():
    cleaned = clean_retail(_retail_ii_frame())
    assert cleaned.loc[0, "revenue"] == pytest.approx(12 * 6.95)


def test_clean_retail_parses_invoice_date():
    cleaned = clean_retail(_retail_ii_frame())
    assert pd.api.types.is_datetime64_any_dtype(cleaned["invoice_date"])


def test_clean_retail_keeps_negative_quantity_returns():
    """Returns are data, not noise -- only 'C' *cancellations* are dropped."""
    frame = _retail_ii_frame()
    frame.loc[0, "Quantity"] = -3
    cleaned = clean_retail(frame)
    assert cleaned.loc[0, "revenue"] < 0


def test_clean_retail_does_not_mutate_input():
    frame = _retail_ii_frame()
    clean_retail(frame)
    assert len(frame) == 5
    assert "Invoice" in frame.columns
