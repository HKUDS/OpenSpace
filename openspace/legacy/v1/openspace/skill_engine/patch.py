"""patch — Multi-file patch application and diff generation for Skills.

A skill is a directory containing ``SKILL.md`` and optional auxiliary files
(scripts, configs, examples). Three LLM output formats are supported:

  FULL  — Complete file content (single-file or multi-file via ``*** Begin Files``)
  DIFF  — SEARCH/REPLACE blocks (single-file, applied to SKILL.md)
  PATCH — ``*** Begin Patch`` multi-file format (supports Add / Update / Delete across multiple files)

Auto-detection (``PatchType.AUTO``) inspects the LLM output and dispatches
to the correct parser.

Three skill operations:
  fix_skill    — in-place repair of an existing skill directory
  derive_skill — copy directory → apply changes in the copy
  create_skill — create a brand-new skill directory (for CAPTURED)
"""

from __future__ import annotations

import difflib
import re
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from .fuzzy_match import fuzzy_find_match
from .skill_utils import normalize_frontmatter
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

SKILL_FILENAME = "SKILL.md"

# Sidecar file that stores the persistent skill_id — excluded from diffs/snapshots
_SKILL_ID_FILENAME = ".skill_id"


def _normalize_skill_frontmatter(skill_dir: Path) -> None:
    """Re-quote SKILL.md frontmatter in-place so values are valid YAML."""
    skill_file = skill_dir / SKILL_FILENAME
    if not skill_file.exists():
        return
    try:
        raw = skill_file.read_text(encoding="utf-8")
        normalized = normalize_frontmatter(raw)
        if normalized != raw:
            skill_file.write_text(normalized, encoding="utf-8")
    except Exception as e:
        logger.debug(f"frontmatter normalize skipped for {skill_dir.name}: {e}")


class PatchType(str, Enum):
    """LLM output format for skill edits."""
    AUTO  = "auto"   # Auto-detect from content
    FULL  = "full"   # Complete content (single or multi-file)
    DIFF  = "diff"   # SEARCH/REPLACE blocks (single-file)
    PATCH = "patch"  # *** Begin Patch multi-file format


@dataclass
class UpdateChunk:
    """A single change block inside an Update File hunk."""
    old_lines: List[str]
    new_lines: List[str]
    change_context: Optional[str] = None
    is_end_of_file: bool = False


@dataclass
class PatchHunk:
    """One file-level operation inside a patch."""
    type: str  # "add" | "update" | "delete"
    path: str
    contents: str = ""                # type="add": new file body
    move_path: Optional[str] = None   # type="update": optional rename target
    chunks: List[UpdateChunk] = field(default_factory=list)


@dataclass
class PatchResult:
    """Parsed representation of a ``*** Begin Patch`` block."""
    hunks: List[PatchHunk]


@dataclass
class SkillEditResult:
    """Result of a skill edit operation.

    Attributes:
        skill_dir: Final skill directory location.
        content_diff: Combined unified diff (git diff format) covering all
            modified files, for lineage recording.
        content_snapshot: Full directory snapshot ``{relative_path: content}``,
            for lineage recording.
        error: Non-None means the operation failed.
    """
    skill_dir: Path = field(default_factory=lambda: Path("."))
    content_diff: str = ""
    content_snapshot: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class PatchError(RuntimeError):
    """Raised when a patch cannot be applied."""
    pass


class PatchParseError(PatchError):
    """Raised when the patch text cannot be parsed."""
    pass


PATCH_PATTERN = re.compile(
    r"<{7}\s*SEARCH\s*\n(.*?)\n\s*={7}\s*\n(.*?)\n\s*>{7}\s*REPLACE\s*",
    re.DOTALL,
)


