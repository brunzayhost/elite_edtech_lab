"""Generate one lesson plan and store it.

Usage (from the repo root, with the venv active or via ``poetry run``):

    set -a; source .env; set +a
    poetry run python -m src.run_pipeline "Chinese Calligraphy"
    poetry run python -m src.run_pipeline "Chinese Calligraphy" --rag   # use Open Notebook

What it does, in order:
1. Optionally retrieves reference material from Open Notebook (``--rag``).
   If it is unreachable the pipeline continues without it.
2. Asks the shared Ollama server for JSON matching ``LessonPlan``
   (schema-constrained, validated, retried on failure).
3. Checks that ``title_zh`` is Traditional Chinese (when opencc is installed).
4. Writes lesson + provenance to the local spool and uploads to Nextcloud.
5. Prints the provenance for the research journal: commit, model digest,
   attempts (1 == passed first try), latency, and which RAG sources were used.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from src.models.lesson import LessonPlan
from src.services.nextcloud_sync import NextcloudSync
from src.services.ollama_client import generate_structured
from src.services.open_notebook_client import search_context

SYSTEM_PROMPT = (
    "You are an experienced Hong Kong curriculum designer. Produce a lesson plan "
    "as JSON that follows the given schema exactly. Write English fields in English "
    "and Chinese fields in Traditional Chinese as used in Hong Kong. "
    "Do not use Simplified Chinese."
)


def build_request(topic: str, use_rag: bool = False) -> tuple[str, dict[str, Any]]:
    """Return ``(user_prompt, extra_provenance)``; shared by the CLI and the dashboard."""
    user = f"Create a 40-minute lesson plan on the topic: {topic}"
    meta: dict[str, Any] = {"rag_requested": use_rag, "rag_sources": []}
    if use_rag:
        context, sources = search_context(topic)
        if context:
            user += (
                "\n\nReference material from the school's knowledge base. Use it where "
                "relevant and stay consistent with it; do not invent facts beyond it:\n" + context
            )
            meta["rag_sources"] = sources
    meta["rag_used"] = bool(meta["rag_sources"])
    return user, meta


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--rag"]
    use_rag = "--rag" in argv[1:]
    if not args:
        print('Usage: python -m src.run_pipeline "<topic>" [--rag]', file=sys.stderr)
        return 2
    topic = " ".join(args)

    user, meta = build_request(topic, use_rag)
    lesson, provenance = generate_structured(
        system=SYSTEM_PROMPT,
        user=user,
        schema_model=LessonPlan,
        traditional_fields=("title_zh",),
        extra_provenance=meta,
    )

    stem = "".join(c if c.isalnum() else "_" for c in topic)[:40] or "lesson"
    path = NextcloudSync().save(lesson.model_dump(), provenance, stem)

    # ensure_ascii=False so Traditional Chinese prints as characters, not \u escapes.
    print(json.dumps({"lesson": lesson.model_dump(), "provenance": provenance}, ensure_ascii=False, indent=2))
    print(f"\nSpool path (removed after a successful upload): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
