"""Regression: trailing prose with braces must not drop skill-selection JSON."""

from __future__ import annotations

from openspace.skill_engine.registry import SkillRegistry


def test_parse_skill_selection_tolerates_trailing_prose_braces():
    text = '{"brief_plan": "ok", "skills": ["a"]}\n\nnote {x}'
    ids, plan = SkillRegistry._parse_skill_selection_response(text)
    assert ids == ["a"]
    assert plan == "ok"
