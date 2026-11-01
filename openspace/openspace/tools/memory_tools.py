"""Auto-memory tools and command helpers.

Implementation notes:
- ``commands/memory/index.ts`` (10 lines)
- ``commands/memory/memory.tsx`` (89 lines)
- ``components/memory/MemoryFileSelector.tsx`` (437 lines)
- ``components/memory/MemoryUpdateNotification.tsx`` (44 lines)

OpenSpace does not define dedicated MemoryRead/MemoryWrite tools.  The equivalent
write path is the OpenSpace memdir prompt plus generic Read/Write/Edit tools scoped
to the auto-memory directory.  OpenSpace exposes explicit memory tools so the
agent can manage memdir records without hand-assembling frontmatter and index
entries, while preserving OpenSpace's storage shape.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from openspace.grounding.core.permissions.types import (
    DecisionReasonOther,
    PermissionDeny,
)
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.services.memory import (
    ENTRYPOINT_NAME,
    MemoryHeader,
    MemoryType,
    clear_memory_file_caches,
    ensure_memory_dir_exists,
    find_project_root,
    get_auto_mem_entrypoint,
    get_auto_mem_path,
    get_memory_base_dir,
    get_memory_files,
    get_memory_path,
    get_openspace_config_home_dir,
    is_auto_memory_enabled,
    parse_memory_type,
    scan_memory_files,
)
from openspace.services.memory.recall import memory_header
from openspace.services.tooling.context import ReadFileEntry

MemoryTargetKind = Literal[
    "Managed",
    "User",
    "Project",
    "Local",
    "AutoMemIndex",
    "AutoMemTopic",
    "AutoMemFolder",
]

MEMORY_WRITE_TOOL_NAME = "memory_write"
MEMORY_READ_TOOL_NAME = "memory_read"

_OPEN_FOLDER_LABEL = "Open auto-memory folder"


@dataclass(frozen=True)
class MemoryTarget:
    """A selectable memory file or folder for the ``/memory`` command."""

    label: str
    path: Path
    kind: MemoryTargetKind
    description: str = ""
    exists: bool = True
    is_folder: bool = False


def get_relative_memory_path(path: str | Path, *, cwd: str | Path | None = None) -> str:
    """OpenSpace ``getRelativeMemoryPath`` equivalent.

    Prefer the shorter display path between ``~`` and ``./``; fall back to the
    absolute path when neither applies.
    """

    resolved = Path(path).expanduser()
    cwd_path = Path(cwd or os.getcwd()).expanduser().resolve()
    home = Path.home().resolve()
    try:
        absolute = resolved.resolve()
    except OSError:
        absolute = resolved.absolute()

    relative_home: str | None = None
    relative_cwd: str | None = None
    try:
        relative_home = "~/" + absolute.relative_to(home).as_posix()
    except ValueError:
        pass
    try:
        relative_cwd = "./" + absolute.relative_to(cwd_path).as_posix()
    except ValueError:
        pass

    if relative_home and relative_cwd:
        return relative_home if len(relative_home) <= len(relative_cwd) else relative_cwd
    return relative_home or relative_cwd or str(absolute)


def list_memory_targets(
    *,
    cwd: str | Path | None = None,
    project_root: str | Path | None = None,
    config_home: str | Path | None = None,
    include_auto_topics: bool = True,
) -> list[MemoryTarget]:
    """Return OpenSpace memory selector entries plus auto-memory files.

    OpenSpace's selector lists existing CLAUDE.md files, injects new User/Project
    targets when missing, and offers an "Open auto-memory folder" row instead
    of listing the AutoMem ``MEMORY.md`` entrypoint.  OpenSpace also includes
    auto-memory topic files because 15.5 explicitly requires list/edit memory
    files from the backend command surface.
    """

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else find_project_root(current_dir)
    )
    config_base = get_openspace_config_home_dir(config_home)

    clear_memory_file_caches()
    memory_files = get_memory_files(
        cwd=current_dir,
        project_root=project_base,
        config_home=config_base,
        use_cache=False,
    )

    targets: list[MemoryTarget] = []
    for info in memory_files:
        if info.source == "AutoMem":
            continue
        targets.append(
            MemoryTarget(
                label=_label_for_instruction_memory(info.source, info.path, current_dir),
                path=info.path,
                kind=info.source,
                description=_description_for_instruction_memory(info.source, info.path, current_dir),
                exists=True,
            )
        )

    user_path = get_memory_path("User", cwd=current_dir, config_home=config_base)
    project_path = get_memory_path("Project", cwd=current_dir)
    existing_paths = {target.path.resolve() for target in targets if target.exists}
    if user_path is not None and user_path.resolve() not in existing_paths:
        targets.append(
            MemoryTarget(
                label="User memory",
                path=user_path,
                kind="User",
                description="Saved in ~/.openspace/OPENSPACE.md",
                exists=False,
            )
        )
    if project_path is not None and project_path.resolve() not in existing_paths:
        targets.append(
            MemoryTarget(
                label="Project memory",
                path=project_path,
                kind="Project",
                description="Checked in at ./OPENSPACE.md" if _project_is_git_repo(current_dir) else "Saved in ./OPENSPACE.md",
                exists=False,
            )
        )

    if is_auto_memory_enabled():
        memory_dir = get_auto_mem_path(
            cwd=current_dir,
            project_root=project_base,
            config_home=config_base,
        )
        targets.append(
            MemoryTarget(
                label=_OPEN_FOLDER_LABEL,
                path=memory_dir,
                kind="AutoMemFolder",
                description="",
                exists=memory_dir.exists(),
                is_folder=True,
            )
        )
        entrypoint = memory_dir / ENTRYPOINT_NAME
        targets.append(
            MemoryTarget(
                label="Auto-memory index",
                path=entrypoint,
                kind="AutoMemIndex",
                description=f"Saved in {get_relative_memory_path(entrypoint, cwd=current_dir)}",
                exists=entrypoint.exists(),
            )
        )
        if include_auto_topics:
            for header in scan_memory_files(memory_dir):
                targets.append(_target_from_header(header, current_dir))

    return targets


def format_memory_targets(
    targets: list[MemoryTarget],
    *,
    cwd: str | Path | None = None,
) -> str:
    """Render target rows for the backend ``/memory`` command."""

    if not targets:
        return "No memory files are currently available."
    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    lines = ["Memory files:"]
    for target in targets:
        suffix = " (new)" if not target.exists and not target.is_folder else ""
        folder = " [folder]" if target.is_folder else ""
        description = f" - {target.description}" if target.description else ""
        lines.append(
            f"- {target.label}{folder}{suffix}: "
            f"{get_relative_memory_path(target.path, cwd=current_dir)}{description}"
        )
    lines.append("")
    lines.append("Use `/memory edit user|project|local|auto|folder|<listed-path>` to open or create a memory file.")
    lines.append("Use `/memory read [filename]` to print an auto-memory topic or MEMORY.md.")
    lines.append("Use `/memory logs [all]` to inspect daily-log entries.")
    return "\n".join(lines)


def ensure_memory_file(path: str | Path) -> Path:
    """Create a memory file if missing, matching OpenSpace's ``writeFile(..., wx)`` branch."""

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.open("x", encoding="utf-8").close()
    except FileExistsError:
        pass
    return target.resolve()