def fix_skill(
    skill_dir: Path,
    content: str,
    patch_type: PatchType = PatchType.AUTO,
) -> SkillEditResult:
    """In-place repair of an existing skill directory.

    Applies the LLM output to the skill directory, overwrites files on disk,
    returns a combined diff and snapshot for lineage recording.

    Args:
        skill_dir: Existing skill directory.
        content: LLM output (FULL, DIFF, or PATCH format).
        patch_type: Output format (AUTO to auto-detect).
    """
    if not skill_dir.is_dir():
        return SkillEditResult(error=f"Skill directory not found: {skill_dir}")
    skill_file = skill_dir / SKILL_FILENAME
    if not skill_file.exists():
        return SkillEditResult(error=f"SKILL.md not found: {skill_file}")

    # Snapshot before edit
    old_files = _collect_files(skill_dir)

    # Resolve patch type
    if patch_type == PatchType.AUTO:
        patch_type = detect_patch_type(content)

    try:
        if patch_type == PatchType.PATCH:
            _apply_multi_file_patch(content, skill_dir)
        elif patch_type == PatchType.FULL:
            _apply_multi_file_full(content, skill_dir)
        elif patch_type == PatchType.DIFF:
            _apply_search_replace_to_file(content, skill_file)
        else:
            return SkillEditResult(error=f"Unknown patch type: {patch_type}")
    except PatchError as e:
        return SkillEditResult(error=str(e))
    except Exception as e:
        return SkillEditResult(error=f"Unexpected error: {e}")

    _normalize_skill_frontmatter(skill_dir)

    # Snapshot after edit
    new_files = _collect_files(skill_dir)
    diff = _compute_files_diff(old_files, new_files)

    logger.info(f"fix_skill: {skill_dir.name} ({patch_type.value})")
    return SkillEditResult(
        skill_dir=skill_dir,
        content_diff=diff,
        content_snapshot=new_files,
    )

def derive_skill(
    source_dirs: Union[Path, List[Path]],
    target_dir: Path,
    content: str,
    patch_type: PatchType = PatchType.AUTO,
) -> SkillEditResult:
    """Derive a new skill from one or more existing skills.

    **Single parent** (``source_dirs`` is one Path):
      Copies parent directory → applies LLM output.  Supports PATCH / DIFF / FULL.

    **Multiple parents** (``source_dirs`` is a list of Paths):
      Creates a brand-new directory and applies LLM output as FULL or PATCH
      (DIFF not supported for multi-parent — no single base to search/replace).
      ``content_diff`` is empty for multi-parent (no meaningful single-parent
      diff); ``content_snapshot`` captures the full result for lineage tracking.

    Source directories stay unchanged in both cases.

    Args:
        source_dirs: Parent skill directory (or list for multi-parent merge).
        target_dir: New skill directory (must not exist).
        content: LLM output.
        patch_type: Output format (AUTO to auto-detect).
    """
    # Normalise to list
    if isinstance(source_dirs, Path):
        sources = [source_dirs]
    else:
        sources = list(source_dirs)

    if not sources:
        return SkillEditResult(error="derive_skill requires at least one source directory")
    if target_dir.exists():
        return SkillEditResult(error=f"Target already exists: {target_dir}")

    # Validate all sources
    for sd in sources:
        if not sd.is_dir():
            return SkillEditResult(error=f"Source does not exist: {sd}")
        if not (sd / SKILL_FILENAME).exists():
            return SkillEditResult(error=f"Source SKILL.md not found: {sd / SKILL_FILENAME}")

    first_source = sources[0]
    is_multi_parent = len(sources) > 1

    if is_multi_parent:
        # Multi-parent merge: create new directory, apply content (FULL or PATCH)
        if patch_type == PatchType.AUTO:
            patch_type = detect_patch_type(content)
        if patch_type == PatchType.DIFF:
            # DIFF (SEARCH/REPLACE) is not meaningful for merge — no single base
            patch_type = PatchType.FULL

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if patch_type == PatchType.PATCH:
                _apply_multi_file_patch(content, target_dir)
            else:
                _apply_multi_file_full(content, target_dir)
        except (PatchError, Exception) as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            return SkillEditResult(error=str(e))
    else:
        # Single parent: copy → apply (original behaviour)
        shutil.copytree(first_source, target_dir)

        if patch_type == PatchType.AUTO:
            patch_type = detect_patch_type(content)

        try:
            if patch_type == PatchType.PATCH:
                _apply_multi_file_patch(content, target_dir)
            elif patch_type == PatchType.FULL:
                _apply_multi_file_full(content, target_dir)
            elif patch_type == PatchType.DIFF:
                _apply_search_replace_to_file(content, target_dir / SKILL_FILENAME)
            else:
                shutil.rmtree(target_dir, ignore_errors=True)
                return SkillEditResult(error=f"Unknown patch type: {patch_type}")
        except (PatchError, Exception) as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            return SkillEditResult(error=str(e))

    _normalize_skill_frontmatter(target_dir)

    new_files = _collect_files(target_dir)

    # Single parent: diff against the parent.  Multi-parent: no meaningful diff → empty.
    # (content_snapshot already captures the full result for lineage tracking.)
    diff = compute_skill_diff(first_source, target_dir) if not is_multi_parent else ""

    src_names = " + ".join(sd.name for sd in sources)
    logger.info(f"derive_skill: {src_names} → {target_dir.name} ({patch_type.value})")
    return SkillEditResult(
        skill_dir=target_dir,
        content_diff=diff,
        content_snapshot=new_files,
    )

