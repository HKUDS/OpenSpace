"""Regression: multiline frontmatter values must round-trip via parse_frontmatter."""

from __future__ import annotations

from openspace.skill_engine.skill_utils import parse_frontmatter, set_frontmatter_field


def test_set_frontmatter_field_preserves_multiline_description():
    out = set_frontmatter_field("---\nname: x\n---\n", "description", "line1\nline2")
    assert parse_frontmatter(out)["description"] == "line1\nline2"


def test_set_frontmatter_field_replaces_block_scalar_without_orphan_lines():
    src = "---\nname: x\ndescription: |\n  old1\n  old2\n---\nbody\n"
    out = set_frontmatter_field(src, "description", "new1\nnew2")
    assert parse_frontmatter(out)["description"] == "new1\nnew2"
    assert "old1" not in out
    assert "old2" not in out