def resolve_memory_target(
    selector: str,
    *,
    cwd: str | Path | None = None,
    project_root: str | Path | None = None,
    config_home: str | Path | None = None,
) -> MemoryTarget:
    """Resolve a ``/memory edit`` selector to a concrete target."""

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else find_project_root(current_dir)
    )
    config_base = get_openspace_config_home_dir(config_home)
    normalized = selector.strip()
    key = normalized.lower()

    if key in {"user", "global"}:
        path = get_memory_path("User", cwd=current_dir, config_home=config_base)
        assert path is not None
        return MemoryTarget("User memory", path, "User", "Saved in ~/.openspace/OPENSPACE.md", path.exists())
    if key == "project":
        path = get_memory_path("Project", cwd=current_dir)
        assert path is not None
        return MemoryTarget("Project memory", path, "Project", "Checked in at ./OPENSPACE.md", path.exists())
    if key == "local":
        path = get_memory_path("Local", cwd=current_dir)
        assert path is not None
        return MemoryTarget("Local memory", path, "Local", "Saved in ./OPENSPACE.local.md", path.exists())
    if key in {"auto", "automem", "auto-memory", ENTRYPOINT_NAME.lower()}:
        if not is_auto_memory_enabled():
            raise ValueError("Auto memory is disabled.")
        path = get_auto_mem_entrypoint(
            cwd=current_dir,
            project_root=project_base,
            config_home=config_base,
        )
        return MemoryTarget("Auto-memory index", path, "AutoMemIndex", "Auto-memory MEMORY.md index", path.exists())
    if key in {"folder", "auto-folder", "auto-memory-folder"}:
        if not is_auto_memory_enabled():
            raise ValueError("Auto memory is disabled.")
        path = get_auto_mem_path(
            cwd=current_dir,
            project_root=project_base,
            config_home=config_base,
        )
        return MemoryTarget(_OPEN_FOLDER_LABEL, path, "AutoMemFolder", "", path.exists(), is_folder=True)

    for target in list_memory_targets(
        cwd=current_dir,
        project_root=project_base,
        config_home=config_base,
    ):
        display = get_relative_memory_path(target.path, cwd=current_dir)
        if normalized in {str(target.path), display, target.label}:
            return target

    raise ValueError(
        f"Unknown memory target: {selector}. Use `/memory list` and choose one "
        "of the listed labels or paths."
    )


