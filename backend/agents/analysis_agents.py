"""Data Analysis, Visualization, and Insight agents.

These operate on the pandas DataFrame returned by query execution. Chart type
selection is heuristic (deterministic, testable); insight text is LLM-generated
from computed statistics rather than free-form to reduce hallucination.
"""
from __future__ import annotations
import pandas as pd
from backend.services.llm import get_llm


def profile_result(df: pd.DataFrame) -> dict:
    """Compute quick KPIs used both for charts and insight prompts."""
    num = df.select_dtypes("number")
    return {
        "rows": len(df),
        "columns": list(df.columns),
        "numeric_columns": list(num.columns),
        "summary": num.describe().to_dict() if not num.empty else {},
    }


def choose_chart(df: pd.DataFrame) -> str:
    """Deterministic chart-type heuristic."""
    num = df.select_dtypes("number").columns
    cat = df.select_dtypes(exclude="number").columns
    has_time = any(
        pd.api.types.is_datetime64_any_dtype(df[c]) or
        ("date" in c.lower() or "month" in c.lower() or "year" in c.lower())
        for c in df.columns
    )
    if has_time and len(num) >= 1:
        return "line"
    if len(cat) >= 1 and len(num) >= 1:
        return "bar" if df[cat[0]].nunique() > 6 else "pie"
    if len(num) >= 2:
        return "scatter"
    if len(num) == 1:
        return "histogram"
    return "table"


def build_figure(df: pd.DataFrame, chart: str):
    """Return a Plotly figure (imported lazily so core stays light)."""
    import plotly.express as px
    num = df.select_dtypes("number").columns.tolist()
    cat = df.select_dtypes(exclude="number").columns.tolist()
    if chart == "line" and num:
        x = cat[0] if cat else df.columns[0]
        return px.line(df, x=x, y=num[0])
    if chart == "bar" and cat and num:
        return px.bar(df, x=cat[0], y=num[0])
    if chart == "pie" and cat and num:
        return px.pie(df, names=cat[0], values=num[0])
    if chart == "scatter" and len(num) >= 2:
        return px.scatter(df, x=num[0], y=num[1])
    if chart == "histogram" and num:
        return px.histogram(df, x=num[0])
    return None


def generate_insight(question: str, df: pd.DataFrame) -> str:
    """Grounded insight: feed the model computed stats, not raw rows."""
    stats = profile_result(df)
    sample = df.head(20).to_csv(index=False)
    system = ("You are a business analyst. Given a question and a data sample "
              "with summary statistics, explain the key finding in 2-4 sentences. "
              "Only state what the numbers support; do not invent figures.")
    user = f"QUESTION: {question}\nSTATS: {stats}\nSAMPLE:\n{sample}"
    return get_llm().complete(system, user)
