"""SkillRanker — BM25 + embedding hybrid ranking for skills.

Provides a two-stage retrieval pipeline for skill selection:
  Stage 1 (BM25): Fast lexical rough-rank over all skills
  Stage 2 (Embedding): Semantic re-rank on BM25 candidates

Embedding strategy:
  - Text = ``name + description + SKILL.md body`` (consistent with MCP
    ``search_skills`` and the clawhub cloud platform)
  - Model: ``qwen/qwen3-embedding-8b`` via OpenRouter API
  - Embeddings are cached in-memory keyed by ``skill_id`` and optionally
    persisted to a pickle file for cross-session reuse

Reused by:
  - ``SkillRegistry.select_skills_with_llm`` — pre-filter before LLM selection
  - ``mcp_server.search_skills`` — BM25 stage of the MCP search tool
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import pickle
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

# Embedding model — must match clawhub platform for vector-space compatibility
SKILL_EMBEDDING_MODEL = "openai/text-embedding-3-small"
SKILL_EMBEDDING_MAX_CHARS = 12_000

# Pre-filter threshold: when local skills exceed this count, BM25 pre-filter
# is activated before LLM selection.  Below this, all skills go directly to LLM.
PREFILTER_THRESHOLD = 10

# How many candidates to keep after BM25 rough-rank (before embedding re-rank)
BM25_CANDIDATES_MULTIPLIER = 3  # top_k * 3

# Cache version — increment when format changes.
#
# v2 intentionally does not migrate v1 skill_id-only entries: old entries did
# not store the source text hash, so they cannot be proven fresh.
_CACHE_VERSION = 2


@dataclass
class SkillCandidate:
    """Lightweight skill representation for ranking."""
    skill_id: str
    name: str
    description: str
    body: str = ""             # SKILL.md body (frontmatter stripped)
    source: str = "local"      # "local" | "cloud"
    # Internal ranking fields
    embedding: Optional[List[float]] = None
    embedding_text: str = ""   # text used to compute embedding
    score: float = 0.0
    bm25_score: float = 0.0
    vector_score: float = 0.0
    # Pass-through metadata (for MCP search results)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SkillRanker:
    """Hybrid BM25 + embedding ranker for skills.

    Usage::

        ranker = SkillRanker()
        candidates = [SkillCandidate(skill_id=..., name=..., description=..., body=...)]
        ranked = ranker.hybrid_rank(query, candidates, top_k=10)
    """

    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        enable_cache: bool = True,
    ) -> None:
        # Embedding cache: encoded skill_id + content hash → List[float]
        self._embedding_cache: Dict[str, List[float]] = {}
        self._enable_cache = enable_cache
        self._cache_lock = threading.RLock()

        if cache_dir is None:
            try:
                from openspace.config.constants import PROJECT_ROOT
                cache_dir = PROJECT_ROOT / ".openspace" / "skill_embedding_cache"
            except Exception:
                cache_dir = Path(".openspace") / "skill_embedding_cache"
        self._cache_dir = Path(cache_dir)

        if self._enable_cache:
            self._load_cache()

    def hybrid_rank(
        self,
        query: str,
        candidates: List[SkillCandidate],
        top_k: int = 10,
    ) -> List[SkillCandidate]:
        """BM25 rough-rank → embedding re-rank → return top_k.

        Falls back gracefully:
          - No BM25 lib → simple token overlap
          - No embedding API key → BM25-only
          - Both fail → return first top_k candidates
        """
        if not candidates or not query.strip():
            return candidates[:top_k]

        # Stage 1: BM25 rough-rank
        bm25_top = self._bm25_rank(query, candidates, top_k * BM25_CANDIDATES_MULTIPLIER)
        if not bm25_top:
            # BM25 found nothing — try embedding on all candidates
            emb_results = self._embedding_rank(query, candidates, top_k)
            return emb_results if emb_results else candidates[:top_k]

        # Stage 2: Embedding re-rank on BM25 candidates
        emb_results = self._embedding_rank(query, bm25_top, top_k)
        if emb_results:
            return emb_results

        # Embedding unavailable — return BM25 results
        logger.debug("Embedding unavailable, using BM25-only results")
        return bm25_top[:top_k]

    def bm25_only(
        self,
        query: str,
        candidates: List[SkillCandidate],
        top_k: int = 30,
    ) -> List[SkillCandidate]:
        """BM25-only ranking (for MCP search Phase 1)."""
        return self._bm25_rank(query, candidates, top_k)

    def embedding_only(
        self,
        query: str,
        candidates: List[SkillCandidate],
        top_k: int = 10,
    ) -> List[SkillCandidate]:
        """Embedding-only ranking."""
        return self._embedding_rank(query, candidates, top_k)

    @staticmethod
    def _skill_key_prefix(skill_id: str) -> str:
        """Return a collision-safe prefix for all cache entries of a skill."""
        encoded = base64.urlsafe_b64encode(skill_id.encode("utf-8")).decode("ascii")
        return f"{encoded}:"

    @classmethod
    def _content_key(cls, skill_id: str, embedding_text: str) -> str:
        """Compose a content-addressed cache key.

        The cache stores embeddings under ``"{encoded_skill_id}:{sha256(text)[:16]}"``
        so that any change to the text used for embedding (name, description,
        or body) automatically invalidates the cached entry without relying
        on an external trigger.
        """
        digest = hashlib.sha256(embedding_text.encode("utf-8")).hexdigest()[:16]
        return f"{cls._skill_key_prefix(skill_id)}{digest}"

    def _drop_stale_entries_locked(self, skill_id: str, keep_key: str) -> None:
        """Drop older content-addressed entries for a skill.

        Caller must hold ``self._cache_lock``.
        """
        prefix = self._skill_key_prefix(skill_id)
        for stale in [
            k for k in self._embedding_cache
            if k != keep_key and k.startswith(prefix)
        ]:
            self._embedding_cache.pop(stale, None)

    def get_or_compute_embedding(
        self, candidate: SkillCandidate,
    ) -> Optional[List[float]]:
        """Get embedding from cache or compute it.

        Returns None if embedding cannot be generated.
        """
        # Already has embedding (e.g. cloud pre-computed)
        if candidate.embedding:
            return candidate.embedding

        text = self._build_embedding_text(candidate)
        if not candidate.skill_id:
            emb = self._generate_embedding(text)
            if emb:
                candidate.embedding = emb
            return emb

        content_key = self._content_key(candidate.skill_id, text)
        dropped_legacy = False
        with self._cache_lock:
            # Check content-addressed cache. If a legacy in-memory entry exists,
            # discard it instead of migrating unverifiable stale data.
            cached = self._embedding_cache.get(content_key)
            if cached:
                candidate.embedding = cached
                return cached
            dropped_legacy = self._embedding_cache.pop(candidate.skill_id, None) is not None

        # Compute
        emb = self._generate_embedding(text)
        with self._cache_lock:
            cached = self._embedding_cache.get(content_key)
            if cached:
                candidate.embedding = cached
                return cached
            if emb:
                candidate.embedding = emb
                self._embedding_cache[content_key] = emb
                # Bound cache growth: previous versions of this skill are now
                # obsolete, drop them in the same write.
                self._drop_stale_entries_locked(candidate.skill_id, content_key)
                self._save_cache()
            elif dropped_legacy:
                self._save_cache()
        return emb

    def invalidate_cache(self, skill_id: str) -> None:
        """Remove all cached embeddings for a skill (e.g. after evolution).

        Removes every cache entry whose key matches either the exact
        ``skill_id`` (legacy format) or the ``"{skill_id}:*"`` content-addressed
        prefix, covering any historical content version that might linger.
        """
        prefix = self._skill_key_prefix(skill_id)
        with self._cache_lock:
            keys_to_drop = [
                k for k in self._embedding_cache
                if k == skill_id or k.startswith(prefix)
            ]
            for k in keys_to_drop:
                self._embedding_cache.pop(k, None)
            if keys_to_drop:
                self._save_cache()

    def clear_cache(self) -> None:
        """Clear all cached embeddings."""
        with self._cache_lock:
            self._embedding_cache.clear()
            self._save_cache()

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Tokenize text for BM25."""
        tokens = re.split(r"[^\w]+", text.lower())
        return [t for t in tokens if t]

    def _bm25_rank(
        self,
        query: str,
        candidates: List[SkillCandidate],
        top_k: int,
    ) -> List[SkillCandidate]:
        """Rank candidates using BM25."""
        if not candidates:
            return []

        try:
            from rank_bm25 import BM25Okapi  # type: ignore
        except ImportError:
            BM25Okapi = None

        # Build corpus: name + description + truncated body for richer matching
        corpus_tokens = []
        for c in candidates:
            text = f"{c.name} {c.description}"
            if c.body:
                text += f" {c.body[:2000]}"  # include body for BM25 but cap length
            corpus_tokens.append(self._tokenize(text))

        query_tokens = self._tokenize(query)

        if BM25Okapi and corpus_tokens:
            bm25 = BM25Okapi(corpus_tokens)
            scores = bm25.get_scores(query_tokens)
            for c, s in zip(candidates, scores):
                c.bm25_score = float(s)
        else:
            # Fallback: simple token overlap
            q_set = set(query_tokens)
            for c, toks in zip(candidates, corpus_tokens):
                if not toks or not q_set:
                    c.bm25_score = 0.0
                else:
                    overlap = q_set.intersection(toks)
                    c.bm25_score = len(overlap) / len(q_set)

        # Sort and filter
        ranked = sorted(candidates, key=lambda c: c.bm25_score, reverse=True)

        # If all scores are 0 (no match), return all candidates (let embedding decide)
        if all(c.bm25_score == 0.0 for c in ranked):
            logger.debug("BM25 found no matches, passing all candidates to embedding stage")
            return candidates[:top_k]

        return ranked[:top_k]

    @staticmethod
    def _get_openai_api_key() -> Optional[str]:
        """Resolve OpenAI-compatible API key for embedding requests."""
        from openspace.cloud.embedding import resolve_embedding_api
        api_key, _ = resolve_embedding_api()
        return api_key

    @staticmethod
    def _build_embedding_text(candidate: SkillCandidate) -> str:
        """Build text for embedding, consistent with MCP search_skills."""
        if candidate.embedding_text:
            return candidate.embedding_text
        header = "\n".join(filter(None, [candidate.name, candidate.description]))
        raw = "\n\n".join(filter(None, [header, candidate.body]))
        if len(raw) > SKILL_EMBEDDING_MAX_CHARS:
            raw = raw[:SKILL_EMBEDDING_MAX_CHARS]
        candidate.embedding_text = raw
        return raw

    def _embedding_rank(
        self,
        query: str,
        candidates: List[SkillCandidate],
        top_k: int,
    ) -> List[SkillCandidate]:
        """Rank candidates using embedding cosine similarity."""
        api_key = self._get_openai_api_key()
        if not api_key:
            return []

        # Generate query embedding
        query_emb = self._generate_embedding(query, api_key=api_key)
        if not query_emb:
            return []

        # Ensure all candidates have embeddings (content-addressed cache
        # with backward-compat fallback for legacy skill_id-only keys).
        cache_dirty = False
        for c in candidates:
            if c.embedding:
                continue
            text = self._build_embedding_text(c)
            if not c.skill_id:
                emb = self._generate_embedding(text, api_key=api_key)
                if emb:
                    c.embedding = emb
                continue
            content_key = self._content_key(c.skill_id, text)
            with self._cache_lock:
                cached = self._embedding_cache.get(content_key)
                if cached:
                    c.embedding = cached
                    continue
                # Do not migrate legacy skill_id-only entries; they may be
                # stale because the old format did not store a text hash.
                if self._embedding_cache.pop(c.skill_id, None) is not None:
                    cache_dirty = True
            emb = self._generate_embedding(text, api_key=api_key)
            if emb:
                c.embedding = emb
                with self._cache_lock:
                    self._embedding_cache[content_key] = emb
                    # Bound cache growth: previous versions of this skill are
                    # obsolete, drop them.
                    self._drop_stale_entries_locked(c.skill_id, content_key)
                    cache_dirty = True

        # Save newly computed / migrated embeddings
        if cache_dirty:
            self._save_cache()

        # Score
        for c in candidates:
            if c.embedding:
                c.vector_score = _cosine_similarity(query_emb, c.embedding)
            else:
                c.vector_score = 0.0
            c.score = c.vector_score

        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
        return ranked[:top_k]

    @staticmethod
    def _generate_embedding(
        text: str,
        api_key: Optional[str] = None,
    ) -> Optional[List[float]]:
        """Generate embedding via OpenAI-compatible API (text-embedding-3-small).

        Delegates credential / base-URL resolution to
        :func:`openspace.cloud.embedding.resolve_embedding_api`.
        """
        from openspace.cloud.embedding import resolve_embedding_api

        resolved_key, base_url = resolve_embedding_api()
        if not api_key:
            api_key = resolved_key
        if not api_key:
            return None

        import urllib.request

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
        import time
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("data", [{}])[0].get("embedding")
            except Exception as e:
                last_err = e
                if attempt < 2:
                    delay = 2 * (attempt + 1)
                    logger.debug("Embedding request failed (attempt %d/3), retrying in %ds: %s", attempt + 1, delay, e)
                    time.sleep(delay)
        logger.warning("Skill embedding generation failed after 3 attempts: %s", last_err)
        return None

    def _cache_file(self) -> Path:
        return self._cache_dir / f"skill_embeddings_v{_CACHE_VERSION}.pkl"

    def _load_cache(self) -> None:
        """Load embedding cache from disk."""
        path = self._cache_file()
        if not path.exists():
            return
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            if (
                isinstance(data, dict)
                and data.get("version") == _CACHE_VERSION
                and data.get("model") == SKILL_EMBEDDING_MODEL
                and isinstance(data.get("embeddings"), dict)
            ):
                with self._cache_lock:
                    self._embedding_cache = data.get("embeddings", {})
                logger.debug(f"Loaded {len(self._embedding_cache)} skill embeddings from cache")
        except Exception as e:
            logger.warning(f"Failed to load skill embedding cache: {e}")
            self._embedding_cache = {}

    def _save_cache(self) -> None:
        """Persist embedding cache to disk."""
        if not self._enable_cache:
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            with self._cache_lock:
                data = {
                    "version": _CACHE_VERSION,
                    "model": SKILL_EMBEDDING_MODEL,
                    "last_updated": datetime.now().isoformat(),
                    "embeddings": dict(self._embedding_cache),
                }
            tmp_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=self._cache_dir,
                    prefix=".skill_embeddings_",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    tmp_name = f.name
                    pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp_name, self._cache_file())
            finally:
                if tmp_name and os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        except Exception as e:
            logger.warning(f"Failed to save skill embedding cache: {e}")

def _cosine_similarity(a: List[float], b: List[float]) -> float:
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

