"""Pydantic request/response models for the API."""
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field
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


# --- Authentication -------------------------------------------------------

# 72 bytes is bcrypt's hard limit; enforcing it here turns a 500 into a 422 with
# a readable message. The 8-character floor is a minimum, not a policy -- real
# deployments should also check against a breached-password list.
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int          # seconds until the *access* token expires


class RefreshRequest(BaseModel):
    refresh_token: str


# --- RAG documents --------------------------------------------------------

class DocumentQueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=4, ge=1, le=20)


class DocumentQueryResponse(BaseModel):
    question: str
    answer: Optional[str] = None
    chunks: list[str] = []
    document_count: int = 0


class DocumentIngestResponse(BaseModel):
    filename: str
    chunks_added: int
    document_count: int
    backend: str             # "memory" | "chroma"
