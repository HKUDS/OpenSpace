"""Embedding generation via OpenAI-compatible API."""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from typing import List, Optional, Tuple

logger = logging.getLogger("openspace.cloud")

# Constants (duplicated here to avoid top-level import of skill_ranker)
SKILL_EMBEDDING_MODEL = "openai/text-embedding-3-small"
SKILL_EMBEDDING_MAX_CHARS = 12_000
SKILL_EMBEDDING_DIMENSIONS = 1536

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENAI_BASE = "https://api.openai.com/v1"


def resolve_embedding_api() -> Tuple[Optional[str], str]:
    """Resolve API key and base URL for embedding requests.

    Priority:
      1. ``OPENROUTER_API_KEY`` → OpenRouter base URL
      2. ``OPENAI_API_KEY`` + ``OPENAI_BASE_URL`` (default ``api.openai.com``)
      3. host-agent config (nanobot / openclaw)

    Returns:
        ``(api_key, base_url)`` — *api_key* may be ``None`` when no key is found.
    """
    or_key = os.environ.get("OPENROUTER_API_KEY")
    if or_key:
        return or_key, _OPENROUTER_BASE

    oa_key = os.environ.get("OPENAI_API_KEY")
    if oa_key:
        base = os.environ.get("OPENAI_BASE_URL", _OPENAI_BASE).rstrip("/")
        return oa_key, base

    try:
        from openspace.host_detection import get_openai_api_key
        host_key = get_openai_api_key()
        if host_key:
            base = os.environ.get("OPENAI_BASE_URL", _OPENAI_BASE).rstrip("/")
            return host_key, base
    except Exception:
        pass

    return None, _OPENAI_BASE


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_skill_embedding_text(
    name: str,
    description: str,
    readme_body: str,
    max_chars: int = SKILL_EMBEDDING_MAX_CHARS,
) -> str:
    """Build text for skill embedding: ``name + description + SKILL.md body``.

    Unified strategy matching MCP search_skills and clawhub platform.
    """
    header = "\n".join(filter(None, [name, description]))
    raw = "\n\n".join(filter(None, [header, readme_body]))
    if len(raw) <= max_chars:
        return raw
    return raw[:max_chars]


def generate_embedding(text: str, api_key: Optional[str] = None) -> Optional[List[float]]:
    """Generate embedding using OpenAI-compatible API.

    When *api_key* is ``None``, credentials are resolved automatically via
    :func:`resolve_embedding_api` (``OPENROUTER_API_KEY`` → ``OPENAI_API_KEY``
    → host-agent config).

    This is a **synchronous** call (uses urllib).  In async contexts,
    wrap with ``asyncio.to_thread()``.

    Args:
        text: The text to embed.
        api_key: Explicit API key.  When provided, base URL is still resolved
                 from environment (``OPENROUTER_API_KEY`` presence determines
                 the endpoint).

    Returns:
        Embedding vector, or None on failure.
    """
    resolved_key, base_url = resolve_embedding_api()
    if api_key is None:
        api_key = resolved_key
    if not api_key:
        return None

    body = json.dumps({
        "model": SKILL_EMBEDDING_MODEL,
        "input": text,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/embeddings",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", [{}])[0].get("embedding")
    except Exception as e:
        logger.warning("Embedding generation failed: %s", e)
        return None
