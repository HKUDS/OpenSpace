"""Auto-memory directory primitives.

This module owns auto-memory paths, entrypoint truncation, directory creation,
prompt-line builders, and the system-prompt loader.
"""

from __future__ import annotations

import os
import re
import subprocess
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from .memory_types import (
    MEMORY_FRONTMATTER_EXAMPLE,
    TRUSTING_RECALL_SECTION,
    TYPES_SECTION_INDIVIDUAL,
    WHAT_NOT_TO_SAVE_SECTION,
    WHEN_TO_ACCESS_SECTION,
)
from .paths import find_project_root, get_openspace_config_home_dir

ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000
AUTO_MEM_DIRNAME = "memory"
AUTO_MEM_DISPLAY_NAME = "auto memory"

OPENSPACE_DISABLE_AUTO_MEMORY_ENV = "OPENSPACE_DISABLE_AUTO_MEMORY"
OPENSPACE_SIMPLE_ENV = "OPENSPACE_SIMPLE"
OPENSPACE_REMOTE_ENV = "OPENSPACE_REMOTE"
OPENSPACE_REMOTE_MEMORY_DIR_ENV = "OPENSPACE_REMOTE_MEMORY_DIR"
OPENSPACE_AUTO_MEMORY_PATH_OVERRIDE_ENV = "OPENSPACE_AUTO_MEMORY_PATH_OVERRIDE"
OPENSPACE_AUTO_MEMORY_DIRECTORY_ENV = "OPENSPACE_AUTO_MEMORY_DIRECTORY"
OPENSPACE_MEMORY_EXTRA_GUIDELINES_ENV = "OPENSPACE_MEMORY_EXTRA_GUIDELINES"


@dataclass(frozen=True)
class EntrypointTruncation:
    content: str
    line_count: int
    byte_count: int
    was_line_truncated: bool
    was_byte_truncated: bool

    @property
    def lineCount(self) -> int:
        """legacy-compatible camelCase alias."""

        return self.line_count

    @property
    def byteCount(self) -> int:
        """legacy-compatible camelCase alias."""

        return self.byte_count

    @property
    def wasLineTruncated(self) -> bool:
        """legacy-compatible camelCase alias."""

        return self.was_line_truncated

    @property
    def wasByteTruncated(self) -> bool:
        """legacy-compatible camelCase alias."""

        return self.was_byte_truncated


DIR_EXISTS_GUIDANCE = (
    "This directory already exists - write to it directly with the Write tool "
    "(do not run mkdir or check for its existence)."
)
DIRS_EXIST_GUIDANCE = (
    "Both directories already exist - write to them directly with the Write tool "
    "(do not run mkdir or check for their existence)."
)


def is_auto_memory_enabled(
    auto_memory_enabled: Optional[bool] = None,
    *,
    cwd: Optional[str | Path] = None,
) -> bool:
    """Return whether auto-memory features are enabled.

    Branches match OpenSpace's priority chain, with OpenSpace-native env names:
    disable env, simple mode, remote-without-storage, explicit setting, default.
    """

    env_val = os.environ.get(OPENSPACE_DISABLE_AUTO_MEMORY_ENV)
    if _is_env_truthy(env_val):
        return False
    if _is_env_defined_falsy(env_val):
        return True
    if _is_env_truthy(os.environ.get(OPENSPACE_SIMPLE_ENV)):
        return False
    if (
        _is_env_truthy(os.environ.get(OPENSPACE_REMOTE_ENV))
        and not os.environ.get(OPENSPACE_REMOTE_MEMORY_DIR_ENV)
    ):
        return False
    if auto_memory_enabled is not None:
        return bool(auto_memory_enabled)
    try:
        from openspace.services.runtime_support.settings import get_setting

        return bool(get_setting("autoMemoryEnabled", True, cwd=cwd))
    except Exception:
        pass
    return True


def is_extract_mode_active(
    *,
    feature_enabled: bool = False,
    non_interactive: bool = False,
    allow_non_interactive: bool = False,
) -> bool:
    """Step-15.6-ready gate for the extract-memories background agent.

    OpenSpace gates this on GrowthBook and non-interactive session state. OpenSpace has
    no GrowthBook layer, so callers pass both explicit feature decisions.
    """

    if not feature_enabled:
        return False
    return bool((not non_interactive) or allow_non_interactive)


def get_memory_base_dir(config_home: Optional[str | Path] = None) -> Path:
    """Return the base directory for persistent memory storage."""

    remote_dir = os.environ.get(OPENSPACE_REMOTE_MEMORY_DIR_ENV)
    if remote_dir:
        return Path(remote_dir).expanduser().resolve()
    return get_openspace_config_home_dir(config_home)