def create_skill(
    target_dir: Path,
    content: str,
    patch_type: PatchType = PatchType.AUTO,
) -> SkillEditResult:
    """Create a brand-new skill directory (for CAPTURED).

    Args:
        target_dir: New skill directory (must not exist).
        content: LLM output — complete skill content.
        patch_type: Output format (AUTO to auto-detect, usually FULL).
    """
    if target_dir.exists():
        return SkillEditResult(error=f"Target already exists: {target_dir}")

    if patch_type == PatchType.AUTO:
        patch_type = detect_patch_type(content)

    try:
        target_dir.mkdir(parents=True, exist_ok=True)

        if patch_type == PatchType.PATCH:
            # PATCH with only Add File hunks
            _apply_multi_file_patch(content, target_dir)
        elif patch_type == PatchType.FULL:
            _apply_multi_file_full(content, target_dir)
        elif patch_type == PatchType.DIFF:
            # For CAPTURED, DIFF doesn't make sense — treat as single-file FULL
            (target_dir / SKILL_FILENAME).write_text(content, encoding="utf-8")
        else:
            shutil.rmtree(target_dir, ignore_errors=True)
            return SkillEditResult(error=f"Unknown patch type: {patch_type}")
    except (PatchError, Exception) as e:
        shutil.rmtree(target_dir, ignore_errors=True)
        return SkillEditResult(error=str(e))

    _normalize_skill_frontmatter(target_dir)

    new_files = _collect_files(target_dir)
    # Add-all diff (everything is new)
    add_all = "\n".join(
        compute_unified_diff("", text, filename=name)
        for name, text in sorted(new_files.items())
        if compute_unified_diff("", text, filename=name)
    )

    logger.info(f"create_skill: {target_dir.name} ({patch_type.value})")
    return SkillEditResult(
        skill_dir=target_dir,
        content_diff=add_all,
        content_snapshot=new_files,
    )

def detect_patch_type(content: str) -> PatchType:
    """Auto-detect the patch format from LLM output.

    Detection uses **structural markers** that are unlikely to appear in
    normal skill prose, checked in specificity order:

      1. ``*** Begin Patch``  → PATCH (multi-file diff)
      2. ``*** Begin Files``  → FULL  (multi-file envelope)
      3. ``*** File:`` as a standalone marker line → FULL (multi-file, no envelope)
      4. ``<<<<<<< SEARCH``   → DIFF  (single-file SEARCH/REPLACE)
      5. Default              → FULL  (single-file complete content)

    For (3), we require ``*** File:`` to appear at the start of a line
    (with optional leading whitespace) AND the content to contain at least
    two such markers or one marker followed by meaningful content — this
    avoids false positives when a skill happens to mention the marker in
    prose (e.g. inside a code example).
    """
    if "*** Begin Patch" in content:
        return PatchType.PATCH
    if "*** Begin Files" in content:
        return PatchType.FULL

    # Detect bare *** File: markers (no *** Begin Files envelope).
    # Must appear at line start; require at least one to look structural.
    file_header_hits = _FILE_HEADER_RE.findall(content)
    if file_header_hits:
        return PatchType.FULL

    if "<<<<<<< SEARCH" in content:
        return PatchType.DIFF
    return PatchType.FULL


