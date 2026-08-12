"""Regression: get_frontmatter_field must read YAML block scalars."""

from __future__ import annotations

from openspace.skill_engine.skill_utils import get_frontmatter_field


def test_get_frontmatter_field_reads_block_scalar_description():
    src = "---\nname: x\ndescription: |\n  line1\n  line2\n---\nbody\n"
    assert get_frontmatter_field(src, "description") == "line1\nline2"


def test_get_frontmatter_field_reads_folded_scalar():
    src = "---\nname: x\ndescription: >\n  line1\n  line2\n---\n"
    assert get_frontmatter_field(src, "description") == "line1\nline2"


def test_get_frontmatter_field_missing_returns_none():
    src = "---\nname: x\n---\n"
    assert get_frontmatter_field(src, "description") is None


def test_get_frontmatter_field_keeps_plain_scalar_text():
    src = "---\nenabled: true\n---\n"
    assert get_frontmatter_field(src, "enabled") == "true"