def open_memory_target(
    target: MemoryTarget,
    *,
    cwd: str | Path | None = None,
    editor: str | None = None,
    launch_editor: bool = True,
) -> tuple[str, bool]:
    """Create/open a selected memory file or folder.

    Returns ``(message, opened)``.  ``opened`` means an editor command was
    launched; creating the file succeeds even when no editor is configured.
    """

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    if target.is_folder:
        target.path.mkdir(parents=True, exist_ok=True)
        opened = launch_editor and _run_open_path(target.path)
        prefix = "Opened auto-memory folder at" if opened else "Auto-memory folder:"
        return (
            f"{prefix} {get_relative_memory_path(target.path, cwd=current_dir)}",
            opened,
        )

    path = ensure_memory_file(target.path)
    editor_source, editor_value = _select_editor(editor)
    opened = False
    if launch_editor and editor_value:
        _run_editor(editor_value, path)
        opened = True

    if editor_source != "default":
        hint = f'> Using {editor_source}="{editor_value}". To change editor, set $EDITOR or $VISUAL environment variable.'
    else:
        hint = "> To use a different editor, set the $EDITOR or $VISUAL environment variable."
    return (
        f"Opened memory file at {get_relative_memory_path(path, cwd=current_dir)}\n\n{hint}",
        opened,
    )


