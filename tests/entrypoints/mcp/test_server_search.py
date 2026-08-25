from __future__ import annotations

from types import SimpleNamespace

import pytest

from openspace.entrypoints.mcp import server


class _Registry:
    def __init__(self, skills):
        self._skills = skills

    def list_skills(self):
        return self._skills


@pytest.mark.asyncio
async def test_local_search_context_builds_registry_without_full_runtime(
    monkeypatch,
):
    expected = [SimpleNamespace(skill_id="skill-1")]
    calls = []

    def build_registry(*, workspace_dir=None):
        calls.append(workspace_dir)
        return _Registry(expected)

    async def fail_if_full_runtime_starts():
        raise AssertionError(
            "local skill search must not initialize OpenSpace"
        )

    monkeypatch.setattr(server, "_openspace_instance", None)
    monkeypatch.setattr(server, "_get_openspace", fail_if_full_runtime_starts)
    monkeypatch.setenv("OPENSPACE_WORKSPACE", "test-workspace")
    monkeypatch.setattr(
        "openspace.runtime.skill_registry.build_skill_registry",
        build_registry,
    )

    skills, store = await server._get_local_search_context()

    assert skills == expected
    assert store is None
    assert calls == ["test-workspace"]


@pytest.mark.asyncio
async def test_local_search_context_reuses_initialized_runtime(monkeypatch):
    expected = [SimpleNamespace(skill_id="skill-1")]
    store = SimpleNamespace(_closed=False)
    runtime = SimpleNamespace(
        is_initialized=lambda: True,
        get_skill_registry=lambda: _Registry(expected),
        get_skill_store=lambda: store,
    )

    monkeypatch.setattr(server, "_openspace_instance", runtime)
    monkeypatch.delenv("OPENSPACE_HOST_SKILL_DIRS", raising=False)

    skills, actual_store = await server._get_local_search_context()

    assert skills == expected
    assert actual_store is store


@pytest.mark.asyncio
async def test_local_search_context_ignores_closed_runtime_store(monkeypatch):
    runtime = SimpleNamespace(
        is_initialized=lambda: True,
        get_skill_registry=lambda: _Registry([]),
        get_skill_store=lambda: SimpleNamespace(_closed=True),
    )

    monkeypatch.setattr(server, "_openspace_instance", runtime)
    monkeypatch.delenv("OPENSPACE_HOST_SKILL_DIRS", raising=False)

    skills, store = await server._get_local_search_context()

    assert skills == []
    assert store is None
