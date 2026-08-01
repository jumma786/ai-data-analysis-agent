"""Load the UCI *Online Retail II* dataset into a SQL table.

The dataset is not bundled with this repo (it is ~45 MB). Download it yourself:

    https://archive.ics.uci.edu/dataset/502/online+retail+ii
    -> online_retail_II.xlsx

Then point this script at the file:

    python scripts/load_online_retail.py /path/to/online_retail_II.xlsx

The 2009-2011 workbook has two sheets ("Year 2009-2010", "Year 2010-2011"); both
are read and concatenated by default. The older single-sheet "Online Retail"
file uses different column names (InvoiceNo / UnitPrice / CustomerID) and is
handled by the same normalization step.

Cleaning applied (each step is logged with the row count it removed):
  1. Normalize column names to snake_case.
  2. Drop rows with a null Invoice or StockCode.
  3. Drop cancelled invoices -- invoice numbers starting with "C".
  4. Derive `revenue` = quantity * price.

Note that steps 2-4 are the only transformations: no rows are synthesized and
returns with negative quantities are kept, so `revenue` can be negative. Filter
those out in your query if you want gross rather than net revenue.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.utils.config import get_settings          # noqa: E402
from backend.utils.logging_config import logger        # noqa: E402

DEFAULT_TABLE = "online_retail"

# Source column name (after snake_casing) -> canonical name. Covers both the
# "Online Retail II" and the original "Online Retail" naming.
_COLUMN_ALIASES = {
    "invoice": "invoice",
    "invoice_no": "invoice",
    "stock_code": "stock_code",
    "description": "description",
    "quantity": "quantity",
    "invoice_date": "invoice_date",
    "price": "price",
    "unit_price": "price",
    "customer_id": "customer_id",
    "country": "country",
}

REQUIRED_COLUMNS = ("invoice", "stock_code", "quantity", "invoice_date",
                    "price", "country")


def to_snake_case(name: str) -> str:
    """"Customer ID" -> "customer_id", "InvoiceNo" -> "invoice_no"."""
    name = name.strip().replace(" ", "_")
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"__+", "_", name).lower()


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename source columns to the canonical schema used by the table."""
    renamed = {c: _COLUMN_ALIASES.get(to_snake_case(c), to_snake_case(c))
               for c in df.columns}
    out = df.rename(columns=renamed)
    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(
            f"Input is missing expected column(s) {missing}. "
            f"Found: {sorted(out.columns)}"
        )
    return out


def is_cancellation(invoice: pd.Series) -> pd.Series:
    """Boolean mask of cancelled invoices (UCI marks these with a leading 'C')."""
    return invoice.astype("string").str.strip().str.upper().str.startswith("C")


def clean_retail(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the documented cleaning steps and derive `revenue`.

    Pure function over the DataFrame so it can be unit-tested without an Excel
    file or a database.
    """
    out = normalize_columns(df)
    start = len(out)

    out = out.dropna(subset=["invoice", "stock_code"])
    logger.info("Dropped %s row(s) with a null invoice/stock_code.",
                start - len(out))

    before_cancel = len(out)
    out = out[~is_cancellation(out["invoice"])]
    logger.info("Dropped %s cancelled invoice row(s).", before_cancel - len(out))

    out = out.copy()
    out["invoice"] = out["invoice"].astype("string").str.strip()
    out["stock_code"] = out["stock_code"].astype("string").str.strip()
    out["invoice_date"] = pd.to_datetime(out["invoice_date"], errors="coerce")
    out["quantity"] = pd.to_numeric(out["quantity"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out["revenue"] = out["quantity"] * out["price"]

    return out.reset_index(drop=True)


def read_workbook(path: Path, sheet: str | None = None) -> pd.DataFrame:
    """Read one sheet, or concatenate every sheet when `sheet` is None."""
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    logger.info("Reading %s (this takes a minute for the full workbook)...", path)
    frames = pd.read_excel(path, sheet_name=sheet)
    if isinstance(frames, dict):
        logger.info("Found %s sheet(s): %s", len(frames), list(frames))
        return pd.concat(frames.values(), ignore_index=True)
    return frames


def load_to_table(df: pd.DataFrame, engine: Engine, table: str,
                  if_exists: str = "replace", chunksize: int = 10_000) -> int:
    """Write the DataFrame, then read the row count back from the database."""
    df.to_sql(table, engine, if_exists=if_exists, index=False,
              chunksize=chunksize, method="multi")
    with engine.connect() as conn:
        return int(conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("excel_path", type=Path,
                        help="Path to online_retail_II.xlsx (or Online Retail.xlsx)")
    parser.add_argument("--database-url", default=None,
                        help="SQLAlchemy URL. Defaults to DATABASE_URL from settings.")
    parser.add_argument("--table", default=DEFAULT_TABLE,
                        help=f"Destination table (default: {DEFAULT_TABLE})")
    parser.add_argument("--sheet", default=None,
                        help="Read a single sheet instead of concatenating all")
    parser.add_argument("--if-exists", default="replace",
                        choices=["fail", "replace", "append"],
                        help="Behaviour when the table already exists")
    parser.add_argument("--chunksize", type=int, default=10_000,
                        help="Rows per INSERT batch")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.getLogger("ai_agent").setLevel(logging.INFO)

    database_url = args.database_url or get_settings().database_url
    raw = read_workbook(args.excel_path, args.sheet)
    logger.info("Read %s raw row(s).", len(raw))

    cleaned = clean_retail(raw)
    logger.info("%s row(s) remain after cleaning.", len(cleaned))

    engine = create_engine(database_url)
    loaded = load_to_table(cleaned, engine, args.table,
                           if_exists=args.if_exists, chunksize=args.chunksize)
    print(f"Loaded {loaded} rows into table '{args.table}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
