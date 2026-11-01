"""SkillRegistry — discover, load, match, and inject skills.

Skills follow the official SKILL.md format:
  - YAML frontmatter with ``name`` and ``description``
  - Markdown body with instructions (loaded only after selection)

Skills are discovered from user-configured directories and matched to
tasks via LLM-based selection (with keyword fallback).

Skill identity:
  Every skill directory may contain a ``.skill_id`` sidecar file that
  stores the persistent unique identifier.  On **first discovery**
  (no ``.skill_id`` file present), an ID is generated and written to
  the file.  On subsequent runs the ID is **read** from the file —
  this makes the ID portable (survives directory moves, machine changes)
  and deterministic (never regenerated).

  Imported skills: ``{directory_name}__imp_{uuid_hex[:8]}``
  Evolved skills:  ``{directory_name}__v{gen}_{uuid_hex[:8]}``  (written by evolver)
"""

from __future__ import annotations

import json
import fnmatch
import hashlib
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING

from openspace.utils.logging import Logger
from openspace.telemetry.call_source import reset_call_source, set_call_source
from .skill_utils import parse_frontmatter, strip_frontmatter, check_skill_safety, is_skill_safe
from .skill_ranker import SkillRanker, SkillCandidate, PREFILTER_THRESHOLD

if TYPE_CHECKING:
    from openspace.llm import LLMClient

logger = Logger.get_logger(__name__)

# Sidecar filename that stores the persistent skill_id
SKILL_ID_FILENAME = ".skill_id"
_SKILL_ROOTS = {
    "claude": Path(".claude") / "skills",
    "openspace": Path(".openspace") / "skills",
    "codex": Path(".agents") / "skills",
}
_METADATA_ONLY_READ_CHARS = 64 * 1024


def _is_path_gitignored(path: Path, root: Path | None) -> bool:
    """Return whether ``path`` is ignored under ``root`` using git semantics."""

    if root is None:
        return False
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    if not str(rel) or str(rel) == ".":
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", rel.as_posix()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _resolve_dir(path: str | Path) -> Path:
    expanded = Path(path).expanduser()
    try:
        return expanded.resolve()
    except OSError:
        return expanded.absolute()


def _read_skill_metadata_content(path: Path) -> str:
    """Read enough of SKILL.md to parse frontmatter without loading the body."""

    text = path.read_text(encoding="utf-8")[:_METADATA_ONLY_READ_CHARS]
    if not text.startswith("---"):
        return text
    marker = "\n---"
    closing = text.find(marker, len("---"))
    if closing < 0:
        return text
    end = closing + len(marker)
    if end < len(text) and text[end : end + 1] == "\n":
        end += 1
    return text[:end]


def _skill_file_fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _skill_body_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _iter_skill_directories(root: Path) -> List[Path]:
    """Yield skill directories under a root, including nested category trees."""

    if not root.exists() or not root.is_dir():
        return []
    found: List[Path] = []
    for skill_file in sorted(root.rglob("SKILL.md")):
        skill_dir = skill_file.parent
        nested_inside_skill = False
        for parent in skill_dir.parents:
            if parent == root:
                break
            if (parent / "SKILL.md").exists():
                nested_inside_skill = True
                break
        if nested_inside_skill:
            continue
        found.append(skill_dir)
    return found


def default_project_skill_roots(
    project_root: str | Path,
    *,
    cwd: str | Path | None = None,
) -> list[Path]:
    """Return default project-level skill roots in discovery priority order.

    OpenSpace keeps existing OpenSpace/OpenSpace root precedence, then adds
    Codex-compatible ``.agents/skills`` roots from cwd upward to project_root.
    """

    root = _resolve_dir(project_root)
    roots = [
        root / _SKILL_ROOTS["claude"],
        root / _SKILL_ROOTS["openspace"],
    ]

    start = _resolve_dir(cwd or root)
    try:
        start.relative_to(root)
    except ValueError:
        start = root

    for parent in (start, *start.parents):
        roots.append(parent / _SKILL_ROOTS["codex"])
        if parent == root:
            break
    return roots


def default_user_skill_roots(home: str | Path | None = None) -> list[Path]:
    """Return default user-level skill roots in discovery priority order."""

    base = Path(home).expanduser() if home is not None else Path.home()
    return [
        base / _SKILL_ROOTS["claude"],
        base / _SKILL_ROOTS["openspace"],
        base / _SKILL_ROOTS["codex"],
    ]

_KNOWN_FRONTMATTER_FIELDS = {
    "name",
    "description",
    "allowed-tools",
    "paths",
    "disable-model-invocation",
    "user-invocable",
    "model",
    "effort",
    "hooks",
    "when_to_use",
    "when-to-use",
    "argument-hint",
    "arguments",
    "version",
    "context",
    "agent",
    "shell",
}

_LOW_RISK_OVERLAY_FIELDS = {
    "description",
    "when_to_use",
    "when-to-use",
    "argument-hint",
    "arguments",
    "version",
    "paths",
}


def _read_or_create_skill_id(name: str, skill_dir: Path) -> str:
    """Read ``skill_id`` from ``.skill_id`` sidecar, or create one.

    The sidecar file is a single-line plain-text file containing only
    the ``skill_id`` string.  It lives alongside ``SKILL.md`` inside
    the skill directory.

    First call (no file): generates ``{name}__imp_{uuid8}`` and writes it.
    Subsequent calls: reads and returns the existing ID.
    """
    id_file = skill_dir / SKILL_ID_FILENAME
    if id_file.exists():
        try:
            existing = id_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass  # fall through to generate

    # Generate a new ID and persist
    new_id = f"{name}__imp_{uuid.uuid4().hex[:8]}"
    try:
        id_file.write_text(new_id + "\n", encoding="utf-8")
        logger.debug(f"Created .skill_id for '{name}': {new_id}")
    except OSError as e:
        logger.warning(f"Cannot write {id_file}: {e} — ID will not persist across restarts")
    return new_id


def write_skill_id(
    skill_dir: Path,
    skill_id: str,
    *,
    raise_on_error: bool = False,
) -> None:
    """Write (or overwrite) the ``.skill_id`` sidecar in *skill_dir*.

    Called by ``SkillEvolver`` after FIX / DERIVED / CAPTURED to stamp
    the new ``skill_id`` into the skill directory so that the next
    ``discover()`` picks it up correctly.
    """
    id_file = skill_dir / SKILL_ID_FILENAME
    try:
        id_file.write_text(skill_id + "\n", encoding="utf-8")
    except OSError as e:
        if raise_on_error:
            raise
        logger.warning(f"Cannot write {id_file}: {e}")


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "inherit":
        return None
    return text


