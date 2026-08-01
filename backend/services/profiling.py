"""Data Upload Module — schema/type detection, missing values, duplicates,
basic statistics, per-column profiling. Supports CSV / Excel / Parquet.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd


def load_dataframe(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".csv":
        return pd.read_csv(p)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(p)
    if ext == ".parquet":
        return pd.read_parquet(p)
    raise ValueError(f"Unsupported file type: {ext}")


def profile_dataset(df: pd.DataFrame) -> dict:
    missing = df.isna().sum()
    dup_count = int(df.duplicated().sum())
    columns = []
    for col in df.columns:
        s = df[col]
        columns.append({
            "name": col,
            "dtype": str(s.dtype),
            "missing": int(s.isna().sum()),
            "unique": int(s.nunique(dropna=True)),
            "sample": s.dropna().head(3).tolist(),
        })
    return {
        "rows": int(len(df)),
        "columns_count": int(df.shape[1]),
        "missing_total": int(missing.sum()),
        "missing_detected": bool(missing.sum() > 0),
        "duplicate_records": dup_count,
        "columns": columns,
    }


def clean_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """Basic cleaning: drop exact duplicate rows."""
    return df.drop_duplicates().reset_index(drop=True)
