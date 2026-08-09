"""Regression tests for local skill search ranking.

Issue #115: ``SkillSearchEngine._bm25_phase`` computed BM25 scores and used
them for candidate filtering, but never propagated them to
``SkillSearchEngine._score_phase``. Without an embedding provider the ranking
signal collapsed to zero, so multi-word queries could fall back to registry
order instead of BM25 relevance order.

The BM25 signal is only a fallback final-score signal: it must not alter
existing embedding-backed or server-ranked search semantics.

These tests use synthetic candidates only (no bundled skill fixtures) so they
exercise the public ``SkillSearchEngine.search()`` API in isolation.
"""

from openspace.cloud.search import SkillSearchEngine
from openspace.skill_engine.skill_ranker import SkillRanker


def _candidate(skill_id: str, name: str, description: str, **extra) -> dict:
    return {
        "skill_id": skill_id,
        "name": name,
        "description": description,
        "source": "test",
        **extra,
    }


def test_multiword_no_embedding_ranking_uses_bm25_signal():
    """Multi-word query without embeddings must rank by BM25 relevance.

    Candidates are deliberately ordered with the relevant one LAST so that a
    registry-order fallback would rank it at the bottom. The relevant
    candidate does not satisfy the current all-query-token lexical-boost
    condition, so its ranking advantage must come from BM25.
    """
    candidates = [
        _candidate(
            "apple-notes",
            "Manage Apple Notes and local notes",
            "Manage Apple Notes and local notes",
        ),
        _candidate(
            "weather",
            "Get weather forecasts and conditions",
            "Get weather forecasts and conditions",
        ),
        _candidate(
            "docx",
            "Word file editing utility",
            "Create and edit Word DOCX documents",
        ),
    ]

    results = SkillSearchEngine().search(
        "docx document",
        candidates,
        query_embedding=None,
        limit=5,
    )

    skill_ids = [r["skill_id"] for r in results]

    # The docx candidate must rank above both unrelated candidates.
    assert skill_ids.index("docx") < skill_ids.index("apple-notes")
    assert skill_ids.index("docx") < skill_ids.index("weather")

    # The docx candidate must carry a non-zero BM25 ranking signal.
    docx_result = next(r for r in results if r["skill_id"] == "docx")
    assert docx_result["score"] > 0


def test_embedding_ranking_not_affected_by_bm25_signal(monkeypatch):
    """Embedding ranking must not receive the BM25 addition.

    With a query embedding available, final ranking is decided by
    vector score + lexical boost; BM25 is only a Phase 1 filtering signal.
    """
    def fake_bm25_only(self, query, candidates, top_k):
        for c in candidates:
            c.bm25_score = 10.0 if c.skill_id == "bm25-favored" else 0.0
        return sorted(candidates, key=lambda c: c.bm25_score, reverse=True)[:top_k]

    monkeypatch.setattr(SkillRanker, "bm25_only", fake_bm25_only)

    candidates = [
        _candidate(
            "bm25-favored",
            "Office utilities for writing files",
            "Office utilities for writing files",
            _embedding=[0.8, 0.6],
        ),
        _candidate(
            "vector-favored",
            "Quick notes manager",
            "Quick notes manager",
            _embedding=[0.9, 0.435889894],
        ),
    ]

    results = SkillSearchEngine().search(
        "docx document",
        candidates,
        query_embedding=[1.0, 0.0],
        limit=5,
    )

    skill_ids = [r["skill_id"] for r in results]

    # vector-favored has higher vector similarity (0.9 > 0.8) and must rank
    # first despite bm25-favored carrying a large BM25 score.
    assert skill_ids.index("vector-favored") < skill_ids.index("bm25-favored")


def test_server_search_rank_not_affected_by_bm25_signal(monkeypatch):
    """Server search rank must not receive the BM25 addition.

    Without a query embedding but with a numeric server ``_search_rank``,
    final ranking is decided by server search rank + lexical boost.
    """
    def fake_bm25_only(self, query, candidates, top_k):
        for c in candidates:
            c.bm25_score = 10.0 if c.skill_id == "bm25-favored" else 0.0
        return sorted(candidates, key=lambda c: c.bm25_score, reverse=True)[:top_k]

    monkeypatch.setattr(SkillRanker, "bm25_only", fake_bm25_only)

    candidates = [
        _candidate(
            "bm25-favored",
            "Office utilities for writing files",
            "Office utilities for writing files",
            _search_rank=0.2,
        ),
        _candidate(
            "server-favored",
            "Quick notes manager",
            "Quick notes manager",
            _search_rank=0.9,
        ),
    ]

    results = SkillSearchEngine().search(
        "docx document",
        candidates,
        query_embedding=None,
        limit=5,
    )

    skill_ids = [r["skill_id"] for r in results]

    # server-favored has the higher server search rank and must rank first
    # despite bm25-favored carrying a large BM25 score.
    assert skill_ids.index("server-favored") < skill_ids.index("bm25-favored")