def has_auto_mem_path_override() -> bool:
    return _get_auto_mem_path_override() is not None


def get_auto_mem_path(
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    auto_memory_directory: Optional[str | Path] = None,
) -> Path:
    """Return the auto-memory directory.

    Resolution order:
    1. ``OPENSPACE_AUTO_MEMORY_PATH_OVERRIDE`` full path override
    2. explicit ``auto_memory_directory`` or ``OPENSPACE_AUTO_MEMORY_DIRECTORY``
    3. ``<config_home>/projects/<sanitized-canonical-project-root>/memory``
    """

    override = _get_auto_mem_path_override()
    if override is not None:
        return override

    if auto_memory_directory is None:
        try:
            from openspace.services.runtime_support.settings import get_setting

            auto_memory_directory = get_setting(
                "autoMemoryDirectory",
                None,
                cwd=cwd,
            )
        except Exception:
            auto_memory_directory = None

    setting = _get_auto_mem_path_setting(auto_memory_directory)
    if setting is not None:
        return setting

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    stable_root = _get_auto_mem_base(current_dir, project_root)
    return (
        get_memory_base_dir(config_home)
        / "projects"
        / _sanitize_path(str(stable_root))
        / AUTO_MEM_DIRNAME
    ).resolve()


def get_auto_mem_entrypoint(**kwargs: object) -> Path:
    return get_auto_mem_path(**kwargs) / ENTRYPOINT_NAME


def get_auto_mem_daily_log_path(
    day: Optional[date] = None,
    **kwargs: object,
) -> Path:
    current = day or date.today()
    yyyy = f"{current.year:04d}"
    mm = f"{current.month:02d}"
    dd = f"{current.day:02d}"
    return get_auto_mem_path(**kwargs) / "logs" / yyyy / mm / f"{yyyy}-{mm}-{dd}.md"


def is_auto_mem_path(
    absolute_path: str | Path,
    **kwargs: object,
) -> bool:
    """Return True when *absolute_path* is inside the auto-memory directory."""

    try:
        candidate = Path(absolute_path).expanduser().resolve()
        candidate.relative_to(get_auto_mem_path(**kwargs))
        return True
    except (OSError, ValueError):
        return False


def truncate_entrypoint_content(raw: str) -> EntrypointTruncation:
    """Truncate MEMORY.md by both line and byte caps, matching OpenSpace semantics."""

    trimmed = raw.strip()
    content_lines = trimmed.split("\n")
    line_count = len(content_lines)
    # OpenSpace uses JS string length for this cap despite the "bytes" field name.
    byte_count = len(trimmed)

    was_line_truncated = line_count > MAX_ENTRYPOINT_LINES
    was_byte_truncated = byte_count > MAX_ENTRYPOINT_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return EntrypointTruncation(
            content=trimmed,
            line_count=line_count,
            byte_count=byte_count,
            was_line_truncated=False,
            was_byte_truncated=False,
        )

    truncated = (
        "\n".join(content_lines[:MAX_ENTRYPOINT_LINES])
        if was_line_truncated
        else trimmed
    )
    if len(truncated) > MAX_ENTRYPOINT_BYTES:
        truncated = _truncate_at_newline(truncated, MAX_ENTRYPOINT_BYTES)

    if was_byte_truncated and not was_line_truncated:
        reason = (
            f"{_format_file_size(byte_count)} "
            f"(limit: {_format_file_size(MAX_ENTRYPOINT_BYTES)}) - "
            "index entries are too long"
        )
    elif was_line_truncated and not was_byte_truncated:
        reason = f"{line_count} lines (limit: {MAX_ENTRYPOINT_LINES})"
    else:
        reason = f"{line_count} lines and {_format_file_size(byte_count)}"

    content = (
        truncated
        + "\n\n"
        + f"> WARNING: {ENTRYPOINT_NAME} is {reason}. Only part of it was loaded. "
        + "Keep index entries to one line under ~200 chars; move detail into topic files."
    )
    return EntrypointTruncation(
        content=content,
        line_count=line_count,
        byte_count=byte_count,
        was_line_truncated=was_line_truncated,
        was_byte_truncated=was_byte_truncated,
    )


def ensure_memory_dir_exists(memory_dir: str | Path) -> None:
    """Create the memory directory if possible; callers surface write errors."""

    try:
        Path(memory_dir).expanduser().mkdir(parents=True, exist_ok=True)
    except OSError:
        # OpenSpace logs and continues so prompt construction never fails.
        return


