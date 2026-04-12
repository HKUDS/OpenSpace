from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

from openspace.cloud import embedding
from openspace.cloud.search import SkillSearchEngine
from openspace.skill_engine.skill_ranker import SkillCandidate, SkillRanker


class _DummyTextEmbedding:
    instances: list["_DummyTextEmbedding"] = []

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.embed_inputs: list[list[str]] = []
        type(self).instances.append(self)

    def embed(self, texts):
        batch = list(texts)
        self.embed_inputs.append(batch)
        for text in batch:
            yield [float(len(text)), float(len(self.model_name))]


def _install_fastembed_stub(monkeypatch) -> None:
    module = ModuleType("fastembed")
    module.TextEmbedding = _DummyTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)


def _reset_embedding_state(monkeypatch) -> None:
    monkeypatch.setattr(embedding, "_LOCAL_EMBEDDER", None, raising=False)
    monkeypatch.setattr(embedding, "_LOCAL_EMBEDDER_MODEL", None, raising=False)
    _DummyTextEmbedding.instances.clear()


def test_load_local_embedder_reuses_same_model_instance(monkeypatch) -> None:
    _install_fastembed_stub(monkeypatch)
    _reset_embedding_state(monkeypatch)

    first = embedding._load_local_embedder("unit-model")
    second = embedding._load_local_embedder("unit-model")
    third = embedding._load_local_embedder("other-model")

    assert first is second
    assert third is not first
    assert [instance.model_name for instance in _DummyTextEmbedding.instances] == [
        "unit-model",
        "other-model",
    ]


def test_generate_embedding_reuses_prewarmed_local_embedder(monkeypatch) -> None:
    _install_fastembed_stub(monkeypatch)
    _reset_embedding_state(monkeypatch)
    monkeypatch.setenv("OPENSPACE_SKILL_EMBEDDING_BACKEND", "local")
    monkeypatch.setenv("OPENSPACE_SKILL_EMBEDDING_MODEL", "unit-model")

    first = embedding.generate_embedding("alpha")
    second = embedding.generate_embedding("beta")

    assert first == [5.0, 10.0]
    assert second == [4.0, 10.0]
    assert len(_DummyTextEmbedding.instances) == 1
    assert _DummyTextEmbedding.instances[0].embed_inputs == [["alpha"], ["beta"]]


def test_skill_ranker_reuses_persisted_embedding_cache_between_instances(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "openspace.cloud.embedding.resolve_skill_embedding_model",
        lambda backend=None: "unit-model",
    )

    def fake_generate_embedding(text: str, api_key=None):
        calls.append(text)
        return [float(len(text)), 1.0]

    monkeypatch.setattr(
        SkillRanker,
        "_generate_embedding",
        staticmethod(fake_generate_embedding),
    )

    first_ranker = SkillRanker(cache_dir=tmp_path, enable_cache=True)
    candidate = SkillCandidate(
        skill_id="skill-1",
        name="alpha",
        description="beta",
        body="gamma",
    )
    first_ranker.hybrid_rank("query text", [candidate], top_k=1)

    cache_file = tmp_path / "skill_embeddings_unit-model_v2.pkl"
    assert cache_file.exists()
    assert calls == [
        "query text",
        embedding.build_skill_embedding_text("alpha", "beta", "gamma"),
    ]

    calls.clear()

    second_ranker = SkillRanker(cache_dir=tmp_path, enable_cache=True)
    assert "skill-1" in second_ranker._embedding_cache

    second_candidate = SkillCandidate(
        skill_id="skill-1",
        name="alpha",
        description="beta",
        body="gamma",
    )
    second_ranker.hybrid_rank("query text", [second_candidate], top_k=1)

    assert calls == ["query text"]


def test_skill_search_engine_uses_ranker_cache_for_local_candidates(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    class _DummyRanker:
        def __init__(self, enable_cache: bool = True):
            self.enable_cache = enable_cache

        def get_cached_embedding(self, skill_id: str):
            events.append(("cached", skill_id))
            return [0.5, 0.5]

        def prime_candidates(self, candidates):
            events.append(("prime", candidates[0].skill_id))
            return 1

    monkeypatch.setattr(
        "openspace.skill_engine.skill_ranker.SkillRanker",
        _DummyRanker,
    )
    monkeypatch.setattr(
        "openspace.cloud.embedding.cosine_similarity",
        lambda a, b: 0.75,
    )

    engine = SkillSearchEngine()
    scored = engine._score_phase(
        candidates=[
            {
                "skill_id": "skill-local",
                "name": "Local Skill",
                "description": "demo",
                "source": "openspace-local",
                "_embedding_text": "Local Skill\ndemo",
            }
        ],
        query_tokens=["local"],
        query_embedding=[1.0, 1.0],
    )

    assert events == [("cached", "skill-local")]
    assert scored[0]["vector_score"] == 0.75
