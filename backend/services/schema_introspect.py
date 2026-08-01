"""Schema Agent helper — introspect a SQL database into a compact text schema
that fits an LLM prompt. Uses SQLAlchemy's Inspector, so it works across
PostgreSQL / MySQL / SQL Server / SQLite.
"""
from __future__ import annotations
from sqlalchemy import create_engine, inspect


def introspect_schema(database_url: str, max_tables: int = 40) -> str:
    engine = create_engine(database_url)
    insp = inspect(engine)
    lines: list[str] = []
    for table in insp.get_table_names()[:max_tables]:
        cols = insp.get_columns(table)
        col_str = ", ".join(f"{c['name']} {c['type']}" for c in cols)
        lines.append(f"TABLE {table} ({col_str})")
        for fk in insp.get_foreign_keys(table):
            if fk.get("referred_table"):
                lines.append(
                    f"  FK {table}.{fk['constrained_columns']} -> "
                    f"{fk['referred_table']}.{fk['referred_columns']}")
    return "\n".join(lines) if lines else "(no tables found)"