def load_memory_prompt(
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    auto_memory_enabled: Optional[bool] = None,
    auto_memory_directory: Optional[str | Path] = None,
    extra_guidelines: Optional[Iterable[str]] = None,
    skip_index: bool = False,
    include_searching_past_context: bool = False,
    transcript_dir: Optional[str | Path] = None,
    memory_mode: str | None = None,
) -> Optional[str]:
    """Load auto-memory behavioral instructions for the system prompt.

    OpenSpace ``loadMemoryPrompt()`` returns memory *instructions* for
    ``systemPromptSection("memory", ...)``.  In the auto-only branch it does
    not append ``MEMORY.md`` content; that index is injected separately through
    ``getClaudeMds(getMemoryFiles())``.  OpenSpace mirrors that split via
    ``get_openspace_mds(get_memory_files())`` in the project-context section.

    Team memory is not represented in this runtime surface (DEC-025 SKIP).
    ``daily_log`` mode mirrors OpenSpace's KAIROS branch at the prompt level: new
    memories go to an append-only daily log and Dream later distills them into
    topic files + ``MEMORY.md``.
    """

    if not is_auto_memory_enabled(auto_memory_enabled):
        return None

    memory_dir = get_auto_mem_path(
        cwd=cwd,
        project_root=project_root,
        config_home=config_home,
        auto_memory_directory=auto_memory_directory,
    )
    ensure_memory_dir_exists(memory_dir)

    if _normalize_memory_mode(memory_mode) == "daily_log":
        return "\n".join(
            build_assistant_daily_log_lines(
                memory_dir,
                extra_guidelines,
                skip_index=skip_index,
                include_searching_past_context=include_searching_past_context,
                transcript_dir=transcript_dir,
            )
        )

    guidelines = _resolve_extra_guidelines(extra_guidelines)
    return "\n".join(
        build_memory_lines(
            AUTO_MEM_DISPLAY_NAME,
            memory_dir,
            guidelines,
            skip_index=skip_index,
            include_searching_past_context=include_searching_past_context,
            transcript_dir=transcript_dir,
        )
    )


def build_assistant_daily_log_lines(
    memory_dir: str | Path,
    extra_guidelines: Optional[Iterable[str]] = None,
    skip_index: bool = False,
    include_searching_past_context: bool = False,
    transcript_dir: Optional[str | Path] = None,
) -> list[str]:
    """Build OpenSpace KAIROS-style daily-log memory instructions.

    OpenSpace asks the assistant to append directly to ``logs/YYYY/MM/YYYY-MM-DD.md``.
    OpenSpace exposes the structured ``memory_log`` tool for the same runtime
    intent so entries remain parseable and Dream can mark them consolidated.
    """

    memory_path = Path(memory_dir).expanduser().resolve()
    log_path_pattern = memory_path / "logs" / "YYYY" / "MM" / "YYYY-MM-DD.md"
    guidelines = _resolve_extra_guidelines(extra_guidelines)
    lines = [
        "# auto memory",
        "",
        f"You have a persistent, file-based memory system found at: `{memory_path}`",
        "",
        "This session is long-lived. As you work, record anything worth remembering by appending to today's daily log.",
        "",
        f"Daily log path pattern: `{log_path_pattern}`",
        "",
        "Use the `memory_log` tool to append structured candidate memories. Do not directly edit files under `logs/`, and do not update `MEMORY.md` or topic memory files for new memories in daily-log mode.",
        "",
        "A separate Dream pass distills these logs into durable topic memory files and `MEMORY.md`, then marks log entries consolidated.",
        "",
        "## What to log",
        '- User corrections and preferences ("use uv, not pip"; "stop summarizing diffs")',
        "- Facts about the user, their role, or their goals",
        "- Project context that is not derivable from the code (deadlines, incidents, decisions and their rationale)",
        "- Pointers to external systems (dashboards, Linear projects, Slack or Feishu channels)",
        "- Anything the user explicitly asks you to remember",
        "",
        *WHAT_NOT_TO_SAVE_SECTION,
        "",
    ]
    if guidelines:
        lines.extend(["## Additional memory guidelines", "", *guidelines, ""])
    if not skip_index:
        lines.extend(
            [
                f"## {ENTRYPOINT_NAME}",
                f"`{ENTRYPOINT_NAME}` is the distilled index maintained by Dream from your logs and is loaded into context automatically. Read it for orientation, but record new information with `memory_log` instead of editing it directly.",
                "",
            ]
        )
    lines.extend(
        build_searching_past_context_section(
            memory_path,
            enabled=include_searching_past_context,
            transcript_dir=transcript_dir,
        )
    )
    return lines