#  MULTI-FILE FULL FORMAT
#  Format:
#   *** Begin Files
#   *** File: SKILL.md
#   (complete file content, no prefix)
#   *** File: examples/helper.sh
#   (complete file content)
#   *** End Files
#
# Or just raw content (single-file fallback to SKILL.md).

_FILE_HEADER_RE = re.compile(r"^\*\*\*\s*File:\s*(.+)$", re.MULTILINE)


def parse_multi_file_full(content: str) -> Dict[str, str]:
    """Parse ``*** Begin Files`` format into ``{relative_path: content}``.

    Falls back to ``{SKILL.md: content}`` if no markers are found (single-file).
    """
    # Strip optional envelope
    stripped = content.strip()
    if stripped.startswith("*** Begin Files"):
        stripped = stripped[len("*** Begin Files"):].strip()
    # Strip *** End Files and anything after it (e.g. stray <EVOLUTION_COMPLETE> tokens)
    end_files_idx = stripped.rfind("*** End Files")
    if end_files_idx != -1:
        stripped = stripped[:end_files_idx].strip()

    # Find all *** File: headers
    headers = list(_FILE_HEADER_RE.finditer(stripped))
    if not headers:
        # No multi-file markers — treat entire content as SKILL.md
        return {SKILL_FILENAME: content}

    files: Dict[str, str] = {}
    for i, match in enumerate(headers):
        file_path = match.group(1).strip()
        start = match.end()
        # Content extends to the next header or end of string
        if i + 1 < len(headers):
            end = headers[i + 1].start()
        else:
            end = len(stripped)
        file_content = stripped[start:end].strip("\n")
        # Preserve one trailing newline for non-empty files
        if file_content and not file_content.endswith("\n"):
            file_content += "\n"
        files[file_path] = file_content

    return files


