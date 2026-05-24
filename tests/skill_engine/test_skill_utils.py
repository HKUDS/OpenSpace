import sys
import types

sys.modules.setdefault("colorama", types.SimpleNamespace(init=lambda **_: None))

from openspace.skill_engine.skill_utils import (
    get_frontmatter_field,
    normalize_frontmatter,
    parse_frontmatter,
)


def _content(frontmatter: str) -> str:
    return f"---\n{frontmatter}\n---\nBody\n"


def test_inline_unquoted():
    assert parse_frontmatter(_content("description: hello"))["description"] == "hello"


def test_inline_double_quoted():
    assert (
        parse_frontmatter(_content('description: "hello: world"'))["description"]
        == "hello: world"
    )


def test_inline_single_quoted():
    assert parse_frontmatter(_content("description: 'don''t'"))["description"] == "don't"


def test_block_folded():
    fm = parse_frontmatter(_content("description: >\n  line1\n  line2"))
    assert fm["description"] == "line1 line2\n"


def test_block_literal():
    fm = parse_frontmatter(_content("description: |\n  line1\n  line2"))
    assert fm["description"] == "line1\nline2\n"


def test_block_folded_strip():
    fm = parse_frontmatter(_content("description: >-\n  line1\n  line2"))
    assert fm["description"] == "line1 line2"


def test_block_literal_keep_all():
    fm = parse_frontmatter(_content("description: |+\n  line1\n  line2\n\n"))
    assert fm["description"] == "line1\nline2\n\n\n"


def test_block_with_blank_line_in_folded():
    fm = parse_frontmatter(_content("description: >\n  line1\n\n  line2"))
    assert fm["description"] == "line1\nline2\n"


def test_block_followed_by_next_key():
    fm = parse_frontmatter(_content("description: >-\n  line1\n  line2\nname: next"))
    assert fm["description"] == "line1 line2"
    assert fm["name"] == "next"


def test_get_frontmatter_field_block_scalar():
    content = _content("description: >-\n  line1\n  line2")
    assert get_frontmatter_field(content, "description") == "line1 line2"


def test_normalize_roundtrip_block_scalar():
    content = _content("description: >-\n  foo\n  bar")
    normalized = normalize_frontmatter(content)
    assert parse_frontmatter(normalized)["description"] == "foo bar"


def test_normalize_preserves_existing_inline():
    content = _content("description: hello\nname: skill")
    assert normalize_frontmatter(content) == content


FIXTURE = """---
name: by-codex-delegation
description: >
  Decide whether to delegate implementation to Codex vs implement inline,
  write the SPARC brief, and hand off correctly.
triggers:
  - 'delegate to codex'
---

# By Codex Delegation
Body content.
"""


def test_fixture_realworld():
    fm = parse_frontmatter(FIXTURE)
    assert fm["name"] == "by-codex-delegation"
    assert "Decide whether to delegate" in fm["description"]
    assert "SPARC brief" in fm["description"]
    assert fm["description"] != ">"