class MemoryWriteTool(BaseTool):
    """Write a typed auto-memory topic and update ``MEMORY.md``."""

    _name = MEMORY_WRITE_TOOL_NAME
    _description = (
        "Save a persistent auto-memory topic file and update the MEMORY.md index."
    )
    backend_type = BackendType.SHELL
    _is_read_only = False
    _is_concurrency_safe = False
    search_hint = "save persistent user project feedback memory"
    parameter_descriptions = {
        "title": "Short title for the memory, used in MEMORY.md.",
        "content": "The memory body. Include Why and How to apply for feedback/project memories when known.",
        "memory_type": "One of: user, feedback, project, reference.",
        "description": "One-line description used for future memory recall.",
        "filename": "Optional relative .md filename under the auto-memory directory.",
    }

    def __init__(self, session: Any | None = None) -> None:
        self._session = session
        self._current_context: Any | None = None
        super().__init__()

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> Optional[str]:
        if not is_auto_memory_enabled():
            return "Auto memory is disabled."
        if not str(input.get("title") or "").strip():
            return "title is required."
        if not str(input.get("content") or "").strip():
            return "content is required."
        memory_type = parse_memory_type(input.get("memory_type") or "project")
        if memory_type is None:
            return "memory_type must be one of: user, feedback, project, reference."
        try:
            memory_dir = self._memory_dir(context)
            _resolve_topic_path(memory_dir, input.get("filename"), input.get("title"))
        except ValueError as exc:
            return str(exc)
        return None

    async def check_permissions(self, input: dict[str, Any], context: Any = None):
        from openspace.grounding.core.permissions import (
            PermissionAllow,
            check_write_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)
        if not is_auto_memory_enabled():
            return PermissionDeny(
                message="Auto memory is disabled.",
                decision_reason=DecisionReasonOther(reason="Auto memory is disabled"),
            )
        try:
            memory_dir = self._memory_dir(context)
            topic_path = _resolve_topic_path(
                memory_dir,
                input.get("filename"),
                input.get("title"),
            )
        except ValueError as exc:
            return PermissionDeny(
                message=f"Invalid memory filename: {exc}",
                decision_reason=DecisionReasonOther(reason="Invalid memory filename"),
            )
        return check_write_permission_for_tool(
            tool_name=self._name,
            input_path=str(topic_path),
            context=perm_ctx,
        )

    async def _arun(
        self,
        title: str,
        content: str,
        memory_type: str = "project",
        description: str = "",
        filename: str | None = None,
    ) -> ToolResult:
        validation_error = await self.validate_input(
            {
                "title": title,
                "content": content,
                "memory_type": memory_type,
                "description": description,
                "filename": filename,
            },
            self._current_context,
        )
        if validation_error is not None:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=validation_error,
                error=validation_error,
            )

        resolved_type = parse_memory_type(memory_type)
        if resolved_type is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="memory_type must be one of: user, feedback, project, reference.",
            )

        memory_dir = self._memory_dir(self._current_context)
        ensure_memory_dir_exists(memory_dir)
        topic_path = _resolve_topic_path(memory_dir, filename, title)
        topic_path.parent.mkdir(parents=True, exist_ok=True)

        existing = topic_path.exists()
        body = _build_topic_file(
            title=title,
            description=description or _derive_description(content),
            memory_type=resolved_type,
            content=content,
        )
        topic_path.write_text(body, encoding="utf-8")

        entrypoint = memory_dir / ENTRYPOINT_NAME
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        index_line = _update_memory_index(
            entrypoint,
            topic_path,
            title=title,
            description=description or _derive_description(content),
            memory_dir=memory_dir,
        )

        self._update_read_file_state(topic_path, body)
        try:
            self._update_read_file_state(entrypoint, entrypoint.read_text(encoding="utf-8"))
        except OSError:
            pass
        clear_memory_file_caches()
        _clear_prompt_section_cache()

        ctx = self._current_context
        if ctx is not None:
            await ctx.emit_event(
                "memory_written",
                {
                    "file_path": str(topic_path),
                    "entrypoint_path": str(entrypoint),
                    "memory_type": resolved_type,
                    "created": not existing,
                },
            )

        rel_topic = topic_path.relative_to(memory_dir).as_posix()
        action = "created" if not existing else "updated"
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=(
                f"Memory {action}: {rel_topic}\n"
                f"Updated {ENTRYPOINT_NAME}: {index_line}"
            ),
            metadata={
                "type": "memory_write",
                "file_path": str(topic_path),
                "entrypoint_path": str(entrypoint),
                "memory_dir": str(memory_dir),
                "filename": rel_topic,
                "title": title,
                "description": description or _derive_description(content),
                "memory_type": resolved_type,
                "created": not existing,
            },
        )

    def _memory_dir(self, context: Any | None = None) -> Path:
        cwd = _resolve_cwd(self._session, context or self._current_context)
        return get_auto_mem_path(cwd=cwd)

    def _update_read_file_state(self, path: Path, content: str) -> None:
        ctx = self._current_context
        if ctx is None or not hasattr(ctx, "read_file_state"):
            return
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        ctx.read_file_state[str(path.resolve())] = ReadFileEntry(
            content=content,
            timestamp=mtime_ns,
            offset=None,
            limit=None,
            is_partial_view=False,
        )


