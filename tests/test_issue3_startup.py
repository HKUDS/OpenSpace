import json

import pytest

from openspace import mcp_server


@pytest.mark.asyncio
async def test_local_search_skills_does_not_initialize_openspace(monkeypatch, tmp_path):
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "demo-local-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Demo Local Skill
description: Local regression test skill
---

Find me when querying demo local skill.
""",
        encoding="utf-8",
    )

    async def forbidden_get_openspace():
        pytest.fail("_get_openspace should not run for source='local'")

    monkeypatch.setenv("OPENSPACE_HOST_SKILL_DIRS", str(skill_root))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(mcp_server, "_get_openspace", forbidden_get_openspace)
    monkeypatch.setattr(
        "openspace.cloud.embedding.generate_embedding",
        lambda text, api_key=None: None,
    )

    response = await mcp_server.search_skills(
        query="demo local skill",
        source="local",
        limit=5,
        auto_import=False,
    )

    payload = json.loads(response)
    assert payload["count"] >= 1
    assert any(
        item["name"] == "Demo Local Skill"
        for item in payload["results"]
    )
