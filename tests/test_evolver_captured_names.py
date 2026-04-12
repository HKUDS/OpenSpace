from openspace.skill_engine.evolver import (
    _extract_generated_frontmatter_field,
    _fallback_captured_skill_name,
    _set_generated_frontmatter_field,
)


def test_extract_generated_frontmatter_field_from_single_file() -> None:
    content = """---
name: safe-file-write
description: Write files with validation.
---

# Safe File Write
"""

    assert _extract_generated_frontmatter_field(content, "name") == "safe-file-write"
    assert (
        _extract_generated_frontmatter_field(content, "description")
        == "Write files with validation."
    )


def test_extract_generated_frontmatter_field_from_multi_file_full() -> None:
    content = """*** Begin Files
*** File: SKILL.md
---
name: local-acceptance-entry
description: Run a repo's backend and frontend together for review.
---

# Local Acceptance Entry
*** File: examples/start.sh
#!/usr/bin/env bash
echo start
*** End Files
"""

    assert (
        _extract_generated_frontmatter_field(content, "name")
        == "local-acceptance-entry"
    )
    assert (
        _extract_generated_frontmatter_field(content, "description")
        == "Run a repo's backend and frontend together for review."
    )


def test_set_generated_frontmatter_field_updates_skill_md_in_multi_file_full() -> None:
    content = """*** Begin Files
*** File: SKILL.md
---
description: Run a repo's backend and frontend together for review.
---

# Local Acceptance Entry
*** File: examples/start.sh
#!/usr/bin/env bash
echo start
*** End Files
"""

    updated = _set_generated_frontmatter_field(content, "name", "local-acceptance-entry")

    assert "*** File: SKILL.md" in updated
    assert "name: local-acceptance-entry" in updated
    assert "*** File: examples/start.sh" in updated


def test_fallback_captured_skill_name_uses_direction_signal() -> None:
    direction = (
        "Capture a reusable workflow for adding a local acceptance entry in "
        "split backend/frontend repos: create one canonical foreground acceptance script."
    )

    fallback = _fallback_captured_skill_name(direction, None)

    assert fallback.startswith("local-acceptance-entry-in-split-backend")
    assert len(fallback) <= 50