class MemoryReadTool(BaseTool):
    """Read a topic file or ``MEMORY.md`` from the auto-memory directory."""

    _name = MEMORY_READ_TOOL_NAME
    _description = "Read an auto-memory topic file or MEMORY.md index."
    backend_type = BackendType.SHELL
    _is_read_only = True
    _is_concurrency_safe = True
    search_hint = "recall persistent memory topics"
    parameter_descriptions = {
        "filename": "Relative filename under the auto-memory directory. Defaults to MEMORY.md.",
    }

    def __init__(self, session: Any | None = None) -> None:
        self._session = session
        self._current_context: Any | None = None
        super().__init__()

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> Optional[str]:
        if not is_auto_memory_enabled():
            return "Auto memory is disabled."
        try:
            self._resolve_memory_file(str(input.get("filename") or ENTRYPOINT_NAME), context)
        except ValueError as exc:
            return str(exc)
        return None

    async def check_permissions(self, input: dict[str, Any], context: Any = None):
        from openspace.grounding.core.permissions import (
            check_read_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)
        if not is_auto_memory_enabled():
            return PermissionDeny(
                message="Auto memory is disabled.",
                decision_reason=DecisionReasonOther(reason="Auto memory is disabled"),
            )
        try:
            target = self._resolve_memory_file(
                str(input.get("filename") or ENTRYPOINT_NAME),
                context,
            )
        except ValueError as exc:
            return PermissionDeny(
                message=f"Invalid memory filename: {exc}",
                decision_reason=DecisionReasonOther(reason="Invalid memory filename"),
            )
        return check_read_permission_for_tool(
            tool_name=self._name,
            input_path=str(target),
            context=perm_ctx,
        )

    async def _arun(self, filename: str = ENTRYPOINT_NAME) -> ToolResult:
        validation_error = await self.validate_input(
            {"filename": filename},
            self._current_context,
        )
        if validation_error is not None:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=validation_error,
                error=validation_error,
            )

        try:
            target = self._resolve_memory_file(filename, self._current_context)
            content = target.read_text(encoding="utf-8")
            stat = target.stat()
        except FileNotFoundError:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Memory file does not exist: {filename}",
            )
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Failed to read memory file: {exc}",
            )

        mtime_ms = stat.st_mtime * 1000
        header = memory_header(target, mtime_ms)
        rendered = f"{header}\n\n{content.strip()}" if content.strip() else f"{header}\n\n<empty>"
        ctx = self._current_context
        if ctx is not None and hasattr(ctx, "read_file_state"):
            ctx.read_file_state[str(target.resolve())] = ReadFileEntry(
                content=content,
                timestamp=stat.st_mtime_ns,
                offset=None,
                limit=None,
                is_partial_view=False,
            )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=rendered,
            metadata={
                "type": "memory_read",
                "file_path": str(target.resolve()),
                "filename": target.name,
                "mtime_ms": mtime_ms,
            },
        )

    def _resolve_memory_file(self, filename: str, context: Any | None = None) -> Path:
        cwd = _resolve_cwd(self._session, context or self._current_context)
        memory_dir = get_auto_mem_path(cwd=cwd)
        candidate = Path(filename)
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve()
                resolved.relative_to(memory_dir.resolve())
            except ValueError as exc:
                raise ValueError("filename must stay inside the auto-memory directory.") from exc
            if resolved.suffix != ".md":
                raise ValueError("filename must be a markdown file.")
            return resolved
        normalized = (memory_dir / candidate).resolve()
        try:
            normalized.relative_to(memory_dir.resolve())
        except ValueError as exc:
            raise ValueError("filename must stay inside the auto-memory directory.") from exc
        if normalized.suffix != ".md":
            raise ValueError("filename must be a markdown file.")
        return normalized


def _target_from_header(header: MemoryHeader, cwd: Path) -> MemoryTarget:
    description = header.description or ""
    label = header.filename
    if header.memory_type:
        label = f"[{header.memory_type}] {label}"
    return MemoryTarget(
        label=label,
        path=header.file_path,
        kind="AutoMemTopic",
        description=description,
        exists=True,
    )


def _label_for_instruction_memory(source: str, path: Path, cwd: Path) -> str:
    if source == "User":
        return "User memory"
    if source == "Project" and path == (cwd / "OPENSPACE.md"):
        return "Project memory"
    if source == "Local":
        return "Local memory"
    return get_relative_memory_path(path, cwd=cwd)


def _description_for_instruction_memory(source: str, path: Path, cwd: Path) -> str:
    if source == "User":
        return "Saved in ~/.openspace/OPENSPACE.md"
    if source == "Project" and path == (cwd / "OPENSPACE.md"):
        return "Checked in at ./OPENSPACE.md" if _project_is_git_repo(cwd) else "Saved in ./OPENSPACE.md"
    if source == "Local":
        return "Saved in ./OPENSPACE.local.md"
    if source == "Managed":
        return "Managed global memory"
    return ""


