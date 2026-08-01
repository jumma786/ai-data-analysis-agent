"""Streamlit frontend. Talks to the FastAPI backend over HTTP.

Run: streamlit run frontend/streamlit_app.py
Set API_URL env var to point at the backend (default http://localhost:8000).
"""
from __future__ import annotations
import os
import requests
import pandas as pd
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(page_title="AI Data Analysis Agent", layout="wide")
st.title("AI Data Analysis Agent")


def auth_headers() -> dict[str, str]:
    """Bearer header for the logged-in session, or empty if signed out."""
    token = st.session_state.get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


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
                st.session_state.pop("access_token", None)
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
                st.session_state.access_token = r.json()["access_token"]
                st.session_state.email = email
                st.rerun()
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
        st.chat_message(m["role"]).write(m["content"])
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
                st.chat_message("assistant").write(data.get("insight", ""))
                st.code(data.get("sql", ""), language="sql")
                if data.get("rows"):
                    st.dataframe(pd.DataFrame(data["rows"]))
                st.session_state.messages.append(
                    {"role": "assistant", "content": data.get("insight", "")})
        else:
            st.error(r.text)

elif page == "Document Search":
    st.subheader("RAG document Q&A")
    st.info("Upload documents via the backend RAG pipeline, then query here.")

elif page == "Reports":
    st.subheader("Generate a PDF report")
    q = st.text_input("Report question")
    if st.button("Generate") and q:
        r = requests.post(f"{API_URL}/generate-report", json={"question": q},
                          headers=auth_headers())
        st.json(r.json()) if r.ok else st.error(r.text)

elif page == "Settings":
    st.subheader("Settings")
    st.write("Configure providers and keys via backend .env")
