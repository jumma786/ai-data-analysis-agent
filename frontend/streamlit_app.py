"""Streamlit frontend. Talks to the FastAPI backend over HTTP.

Run: streamlit run frontend/streamlit_app.py
Set API_URL env var to point at the backend (default http://localhost:8000).
"""
from __future__ import annotations
import os
import requests
import pandas as pd
import streamlit as st

def _api_url() -> str:
    """Base URL of the backend, from Streamlit secrets or the environment.

    Three hosts, three mechanisms: `API_URL` in the environment locally and in
    the Docker image, and `st.secrets` on Streamlit Community Cloud, whose docs
    do not promise that dashboard secrets are mirrored into the environment.
    Checking both keeps one file working everywhere without a deploy-time edit.

    Reading `st.secrets` raises when no secrets file exists -- the normal local
    case -- and the exception type has moved between Streamlit versions, so the
    guard is deliberately broad. A trailing slash is stripped because it would
    otherwise produce `//query` in every request path.
    """
    try:
        if "API_URL" in st.secrets:
            return str(st.secrets["API_URL"]).rstrip("/")
    except Exception:            # No secrets file, or an unreadable one.
        pass
    return os.getenv("API_URL", "http://localhost:8000").rstrip("/")


API_URL = _api_url()

st.set_page_config(page_title="AI Data Analysis Agent", layout="wide")
st.title("AI Data Analysis Agent")


def auth_headers() -> dict[str, str]:
    """Bearer header for the logged-in session, or empty if signed out."""
    token = st.session_state.get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def escape_markdown(text: str) -> str:
    """Neutralise Markdown/LaTeX metacharacters in model-generated text.

    Streamlit treats `$...$` as LaTeX, so an insight mentioning two currency
    amounts renders the text between them as a mangled equation. Model output is
    prose, not markup, so escape the delimiters rather than trusting it.
    """
    return (text or "").replace("\\", "\\\\").replace("$", r"\$")


def _sidebar_auth() -> None:
    """Minimal signup/login form.

    /query, /chat, /upload and /generate-report require a token, so the UI needs
    somewhere to get one. The token lives in Streamlit's per-session state; it is
    not persisted across browser reloads.
    """
    with st.sidebar:
        st.divider()
        if st.session_state.get("access_token"):
            st.caption(f"Signed in as {st.session_state.get('email', '?')}")
            if st.button("Sign out"):
                # Revoke server-side too, otherwise the refresh token stays
                # valid for weeks after the user thinks they have logged out.
                token = st.session_state.get("refresh_token")
                if token:
                    try:
                        requests.post(f"{API_URL}/auth/logout",
                                      json={"refresh_token": token}, timeout=5)
                    except requests.RequestException:
                        pass          # Local sign-out should still proceed.
                st.session_state.pop("access_token", None)
                st.session_state.pop("refresh_token", None)
                st.rerun()
            return

        st.caption("Sign in to use query, chat, upload and reports.")
        email = st.text_input("Email", key="auth_email")
        password = st.text_input("Password", type="password", key="auth_password")
        col_login, col_signup = st.columns(2)

        if col_login.button("Log in") and email and password:
            r = requests.post(f"{API_URL}/auth/login",
                              json={"email": email, "password": password})
            if r.ok:
                body = r.json()
                st.session_state.access_token = body["access_token"]
                st.session_state.refresh_token = body["refresh_token"]
                st.session_state.email = email
                st.rerun()
            elif r.status_code == 429:
                st.error("Too many failed attempts. Wait a moment and retry.")
            else:
                st.error("Login failed. Check your email and password.")

        if col_signup.button("Sign up") and email and password:
            r = requests.post(f"{API_URL}/auth/signup",
                              json={"email": email, "password": password})
            st.success("Account created — now log in.") if r.ok else st.error(r.text)


page = st.sidebar.radio(
    "Navigate",
    ["Dashboard", "Chat With Data", "Upload Dataset",
     "Database Connection", "Document Search", "Reports", "Settings"],
)
_sidebar_auth()

if page == "Dashboard":
    st.subheader("Overview")
    try:
        h = requests.get(f"{API_URL}/health", timeout=5).json()
        st.metric("Backend", h.get("status", "?"))
        st.metric("LLM Provider", h.get("provider", "?"))
    except Exception as e:  # noqa: BLE001
        st.error(f"Backend unreachable: {e}")