def _project_is_git_repo(cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _resolve_cwd(session: Any | None, context: Any | None) -> str:
    context_cwd = getattr(context, "cwd", None)
    if isinstance(context_cwd, str) and context_cwd.strip():
        return context_cwd
    session_cwd = getattr(session, "default_working_dir", None) if session is not None else None
    if isinstance(session_cwd, str) and session_cwd.strip():
        return session_cwd
    return os.getcwd()


def _safe_filename_from_title(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip().lower()).strip("_")
    slug = slug[:80].strip("_") or "memory"
    return f"{slug}.md"


def _resolve_topic_path(memory_dir: Path, filename: object, title: object) -> Path:
    raw = str(filename or "").strip() or _safe_filename_from_title(str(title or "memory"))
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ValueError("filename must be relative to the auto-memory directory.")
    if candidate.name == ENTRYPOINT_NAME:
        raise ValueError(f"{ENTRYPOINT_NAME} is an index; write memories to topic files.")
    if candidate.suffix == "":
        candidate = candidate.with_suffix(".md")
    if candidate.suffix != ".md":
        raise ValueError("filename must be a markdown file.")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("filename must not contain empty, current, or parent path segments.")
    resolved = (memory_dir / candidate).resolve()
    try:
        resolved.relative_to(memory_dir.resolve())
    except ValueError as exc:
        raise ValueError("filename must stay inside the auto-memory directory.") from exc
    return resolved


def _build_topic_file(
    *,
    title: str,
    description: str,
    memory_type: MemoryType,
    content: str,
) -> str:
    body = content.strip()
    return (
        "---\n"
        f'name: "{_yaml_escape(title.strip())}"\n'
        f'description: "{_yaml_escape(description.strip())}"\n'
        f"type: {memory_type}\n"
        "---\n\n"
        f"{body}\n"
    )


def _update_memory_index(
    entrypoint: Path,
    topic_path: Path,
    *,
    title: str,
    description: str,
    memory_dir: Path,
) -> str:
    relative = topic_path.relative_to(memory_dir).as_posix()
    clean_title = _clean_inline(title, limit=80)
    clean_description = _clean_inline(description, limit=130)
    line = f"- [{clean_title}]({relative})"
    if clean_description:
        line += f" - {clean_description}"

    try:
        existing = entrypoint.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        existing = ""

    lines = existing.splitlines()
    link_pattern = re.compile(r"\]\((?:\./)?" + re.escape(relative) + r"\)")
    replaced = False
    new_lines: list[str] = []
    for current in lines:
        if link_pattern.search(current):
            if not replaced:
                new_lines.append(line)
                replaced = True
            continue
        new_lines.append(current)
    if not replaced:
        if new_lines and new_lines[-1].strip():
            new_lines.append(line)
        elif new_lines:
            new_lines[-1] = line
        else:
            new_lines.append(line)
    entrypoint.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    return line


def _derive_description(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        if stripped:
            return _clean_inline(stripped, limit=130)
    return ""


def _clean_inline(text: str, *, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    normalized = normalized.replace("[", "(").replace("]", ")")
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _yaml_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _select_editor(editor: str | None = None) -> tuple[str, str]:
    if editor:
        return ("argument", editor)
    visual = os.environ.get("VISUAL")
    if visual:
        return ("$VISUAL", visual)
    env_editor = os.environ.get("EDITOR")
    if env_editor:
        return ("$EDITOR", env_editor)
    return ("default", "")


def _run_editor(editor: str, path: Path) -> None:
    overrides = {"code": "code -w", "subl": "subl --wait"}
    command = overrides.get(editor, editor)
    argv = shlex.split(command)
    if not argv:
        return
    subprocess.run([*argv, str(path)], check=False)


def _run_open_path(path: Path) -> bool:
    """Open a folder with the platform shell, matching OpenSpace's ``openPath`` branch."""

    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        argv = (
            ["open", str(path)]
            if sys.platform == "darwin"
            else ["xdg-open", str(path)]
        )
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, AttributeError):
        return False
    return True


def _clear_prompt_section_cache() -> None:
    try:
        from openspace.prompts.grounding_agent_prompts import (
            clear_system_prompt_sections,
        )

        clear_system_prompt_sections()
    except Exception:
        return


def _is_auto_memory_internal_path(absolute_path: str) -> bool:
    try:
        candidate = Path(absolute_path).expanduser().resolve()
    except OSError:
        return False

    # Explicit override / setting branch: exact configured memory directory.
    try:
        configured = get_auto_mem_path()
        candidate.relative_to(configured)
        return True
    except (OSError, ValueError):
        pass

    # Default branch: <memory-base>/projects/<project-key>/memory/**.
    try:
        projects_root = (get_memory_base_dir() / "projects").resolve()
        relative = candidate.relative_to(projects_root)
    except (OSError, ValueError):
        return False
    parts = relative.parts
    return len(parts) >= 3 and parts[1] == "memory"


def _register_auto_memory_internal_path_predicates() -> None:
    try:
        from openspace.grounding.core.permissions.filesystem import (
            register_internal_path_predicate,
        )

        register_internal_path_predicate(
            category="editable",
            reason="Auto-memory files are internal OpenSpace memory storage",
            predicate=_is_auto_memory_internal_path,
        )
        register_internal_path_predicate(
            category="readable",
            reason="Auto-memory files are internal OpenSpace memory storage",
            predicate=_is_auto_memory_internal_path,
        )
    except Exception:
        # Import-time registration should never break tool availability.
        return


_register_auto_memory_internal_path_predicates()
