"""Ollama client for the Elite EdTech Lab.

Why this module exists
----------------------
The Lab talks to an *existing* Ollama container over HTTP. This module fixes
the weak points of a naive "call the API and hope" approach:

1. Schema-enforced output: Ollama's ``format`` parameter accepts a JSON Schema,
   so the model is constrained to emit valid JSON, replacing the old advice of
   "raise the temperature".
2. Validate + retry: even valid JSON can violate business rules (missing
   fields, Simplified instead of Traditional Chinese). On failure we re-ask,
   feeding the error back to the model, up to ``OLLAMA_MAX_RETRIES`` times.
3. Explicit context window: Ollama's default ``num_ctx`` is small and silently
   truncates long prompts, which is dangerous once RAG context is injected.
4. Provenance: every result carries the git commit, model digest and sampling
   parameters, so each output is traceable to the exact experimental setup
   (PhD reproducibility).
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Type, TypeVar

import requests
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def _cfg() -> dict[str, Any]:
    """Read settings at call time (not import time) so tests can override env vars."""
    return {
        "base_url": os.environ["OLLAMA_BASE_URL"].rstrip("/"),
        "model": os.environ["MODEL_NAME"],
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.1")),
        "seed": int(os.getenv("OLLAMA_SEED", "42")),
        "timeout": int(os.getenv("OLLAMA_TIMEOUT", "600")),
        "max_retries": int(os.getenv("OLLAMA_MAX_RETRIES", "3")),
    }


def git_commit() -> str:
    """Exact code state. ``--dirty`` flags uncommitted edits that would make a hash misleading."""
    try:
        return subprocess.check_output(
            ["git", "describe", "--always", "--dirty", "--abbrev=40"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def model_digest(cfg: dict[str, Any]) -> str:
    """Tags like ``qwen2:7b...`` can be re-pushed; the digest identifies the actual weights."""
    try:
        r = requests.get(f"{cfg['base_url']}/api/tags", timeout=10)
        r.raise_for_status()
        for m in r.json().get("models", []):
            if m.get("name") == cfg["model"]:
                return m.get("digest", "unknown")
    except requests.RequestException:
        pass
    return "unknown"


def looks_traditional(text: str) -> bool:
    """Return True if ``text`` has no Simplified-only characters.

    Qwen often drifts into Simplified Chinese. If the optional ``opencc``
    package is installed we convert to Hong Kong Traditional (s2hk) and check
    that nothing changes; otherwise the check is skipped (returns True).
    """
    try:
        from opencc import OpenCC  # type: ignore
    except ImportError:
        return True
    return OpenCC("s2hk").convert(text) == text


def generate_structured(
    system: str,
    user: str,
    schema_model: Type[T],
    traditional_fields: tuple[str, ...] = (),
    extra_provenance: dict[str, Any] | None = None,
) -> tuple[T, dict[str, Any]]:
    """Ask the model for JSON matching ``schema_model``; retry with feedback on failure.

    Returns ``(validated_object, provenance_dict)``. Raises RuntimeError when
    every attempt fails, so callers never silently save invalid research data.
    """
    cfg = _cfg()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_error = "no attempt made"
    started = time.monotonic()

    for attempt in range(1, cfg["max_retries"] + 1):
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "stream": False,
            # JSON Schema generated from the Pydantic model constrains decoding.
            "format": schema_model.model_json_schema(),
            "options": {
                "num_ctx": cfg["num_ctx"],
                "temperature": cfg["temperature"],
                "seed": cfg["seed"],
            },
        }
        try:
            r = requests.post(f"{cfg['base_url']}/api/chat", json=payload, timeout=cfg["timeout"])
            r.raise_for_status()
            content = r.json()["message"]["content"]
        except (requests.RequestException, KeyError) as exc:
            # Network/peer failure: back off exponentially (2s, 4s, 8s...).
            last_error = f"transport error: {exc}"
            time.sleep(2 ** attempt)
            continue

        try:
            obj = schema_model.model_validate_json(content)
            for field in traditional_fields:
                if not looks_traditional(str(getattr(obj, field))):
                    raise ValueError(f"field '{field}' contains Simplified Chinese; use Traditional (Hong Kong).")
        except (ValidationError, ValueError) as exc:
            # Feed the exact error back so the model can self-correct. The
            # attempt count is recorded, since "passed first try?" is a research metric.
            last_error = str(exc)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"Your JSON was invalid: {last_error}\nReturn corrected JSON only."})
            continue

        provenance = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": git_commit(),
            "model": cfg["model"],
            "model_digest": model_digest(cfg),
            "num_ctx": cfg["num_ctx"],
            "temperature": cfg["temperature"],
            "seed": cfg["seed"],
            "attempts": attempt,                       # 1 == validated on the first try
            "latency_seconds": round(time.monotonic() - started, 2),
            **(extra_provenance or {}),               # e.g. RAG sources used
        }
        return obj, provenance

    raise RuntimeError(f"Model output failed after {cfg['max_retries']} attempts: {last_error}")
