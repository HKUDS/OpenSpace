"""Regression: mid-document *** File: prose must not drop SKILL.md."""

from __future__ import annotations

from pathlib import Path

from openspace.skill_engine.patch import (
    PatchType,
    create_skill,
    detect_patch_type,
    parse_multi_file_full,
)


def test_detect_patch_type_ignores_single_mid_document_file_marker():
    content = "# My Skill\n\n*** File: helper.sh\n#!/bin/bash\necho hi\n"
    assert detect_patch_type(content) == PatchType.FULL
    parsed = parse_multi_file_full(content)
    assert list(parsed.keys()) == ["SKILL.md"]
    assert "*** File: helper.sh" in parsed["SKILL.md"]


def test_create_skill_keeps_skill_md_when_file_marker_is_prose(tmp_path: Path):
    content = "# My Skill\n\n*** File: helper.sh\n#!/bin/bash\necho hi\n"
    result = create_skill(tmp_path / "s", content)
    assert result.ok
    skill_md = tmp_path / "s" / "SKILL.md"
    assert skill_md.is_file()
    assert "*** File: helper.sh" in skill_md.read_text()
    assert not (tmp_path / "s" / "helper.sh").exists()


def test_detect_patch_type_accepts_structural_multi_file_at_start():
    content = "*** File: SKILL.md\n# Skill\n\n*** File: helper.sh\n#!/bin/bash\necho hi\n"
    assert detect_patch_type(content) == PatchType.FULL
    parsed = parse_multi_file_full(content)
    assert "SKILL.md" in parsed
    assert "helper.sh" in parsed
