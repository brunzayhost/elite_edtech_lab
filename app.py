"""Streamlit dashboard for the Elite EdTech Lab.

Started by systemd as:  streamlit run src/app.py --server.port 8501 ...
Settings come from the environment (.env loaded by systemd's EnvironmentFile).

Access control: Streamlit has no built-in login, so this page shows nothing
until the admin password matches ``ADMIN_PASS_HASH`` (a bcrypt hash). For
anything beyond a trusted LAN, put a TLS reverse proxy in front of it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import bcrypt
import streamlit as st

# Make ``src.*`` imports work when Streamlit runs this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.lesson import LessonPlan  # noqa: E402
from src.run_pipeline import SYSTEM_PROMPT, build_request  # noqa: E402
from src.services.nextcloud_sync import NextcloudSync  # noqa: E402
from src.services.ollama_client import generate_structured  # noqa: E402

st.set_page_config(page_title="Elite EdTech Lab", page_icon="📚", layout="wide")


def _check_login() -> bool:
    """Return True once the admin has authenticated in this browser session."""
    if st.session_state.get("authed"):
        return True
    st.title("Elite EdTech Lab")
    user = st.text_input("Username")
    pw = st.text_input("Password", type="password")
    if st.button("Log in"):
        expected_user = os.environ.get("ADMIN_USER", "admin")
        hashed = os.environ.get("ADMIN_PASS_HASH", "").encode()
        try:
            ok = user == expected_user and bcrypt.checkpw(pw.encode(), hashed)
        except ValueError:  # ADMIN_PASS_HASH is not a valid bcrypt hash (still 'change-me')
            ok = False
        if ok:
            st.session_state["authed"] = True
            st.rerun()
        st.error("Invalid credentials, or ADMIN_PASS_HASH is not set in .env.")
    return False


if not _check_login():
    st.stop()

st.title("Lesson generator")
st.caption(f"Model: {os.environ.get('MODEL_NAME', '?')}  |  Ollama: {os.environ.get('OLLAMA_BASE_URL', '?')}")

topic = st.text_input("Topic", placeholder="Chinese Calligraphy")
use_rag = st.checkbox(
    "Use school knowledge base (Open Notebook)",
    help="Needs the Open Notebook container running and OPEN_NOTEBOOK_PASSWORD set. "
    "If unavailable, generation continues without it.",
)
if st.button("Generate lesson", disabled=not topic):
    with st.spinner("Generating on local CPU; this can take a few minutes..."):
        try:
            user_prompt, meta = build_request(topic, use_rag)
            lesson, prov = generate_structured(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                schema_model=LessonPlan,
                traditional_fields=("title_zh",),
                extra_provenance=meta,
            )
        except RuntimeError as exc:
            st.error(str(exc))
        else:
            stem = "".join(c if c.isalnum() else "_" for c in topic)[:40] or "lesson"
            NextcloudSync().save(lesson.model_dump(), prov, stem)
            st.subheader(f"{lesson.title_en} / {lesson.title_zh}")
            st.json(lesson.model_dump())
            st.caption("Provenance (record this in your research journal)")
            st.code(json.dumps(prov, ensure_ascii=False, indent=2), language="json")
