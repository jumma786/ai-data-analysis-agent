# Architecture

## Layers
1. **Frontend (Streamlit)** — 7 pages; talks to backend over HTTP only.
2. **API (FastAPI)** — thin controllers; validation via Pydantic.
3. **Agent pipeline (LangGraph)** — stateful graph; conditional edge halts on
   unsafe SQL. Sequential fallback runner when langgraph is absent.
4. **Services** — LLM provider abstraction, schema introspection, profiling,
   report generation.
5. **Persistence** — SQLAlchemy models for app metadata (users, refresh
   tokens, reports, documents). The *analytics* database the
   user queries is separate and connected at runtime.

## Data flow (query)
question → [generate_sql] → [validate] →(safe)→ [execute] → [analyze] →
[insight] → response. On unsafe SQL the graph routes to END with an error and
never touches the database.

## Safety model (defense in depth)
- Agent-level: read-only enforcement, keyword denylist, no stacked statements,
  comment stripping, auto-LIMIT.
- Deployment-level (recommended): least-privilege read-only DB role, statement
  timeout, network isolation. The agent guard is not a substitute for these.

## LLM modularity
`get_llm()` returns an `OpenAIProvider` or `OllamaProvider` behind a common
`complete(system, user)` interface. Add a provider by implementing that method.

## Extending to RAG
Documents → extract → chunk → embed → vector store (ChromaDB in prod, in-memory
cosine store as a zero-dependency fallback) → retrieve top-k → grounded answer.
