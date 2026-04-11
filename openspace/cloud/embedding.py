"""Embedding generation for skill routing.

Supports a dedicated skill-embedding path that can be routed
independently from the main LLM:

- ``OPENSPACE_SKILL_EMBEDDING_BACKEND=local``  → local fastembed model
- ``OPENSPACE_SKILL_EMBEDDING_BACKEND=remote`` → dedicated OpenAI-compatible endpoint
- ``OPENSPACE_SKILL_EMBEDDING_BACKEND=auto``   → prefer dedicated/generic remote config,
  then fall back to legacy OpenAI-compatible env vars, then local fastembed
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from typing import List, Optional, Tuple

logger = logging.getLogger("openspace.cloud")

# Defaults
SKILL_REMOTE_EMBEDDING_MODEL = "openai/text-embedding-3-small"
SKILL_LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
SKILL_EMBEDDING_MAX_CHARS = 12_000
SKILL_EMBEDDING_DIMENSIONS = 1536

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENAI_BASE = "https://api.openai.com/v1"
_VALID_BACKENDS = {"auto", "local", "remote"}
_LOCAL_EMBEDDER = None
_LOCAL_EMBEDDER_MODEL = None


def resolve_skill_embedding_backend() -> str:
    """Resolve skill-embedding backend mode."""
    value = os.environ.get("OPENSPACE_SKILL_EMBEDDING_BACKEND", "auto").strip().lower()
    if value in _VALID_BACKENDS:
        return value
    return "auto"


def resolve_skill_embedding_model(backend: Optional[str] = None) -> str:
    """Resolve the model name for skill embeddings."""
    backend = backend or resolve_skill_embedding_backend()
    explicit = os.environ.get("OPENSPACE_SKILL_EMBEDDING_MODEL", "").strip()
    if explicit:
        return explicit
    if backend == "local":
        return SKILL_LOCAL_EMBEDDING_MODEL
    if backend == "auto":
        remote_key, _ = _resolve_remote_embedding_api()
        if not remote_key:
            return SKILL_LOCAL_EMBEDDING_MODEL
    return SKILL_REMOTE_EMBEDDING_MODEL


def _resolve_remote_embedding_api() -> Tuple[Optional[str], str]:
    """Resolve remote embedding credentials/base URL for skill routing."""
    dedicated_key = os.environ.get("OPENSPACE_SKILL_EMBEDDING_API_KEY")
    dedicated_base = os.environ.get("OPENSPACE_SKILL_EMBEDDING_API_BASE")
    if dedicated_key and dedicated_base:
        return dedicated_key, dedicated_base.rstrip("/")

    generic_key = os.environ.get("EMBEDDING_API_KEY")
    generic_base = os.environ.get("EMBEDDING_BASE_URL")
    if generic_key and generic_base:
        return generic_key, generic_base.rstrip("/")

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


def resolve_embedding_api() -> Tuple[Optional[str], str]:
    """Resolve API key and base URL for remote embedding requests.

    Priority:
      1. ``OPENSPACE_SKILL_EMBEDDING_API_*`` dedicated skill-router endpoint
      2. ``EMBEDDING_*`` generic embedding endpoint
      3. ``OPENROUTER_API_KEY`` → OpenRouter base URL
      4. ``OPENAI_API_KEY`` + ``OPENAI_BASE_URL`` (default ``api.openai.com``)
      5. host-agent config (nanobot / openclaw)

    Returns:
        ``(api_key, base_url)`` — *api_key* may be ``None`` when no key is found.
    """
    return _resolve_remote_embedding_api()


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


def _load_local_embedder(model_name: str):
    """Load and cache the local embedding model."""
    global _LOCAL_EMBEDDER, _LOCAL_EMBEDDER_MODEL

    if _LOCAL_EMBEDDER is not None and _LOCAL_EMBEDDER_MODEL == model_name:
        return _LOCAL_EMBEDDER

    try:
        from fastembed import TextEmbedding
    except ImportError:
        logger.warning(
            "Local skill embeddings requested but fastembed is not installed. "
            "Install it with `pip install fastembed`."
        )
        return None

    try:
        logger.info("Loading local skill embedding model: %s", model_name)
        _LOCAL_EMBEDDER = TextEmbedding(model_name=model_name)
        _LOCAL_EMBEDDER_MODEL = model_name
        return _LOCAL_EMBEDDER
    except Exception as exc:
        logger.warning("Failed to load local skill embedding model %s: %s", model_name, exc)
        return None


def _generate_local_embedding(text: str, model_name: str) -> Optional[List[float]]:
    embedder = _load_local_embedder(model_name)
    if embedder is None:
        return None

    try:
        vector = next(iter(embedder.embed([text])))
        if hasattr(vector, "tolist"):
            return vector.tolist()
        return list(vector)
    except Exception as exc:
        logger.warning("Local skill embedding generation failed: %s", exc)
        return None


def generate_embedding(text: str, api_key: Optional[str] = None) -> Optional[List[float]]:
    """Generate skill embedding using the configured local/remote backend.

    When *api_key* is ``None``, credentials are resolved automatically via
    :func:`resolve_embedding_api`.

    Local mode uses ``fastembed``.
    Remote mode uses an OpenAI-compatible ``/embeddings`` endpoint.

    Args:
        text: The text to embed.
        api_key: Explicit API key for remote mode.

    Returns:
        Embedding vector, or None on failure.
    """
    backend = resolve_skill_embedding_backend()
    model_name = resolve_skill_embedding_model(backend)

    if backend == "local":
        return _generate_local_embedding(text, model_name)

    resolved_key, base_url = resolve_embedding_api()
    if api_key is None:
        api_key = resolved_key

    if not api_key:
        if backend == "remote":
            logger.warning(
                "Remote skill embeddings requested but no embedding API key/base was resolved."
            )
            return None
        return _generate_local_embedding(text, SKILL_LOCAL_EMBEDDING_MODEL)

    body = json.dumps({
        "model": model_name,
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
        logger.warning("Remote skill embedding generation failed: %s", e)
        if backend == "auto":
            return _generate_local_embedding(text, SKILL_LOCAL_EMBEDDING_MODEL)
        return None