elif page == "Upload Dataset":
    st.subheader("Upload CSV / Excel / Parquet")
    f = st.file_uploader("Choose a file", type=["csv", "xlsx", "xls", "parquet"])
    if f and st.button("Profile dataset"):
        r = requests.post(f"{API_URL}/upload",
                          files={"file": (f.name, f.getvalue())},
                          headers=auth_headers())
        if r.ok:
            st.json(r.json())
        else:
            st.error(r.text)

elif page == "Database Connection":
    st.subheader("Connect a database")
    url = st.text_input("SQLAlchemy URL",
                        "postgresql+psycopg2://user:pass@host:5432/db")
    if st.button("Connect"):
        r = requests.post(f"{API_URL}/connect-database",
                          json={"database_url": url}, headers=auth_headers())
        st.json(r.json()) if r.ok else st.error(r.text)

elif page == "Chat With Data":
    st.subheader("Ask your data a question")
    if "messages" not in st.session_state:
        st.session_state.messages = []
    for m in st.session_state.messages:
        st.chat_message(m["role"]).write(escape_markdown(m["content"]))
    q = st.chat_input("e.g. Show monthly revenue trend")
    if q:
        st.session_state.messages.append({"role": "user", "content": q})
        st.chat_message("user").write(q)
        payload = {"messages": st.session_state.messages}
        r = requests.post(f"{API_URL}/chat", json=payload, headers=auth_headers())
        if r.ok:
            data = r.json()
            if not data["valid"]:
                st.chat_message("assistant").error(data.get("error"))
            else:
                st.chat_message("assistant").write(
                    escape_markdown(data.get("insight", "")))
                st.code(data.get("sql", ""), language="sql")
                if data.get("rows"):
                    st.dataframe(pd.DataFrame(data["rows"]))
                st.session_state.messages.append(
                    {"role": "assistant", "content": data.get("insight", "")})
        else:
            st.error(r.text)

elif page == "Document Search":
    st.subheader("RAG document Q&A")
    if not st.session_state.get("access_token"):
        st.warning("Sign in from the sidebar to use document search.")
    else:
        status = requests.get(f"{API_URL}/documents/status",
                              headers=auth_headers())
        if status.ok:
            info = status.json()
            st.caption(f"{info['document_count']} chunk(s) stored "
                       f"· backend: {info['backend']}")

        doc = st.file_uploader("Add a document", type=["txt", "pdf", "docx"])
        if doc and st.button("Ingest"):
            r = requests.post(f"{API_URL}/documents/upload",
                              files={"file": (doc.name, doc.getvalue())},
                              headers=auth_headers())
            if r.ok:
                st.success(f"Ingested {r.json()['chunks_added']} chunk(s).")
                st.rerun()
            else:
                st.error(r.text)

        question = st.text_input("Ask a question about your documents")
        if st.button("Search") and question:
            r = requests.post(f"{API_URL}/documents/query",
                              json={"question": question},
                              headers=auth_headers())
            if not r.ok:
                st.error(r.text)
            else:
                data = r.json()
                if not data["chunks"]:
                    st.info("Nothing ingested yet — add a document above.")
                else:
                    st.write(escape_markdown(data.get("answer") or ""))
                    # Sources are shown so the answer can be checked against
                    # them rather than taken on trust.
                    with st.expander(f"Sources ({len(data['chunks'])} chunks)"):
                        for i, chunk in enumerate(data["chunks"], start=1):
                            st.markdown(f"**{i}.** {escape_markdown(chunk)}")

elif page == "Reports":
    st.subheader("Generate a PDF report")
    q = st.text_input("Report question")
    if st.button("Generate") and q:
        r = requests.post(f"{API_URL}/generate-report", json={"question": q},
                          headers=auth_headers())
        if r.ok:
            report_id = r.json()["report_id"]
            dl = requests.get(f"{API_URL}/reports/{report_id}/download",
                              headers=auth_headers())
            if dl.ok:
                st.download_button("Download report PDF", data=dl.content,
                                   file_name="report.pdf", mime="application/pdf")
            else:
                st.error(dl.text)
        else:
            st.error(r.text)

elif page == "Settings":
    st.subheader("Settings")
    st.write("Configure providers and keys via backend .env")
