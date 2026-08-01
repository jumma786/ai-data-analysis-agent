"""SQL Validation Agent — blocks destructive/unsafe SQL before execution.

Defense in depth:
  1. Statement-count check (reject stacked queries).
  2. Keyword denylist (DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/GRANT...).
  3. Read-only enforcement (must start with SELECT or WITH).
  4. Comment stripping so keywords can't hide behind `--` / block comments.
This is a guardrail, not a substitute for a least-privilege read-only DB user,
which the deployment docs also require.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

_DENY = {
    "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE",
    "GRANT", "REVOKE", "CREATE", "REPLACE", "MERGE", "CALL",
    "EXEC", "EXECUTE", "ATTACH", "PRAGMA", "COPY", "VACUUM",
}


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)          # line comments
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)  # block comments
    return sql


def validate_sql(sql: str) -> ValidationResult:
    if not sql or not sql.strip():
        return ValidationResult(False, "Empty query.")
    clean = _strip_comments(sql).strip().rstrip(";").strip()

    # Reject stacked statements.
    if ";" in clean:
        return ValidationResult(False, "Multiple statements are not allowed.")

    upper = clean.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return ValidationResult(False, "Only SELECT/WITH read queries are allowed.")

    tokens = set(re.findall(r"[A-Z_]+", upper))
    hit = tokens & _DENY
    if hit:
        return ValidationResult(False, f"Blocked keyword(s): {', '.join(sorted(hit))}")

    return ValidationResult(True)


def enforce_limit(sql: str, max_rows: int) -> str:
    """Append a LIMIT if the query lacks one, to cap result size."""
    if re.search(r"\bLIMIT\b", sql, re.I):
        return sql
    return f"{sql.rstrip().rstrip(';')} LIMIT {max_rows}"