def _apply_multi_file_full(content: str, skill_dir: Path) -> None:
    """Apply multi-file FULL format to a skill directory.

    For each file in the parsed output:
    - If the path exists → overwrite
    - If the path is new → create (with parent dirs)
    """
    files = parse_multi_file_full(content)

    for rel_path, file_content in files.items():
        # Security: ensure path stays within skill_dir
        target = (skill_dir / rel_path).resolve()
        if not str(target).startswith(str(skill_dir.resolve())):
            raise PatchError(f"Path escapes skill directory: {rel_path}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_content, encoding="utf-8")
        logger.debug(f"FULL write: {rel_path}")


#  MULTI-FILE PATCH FORMAT
# Format:
#   *** Begin Patch
#   *** Add File: <path>
#   +line1
#   +line2
#   *** Update File: <path>
#   @@ context line
#   -old line
#   +new line
#   *** Delete File: <path>
#   *** End Patch

Comparator = Callable[[str, str], bool]


def _try_match(
    lines: List[str],
    pattern: List[str],
    start_index: int,
    compare: Comparator,
    eof: bool,
) -> int:
    """Attempt to find *pattern* inside *lines* using *compare*."""
    n = len(lines)
    p = len(pattern)
    if p == 0:
        return -1

    if eof:
        from_end = n - p
        if from_end >= start_index:
            if all(compare(lines[from_end + j], pattern[j]) for j in range(p)):
                return from_end

    for i in range(start_index, n - p + 1):
        if all(compare(lines[i + j], pattern[j]) for j in range(p)):
            return i

    return -1


# Unicode normalisation (from ShinkaEvolve)
_UNICODE_REPLACEMENTS: Dict[str, str] = {
    "\u2018": "'", "\u2019": "'", "\u201A": "'", "\u201B": "'",
    "\u201C": '"', "\u201D": '"', "\u201E": '"', "\u201F": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-",
    "\u2026": "...",
    "\u00A0": " ",
}
_UNICODE_RE = re.compile("|".join(re.escape(k) for k in _UNICODE_REPLACEMENTS))


def _normalize_unicode(s: str) -> str:
    return _UNICODE_RE.sub(lambda m: _UNICODE_REPLACEMENTS[m.group()], s)


def seek_sequence(
    lines: List[str],
    pattern: List[str],
    start_index: int,
    eof: bool = False,
) -> int:
    """4-level degrading search for a line pattern inside *lines*.

    Returns the 0-based index of the first matching position, or -1.
    """
    if not pattern:
        return -1

    # Pass 1: exact
    idx = _try_match(lines, pattern, start_index, lambda a, b: a == b, eof)
    if idx != -1:
        return idx

    # Pass 2: rstrip
    idx = _try_match(
        lines, pattern, start_index,
        lambda a, b: a.rstrip() == b.rstrip(), eof,
    )
    if idx != -1:
        return idx

    # Pass 3: strip
    idx = _try_match(
        lines, pattern, start_index,
        lambda a, b: a.strip() == b.strip(), eof,
    )
    if idx != -1:
        return idx

    # Pass 4: Unicode-normalised + strip
    idx = _try_match(
        lines, pattern, start_index,
        lambda a, b: _normalize_unicode(a.strip()) == _normalize_unicode(b.strip()),
        eof,
    )
    return idx

def _parse_patch_header(
    lines: List[str], idx: int,
) -> Optional[Tuple[str, Optional[str], int]]:
    """Parse a ``*** Add/Delete/Update File:`` header line."""
    line = lines[idx]

    if line.startswith("*** Add File:"):
        file_path = line.split(":", 1)[1].strip()
        return (file_path, None, idx + 1) if file_path else None

    if line.startswith("*** Delete File:"):
        file_path = line.split(":", 1)[1].strip()
        return (file_path, None, idx + 1) if file_path else None

    if line.startswith("*** Update File:"):
        file_path = line.split(":", 1)[1].strip()
        if not file_path:
            return None
        move_path: Optional[str] = None
        next_idx = idx + 1
        if next_idx < len(lines) and lines[next_idx].startswith("*** Move to:"):
            move_path = lines[next_idx].split(":", 1)[1].strip()
            next_idx += 1
        return (file_path, move_path, next_idx)

    return None

def _parse_add_file_content(
    lines: List[str], start_idx: int,
) -> Tuple[str, int]:
    """Collect ``+``-prefixed lines for an Add File hunk."""
    content_lines: List[str] = []
    i = start_idx
    while i < len(lines) and not lines[i].startswith("***"):
        if lines[i].startswith("+"):
            content_lines.append(lines[i][1:])
        i += 1
    content = "\n".join(content_lines)
    if content.endswith("\n"):
        content = content[:-1]
    return content, i

def _parse_update_chunks(
    lines: List[str], start_idx: int,
) -> Tuple[List[UpdateChunk], int]:
    """Parse ``@@``-delimited change chunks for an Update File hunk."""
    chunks: List[UpdateChunk] = []
    i = start_idx
    while i < len(lines) and not lines[i].startswith("***"):
        if lines[i].startswith("@@"):
            context_line = lines[i][2:].strip()
            i += 1
            old_lines: List[str] = []
            new_lines: List[str] = []
            is_end_of_file = False

            while i < len(lines):
                cl = lines[i]
                if cl.startswith("@@"):
                    break
                if cl.startswith("***") and cl != "*** End of File":
                    break
                if cl == "*** End of File":
                    is_end_of_file = True
                    i += 1
                    break
                if cl.startswith(" "):
                    old_lines.append(cl[1:])
                    new_lines.append(cl[1:])
                elif cl.startswith("-"):
                    old_lines.append(cl[1:])
                elif cl.startswith("+"):
                    new_lines.append(cl[1:])
                i += 1

            chunks.append(UpdateChunk(
                old_lines=old_lines,
                new_lines=new_lines,
                change_context=context_line or None,
                is_end_of_file=is_end_of_file,
            ))
        else:
            i += 1

    return chunks, i

def parse_patch(patch_text: str) -> PatchResult:
    """Parse a ``*** Begin Patch`` / ``*** End Patch`` block.

    Ported from ShinkaEvolve ``shinka/edit/apply_patch.py``.
    """
    lines = patch_text.strip().split("\n")

    begin_idx = -1
    end_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "*** Begin Patch":
            begin_idx = i
        elif stripped == "*** End Patch":
            end_idx = i

    if begin_idx == -1 or end_idx == -1 or begin_idx >= end_idx:
        raise PatchParseError(
            "Invalid patch format: missing or mis-ordered "
            "*** Begin Patch / *** End Patch markers"
        )

    hunks: List[PatchHunk] = []
    i = begin_idx + 1

    while i < end_idx:
        header = _parse_patch_header(lines, i)
        if header is None:
            i += 1
            continue

        file_path, move_path, next_idx = header

        if lines[i].startswith("*** Add File:"):
            content, next_idx = _parse_add_file_content(lines, next_idx)
            hunks.append(PatchHunk(type="add", path=file_path, contents=content))
            i = next_idx

        elif lines[i].startswith("*** Delete File:"):
            hunks.append(PatchHunk(type="delete", path=file_path))
            i = next_idx

        elif lines[i].startswith("*** Update File:"):
            chunks, next_idx = _parse_update_chunks(lines, next_idx)
            hunks.append(PatchHunk(
                type="update",
                path=file_path,
                move_path=move_path,
                chunks=chunks,
            ))
            i = next_idx
        else:
            i += 1

    return PatchResult(hunks=hunks)

def _compute_replacements(
    original_lines: List[str],
    file_path: str,
    chunks: List[UpdateChunk],
) -> List[Tuple[int, int, List[str]]]:
    """Compute (start, old_count, new_lines) replacement tuples."""
    replacements: List[Tuple[int, int, List[str]]] = []
    line_index = 0

    for chunk in chunks:
        # Context-based seeking
        if chunk.change_context:
            ctx_idx = seek_sequence(
                original_lines, [chunk.change_context], line_index,
            )
            if ctx_idx == -1:
                raise PatchError(
                    f"Cannot locate context anchor "
                    f"'{chunk.change_context}' in {file_path}"
                )
            line_index = ctx_idx

        # Pure addition (no old lines to match)
        if not chunk.old_lines:
            if original_lines and original_lines[-1] == "":
                insert_idx = len(original_lines) - 1
            else:
                insert_idx = len(original_lines)
            replacements.append((insert_idx, 0, chunk.new_lines))
            continue

        pattern = list(chunk.old_lines)
        new_slice = list(chunk.new_lines)
        found = seek_sequence(
            original_lines, pattern, line_index, chunk.is_end_of_file,
        )

        # Retry without trailing empty line
        if found == -1 and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = seek_sequence(
                original_lines, pattern, line_index, chunk.is_end_of_file,
            )

        if found != -1:
            replacements.append((found, len(pattern), new_slice))
            line_index = found + len(pattern)
        else:
            raise PatchError(
                f"Cannot find expected lines in {file_path}:\n"
                + "\n".join(chunk.old_lines)
            )

    replacements.sort(key=lambda x: x[0])
    return replacements

def _apply_replacements(
    lines: List[str],
    replacements: List[Tuple[int, int, List[str]]],
) -> List[str]:
    """Apply pre-sorted replacements in reverse order to avoid index shift."""
    result = list(lines)
    for start_idx, old_len, new_segment in reversed(replacements):
        del result[start_idx: start_idx + old_len]
        for j, line in enumerate(new_segment):
            result.insert(start_idx + j, line)
    return result

def apply_update_chunks(
    file_path: str,
    original_content: str,
    chunks: List[UpdateChunk],
) -> str:
    """Apply *chunks* to *original_content* and return the new content."""
    original_lines = original_content.split("\n")

    # Drop trailing empty element for consistent line counting
    if original_lines and original_lines[-1] == "":
        original_lines.pop()

    replacements = _compute_replacements(original_lines, file_path, chunks)
    new_lines = _apply_replacements(original_lines, replacements)

    # Ensure trailing newline
    if not new_lines or new_lines[-1] != "":
        new_lines.append("")

    return "\n".join(new_lines)

def _apply_multi_file_patch(patch_text: str, skill_dir: Path) -> None:
    """Parse and apply a ``*** Begin Patch`` block to a skill directory.

    Two-phase: validate all hunks first, then write to disk.
    """
    parsed = parse_patch(patch_text)
    if not parsed.hunks:
        raise PatchParseError("Patch contains no file operations")

    resolved_dir = skill_dir.resolve()

    # Phase 1: validate and compute new contents
    changes: List[Tuple[str, Path, str, str]] = []  # (type, abs_path, old, new)

    for hunk in parsed.hunks:
        abs_path = (skill_dir / hunk.path).resolve()

        # Security check
        if not str(abs_path).startswith(str(resolved_dir)):
            raise PatchError(f"Path escapes skill directory: {hunk.path}")

        if hunk.type == "add":
            new_content = hunk.contents
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            changes.append(("add", abs_path, "", new_content))

        elif hunk.type == "delete":
            if not abs_path.exists():
                raise PatchError(f"Cannot delete non-existent file: {hunk.path}")
            changes.append(("delete", abs_path, "", ""))

        elif hunk.type == "update":
            if not abs_path.exists():
                raise PatchError(f"Cannot update non-existent file: {hunk.path}")
            old_content = abs_path.read_text(encoding="utf-8")
            new_content = apply_update_chunks(str(hunk.path), old_content, hunk.chunks)
            changes.append(("update", abs_path, old_content, new_content))

    # Phase 2: write all changes
    for change_type, abs_path, _, new_content in changes:
        if change_type == "add":
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(new_content, encoding="utf-8")
            logger.debug(f"PATCH add: {abs_path.relative_to(resolved_dir)}")

        elif change_type == "delete":
            if abs_path.exists():
                abs_path.unlink()
            logger.debug(f"PATCH delete: {abs_path.relative_to(resolved_dir)}")

        elif change_type == "update":
            abs_path.write_text(new_content, encoding="utf-8")
            logger.debug(f"PATCH update: {abs_path.relative_to(resolved_dir)}")


#  SEARCH/REPLACE (single-file DIFF)
def apply_search_replace(
    patch_text: str,
    original: str,
    *,
    strict: bool = True,
) -> tuple[str, int, Optional[str]]:
    """Apply SEARCH/REPLACE blocks to a single file's content.

    Uses the 6-level fuzzy matching chain from ``fuzzy_match`` for
    robust matching.
    """
    new_text = original
    num_applied = 0

    blocks = list(PATCH_PATTERN.finditer(patch_text))
    if not blocks:
        return new_text, 0, None

    for block in blocks:
        search = _strip_trailing_ws(block.group(1))
        replace = _strip_trailing_ws(block.group(2))

        # Empty SEARCH → append at end
        if not search.strip():
            new_text = new_text.rstrip("\n") + "\n" + replace + "\n"
            num_applied += 1
            continue

        # Use fuzzy matching chain
        matched_search, pos = fuzzy_find_match(new_text, search)

        if pos != -1:
            new_text = new_text[:pos] + replace + new_text[pos + len(matched_search):]
            num_applied += 1
            continue

        # Not found
        if strict:
            first_line = search.splitlines()[0].strip() if search.splitlines() else ""
            similar = _find_similar_lines(first_line, new_text)
            msg_parts = [
                f"SEARCH text not found in {SKILL_FILENAME}",
                "",
                f"Looking for: {first_line!r}",
            ]
            if similar:
                msg_parts.append("")
                msg_parts.append("Similar lines found:")
                for line, line_num in similar:
                    msg_parts.append(f"  Line {line_num}: {line.strip()}")
            msg_parts.extend([
                "",
                "Ensure the SEARCH block matches the file content exactly.",
            ])
            return new_text, num_applied, "\n".join(msg_parts)

    return new_text, num_applied, None


def _apply_search_replace_to_file(
    patch_text: str, skill_file: Path,
) -> None:
    """Apply SEARCH/REPLACE blocks to a file on disk."""
    original = skill_file.read_text(encoding="utf-8")
    updated, num_applied, error = apply_search_replace(patch_text, original)
    if error:
        raise PatchError(error)
    if num_applied == 0:
        raise PatchError("No SEARCH/REPLACE blocks found in LLM output")
    skill_file.write_text(updated, encoding="utf-8")


#  DIFF COMPUTATION & SNAPSHOT
def compute_unified_diff(
    original: str,
    updated: str,
    *,
    filename: str = SKILL_FILENAME,
    context: int = 3,
) -> str:
    """Unified diff (git diff format) between two strings."""
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context,
    )
    return "".join(diff_lines)

