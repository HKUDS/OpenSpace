"""Regression: normalize_frontmatter must accept non-string YAML scalars."""

from __future__ import annotations

from openspace.skill_engine.skill_utils import normalize_frontmatter


def test_normalize_frontmatter_bool_and_int_scalars() -> None:
    raw = "---\nname: demo\nenabled: true\nversion: 2\n---\n# body\n"
    out = normalize_frontmatter(raw)
    assert "enabled: true" in out
    assert "version: 2" in out
    assert "# body" in out


def test_normalize_frontmatter_string_still_quoted_when_needed() -> None:
    raw = "---\nname: demo\ndesc: has: colon\n---\n"
    out = normalize_frontmatter(raw)
    assert 'desc: "has: colon"' in out
