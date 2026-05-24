"""Shared utility functions for the skill engine.

Provides:
  - YAML frontmatter parsing/manipulation (unified across registry, evolver, etc.)
  - LLM output cleaning (markdown fence stripping, change summary extraction)
  - Skill content safety checking (regex-based moderation)
  - Skill directory validation
  - Text truncation
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

SKILL_FILENAME = "SKILL.md"

_SAFETY_RULES = [
    ("blocked.malware",         re.compile(r"(ClawdAuthenticatorTool)", re.IGNORECASE)),
    ("suspicious.keyword",      re.compile(r"(malware|stealer|phish|phishing|keylogger)", re.IGNORECASE)),
    ("suspicious.secrets",      re.compile(r"(api[-_ ]?key|token|password|private key|secret)", re.IGNORECASE)),
    ("suspicious.crypto",       re.compile(r"(wallet|seed phrase|mnemonic|crypto)", re.IGNORECASE)),
    ("suspicious.webhook",      re.compile(r"(discord\.gg|webhook|hooks\.slack)", re.IGNORECASE)),
    ("suspicious.script",       re.compile(r"(curl[^\n]+\|\s*(sh|bash))", re.IGNORECASE)),
    ("suspicious.url_shortener", re.compile(r"(bit\.ly|tinyurl\.com|t\.co|goo\.gl|is\.gd)", re.IGNORECASE)),
]

_BLOCKING_FLAGS = frozenset({"blocked.malware"})


def check_skill_safety(text: str) -> List[str]:
    """Check *text* against safety rules, return list of triggered flag names.

    Returns an empty list if no rules match (= safe).
    """
    return [flag for flag, pat in _SAFETY_RULES if pat.search(text)]


def is_skill_safe(flags: List[str]) -> bool:
    """Return True if *flags* contain no blocking flag.

    ``suspicious.*`` flags are informational (logged / attached to search
    results) but do NOT block.  Only ``blocked.*`` flags cause rejection.
    """
    return not any(f in _BLOCKING_FLAGS for f in flags)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# Characters that require YAML value quoting (colon-space, hash-space,
# or values starting with special YAML indicators).
_YAML_NEEDS_QUOTE_RE = re.compile(r"[:\#\[\]{}&*!|>'\"%@`]")


def _yaml_quote(value: str) -> str:
    """Quote a YAML scalar value if it contains special characters."""
    if "\n" in value:
        trailing_newlines = len(value) - len(value.rstrip("\n"))
        if trailing_newlines == 0:
            chomping = "|-"
        elif trailing_newlines == 1:
            chomping = "|"
        else:
            chomping = "|+"
        lines = value.split("\n")
        if trailing_newlines and lines and lines[-1] == "":
            lines = lines[:-1]
        return chomping + "\n" + "\n".join(f"  {line}" for line in lines)
    if not value or not _YAML_NEEDS_QUOTE_RE.search(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_unquote(value: str) -> str:
    """Strip surrounding quotes and unescape a YAML scalar value."""
    if len(value) >= 2:
        if (value[0] == '"' and value[-1] == '"') or \
           (value[0] == "'" and value[-1] == "'"):
            inner = value[1:-1]
            if value[0] == '"':
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            else:
                inner = inner.replace("''", "'")
            return inner
    return value


_BLOCK_SCALAR_HEADER_RE = re.compile(r"^([>|])([+-]?)([1-9]?)$")


def _parse_yaml_lines(lines: list[str]) -> dict[str, Any]:
    """Parse a flat YAML mapping.

    Supports inline scalars (quoted or bare) and block scalars
    (>, |, >-, |-, >+, |+, with optional indent indicator).
    """
    fm: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" not in line:
            i += 1
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            i += 1
            continue

        parent_indent = len(line) - len(line.lstrip(" "))
        value = value.strip()
        block_header = _BLOCK_SCALAR_HEADER_RE.match(value)
        if not block_header:
            fm[key] = _yaml_unquote(value)
            i += 1
            continue

        style, chomping, indent_indicator = block_header.groups()
        explicit_indent = int(indent_indicator) if indent_indicator else None
        block_indent: int | None = explicit_indent
        block_lines: list[str] = []
        i += 1

        while i < len(lines):
            continuation = lines[i]
            if continuation.strip():
                indent = len(continuation) - len(continuation.lstrip(" "))
                if block_indent is None:
                    if indent <= parent_indent:
                        break
                    block_indent = indent
                if indent < block_indent:
                    break
                block_lines.append(continuation[block_indent:])
            else:
                block_lines.append("")
            i += 1

        parsed = _render_block_scalar(block_lines, style, chomping)
        fm[key] = parsed

    return fm


def _render_block_scalar(lines: list[str], style: str, chomping: str) -> str:
    """Render collected block scalar lines according to the supported subset."""
    if style == "|":
        value = "\n".join(lines)
    else:
        paragraphs: list[str] = []
        current: list[str] = []
        for line in lines:
            if line == "":
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
            else:
                current.append(line)
        if current:
            paragraphs.append(" ".join(current))
        value = "\n".join(paragraphs)

    trailing_empty_count = 0
    for line in reversed(lines):
        if line == "":
            trailing_empty_count += 1
        else:
            break

    value = value.rstrip("\n")
    if chomping == "-":
        return value
    if chomping == "+":
        return value + ("\n" * (trailing_empty_count + 1))
    return value + "\n"


def parse_frontmatter(content: str) -> Dict[str, Any]:
    """Parse YAML frontmatter into a flat dict.

    Dependency-free parser for flat mappings.
    Handles quoted/unquoted values and YAML block scalars.
    Returns ``{}`` if no valid frontmatter is found.
    """
    if not content.startswith("---"):
        return {}
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}
    return _parse_yaml_lines(match.group(1).split("\n"))


def get_frontmatter_field(content: str, field_name: str) -> Optional[str]:
    """Extract a single field value from YAML frontmatter.

    Returns ``None`` if the field is absent or content has no frontmatter.
    """
    if not content.startswith("---"):
        return None
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None
    fm = _parse_yaml_lines(match.group(1).split("\n"))
    value = fm.get(field_name)
    return value if isinstance(value, str) else None


def set_frontmatter_field(content: str, field_name: str, value: str) -> str:
    """Set (or insert) a field in YAML frontmatter.

    Values containing YAML special characters (``:``, ``#``, etc.) are
    automatically double-quoted to produce valid YAML.

    If *content* has no frontmatter, a new one is prepended.
    """
    quoted = _yaml_quote(value)
    if not content.startswith("---"):
        return f"---\n{field_name}: {quoted}\n---\n{content}"

    match = _FRONTMATTER_RE.match(content)
    if not match:
        return content

    fm_text = match.group(1)
    new_line = f"{field_name}: {quoted}"
    found = False
    new_lines = []
    for line in fm_text.split("\n"):
        if ":" in line and line.split(":", 1)[0].strip() == field_name:
            new_lines.append(new_line)
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(new_line)

    new_fm = "\n".join(new_lines)
    return f"---\n{new_fm}\n---{content[match.end():]}"


def normalize_frontmatter(content: str) -> str:
    """Re-serialize frontmatter with proper YAML quoting.

    Parses the existing frontmatter, then re-writes each value through
    :func:`_yaml_quote` so that colons, hashes, and other special
    characters are safely double-quoted.  The body after ``---`` is
    preserved verbatim.

    Returns *content* unchanged if no frontmatter is found.
    """
    if not content.startswith("---"):
        return content
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return content

    fm = parse_frontmatter(content)
    if not fm:
        return content

    safe_lines = [f"{k}: {_yaml_quote(v)}" for k, v in fm.items()]
    new_fm = "\n".join(safe_lines)
    return f"---\n{new_fm}\n---{content[match.end():]}"


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from markdown content."""
    if content.startswith("---"):
        match = re.match(r"^---\n.*?\n---\n?", content, re.DOTALL)
        if match:
            return content[match.end():].strip()
    return content

def strip_markdown_fences(text: str) -> str:
    """Remove surrounding markdown code fences if present.

    Handles common LLM wrapping patterns:
      - ````` ```markdown ```, ````` ```md ```, ````` ``` ```, ````` ```text `````
      - Nested triple-backtick pairs (outermost only)
      - Leading/trailing whitespace around fences
    """
    text = text.strip()

    # Pattern: opening ``` with optional language tag, content, closing ```
    m = re.match(
        r"^```(?:markdown|md|text|yaml|diff|patch)?\s*\n(.*?)\n```\s*$",
        text,
        re.DOTALL,
    )
    if m:
        return m.group(1).strip()

    # Some LLMs emit ``````` (4+ backticks) as outer fence
    m = re.match(
        r"^`{3,}(?:\w+)?\s*\n(.*?)\n`{3,}\s*$",
        text,
        re.DOTALL,
    )
    if m:
        return m.group(1).strip()

    return text


_CHANGE_SUMMARY_RE = re.compile(
    r"^[\s*_]*(?:CHANGE[\s_-]?SUMMARY)\s*[:：]\s*(.+)",
    re.IGNORECASE,
)


def extract_change_summary(content: str) -> tuple[str, str]:
    """Extract ``CHANGE_SUMMARY`` from LLM output.

    Returns ``(clean_content, change_summary)``.
    """
    lines = content.split("\n")

    # Find the first non-blank line
    first_nonblank = -1
    for i, line in enumerate(lines):
        if line.strip():
            first_nonblank = i
            break

    if first_nonblank == -1:
        return content, ""

    m = _CHANGE_SUMMARY_RE.match(lines[first_nonblank])
    if not m:
        return content, ""

    # Strip markdown bold/italic markers (** or __) from both ends
    summary = m.group(1).strip().strip("*_").strip()

    # Skip blank lines after the summary line to find content start
    content_start = first_nonblank + 1
    while content_start < len(lines) and not lines[content_start].strip():
        content_start += 1

    rest = "\n".join(lines[content_start:])
    return rest.strip(), summary

def validate_skill_dir(skill_dir: Path) -> Optional[str]:
    """Validate a skill directory after edit application.

    Returns None if valid, or an error message string.
    Checks:
      1. Directory exists
      2. SKILL.md exists and is non-empty
      3. SKILL.md has valid YAML frontmatter with ``name`` field
      4. No empty files (warning-level, not blocking)
    """
    if not skill_dir.exists():
        return f"Skill directory does not exist: {skill_dir}"

    skill_file = skill_dir / SKILL_FILENAME
    if not skill_file.exists():
        return f"SKILL.md not found in {skill_dir}"

    try:
        content = skill_file.read_text(encoding="utf-8")
    except Exception as e:
        return f"Cannot read SKILL.md: {e}"

    if not content.strip():
        return "SKILL.md is empty"

    # Check frontmatter
    if not content.startswith("---"):
        return "SKILL.md missing YAML frontmatter (should start with '---')"

    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return "SKILL.md has malformed YAML frontmatter (missing closing '---')"

    # Check for required 'name' field in frontmatter
    name = get_frontmatter_field(content, "name")
    if not name:
        return "SKILL.md frontmatter missing 'name' field"

    # Non-blocking checks: log warnings for empty auxiliary files
    for p in skill_dir.rglob("*"):
        if p.is_file() and p != skill_file:
            try:
                if p.stat().st_size == 0:
                    logger.warning(f"Validation: empty auxiliary file: {p.relative_to(skill_dir)}")
            except OSError:
                pass

    return None


def truncate(text: str, max_chars: int) -> str:
    """Truncate *text* to *max_chars* with an ellipsis marker."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
