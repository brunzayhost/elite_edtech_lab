"""Open Notebook (lfnovo/open_notebook) client for optional RAG context.

Open Notebook stores the school's documents and does its own embedding and
retrieval, so the Lab does not build a vector store. We call its REST API:

    POST {OPEN_NOTEBOOK_URL}/api/search
    Authorization: Bearer {OPEN_NOTEBOOK_PASSWORD}
    {"query": ..., "type": "vector"|"text", "limit": N, "minimum_score": X}

Design rules:
* Optional and fail-safe: if the URL/password are unset, the container is
  stopped, or the call fails, we return no context and lesson generation
  simply continues without RAG. RAG must never take the pipeline down.
* Bounded: the context is capped (``RAG_MAX_CONTEXT_CHARS``) so it cannot
  overflow the model's context window (``OLLAMA_NUM_CTX``).
* Traceable: the caller records which sources were used in the provenance.

STATUS: written against the published OpenAPI schema but not yet exercised
with real credentials. The result item field names are not declared in the
schema, so extraction is defensive (see ``_extract``).
"""
from __future__ import annotations

import os
from typing import Any

import requests


def _cfg() -> tuple[str, str]:
    return (
        os.environ.get("OPEN_NOTEBOOK_URL", "").rstrip("/"),
        os.environ.get("OPEN_NOTEBOOK_PASSWORD", ""),
    )


def _extract(item: dict[str, Any]) -> tuple[str, str, float | None]:
    """Pull (title, text, score) out of a result item, tolerating unknown field names."""
    title = str(item.get("title") or item.get("name") or item.get("id") or "untitled")
    text = item.get("content") or item.get("text") or item.get("matches") or item.get("snippet") or ""
    if isinstance(text, list):  # some responses return a list of matched chunks
        text = "\n".join(str(t) for t in text)
    score = item.get("score") or item.get("relevance") or item.get("similarity")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return title, str(text).strip(), score


def search_context(query: str, limit: int = 5, minimum_score: float = 0.3) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(context_text, sources)``; both empty when RAG is unavailable."""
    url, password = _cfg()
    if not url or not password:
        print("[rag] OPEN_NOTEBOOK_URL/PASSWORD not set; continuing without RAG")
        return "", []

    headers = {"Authorization": f"Bearer {password}", "Content-Type": "application/json"}
    max_chars = int(os.environ.get("RAG_MAX_CONTEXT_CHARS", "6000"))

    data: dict[str, Any] | None = None
    # Prefer semantic (vector) search; fall back to keyword search if embeddings
    # are not configured in Open Notebook yet.
    for search_type in ("vector", "text"):
        body = {
            "query": query, "type": search_type, "limit": limit,
            "search_sources": True, "search_notes": True, "minimum_score": minimum_score,
        }
        try:
            r = requests.post(f"{url}/api/search", json=body, headers=headers, timeout=30)
            if r.status_code == 401:
                print("[rag] Open Notebook rejected the password (401); continuing without RAG")
                return "", []
            r.raise_for_status()
            data = r.json()
            break
        except (requests.RequestException, ValueError) as exc:
            print(f"[rag] {search_type} search failed ({exc}); trying next mode")
    if not data:
        return "", []

    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    used = 0
    for item in data.get("results", []):
        title, text, score = _extract(item)
        if not text:
            continue
        chunk = f"[{len(sources) + 1}] {title}\n{text}"
        if used + len(chunk) > max_chars:
            chunk = chunk[: max(0, max_chars - used)]
        if not chunk:
            break
        parts.append(chunk)
        sources.append({"title": title, "score": score})
        used += len(chunk)
        if used >= max_chars:
            break
    return "\n\n".join(parts), sources
