"""Regression: trailing prose with braces must not drop valid analysis JSON."""

from __future__ import annotations

from openspace.skill_engine.analyzer import ExecutionAnalyzer
from openspace.skill_engine.evolution.capture_semantic import _extract_json as capture_extract_json


def test_analyzer_extract_json_tolerates_trailing_prose_braces():
    text = '{"task_completed": true}\n\nReplace {foo} with {bar}.'
    assert ExecutionAnalyzer._extract_json(text) == {"task_completed": True}


def test_capture_semantic_extract_json_tolerates_trailing_prose_braces():
    text = '{"ok": true}\n\nnote {x}'
    assert capture_extract_json(text) == {"ok": True}
