"""Pydantic request/response models for the API."""
from pydantic import BaseModel
from typing import Any, Optional


class QueryRequest(BaseModel):
    question: str
    schema_text: Optional[str] = ""
    history: Optional[str] = ""


class QueryResponse(BaseModel):
    sql: Optional[str] = None
    valid: bool
    error: Optional[str] = None
    chart: Optional[str] = None
    insight: Optional[str] = None
    row_count: Optional[int] = None
    rows: Optional[list[dict[str, Any]]] = None


class DBConnectRequest(BaseModel):
    database_url: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    schema_text: Optional[str] = ""