def compute_skill_diff(old_dir: Path, new_dir: Path) -> str:
    """Compare all files in two skill directories, return combined diff."""
    old_files = _collect_files(old_dir) if old_dir.is_dir() else {}
    new_files = _collect_files(new_dir) if new_dir.is_dir() else {}

    all_names = sorted(set(old_files) | set(new_files))
    parts: list[str] = []
    for name in all_names:
        d = compute_unified_diff(
            old_files.get(name, ""),
            new_files.get(name, ""),
            filename=name,
        )
        if d:
            parts.append(d)
    return "\n".join(parts)

def collect_skill_snapshot(skill_dir: Path) -> Dict[str, str]:
    """Collect all text files in a skill directory.

    Returns ``{relative_path: content}``.  Binary files are silently skipped.
    """
    return _collect_files(skill_dir)

def _compute_files_diff(
    old_files: Dict[str, str],
    new_files: Dict[str, str],
) -> str:
    """Compute combined unified diff from two snapshot dicts."""
    all_names = sorted(set(old_files) | set(new_files))
    parts: list[str] = []
    for name in all_names:
        d = compute_unified_diff(
            old_files.get(name, ""),
            new_files.get(name, ""),
            filename=name,
        )
        if d:
            parts.append(d)
    return "\n".join(parts)

def _collect_files(directory: Path) -> Dict[str, str]:
    """Collect all text files in a directory (recursive).

    Excludes the ``.skill_id`` sidecar (internal metadata, not skill content).
    """
    files: Dict[str, str] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.name != _SKILL_ID_FILENAME:
            rel = str(p.relative_to(directory))
            try:
                files[rel] = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                pass
    return files

def _strip_trailing_ws(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines())

def _find_similar_lines(
    search_line: str,
    text: str,
    max_suggestions: int = 3,
) -> List[Tuple[str, int]]:
    """Fuzzy match for error reporting."""
    import difflib as _dl

    search_clean = search_line.strip()
    if not search_clean:
        return []

    results = []
    for i, line in enumerate(text.splitlines()):
        line_clean = line.strip()
        if not line_clean:
            continue
        ratio = _dl.SequenceMatcher(None, search_clean, line_clean).ratio()
        if ratio > 0.6:
            results.append((line, i + 1, ratio))

    results.sort(key=lambda x: x[2], reverse=True)
    return [(line, num) for line, num, _ in results[:max_suggestions]]
