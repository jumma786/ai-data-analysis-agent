"""LangGraph multi-agent workflow.

Flow:
  schema -> plan -> generate_sql -> validate_sql -> execute -> analyze -> insight

If validation fails, the graph short-circuits to an error node so unsafe SQL is
never executed. State is a TypedDict passed between nodes.

Falls back to a plain sequential runner if langgraph isn't installed, so the
pipeline is testable without the heavy dependency.
"""
from __future__ import annotations
from typing import TypedDict, Optional
import pandas as pd
from sqlalchemy import create_engine, text

from backend.agents.sql_agent import generate_sql
from backend.agents.sql_validation import validate_sql, enforce_limit
from backend.agents.analysis_agents import choose_chart, generate_insight
from backend.utils.config import get_settings
from backend.utils.logging_config import logger


class AgentState(TypedDict, total=False):
    question: str
    schema: str
    history: str
    sql: str
    valid: bool
    error: str
    df: object          # pandas DataFrame
    chart: str
    insight: str


def node_generate_sql(state: AgentState) -> AgentState:
    state["sql"] = generate_sql(state["question"], state.get("schema", ""),
                                state.get("history", ""))
    logger.info("Generated SQL: %s", state["sql"])
    return state


def node_validate(state: AgentState) -> AgentState:
    res = validate_sql(state.get("sql", ""))
    state["valid"] = res.ok
    if not res.ok:
        state["error"] = res.reason
    return state


def node_execute(state: AgentState) -> AgentState:
    s = get_settings()
    sql = enforce_limit(state["sql"], s.max_result_rows)
    engine = create_engine(s.database_url)
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn)
    state["df"] = df
    return state


def node_analyze(state: AgentState) -> AgentState:
    df = state["df"]
    state["chart"] = choose_chart(df)
    return state


def node_insight(state: AgentState) -> AgentState:
    state["insight"] = generate_insight(state["question"], state["df"])
    return state


def build_graph():
    """Build a compiled LangGraph if available, else return None."""
    try:
        from langgraph.graph import StateGraph, END
    except ImportError:
        return None

    g = StateGraph(AgentState)
    g.add_node("generate_sql", node_generate_sql)
    g.add_node("validate", node_validate)
    g.add_node("execute", node_execute)
    g.add_node("analyze", node_analyze)
    g.add_node("insight", node_insight)

    g.set_entry_point("generate_sql")
    g.add_edge("generate_sql", "validate")
    g.add_conditional_edges(
        "validate",
        lambda s: "execute" if s.get("valid") else END,
        {"execute": "execute", END: END},
    )
    g.add_edge("execute", "analyze")
    g.add_edge("analyze", "insight")
    g.add_edge("insight", END)
    return g.compile()


def run_pipeline(question: str, schema: str = "", history: str = "") -> AgentState:
    """Provider-agnostic entry point. Uses LangGraph if present, else sequential."""
    state: AgentState = {"question": question, "schema": schema, "history": history}
    graph = build_graph()
    if graph is not None:
        return graph.invoke(state)

    # Sequential fallback.
    state = node_generate_sql(state)
    state = node_validate(state)
    if not state.get("valid"):
        return state
    state = node_execute(state)
    state = node_analyze(state)
    state = node_insight(state)
    return state