def build_memory_lines(
    display_name: str,
    memory_dir: str | Path,
    extra_guidelines: Optional[Iterable[str]] = None,
    skip_index: bool = False,
    include_searching_past_context: bool = False,
    transcript_dir: Optional[str | Path] = None,
) -> list[str]:
    """Build typed-memory behavior instructions without MEMORY.md content."""

    if skip_index:
        how_to_save = [
            "## How to save memories",
            "",
            "Write each memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            "- Keep the name, description, and type fields in memory files up-to-date with the content",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]
    else:
        how_to_save = [
            "## How to save memories",
            "",
            "Saving a memory is a two-step process:",
            "",
            "**Step 1** - write the memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            f"**Step 2** - add a pointer to that file in `{ENTRYPOINT_NAME}`. `{ENTRYPOINT_NAME}` is an index, not a memory - each entry should be one line, under ~150 characters: `- [Title](file.md) - one-line hook`. It has no frontmatter. Never write memory content directly into `{ENTRYPOINT_NAME}`.",
            "",
            f"- `{ENTRYPOINT_NAME}` is always loaded into your conversation context - lines after {MAX_ENTRYPOINT_LINES} will be truncated, so keep the index concise",
            "- Keep the name, description, and type fields in memory files up-to-date with the content",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]

    memory_path = str(Path(memory_dir).expanduser())
    lines = [
        f"# {display_name}",
        "",
        f"You have a persistent, file-based memory system at `{memory_path}`. {DIR_EXISTS_GUIDANCE}",
        "",
        "You should build up this memory system over time so that future conversations can have a complete picture of who the user is, how they'd like to collaborate with you, what behaviors to avoid or repeat, and the context behind the work the user gives you.",
        "",
        "If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.",
        "",
        *TYPES_SECTION_INDIVIDUAL,
        *WHAT_NOT_TO_SAVE_SECTION,
        "",
        *how_to_save,
        "",
        *WHEN_TO_ACCESS_SECTION,
        "",
        *TRUSTING_RECALL_SECTION,
        "",
        "## Memory and other forms of persistence",
        "Memory is one of several persistence mechanisms available to you as you assist the user in a given conversation. The distinction is often that memory can be recalled in future conversations and should not be used for persisting information that is only useful within the scope of the current conversation.",
        "- When to use or update a plan instead of memory: If you are about to start a non-trivial implementation task and would like to reach alignment with the user on your approach you should use a Plan rather than saving this information to memory. Similarly, if you already have a plan within the conversation and you have changed your approach, persist that change by updating the plan rather than saving a memory.",
        "- When to use or update tasks instead of memory: When you need to break your work in the current conversation into discrete steps or keep track of your progress, use tasks instead of saving to memory. Tasks are great for persisting information about work in the current conversation, but memory should be reserved for information that will be useful in future conversations.",
        "",
        *(list(extra_guidelines) if extra_guidelines is not None else []),
        "",
    ]
    lines.extend(
        build_searching_past_context_section(
            memory_path,
            enabled=include_searching_past_context,
            transcript_dir=transcript_dir,
        )
    )
    return lines


def build_memory_prompt(
    *,
    display_name: str,
    memory_dir: str | Path,
    extra_guidelines: Optional[Iterable[str]] = None,
) -> str:
    """Build typed-memory instructions with MEMORY.md content appended."""

    memory_path = Path(memory_dir).expanduser()
    entrypoint = memory_path / ENTRYPOINT_NAME
    try:
        entrypoint_content = entrypoint.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        entrypoint_content = ""

    lines = build_memory_lines(display_name, memory_path, extra_guidelines)
    if entrypoint_content.strip():
        lines.extend(["## " + ENTRYPOINT_NAME, "", truncate_entrypoint_content(entrypoint_content).content])
    else:
        lines.extend(
            [
                "## " + ENTRYPOINT_NAME,
                "",
                f"Your {ENTRYPOINT_NAME} is currently empty. When you save new memories, they will appear here.",
            ]
        )
    return "\n".join(lines)


def build_searching_past_context_section(
    auto_mem_dir: str | Path,
    *,
    enabled: bool = False,
    transcript_dir: Optional[str | Path] = None,
    grep_tool_name: str = "grep",
) -> list[str]:
    """Build OpenSpace's optional searching-past-context section.

    OpenSpace gates this section with GrowthBook. OpenSpace callers opt in explicitly.
    """

    if not enabled:
        return []
    transcript = str(Path(transcript_dir).expanduser()) if transcript_dir else "<session-transcripts-dir>"
    return [
        "## Searching past context",
        "",
        "When looking for past context:",
        "1. Search topic files in your memory directory:",
        "```",
        f'{grep_tool_name} with pattern="<search term>" path="{auto_mem_dir}" glob="*.md"',
        "```",
        "2. Session transcript logs (last resort - large files, slow):",
        "```",
        f'{grep_tool_name} with pattern="<search term>" path="{transcript}" glob="*.jsonl"',
        "```",
        "Use narrow search terms (error messages, file paths, function names) rather than broad keywords.",
        "",
    ]


def _is_env_truthy(value: Optional[str]) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _is_env_defined_falsy(value: Optional[str]) -> bool:
    return value is not None and value.lower() in {"0", "false", "no", "off", ""}


def _normalize_memory_mode(value: str | None) -> str:
    if value is None:
        value = os.environ.get("OPENSPACE_MEMORY_MODE")
    if value is None:
        try:
            from openspace.services.runtime_support.settings import get_setting

            value = get_setting("memory.mode", None)
        except Exception:
            value = None
    raw = (value or "direct").strip().lower()
    if raw in {"daily-log", "dailylog", "logs"}:
        return "daily_log"
    return raw if raw in {"direct", "daily_log"} else "direct"


def _resolve_extra_guidelines(
    extra_guidelines: Optional[Iterable[str]],
) -> Optional[list[str]]:
    guidelines: list[str] = [
        item.strip()
        for item in (extra_guidelines or [])
        if isinstance(item, str) and item.strip()
    ]
    env_guidelines = os.environ.get(OPENSPACE_MEMORY_EXTRA_GUIDELINES_ENV, "").strip()
    if env_guidelines:
        guidelines.append(env_guidelines)
    return guidelines or None


def _validate_memory_dir_path(raw: Optional[str], *, expand_tilde: bool) -> Optional[Path]:
    if not raw:
        return None
    candidate = raw
    if expand_tilde and (candidate.startswith("~/") or candidate.startswith("~\\")):
        rest = candidate[2:]
        rest_norm = os.path.normpath(rest or ".")
        if rest_norm in {".", ".."}:
            return None
        candidate = str(Path.home() / rest)

    if "\0" in candidate:
        return None
    normalized = os.path.normpath(candidate)
    path = Path(normalized)
    if not path.is_absolute():
        return None
    path_str = str(path)
    if len(path_str.rstrip("/\\")) < 3:
        return None
    if re.fullmatch(r"[A-Za-z]:", path_str.rstrip("/\\")):
        return None
    if path_str.startswith("\\\\") or path_str.startswith("//"):
        return None
    return path.expanduser().resolve()


def _get_auto_mem_path_override() -> Optional[Path]:
    return _validate_memory_dir_path(
        os.environ.get(OPENSPACE_AUTO_MEMORY_PATH_OVERRIDE_ENV),
        expand_tilde=False,
    )


def _get_auto_mem_path_setting(auto_memory_directory: Optional[str | Path]) -> Optional[Path]:
    raw = str(auto_memory_directory) if auto_memory_directory is not None else None
    if raw is None:
        raw = os.environ.get(OPENSPACE_AUTO_MEMORY_DIRECTORY_ENV)
    return _validate_memory_dir_path(raw, expand_tilde=True)


def _get_auto_mem_base(current_dir: Path, project_root: Optional[str | Path]) -> Path:
    root = Path(project_root).expanduser().resolve() if project_root else find_project_root(current_dir)
    return _find_canonical_git_root(root) or root


def _find_canonical_git_root(start_path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(start_path), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    git_root = Path(result.stdout.strip()).expanduser().resolve()
    return _resolve_canonical_worktree_root(git_root)


def _resolve_canonical_worktree_root(git_root: Path) -> Path:
    git_file = git_root / ".git"
    try:
        content = git_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return git_root
    if not content.startswith("gitdir:"):
        return git_root

    try:
        worktree_git_dir = (git_root / content[len("gitdir:"):].strip()).resolve()
        common_dir = (worktree_git_dir / (worktree_git_dir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        if worktree_git_dir.parent.resolve() != (common_dir / "worktrees").resolve():
            return git_root
        backlink = (worktree_git_dir / "gitdir").read_text(encoding="utf-8").strip()
        if Path(backlink).resolve() != (git_root / ".git").resolve():
            return git_root
        if common_dir.name != ".git":
            return common_dir
        return common_dir.parent
    except (OSError, UnicodeDecodeError, ValueError):
        return git_root


def _sanitize_path(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", name)
    if len(sanitized) <= 200:
        return sanitized
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized[:200]}-{digest}"


def _truncate_at_newline(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    cut_at = content.rfind("\n", 0, max_chars)
    return content[:cut_at] if cut_at > 0 else content[:max_chars]


def _format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