def _parse_bool_frontmatter(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_list_frontmatter(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [item for item in _split_frontmatter_list(text)]


def _split_frontmatter_list(text: str) -> List[str]:
    items: List[str] = []
    current: list[str] = []
    quote: str | None = None
    escape = False
    brace_depth = 0
    bracket_depth = 0
    for char in text:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\" and quote:
            current.append(char)
            escape = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            continue
        if char == "{":
            brace_depth += 1
            current.append(char)
            continue
        if char == "}":
            brace_depth = max(0, brace_depth - 1)
            current.append(char)
            continue
        if char == "[":
            bracket_depth += 1
            current.append(char)
            continue
        if char == "]":
            bracket_depth = max(0, bracket_depth - 1)
            current.append(char)
            continue
        if char in {",", ";", "\n"} and brace_depth == 0 and bracket_depth == 0:
            item = "".join(current).strip().strip("\"'")
            if item:
                items.append(item)
            current = []
            continue
        current.append(char)
    item = "".join(current).strip().strip("\"'")
    if item:
        items.append(item)
    return items


def _parse_argument_names_frontmatter(value: Any) -> List[str]:
    """Parse OpenSpace skill argument names.

    OpenSpace accepts either an array or a whitespace-separated string. Numeric names
    are ignored because they conflict with the ``$0``/``$1`` shorthand.
    """

    def _valid(name: Any) -> str:
        text = str(name).strip()
        if not text or text.isdigit():
            return ""
        return text

    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [name for item in value if (name := _valid(item))]

    text = str(value).strip()
    if not text:
        return []
    return [name for part in re.split(r"\s+", text) if (name := _valid(part))]


def _parse_skill_arguments(args: str) -> List[str]:
    """Parse SkillTool args with OpenSpace shell-quote semantics."""

    if not args or not args.strip():
        return []
    try:
        return [part for part in shlex.split(args, posix=True) if part]
    except ValueError:
        return [part for part in re.split(r"\s+", args) if part]


def _substitute_skill_arguments(
    content: str,
    args: str | None,
    *,
    argument_names: Sequence[str] = (),
    append_if_no_placeholder: bool = True,
) -> str:
    """Substitute OpenSpace SkillTool argument placeholders.

    Order matches OpenSpace ``argumentSubstitution.ts``:
    named args -> ``$ARGUMENTS[n]`` -> ``$n`` -> ``$ARGUMENTS`` -> optional
    ``ARGUMENTS: ...`` append when no placeholder was present.
    """

    if args is None:
        return content

    parsed_args = _parse_skill_arguments(args)
    original = content

    for idx, raw_name in enumerate(argument_names):
        name = str(raw_name or "").strip()
        if not name:
            continue
        value = parsed_args[idx] if idx < len(parsed_args) else ""
        content = re.sub(
            rf"\${re.escape(name)}(?![\[\w])",
            lambda _match, value=value: value,
            content,
        )
        content = content.replace(f"${{{name}}}", value)

    content = re.sub(
        r"\$ARGUMENTS\[(\d+)\]",
        lambda match: (
            parsed_args[int(match.group(1))]
            if int(match.group(1)) < len(parsed_args)
            else ""
        ),
        content,
    )
    content = re.sub(
        r"\$(\d+)(?!\w)",
        lambda match: (
            parsed_args[int(match.group(1))]
            if int(match.group(1)) < len(parsed_args)
            else ""
        ),
        content,
    )
    content = content.replace("$ARGUMENTS", args)
    content = content.replace("${ARGUMENTS}", args)

    if content == original and append_if_no_placeholder and args:
        content = f"{content}\n\nARGUMENTS: {args}"
    return content


def _parse_paths_frontmatter(value: Any) -> List[str]:
    paths = _parse_list_frontmatter(value)
    result: List[str] = []
    for path in paths:
        normalized = path[:-3] if path.endswith("/**") else path
        if normalized and normalized != "**":
            result.append(normalized)
    return result


def _parse_dict_frontmatter(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_shell_frontmatter(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"bash", "powershell"}:
        return normalized
    logger.warning(
        "Frontmatter 'shell: %s' is not recognized; valid values are bash or powershell. "
        "Falling back to bash.",
        value,
    )
    return None


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _skill_path_pattern_matches(path: str, pattern: str) -> bool:
    """Match OpenSpace skill ``paths`` globs.

    Python's ``fnmatch`` treats ``src/**/*.py`` as requiring at least one
    directory below ``src`` on some versions.  OpenSpace's glob semantics use ``**``
    as zero-or-more directories, so try both the direct pattern and a
    zero-directory variant.
    """

    normalized_path = path.strip("/").replace("\\", "/")
    normalized_pattern = pattern.strip("/").replace("\\", "/")
    for expanded in _expand_brace_glob(normalized_pattern):
        if _segment_glob_matches(normalized_path, expanded):
            return True
        if "/**/" in expanded and _segment_glob_matches(
            normalized_path,
            expanded.replace("/**/", "/"),
        ):
            return True
        if expanded.endswith("/**"):
            prefix = expanded[:-3].rstrip("/")
            return normalized_path == prefix or normalized_path.startswith(prefix + "/")
    return False


def _expand_brace_glob(pattern: str) -> List[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    before = pattern[: match.start()]
    after = pattern[match.end() :]
    expanded: List[str] = []
    for option in match.group(1).split(","):
        expanded.extend(_expand_brace_glob(before + option.strip() + after))
    return expanded


def _segment_glob_matches(path: str, pattern: str) -> bool:
    path_parts = [part for part in path.split("/") if part]
    pattern_parts = [part for part in pattern.split("/") if part]
    return _segment_glob_parts_match(path_parts, pattern_parts)


def _segment_glob_parts_match(path_parts: List[str], pattern_parts: List[str]) -> bool:
    if not pattern_parts:
        return not path_parts
    head = pattern_parts[0]
    if head == "**":
        return any(
            _segment_glob_parts_match(path_parts[index:], pattern_parts[1:])
            for index in range(len(path_parts) + 1)
        )
    if not path_parts:
        return False
    return fnmatch.fnmatchcase(path_parts[0], head) and _segment_glob_parts_match(
        path_parts[1:],
        pattern_parts[1:],
    )


@dataclass
class SkillMeta:
    """Metadata for a discovered skill.

    ``skill_id`` is the globally unique identifier used throughout the
    system — LLM prompts, database, evolution, and selection all
    reference this field.
    """

    skill_id: str          # Unique — persisted in .skill_id sidecar
    name: str              # Invocation name — always the skill directory name
    description: str
    path: Path             # Absolute path to SKILL.md
    display_name: Optional[str] = None  # Frontmatter ``name`` for UI/docs only
    source: str = "project"
    loaded_from: str = "skills"
    user_invocable: bool = True
    disable_model_invocation: bool = False
    allowed_tools: List[str] = field(default_factory=list)
    model: Optional[str] = None
    effort: Optional[str] = None
    hooks: Dict[str, Any] = field(default_factory=dict)
    conditional_paths: List[str] = field(default_factory=list)
    when_to_use: Optional[str] = None
    argument_hint: Optional[str] = None
    argument_names: List[str] = field(default_factory=list)
    version: Optional[str] = None
    execution_context: Optional[str] = None
    agent: Optional[str] = None
    shell: Optional[str] = None
    raw_frontmatter: Dict[str, Any] = field(default_factory=dict)
    unknown_fields: Dict[str, Any] = field(default_factory=dict)
    body_verified: bool = True
    file_mtime_ns: int = 0
    file_size_bytes: int = 0
    body_sha256: Optional[str] = None


@dataclass
class SkillDiagnostic:
    path: Path
    severity: str
    kind: str
    message: str
    details: Optional[str] = None


class SkillRegistry:
    """Discover, load, select, and inject skills into agent context.

    Args:
        skill_dirs: Ordered list of directories to scan.  Earlier entries have higher
            priority — a skill in the first dir shadows one with the same name
            in later dirs.

    All internal maps are keyed by ``skill_id``, not ``name``.
    """

    def __init__(
        self,
        skill_dirs: Optional[List[Path]] = None,
        skill_dir_sources: Optional[Dict[Path | str, str]] = None,
        skill_dir_loaded_from: Optional[Dict[Path | str, str]] = None,
        skill_override_dir: Optional[Path] = None,
        metadata_only_discovery: bool = False,
    ) -> None:
        self._skill_dirs: List[Path] = skill_dirs or []
        self._skill_dir_sources = {
            str(Path(k).resolve()): str(v)
            for k, v in (skill_dir_sources or {}).items()
        }
        self._skill_dir_loaded_from = {
            str(Path(k).resolve()): str(v)
            for k, v in (skill_dir_loaded_from or {}).items()
        }
        self._skills: Dict[str, SkillMeta] = {}     # skill_id -> SkillMeta
        self._content_cache: Dict[str, str] = {}     # skill_id -> raw SKILL.md content
        self._diagnostics: List[SkillDiagnostic] = []
        self._discovered = False
        self._ranker: Optional[SkillRanker] = None   # lazy-init on first use
        self._skill_override_dir = Path(skill_override_dir) if skill_override_dir else None
        self._metadata_only_discovery = bool(metadata_only_discovery)

    def discover(self) -> List[SkillMeta]:
        """Scan all skill_dirs and populate the registry.

        Each skill is a sub-directory containing a ``SKILL.md`` file.
        The ``skill_id`` is read from the ``.skill_id`` sidecar (created
        automatically on first discovery). Two skills with the same
        ``name`` in different directories get different IDs and can
        coexist in the registry and database.
        """
        self._skills.clear()
        self._content_cache.clear()
        self._diagnostics.clear()

        for skill_dir in self._skill_dirs:
            if not skill_dir.exists():
                logger.debug(f"Skill dir does not exist, skipping: {skill_dir}")
                continue

            for entry in _iter_skill_directories(skill_dir):
                skill_file = entry / "SKILL.md"

                try:
                    content = (
                        _read_skill_metadata_content(skill_file)
                        if self._metadata_only_discovery
                        else skill_file.read_text(encoding="utf-8")
                    )
                    format_issues = self._collect_skill_format_issues(content)
                    for issue in format_issues:
                        self._record_diagnostic(
                            path=skill_file,
                            severity="warn",
                            kind="format_warning",
                            message=f"Skill '{entry.name}' format issue",
                            details=issue,
                        )

                    # Full-body safety is deferred in metadata-only mode; the
                    # Skill tool verifies before injecting the body.
                    safety_flags = check_skill_safety(content)
                    if not is_skill_safe(safety_flags):
                        self._record_diagnostic(
                            path=skill_file,
                            severity="warn",
                            kind="blocked",
                            message=f"Blocked skill '{entry.name}' by safety policy",
                            details=", ".join(safety_flags) if safety_flags else None,
                        )
                        logger.warning(
                            f"BLOCKED skill {entry.name}: "
                            f"safety flags {safety_flags}"
                        )
                        continue

                    meta = self._parse_skill(
                        entry.name,
                        entry,
                        skill_file,
                        content,
                        body_verified=not self._metadata_only_discovery,
                    )
                    sid = meta.skill_id

                    if sid in self._skills:
                        logger.debug(f"Skill '{sid}' already discovered, skipping {skill_file}")
                        continue

                    self._skills[sid] = meta
                    if meta.body_verified:
                        self._content_cache[sid] = content
                    if safety_flags:
                        self._record_diagnostic(
                            path=skill_file,
                            severity="warn",
                            kind="safety_flag",
                            message=f"Skill '{entry.name}' loaded with safety flags",
                            details=", ".join(safety_flags),
                        )
                        logger.debug(f"Discovered skill: {sid} (safety: {safety_flags})")
                    else:
                        logger.debug(f"Discovered skill: {sid} — {meta.description[:60]}")
                except Exception as e:
                    self._record_diagnostic(
                        path=skill_file,
                        severity="fail",
                        kind="parse_error",
                        message=f"Failed to parse skill '{entry.name}'",
                        details=str(e),
                    )
                    logger.warning(f"Failed to parse skill {skill_file}: {e}")

        self._discovered = True
        logger.info(
            f"Skill discovery complete: {len(self._skills)} skill(s) "
            f"from {len(self._skill_dirs)} dir(s)"
        )
        return list(self._skills.values())

    def list_skills(self) -> List[SkillMeta]:
        """List all discovered skills."""
        self._ensure_discovered()
        return list(self._skills.values())

    def get_diagnostics(self) -> List[SkillDiagnostic]:
        """Return parse / safety diagnostics collected during discovery."""
        self._ensure_discovered()
        return list(self._diagnostics)

    def get_skill(self, skill_id: str) -> Optional[SkillMeta]:
        """Get a skill by ``skill_id``."""
        self._ensure_discovered()
        return self._skills.get(skill_id)

    def get_skill_by_name(self, name: str) -> Optional[SkillMeta]:
        """Get a skill by ``name`` (first match).  Use ``get_skill`` when possible."""
        self._ensure_discovered()
        for meta in self._skills.values():
            if meta.name == name:
                return meta
        return None

    def resolve_skill_for_model(self, name: str) -> Optional[SkillMeta]:
        """Resolve a model-provided Skill tool name.

        OpenSpace accepts a leading slash for slash-command compatibility. OS also
        accepts ``skill_id`` for internal callers, while the model-facing
        listing continues to use ``name``.
        """
        self._ensure_discovered()
        normalized = str(name or "").strip()
        if normalized.startswith("/"):
            normalized = normalized[1:]
        if not normalized:
            return None
        return self._skills.get(normalized) or self.get_skill_by_name(normalized)

    def update_skill(self, old_skill_id: str, new_meta: SkillMeta) -> None:
        """Replace a skill entry after FIX evolution.

        Removes *old_skill_id* from the registry and inserts *new_meta*
        under its (new) ``skill_id``.  Content cache is refreshed from
        the filesystem.
        """
        self._skills.pop(old_skill_id, None)
        self._content_cache.pop(old_skill_id, None)

        self._skills[new_meta.skill_id] = new_meta
        if new_meta.path.exists():
            try:
                self._content_cache[new_meta.skill_id] = (
                    new_meta.path.read_text(encoding="utf-8")
                )
            except Exception:
                pass
        logger.debug(
            f"Registry.update_skill: {old_skill_id} → {new_meta.skill_id}"
        )

    def add_skill(self, meta: SkillMeta) -> None:
        """Register a newly-created skill (DERIVED / CAPTURED).

        Does NOT overwrite an existing entry with the same ``skill_id``.
        """
        if meta.skill_id in self._skills:
            logger.debug(
                f"Registry.add_skill: {meta.skill_id} already exists, skipping"
            )
            return
        self._skills[meta.skill_id] = meta
        if meta.path.exists():
            try:
                self._content_cache[meta.skill_id] = (
                    meta.path.read_text(encoding="utf-8")
                )
            except Exception:
                pass
        logger.debug(f"Registry.add_skill: {meta.skill_id}")

    def load_skill_from_dir(self, skill_dir: Path) -> Optional[SkillMeta]:
        """Parse one skill directory from disk using the full registry parser.

        Evolver writes skill files while the process is already running. This
        helper reloads the complete skill frontmatter contract immediately instead
        of constructing a minimal ``SkillMeta`` that would drop runtime fields
        until the next process restart.
        """

        skill_dir = Path(skill_dir)
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None
        content = skill_file.read_text(encoding="utf-8")
        meta = self._parse_skill(
            skill_dir.name,
            skill_dir,
            skill_file,
            content,
            body_verified=True,
        )
        self._content_cache[meta.skill_id] = content
        return meta

    # Hot-reload API (add external skills at runtime)
    def discover_from_dirs(self, extra_dirs: List[Path]) -> List[SkillMeta]:
        """Discover skills from additional directories and add to the registry.

        Unlike :meth:`discover`, this does **NOT** clear existing skills — it
        only adds new ones from the given directories. Useful for hot-loading
        external skills (e.g. host-agent skills, newly downloaded cloud skills).

        Safety: applies the same ``check_skill_safety`` / ``is_skill_safe``
        filtering as :meth:`discover` to prevent malicious external skills.

        Args:
            extra_dirs: Additional directories to scan.
        """
        added: List[SkillMeta] = []
        for skill_dir in extra_dirs:
            if not skill_dir.exists() or not skill_dir.is_dir():
                logger.debug(f"discover_from_dirs: skipping {skill_dir}")
                continue
            for entry in _iter_skill_directories(skill_dir):
                skill_file = entry / "SKILL.md"
                try:
                    content = skill_file.read_text(encoding="utf-8")
                    format_issues = self._collect_skill_format_issues(content)
                    for issue in format_issues:
                        self._record_diagnostic(
                            path=skill_file,
                            severity="warn",
                            kind="format_warning",
                            message=f"External skill '{entry.name}' format issue",
                            details=issue,
                        )

                    # Safety check (same as discover())
                    safety_flags = check_skill_safety(content)
                    if not is_skill_safe(safety_flags):
                        self._record_diagnostic(
                            path=skill_file,
                            severity="warn",
                            kind="blocked",
                            message=f"Blocked external skill '{entry.name}' by safety policy",
                            details=", ".join(safety_flags) if safety_flags else None,
                        )
                        logger.warning(
                            f"BLOCKED external skill {entry.name}: "
                            f"safety flags {safety_flags}"
                        )
                        continue

                    meta = self._parse_skill(entry.name, entry, skill_file, content)
                    if meta.skill_id in self._skills:
                        continue
                    self._skills[meta.skill_id] = meta
                    self._content_cache[meta.skill_id] = content
                    added.append(meta)
                    if safety_flags:
                        self._record_diagnostic(
                            path=skill_file,
                            severity="warn",
                            kind="safety_flag",
                            message=f"External skill '{entry.name}' loaded with safety flags",
                            details=", ".join(safety_flags),
                        )
                    logger.debug(f"Hot-registered: {meta.skill_id} — {meta.description[:60]}")
                except Exception as e:
                    self._record_diagnostic(
                        path=skill_file,
                        severity="fail",
                        kind="parse_error",
                        message=f"Failed to parse skill '{entry.name}'",
                        details=str(e),
                    )
                    logger.warning(f"Failed to parse skill {skill_file}: {e}")

        if added:
            logger.info(
                f"discover_from_dirs: {len(added)} new skill(s) from "
                f"{len(extra_dirs)} dir(s)"
            )
        return added

    def register_skill_dir(self, skill_dir: Path) -> Optional[SkillMeta]:
        """Register a single skill directory (hot-reload).

        Safety: applies ``check_skill_safety`` / ``is_skill_safe`` filtering.

        Args:
            skill_dir: Path to a directory containing ``SKILL.md``.

        Returns:
            :class:`SkillMeta` if newly registered or already present,
            ``None`` if the directory is invalid or the skill fails safety checks.
        """
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            logger.debug(f"register_skill_dir: no SKILL.md in {skill_dir}")
            return None
        try:
            content = skill_file.read_text(encoding="utf-8")
            format_issues = self._collect_skill_format_issues(content)
            for issue in format_issues:
                self._record_diagnostic(
                    path=skill_file,
                    severity="warn",
                    kind="format_warning",
                    message=f"Skill '{skill_dir.name}' format issue",
                    details=issue,
                )

            # Safety check (same as discover())
            safety_flags = check_skill_safety(content)
            if not is_skill_safe(safety_flags):
                self._record_diagnostic(
                    path=skill_file,
                    severity="warn",
                    kind="blocked",
                    message=f"Blocked skill '{skill_dir.name}' by safety policy",
                    details=", ".join(safety_flags) if safety_flags else None,
                )
                logger.warning(
                    f"BLOCKED skill {skill_dir.name}: "
                    f"safety flags {safety_flags}"
                )
                return None

            meta = self._parse_skill(skill_dir.name, skill_dir, skill_file, content)
            if meta.skill_id in self._skills:
                logger.debug(f"register_skill_dir: {meta.skill_id} already exists")
                return self._skills[meta.skill_id]
            self._skills[meta.skill_id] = meta
            self._content_cache[meta.skill_id] = content
            if safety_flags:
                self._record_diagnostic(
                    path=skill_file,
                    severity="warn",
                    kind="safety_flag",
                    message=f"Skill '{skill_dir.name}' loaded with safety flags",
                    details=", ".join(safety_flags),
                )
            logger.info(f"Hot-registered skill: {meta.skill_id}")
            return meta
        except Exception as e:
            self._record_diagnostic(
                path=skill_file,
                severity="fail",
                kind="parse_error",
                message=f"Failed to register skill '{skill_dir.name}'",
                details=str(e),
            )
            logger.warning(f"Failed to register skill {skill_dir}: {e}")
            return None

    def discover_skill_dirs_for_path(
        self,
        file_path: str | Path,
        *,
        cwd: str | Path | None = None,
    ) -> Dict[str, List[SkillMeta]]:
        """Discover nested OpenSpace/OS skill directories near a touched file.

        OpenSpace checks for nested ``.claude/skills`` directories after Read/Edit/Write.
        OS also accepts ``.openspace/skills`` so OpenSpace-native projects do not
        need a compatibility directory, and ``.agents/skills`` for Codex parity.
        """

        self._ensure_discovered()
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            base = Path(cwd).expanduser() if cwd else Path.cwd()
            path = base / path
        try:
            path = path.resolve()
        except OSError:
            path = path.absolute()

        root = Path(cwd).expanduser() if cwd else None
        if root is not None:
            try:
                root = root.resolve()
            except OSError:
                root = root.absolute()

        start = path if path.is_dir() else path.parent
        containers: list[Path] = []
        for parent in (start, *start.parents):
            if root is not None:
                try:
                    parent.relative_to(root)
                except ValueError:
                    break
                # CWD-level skill roots are already loaded at startup; dynamic
                # discovery only announces nested skill dirs below cwd.
                if parent == root:
                    break
                if _is_path_gitignored(parent, root):
                    continue
            for rel in _SKILL_ROOTS.values():
                container = parent / rel
                if not container.is_dir():
                    continue
                try:
                    resolved_container = container.resolve()
                except OSError:
                    resolved_container = container.absolute()
                if root is not None:
                    try:
                        resolved_container.relative_to(root)
                    except ValueError:
                        continue
                    if _is_path_gitignored(resolved_container, root):
                        continue
                if resolved_container not in containers:
                    containers.append(resolved_container)

        by_container: Dict[str, List[SkillMeta]] = {}
        for container in containers:
            self.discover_from_dirs([container])
            skills = [
                skill
                for skill in self.list_skills()
                if skill.path.parent.parent.resolve() == container.resolve()
            ]
            if skills:
                by_container[str(container)] = skills
        return by_container

    def activate_conditional_skills_for_path(
        self,
        file_path: str | Path,
        *,
        cwd: str | Path | None = None,
    ) -> List[SkillMeta]:
        """Return skills whose ``paths`` frontmatter matches a touched path."""

        self._ensure_discovered()
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            base = Path(cwd).expanduser() if cwd else Path.cwd()
            path = base / path
        try:
            absolute = path.resolve()
        except OSError:
            absolute = path.absolute()

        rel = str(absolute)
        if cwd:
            try:
                rel = str(absolute.relative_to(Path(cwd).expanduser().resolve()))
            except Exception:
                return []
        rel = rel.replace("\\", "/")

        matched: list[SkillMeta] = []
        for skill in self._skills.values():
            for pattern in skill.conditional_paths:
                normalized = str(pattern).strip().replace("\\", "/")
                if not normalized:
                    continue
                if _skill_path_pattern_matches(rel, normalized):
                    matched.append(skill)
                    break
                prefix = normalized.rstrip("/")
                if rel == prefix or rel.startswith(prefix + "/"):
                    matched.append(skill)
                    break
        return matched

    def write_runtime_overlay(
        self,
        skill_id: str,
        fields: Dict[str, Any],
        *,
        approved: bool = False,
        field_metadata: Dict[str, Any] | None = None,
    ) -> Path:
        """Persist analyzer/evolver-proposed runtime field overlays.

        Suggested fields are saved for review but never merged into runtime
        parsing. High-risk fields (permissions/hooks/shell/model/fork) only
        take effect after an explicit approval moves them into ``approved``.
        """

        if self._skill_override_dir is None:
            raise RuntimeError("SkillRegistry was not configured with skill_override_dir")
        safe_bucket = "approved" if approved else "suggested"
        self._skill_override_dir.mkdir(parents=True, exist_ok=True)
        path = self._skill_override_dir / f"{skill_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        bucket = data.setdefault(safe_bucket, {})
        for key, value in fields.items():
            bucket[key] = value
        if field_metadata:
            meta_bucket = data.setdefault(f"{safe_bucket}_meta", {})
            if not isinstance(meta_bucket, dict):
                meta_bucket = {}
                data[f"{safe_bucket}_meta"] = meta_bucket
            for key in fields:
                meta_value = field_metadata.get(key)
                if meta_value is not None:
                    meta_bucket[key] = meta_value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if approved:
            self._reload_skill_after_overlay_change(skill_id)
        return path

    def load_runtime_overlay(self, skill_id: str) -> Dict[str, Any]:
        """Load one runtime overlay file, returning an empty dict if absent."""

        if self._skill_override_dir is None:
            return {}
        path = self._skill_override_dir / f"{skill_id}.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def list_runtime_overlays(self) -> List[Dict[str, Any]]:
        """List runtime overlays with suggested/approved fields for review."""

        if self._skill_override_dir is None or not self._skill_override_dir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self._skill_override_dir.glob("*.json")):
            skill_id = path.stem
            data = self.load_runtime_overlay(skill_id)
            suggested = data.get("suggested") if isinstance(data, dict) else {}
            approved = data.get("approved") if isinstance(data, dict) else {}
            suggested_meta = data.get("suggested_meta") if isinstance(data, dict) else {}
            approved_meta = data.get("approved_meta") if isinstance(data, dict) else {}
            rows.append(
                {
                    "skill_id": skill_id,
                    "path": str(path),
                    "suggested": suggested if isinstance(suggested, dict) else {},
                    "approved": approved if isinstance(approved, dict) else {},
                    "suggested_meta": suggested_meta if isinstance(suggested_meta, dict) else {},
                    "approved_meta": approved_meta if isinstance(approved_meta, dict) else {},
                }
            )
        return rows

    def approve_runtime_overlay(
        self,
        skill_id: str,
        fields: Sequence[str] | None = None,
    ) -> List[str]:
        """Move suggested runtime fields into the approved bucket."""

        data = self.load_runtime_overlay(skill_id)
        suggested = data.get("suggested")
        if not isinstance(suggested, dict) or not suggested:
            return []
        requested = {str(field) for field in fields or [] if str(field).strip()}
        approved_fields = [
            key for key in suggested.keys()
            if not requested or key in requested
        ]
        if not approved_fields:
            return []
        approved = data.setdefault("approved", {})
        if not isinstance(approved, dict):
            approved = {}
            data["approved"] = approved
        suggested_meta = data.get("suggested_meta")
        if not isinstance(suggested_meta, dict):
            suggested_meta = {}
        approved_meta = data.setdefault("approved_meta", {})
        if not isinstance(approved_meta, dict):
            approved_meta = {}
            data["approved_meta"] = approved_meta
        for key in approved_fields:
            approved[key] = suggested.pop(key)
            if key in suggested_meta:
                approved_meta[key] = suggested_meta.pop(key)
        if not suggested:
            data.pop("suggested", None)
        if not suggested_meta:
            data.pop("suggested_meta", None)
        self._write_runtime_overlay_data(skill_id, data)
        self._reload_skill_after_overlay_change(skill_id)
        return approved_fields

    def reject_runtime_overlay(
        self,
        skill_id: str,
        fields: Sequence[str] | None = None,
    ) -> List[str]:
        """Remove suggested runtime fields without approving them."""

        data = self.load_runtime_overlay(skill_id)
        suggested = data.get("suggested")
        if not isinstance(suggested, dict) or not suggested:
            return []
        requested = {str(field) for field in fields or [] if str(field).strip()}
        rejected = [
            key for key in list(suggested.keys())
            if not requested or key in requested
        ]
        for key in rejected:
            suggested.pop(key, None)
        suggested_meta = data.get("suggested_meta")
        if isinstance(suggested_meta, dict):
            for key in rejected:
                suggested_meta.pop(key, None)
            if not suggested_meta:
                data.pop("suggested_meta", None)
        if not suggested:
            data.pop("suggested", None)
        self._write_runtime_overlay_data(skill_id, data)
        return rejected

    def _write_runtime_overlay_data(self, skill_id: str, data: Dict[str, Any]) -> Path:
        if self._skill_override_dir is None:
            raise RuntimeError("SkillRegistry was not configured with skill_override_dir")
        self._skill_override_dir.mkdir(parents=True, exist_ok=True)
        path = self._skill_override_dir / f"{skill_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _reload_skill_after_overlay_change(self, skill_id: str) -> None:
        """Refresh in-memory SkillMeta after approved overlay changes."""

        try:
            self._ensure_discovered()
            current = self._skills.get(skill_id)
            if current is None:
                return
            refreshed = self.load_skill_from_dir(current.path.parent)
            if refreshed is None:
                return
            self.update_skill(skill_id, refreshed)
            self._ranker = None
        except Exception:
            logger.debug("Failed to reload skill after overlay change", exc_info=True)

    @property
    def ranker(self) -> SkillRanker:
        """Lazy-initialised :class:`SkillRanker` for hybrid pre-filtering."""
        if self._ranker is None:
            self._ranker = SkillRanker()
        return self._ranker

    async def select_skills_with_llm(
        self,
        task_description: str,
        llm_client: "LLMClient",
        max_skills: int = 2,
        model: Optional[str] = None,
        skill_quality: Optional[Dict[str, Dict[str, Any]]] = None,
        candidate_skills: Optional[List[SkillMeta]] = None,
    ) -> tuple[List[SkillMeta], Optional[Dict[str, Any]]]:
        """Use an LLM to select the most relevant skills.

        When the local registry has more than ``PREFILTER_THRESHOLD`` skills,
        a **BM25 → embedding** pre-filter narrows the candidate set before
        sending to the LLM.  This avoids stuffing an overly long catalog
        into the prompt.

        Progressive disclosure: the LLM only sees skill *headers*
        (skill_id + description + quality stats), not the full SKILL.md
        content.  Full content is loaded only after selection.

        Args:
            task_description: The user's task instruction.
            llm_client: An initialised LLMClient used for the selection call.
            max_skills: Maximum number of skills to inject.
            model: Override model for this selection call.
                If None, falls back to ``llm_client``'s default model.
            skill_quality: Optional mapping ``{skill_id: {total_applied, total_completions, total_fallbacks}}``
                from :class:`SkillStore`.  When provided, skills with high
                fallback rates are filtered out and quality signals are
                included in the LLM selection prompt.
            candidate_skills: Optional candidate subset. ``None`` preserves the
                original behavior of selecting from the full registry.

        Returns:
            tuple[list[SkillMeta], dict | None]: (selected_skills, selection_record).
                selection_record contains the LLM conversation for logging.
        """
        self._ensure_discovered()
        if not task_description:
            return [], None

        available = (
            list(candidate_skills)
            if candidate_skills is not None
            else list(self._skills.values())
        )
        if not available:
            return [], None

        # Quality-based filtering: remove skills that consistently fail
        filtered_out: List[str] = []
        if skill_quality:
            kept: List[SkillMeta] = []
            for s in available:
                q = skill_quality.get(s.skill_id)
                if q:
                    if not bool(q.get("enabled", True)):
                        filtered_out.append(s.skill_id)
                        continue
                    selections = q.get("total_selections", 0)
                    applied = q.get("total_applied", 0)
                    completions = q.get("total_completions", 0)
                    fallbacks = q.get("total_fallbacks", 0)
                    quality_observations = applied + fallbacks
                    # Filter 1: observed multiple times but never completed.
                    # Selection can be counted even when analysis is disabled,
                    # so do not filter on raw selection count alone.
                    if quality_observations >= 2 and completions == 0:
                        filtered_out.append(s.skill_id)
                        continue
                    # Filter 2: high selected->failed rate.
                    if selections >= 2 and fallbacks / selections > 0.5:
                        filtered_out.append(s.skill_id)
                        continue
                kept.append(s)
            if filtered_out:
                logger.info(
                    f"Skill quality filter: removed {len(filtered_out)} "
                    f"high-fallback skill(s): {filtered_out}"
                )
            available = kept

        if not available:
            return [], None

        # Pre-filter when skill count exceeds threshold
        prefilter_used = False
        if len(available) > PREFILTER_THRESHOLD:
            available = self._prefilter_skills(task_description, available, max_skills)
            prefilter_used = True

        # Build a concise skills catalogue for the LLM (skill_id + description + quality)
        catalog_lines: List[str] = []
        for s in available:
            q = skill_quality.get(s.skill_id) if skill_quality else None
            if q:
                trust_label = str(q.get("trust_state") or "trusted")
                selections = q.get("total_selections", 0)
                applied = q.get("total_applied", 0)
                completions = q.get("total_completions", 0)
                if applied > 0:
                    rate = completions / applied
                    catalog_lines.append(
                        f"- **{s.skill_id}**: {s.description}  "
                        f"({trust_label}; success {completions}/{applied} = {rate:.0%})"
                    )
                elif selections > 0:
                    catalog_lines.append(
                        f"- **{s.skill_id}**: {s.description}  "
                        f"({trust_label}; selected {selections}x, never succeeded)"
                    )
                else:
                    catalog_lines.append(
                        f"- **{s.skill_id}**: {s.description}  ({trust_label}; new)"
                    )
            else:
                catalog_lines.append(f"- **{s.skill_id}**: {s.description}")
        skills_catalog = "\n".join(catalog_lines)

        prompt = self._build_skill_selection_prompt(
            task_description, skills_catalog, max_skills
        )

        selection_record: Dict[str, Any] = {
            "method": "llm",
            "task": task_description[:500],
            "available_skills": [s.skill_id for s in available],
            "filtered_out": filtered_out,
            "prefilter_used": prefilter_used,
            "prompt": prompt,
        }

        _src_tok = set_call_source("skill_select")

        try:
            llm_kwargs = {}
            if model:
                llm_kwargs["model"] = model
            call_model = getattr(
                llm_client,
                "call_model_with_fallback",
                llm_client.call_model,
            )
            resp = await call_model(
                messages=[{"role": "user", "content": prompt}],
                **llm_kwargs,
            )
            content = resp.assistant_message.get("content", "").strip()
            selected_ids, brief_plan = self._parse_skill_selection_response(content)

            selection_record["llm_response"] = content
            selection_record["parsed_ids"] = selected_ids
            selection_record["brief_plan"] = brief_plan

            # Validate ids against registry & cap
            result: List[SkillMeta] = []
            lookup = (
                {s.skill_id: s for s in available}
                if candidate_skills is not None
                else self._skills
            )
            for sid in selected_ids:
                if len(result) >= max_skills:
                    break
                meta = lookup.get(sid)
                if meta:
                    result.append(meta)
                else:
                    logger.debug(f"LLM selected unknown skill_id: {sid}")

            selection_record["selected"] = [s.skill_id for s in result]

            if result:
                ids = ", ".join(s.skill_id for s in result)
                logger.info(f"LLM skill selection: [{ids}]")
            else:
                logger.info("LLM decided no skills are relevant for this task")

            return result, selection_record

        except Exception as e:
            logger.warning(f"LLM skill selection failed: {e} — proceeding without skills")
            selection_record["error"] = str(e)
            selection_record["method"] = "llm_failed"
            selection_record["selected"] = []
            return [], selection_record
        finally:
            reset_call_source(_src_tok)

    def _prefilter_skills(
        self,
        task: str,
        available: List[SkillMeta],
        max_skills: int,
    ) -> List[SkillMeta]:
        """Narrow the candidate set using BM25 + embedding hybrid ranking.

        Keeps at most ``max(15, max_skills * 5)`` candidates for the LLM
        selection prompt.
        """
        prefilter_top_k = max(15, max_skills * 5)

        # Build SkillCandidate list
        candidates: List[SkillCandidate] = []
        for s in available:
            body = ""
            raw = self._content_cache.get(s.skill_id, "")
            if raw:
                body = strip_frontmatter(raw)

            candidates.append(SkillCandidate(
                skill_id=s.skill_id,
                name=s.name,
                description=s.description,
                body=body,
            ))

        ranked = self.ranker.hybrid_rank(task, candidates, top_k=prefilter_top_k)

        # Map back to SkillMeta
        ranked_ids = {c.skill_id for c in ranked}
        result = [s for s in available if s.skill_id in ranked_ids]

        if len(result) < len(available):
            logger.info(
                f"Skill pre-filter: {len(available)} → {len(result)} candidates "
                f"(BM25+embedding, threshold={PREFILTER_THRESHOLD})"
            )
        return result

    def load_skill_content(self, skill_id: str) -> Optional[str]:
        """Return the SKILL.md content (with frontmatter stripped) for *skill_id*."""
        self._ensure_discovered()
        meta = self._skills.get(skill_id)
        if meta is None:
            return None
        raw = self._content_cache.get(skill_id)
        if raw is not None and not self._cached_body_current(meta):
            self._content_cache.pop(skill_id, None)
            meta.body_verified = False
            meta.body_sha256 = None
            raw = None
        if raw is None:
            try:
                raw = meta.path.read_text(encoding="utf-8")
                format_issues = self._collect_skill_format_issues(raw)
                for issue in format_issues:
                    self._record_diagnostic(
                        path=meta.path,
                        severity="warn",
                        kind="format_warning",
                        message=f"Skill '{meta.name}' format issue",
                        details=issue,
                    )
                safety_flags = check_skill_safety(raw)
                if not is_skill_safe(safety_flags):
                    self._record_diagnostic(
                        path=meta.path,
                        severity="warn",
                        kind="blocked",
                        message=f"Blocked skill '{meta.name}' by safety policy",
                        details=", ".join(safety_flags) if safety_flags else None,
                    )
                    return None
                if safety_flags:
                    self._record_diagnostic(
                        path=meta.path,
                        severity="warn",
                        kind="safety_flag",
                        message=f"Skill '{meta.name}' loaded with safety flags",
                        details=", ".join(safety_flags),
                    )
                self._content_cache[skill_id] = raw
                meta.body_verified = True
                meta.file_mtime_ns, meta.file_size_bytes = _skill_file_fingerprint(
                    meta.path
                )
                meta.body_sha256 = _skill_body_hash(raw)
            except Exception as exc:
                self._record_diagnostic(
                    path=meta.path,
                    severity="fail",
                    kind="body_load_error",
                    message=f"Failed to load skill '{meta.name}' body",
                    details=str(exc),
                )
                logger.warning("Failed to load skill body %s: %s", meta.path, exc)
                return None
        if raw is None:
            return None
        return self._strip_frontmatter(raw)

    def _cached_body_current(self, meta: SkillMeta) -> bool:
        mtime_ns, size_bytes = _skill_file_fingerprint(meta.path)
        return (
            bool(meta.body_verified)
            and mtime_ns == meta.file_mtime_ns
            and size_bytes == meta.file_size_bytes
            and bool(meta.body_sha256)
        )

    def build_context_injection(
        self,
        skills: List[SkillMeta],
        backends: Optional[List[str]] = None,
    ) -> str:
        """Build a prompt fragment with the full content of *skills*.

        Injected as a system message into the agent's messages before the
        user instruction so the LLM reads skill guidance first.

        Args:
            skills: Skills to inject.
            backends: Active backend names (e.g. ``["shell", "mcp"]``).  Used to
                tailor the guidance so only actually available backends are
                mentioned.  ``None`` falls back to mentioning all backends.

        Key features:
        - Includes the skill directory path so the agent can resolve
          relative references to ``scripts/``, ``references/``, ``assets/``.
        - Replaces ``{baseDir}`` placeholders with the actual skill
          directory path (a convention used in some SKILL.md files).
        """
        parts: List[str] = []
        for skill in skills:
            content = self.load_skill_content(skill.skill_id)
            if content:
                # Resolve {baseDir} placeholder to the skill directory
                skill_dir = str(skill.path.parent)
                content = content.replace("{baseDir}", skill_dir)

                part = (
                    f"### Skill: {skill.skill_id}\n"
                    f"**Skill directory**: `{skill_dir}`\n\n"
                    f"{content}"
                )
                parts.append(part)

        if not parts:
            return ""

        # Build a backend hint that only mentions registered backends
        scope = set(backends) if backends else {"gui", "shell", "mcp", "web", "meta"}
        backend_names: List[str] = []
        if "mcp" in scope:
            backend_names.append("MCP")
        if "shell" in scope:
            backend_names.append("shell")
        if "gui" in scope:
            backend_names.append("GUI")
        tool_hint = ", ".join(backend_names) if backend_names else "available"

        resource_tip = (
            "Use `read` / `ls` / `write` for file operations"
            + (" and `bash` for shell commands" if "shell" in scope else "")
            + ". Paths in skill instructions are relative to the skill "
            "directory listed under each skill heading.\n\n"
        )

        header = (
            "# Active Skills\n\n"
            "The following skills provide **domain knowledge and tested procedures** "
            "relevant to this task.\n\n"
            "**How to use skills:**\n"
            "- If a skill contains **step-by-step procedures or commands**, follow them — "
            "they are verified workflows.\n"
            "- If a skill provides **reference information, best practices, or tool guides**, "
            "use it as context to inform your decisions.\n"
            f"- Skills supplement your available tools — you may use **any** tool "
            f"({tool_hint}) alongside skill guidance. "
            "Choose the best tool for each sub-step.\n\n"
            "**Resource access**: Each skill may include bundled resources "
            "(scripts, references, assets) in its skill directory. "
            + resource_tip
        )
        return header + "\n\n---\n\n".join(parts)

    def build_skill_invocation_context(
        self,
        skill: SkillMeta,
        *,
        args: str = "",
    ) -> str:
        """Build the full prompt loaded by the OpenSpace Skill tool."""

        content = self.load_skill_content(skill.skill_id)
        if content is None:
            return ""
        skill_dir = str(skill.path.parent)
        content = content.replace("{baseDir}", skill_dir)
        content = content.replace("${CLAUDE_SKILL_DIR}", skill_dir)
        content = content.replace(
            "${CLAUDE_SESSION_ID}",
            os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("OPENSPACE_SESSION_ID")
            or "",
        )
        content = _substitute_skill_arguments(
            content,
            args,
            argument_names=skill.argument_names,
        )

        return (
            f"<command-name>{skill.name}</command-name>\n"
            f"# Skill: {skill.name}\n"
            f"**Skill ID**: `{skill.skill_id}`\n"
            f"**Skill directory**: `{skill_dir}`\n\n"
            f"{content}"
        )

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()

    def _record_diagnostic(
        self,
        *,
        path: Path,
        severity: str,
        kind: str,
        message: str,
        details: Optional[str] = None,
    ) -> None:
        self._diagnostics.append(
            SkillDiagnostic(
                path=path,
                severity=severity,
                kind=kind,
                message=message,
                details=details,
            )
        )

    @staticmethod
    def _collect_skill_format_issues(content: str) -> List[str]:
        frontmatter = parse_frontmatter(content)
        issues: List[str] = []
        if not content.startswith("---"):
            issues.append("missing YAML frontmatter")
            return issues
        if not frontmatter:
            issues.append("frontmatter could not be parsed")
            return issues
        if not frontmatter.get("name"):
            issues.append("frontmatter is missing 'name'")
        if not frontmatter.get("description"):
            issues.append("frontmatter is missing 'description'")
        return issues

    def _parse_skill(
        self,
        dir_name: str,
        skill_dir: Path,
        skill_file: Path,
        content: str,
        *,
        body_verified: bool = True,
    ) -> SkillMeta:
        """Parse a SKILL.md file into a SkillMeta.

        Reads Skill Protocol frontmatter fields while tolerating unknown
        fields. ``skill_id`` remains OpenSpace's stable sidecar identity;
        ``name`` remains the model/user-facing Skill tool input and follows OpenSpace:
        it is the skill directory name, not frontmatter ``name``.
        """
        frontmatter = parse_frontmatter(content)
        name = str(dir_name)
        display_name = _optional_str(frontmatter.get("name")) or name
        description = str(frontmatter.get("description", display_name))
        skill_id = _read_or_create_skill_id(name, skill_dir)
        frontmatter = self._merge_runtime_overlay(skill_id, frontmatter)
        display_name = _optional_str(frontmatter.get("name")) or display_name
        description = str(frontmatter.get("description", description))
        source = self._source_for_skill_dir(skill_dir)
        loaded_from = self._loaded_from_for_skill_dir(skill_dir)
        context_value = str(frontmatter.get("context", "")).strip().lower()
        raw_frontmatter = dict(frontmatter)
        unknown_fields = {
            str(key): value
            for key, value in raw_frontmatter.items()
            if key not in _KNOWN_FRONTMATTER_FIELDS and _has_meaningful_value(value)
        }
        file_mtime_ns, file_size_bytes = _skill_file_fingerprint(skill_file)

        return SkillMeta(
            skill_id=skill_id,
            name=name,
            description=description,
            path=skill_file,
            display_name=display_name,
            source=source,
            loaded_from=loaded_from,
            user_invocable=_parse_bool_frontmatter(
                frontmatter.get("user-invocable"),
                default=True,
            ),
            disable_model_invocation=_parse_bool_frontmatter(
                frontmatter.get("disable-model-invocation"),
                default=False,
            ),
            allowed_tools=_parse_list_frontmatter(frontmatter.get("allowed-tools")),
            model=_optional_str(frontmatter.get("model")),
            effort=_optional_str(frontmatter.get("effort")),
            hooks=_parse_dict_frontmatter(frontmatter.get("hooks")),
            conditional_paths=_parse_paths_frontmatter(frontmatter.get("paths")),
            when_to_use=_optional_str(
                frontmatter.get("when_to_use", frontmatter.get("when-to-use"))
            ),
            argument_hint=_optional_str(frontmatter.get("argument-hint")),
            argument_names=_parse_argument_names_frontmatter(frontmatter.get("arguments")),
            version=_optional_str(frontmatter.get("version")),
            execution_context=("fork" if context_value == "fork" else None),
            agent=_optional_str(frontmatter.get("agent")),
            shell=_parse_shell_frontmatter(frontmatter.get("shell")),
            raw_frontmatter=raw_frontmatter,
            unknown_fields=unknown_fields,
            body_verified=body_verified,
            file_mtime_ns=file_mtime_ns,
            file_size_bytes=file_size_bytes,
            body_sha256=_skill_body_hash(content) if body_verified else None,
        )

    def _merge_runtime_overlay(
        self,
        skill_id: str,
        frontmatter: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self._skill_override_dir is None:
            return frontmatter
        overlay_path = self._skill_override_dir / f"{skill_id}.json"
        if not overlay_path.exists():
            return frontmatter
        try:
            data = json.loads(overlay_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read skill overlay %s: %s", overlay_path, exc)
            return frontmatter
        if not isinstance(data, dict):
            return frontmatter
        approved = data.get("approved")
        if not isinstance(approved, dict):
            return frontmatter
        merged = dict(frontmatter)
        for key, value in approved.items():
            merged[str(key)] = value
        return merged

    def _source_for_skill_dir(self, skill_dir: Path) -> str:
        resolved = str(skill_dir.parent.resolve())
        return self._skill_dir_sources.get(resolved) or (
            "bundled" if "/openspace/skills" in resolved.replace("\\", "/") else "project"
        )

    def _loaded_from_for_skill_dir(self, skill_dir: Path) -> str:
        resolved = str(skill_dir.parent.resolve())
        return self._skill_dir_loaded_from.get(resolved) or (
            "bundled" if "/openspace/skills" in resolved.replace("\\", "/") else "skills"
        )

    # Frontmatter parsing is delegated to skill_utils (single source of truth).
    _extract_frontmatter = staticmethod(parse_frontmatter)
    _strip_frontmatter = staticmethod(strip_frontmatter)

    @staticmethod
    def _build_skill_selection_prompt(
        task: str,
        skills_catalog: str,
        max_skills: int,
    ) -> str:
        """Build the prompt for LLM skill selection.

        Uses a plan-then-select pattern: the LLM first writes a brief
        execution plan, then selects skills that match the plan.
        """
        return f"""You are a skill selector for an autonomous agent.

# Task

{task}

# Available Skills

{skills_catalog}

# Instructions

Follow these steps:

**Step 1 — Plan**: Think about how you would accomplish this task. What are the key deliverables? What file formats are needed (PDF, DOCX, XLSX, etc.)? What tools or libraries would you use?

**Step 2 — Match**: Check which skills directly teach workflows for the deliverables or file formats identified in your plan. A skill is relevant ONLY if it provides a tested procedure for a core part of your plan. Skills that only share vague topical overlap (e.g. a "PDF checklist" skill for a task that just happens to involve PDFs) add noise and should be excluded.

**Step 3 — Quality check**: Among matching skills, prefer ones with higher success rates. Avoid skills marked as "never succeeded" or with very low success rates — they waste iterations and actively hurt performance.

**Step 4 — Decide**: Select at most {max_skills} skill(s). If no skill closely matches your plan, you MUST return an empty list. Selecting an irrelevant or low-quality skill is **worse than selecting none** — it forces the agent down an unproductive path and wastes the entire iteration budget. When in doubt, leave it out.

Return a JSON object:
{{"brief_plan": "1-2 sentence plan for this task", "skills": ["skill_id_1", "skill_id_2"]}}

If no skill applies:
{{"brief_plan": "1-2 sentence plan", "skills": []}}

IMPORTANT: Use the **exact skill_id** from the list above."""

    @staticmethod
    def _parse_skill_selection_response(content: str) -> tuple[List[str], str]:
        """Parse the LLM response and extract selected skill IDs + plan.

        Returns:
            (skill_ids, brief_plan)
        """
        # Handle markdown code blocks
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if code_block:
            content = code_block.group(1).strip()
        else:
            # Try to find a raw JSON object
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                content = json_match.group()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM skill selection JSON: {content[:200]}")
            return [], ""

        brief_plan = data.get("brief_plan", "")
        if brief_plan:
            logger.info(f"Skill selection plan: {brief_plan}")

        ids = data.get("skills", [])
        if not isinstance(ids, list):
            return [], brief_plan
        return [str(n).strip() for n in ids if n], brief_plan
