"""File operation tools for reading, editing, and writing local files."""
from __future__ import annotations

import base64
import asyncio
import difflib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.services.conversation.content_blocks import make_document_block, make_image_block
from openspace.persistence.file_history import record_snapshot
from openspace.tools.notebook_edit_tool import (
    notebook_cells_json,
    notebook_cells_to_content_blocks,
    read_notebook,
)
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.grounding.backends.shell.session import ShellSession
    from openspace.services.tooling.context import ToolUseContext

logger = Logger.get_logger(__name__)


def _notify_lsp_file_written(context: Any, file_path: str, content: str) -> None:
    """Notify LSP services after a file edit/write, fire-and-forget."""

    if context is None:
        return
    tracker = getattr(context, "diagnostic_tracker", None)
    if tracker is not None:
        try:
            tracker.track_dirty_file(file_path)
        except Exception:
            logger.debug("diagnostic_tracker.track_dirty_file failed for %s", file_path, exc_info=True)

    try:
        from openspace.services.lsp.diagnostic_registry import clear_delivered_diagnostics_for_file

        clear_delivered_diagnostics_for_file(Path(file_path).resolve().as_uri())
    except Exception:
        logger.debug("LSP delivered-diagnostic cleanup failed for %s", file_path, exc_info=True)

    manager = getattr(context, "lsp_manager", None)
    if manager is None:
        try:
            from openspace.services.lsp.manager import get_lsp_server_manager

            manager = get_lsp_server_manager()
        except Exception:
            manager = None
    if manager is None:
        return

    async def _run() -> None:
        try:
            await manager.change_file(file_path, content)
            await manager.save_file(file_path)
        except Exception:
            logger.debug("LSP didChange/didSave failed for %s", file_path, exc_info=True)

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        pass


# =====================================================================
# ReadFileTool constants
# =====================================================================

FILE_READ_TOOL_NAME = "read"

FILE_UNCHANGED_STUB = (
    "File unchanged since last read. The content from the earlier read "
    "tool_result in this conversation is still current — refer to that "
    "instead of re-reading."
)

MAX_LINES_TO_READ = 2000

DEFAULT_MAX_SIZE_BYTES = 256 * 1024  # 256 KB output size limit
DEFAULT_MAX_TOKEN_ESTIMATE = 25_000  # default token estimate limit
CHARS_PER_TOKEN_ESTIMATE = 4  # rough chars-per-token for token gating

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

PDF_EXTENSIONS = frozenset({"pdf"})

BINARY_EXTENSIONS = frozenset({
    # Images (handled natively by Read)
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    # Videos
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    # Audio
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
    # Executables
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib",
    ".app", ".msi", ".deb", ".rpm",
    # Documents (PDF excluded at call site)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Bytecode
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
    # Database
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
    # Design / 3D
    ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
    # Flash
    ".swf", ".fla",
})

BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})

CYBER_RISK_MITIGATION_REMINDER = (
    "\n\n<system-reminder>\n"
    "Whenever you read a file, you should consider whether it would be "
    "considered malware. You CAN and SHOULD provide analysis of malware, "
    "what it is doing. But you MUST refuse to improve or augment the code. "
    "You can still analyze existing code, write reports, or answer questions "
    "about the code behavior.\n"
    "</system-reminder>\n"
)


def _is_blocked_device_path(file_path: str) -> bool:
    """Return whether path points at an infinite-output or blocking device."""
    if file_path in BLOCKED_DEVICE_PATHS:
        return True
    if file_path.startswith("/proc/") and (
        file_path.endswith("/fd/0")
        or file_path.endswith("/fd/1")
        or file_path.endswith("/fd/2")
    ):
        return True
    return False


def _has_binary_extension(file_path: str) -> bool:
    """Return whether path has an unsupported binary extension."""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in BINARY_EXTENSIONS


# =====================================================================
# Line-number formatting
# =====================================================================

def add_line_numbers(content: str, start_line: int = 1) -> str:
    """Format file content with line numbers (compact tab-separated format).

    Format: ``LINE_NUM\\tLINE_CONTENT``  (1-indexed)
    """
    if not content:
        return ""
    lines = content.split("\n")
    return "\n".join(
        f"{i + start_line}\t{line}" for i, line in enumerate(lines)
    )


# =====================================================================
# Read file in range
# =====================================================================

class FileTooLargeError(Exception):
    """File exceeds max allowed size."""

    def __init__(self, size_bytes: int, max_bytes: int):
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File content ({_format_file_size(size_bytes)}) exceeds maximum "
            f"allowed size ({_format_file_size(max_bytes)}). Use offset and "
            f"limit parameters to read specific portions of the file, or "
            f"search for specific content instead of reading the whole file."
        )


class MaxFileReadTokenExceededError(Exception):
    """Content exceeds the configured token budget."""

    def __init__(self, token_count: int, max_tokens: int):
        self.token_count = token_count
        self.max_tokens = max_tokens
        super().__init__(
            f"File content (~{token_count} tokens) exceeds maximum allowed "
            f"tokens ({max_tokens}). Use offset and limit parameters to read "
            f"specific portions of the file, or search for specific content "
            f"instead of reading the whole file."
        )


def read_file_in_range(
    file_path: str,
    offset: int = 0,
    max_lines: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Read lines [offset, offset+max_lines) from a text file.

    Returns dict with: content, line_count, total_lines, total_bytes,
    read_bytes, mtime_ns.

    Both paths strip UTF-8 BOM and CRLF → LF.
    """
    stat = os.stat(file_path)

    if os.path.isdir(file_path):
        raise IsADirectoryError(
            f"EISDIR: illegal operation on a directory, read '{file_path}'"
        )

    # Size guard (when no explicit limit, check total file size)
    if max_bytes is not None and max_lines is None and stat.st_size > max_bytes:
        raise FileTooLargeError(stat.st_size, max_bytes)

    mtime_ns = stat.st_mtime_ns

    raw = Path(file_path).read_bytes()

    # Strip UTF-8 BOM
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]

    text = raw.decode("utf-8", errors="replace")

    # CRLF → LF
    text = text.replace("\r\n", "\n")
    if text.endswith("\r"):
        text = text[:-1]

    total_bytes = len(raw)
    all_lines = text.split("\n")
    total_lines = len(all_lines)

    end_line = offset + max_lines if max_lines is not None else total_lines
    selected = all_lines[offset:end_line]

    content = "\n".join(selected)
    read_bytes = len(content.encode("utf-8"))

    return {
        "content": content,
        "line_count": len(selected),
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "read_bytes": read_bytes,
        "mtime_ns": mtime_ns,
    }


# =====================================================================
# Image reading
# =====================================================================

def _read_image_file(file_path: str, max_tokens: int = DEFAULT_MAX_TOKEN_ESTIMATE) -> dict[str, Any]:
    """Read an image file and return base64-encoded data.

    Attempts Pillow resize if image exceeds token budget.
    Falls back to raw base64 if Pillow is not available.
    """
    raw = Path(file_path).read_bytes()
    original_size = len(raw)

    if original_size == 0:
        raise ValueError(f"Image file is empty: {file_path}")

    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    media_type_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }
    media_type = media_type_map.get(ext, f"image/{ext}")

    b64_data = base64.b64encode(raw).decode("ascii")
    estimated_tokens = math.ceil(len(b64_data) * 0.125)

    if estimated_tokens <= max_tokens:
        return {
            "type": "image",
            "base64": b64_data,
            "media_type": media_type,
            "original_size": original_size,
        }

    # Try Pillow compression
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw))
        # Resize to fit within token budget
        max_dim = 1024
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)

        buf = io.BytesIO()
        save_format = "JPEG" if ext in ("jpg", "jpeg") else ext.upper()
        if save_format not in ("JPEG", "PNG", "GIF", "WEBP"):
            save_format = "JPEG"

        save_kwargs: dict[str, Any] = {}
        if save_format == "JPEG":
            save_kwargs["quality"] = 60
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
        elif save_format == "WEBP":
            save_kwargs["quality"] = 60

        img.save(buf, format=save_format, **save_kwargs)
        compressed = buf.getvalue()
        compressed_b64 = base64.b64encode(compressed).decode("ascii")

        out_media = "image/jpeg" if save_format == "JPEG" else media_type
        return {
            "type": "image",
            "base64": compressed_b64,
            "media_type": out_media,
            "original_size": original_size,
        }
    except ImportError:
        logger.debug("Pillow not available — returning raw image base64")
        return {
            "type": "image",
            "base64": b64_data,
            "media_type": media_type,
            "original_size": original_size,
        }
    except Exception as exc:
        logger.debug("Image compression failed: %s — returning raw", exc)
        return {
            "type": "image",
            "base64": b64_data,
            "media_type": media_type,
            "original_size": original_size,
        }


# =====================================================================
# ReadFileTool prompt
# =====================================================================

def _build_read_file_prompt() -> str:
    """Full Read tool prompt."""
    return f"""Reads a file from the local filesystem. You can access any file directly by using this tool.
Assume this tool is able to read all files on the machine. If the User provides a path to a file assume that path is valid. It is okay to read a file that does not exist; an error will be returned.

Usage:
- The file_path parameter must be an absolute path, not a relative path
- By default, it reads up to {MAX_LINES_TO_READ} lines starting from the beginning of the file
- You can optionally specify a line offset and limit (especially handy for long files), but it's recommended to read the whole file by not providing these parameters
- Results are returned using cat -n format, with line numbers starting at 1
- This tool allows reading images (eg PNG, JPG, etc). When reading an image file the contents are presented visually as the LLM is multimodal.
- This tool can read Jupyter notebooks (.ipynb files) and returns all cells with their outputs, combining code, text, and visualizations.
- This tool can only read files, not directories. To read a directory, use an ls command via the bash tool.
- You will regularly be asked to read screenshots. If the user provides a path to a screenshot, ALWAYS use this tool to view the file at the path.
- If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents."""


def _resolve_tool_working_dir(
    session: ShellSession | None,
    context: Any | None = None,
) -> str:
    """Resolve the workspace cwd used by shell file tools."""
    context_cwd = getattr(context, "cwd", None)
    if isinstance(context_cwd, str) and context_cwd.strip():
        return context_cwd

    session_cwd = getattr(session, "default_working_dir", None) if session else None
    if isinstance(session_cwd, str) and session_cwd.strip():
        return session_cwd

    return os.getcwd()


def _resolve_tool_file_path(
    file_path: str,
    *,
    session: ShellSession | None,
    context: Any | None = None,
) -> str:
    """Resolve a tool file path against the workspace/session cwd."""
    expanded = os.path.expanduser(file_path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(_resolve_tool_working_dir(session, context), expanded)
    return os.path.normpath(expanded)


# =====================================================================
# ReadFileTool
# =====================================================================

class ReadFileTool(BaseTool):
    """Read files from the local filesystem.

    Supports text files with line numbers, images (base64), PDFs, notebooks,
    and dedup via readFileState.

    Input schema:
        file_path: str  — absolute path to the file
        offset:    int  — 1-based line number to start from (default 1)
        limit:     int  — number of lines to read (optional)

    Output is returned as ``ToolResult`` content appropriate to the file type.
    """

    _name = FILE_READ_TOOL_NAME
    _description = "Read a file from the local filesystem."
    backend_type = BackendType.SHELL

    _is_read_only = True
    _is_concurrency_safe = True
    max_result_size_chars = float("inf")

    search_hint: str = "read files, images, PDFs, notebooks"
    parameter_descriptions = {
        "file_path": "The absolute path to the file to read",
        "offset": "The line number to start reading from. Only provide if the file is too large to read at once",
        "limit": "The number of lines to read. Only provide if the file is too large to read at once.",
    }

    def __init__(self, session: ShellSession | None = None):
        self._session = session
        self._current_context: ToolUseContext | None = None
        super().__init__()

    def get_prompt(self) -> str:
        """Return the full tool prompt."""
        return _build_read_file_prompt()

    # --- check_permissions -------------------------------------------------------------

    async def check_permissions(self, input: Dict[str, Any], context: Any):
        """Delegate to filesystem.check_read_permission_for_tool.

        The path is normalized before delegating to the shared permission
        helper.
        """
        from openspace.grounding.core.permissions import (
            check_read_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        file_path = input.get("file_path", "") or ""
        full_path = (
            _resolve_tool_file_path(file_path, session=self._session, context=context)
            if file_path
            else ""
        )
        return check_read_permission_for_tool(
            tool_name=self._name,
            input_path=full_path,
            context=perm_ctx,
            internal_read_roots=(
                (str(context.tool_results_dir),)
                if getattr(context, "tool_results_dir", None)
                else ()
            ),
        )

    # --- validate_input ---------------------------------------------------------------

    async def validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Pre-execution validation — no I/O, pure path/rule checks.

        Checks required path, unsupported binary extensions, and blocked
        device paths before attempting to read.
        """
        file_path = input.get("file_path", "")
        if not file_path:
            return "file_path is required."

        full_path = _resolve_tool_file_path(
            file_path,
            session=self._session,
            context=context,
        )
        ext = os.path.splitext(full_path)[1].lower()

        # Binary extension check — exclude images and PDF (handled natively)
        if _has_binary_extension(full_path):
            ext_bare = ext.lstrip(".")
            if ext_bare not in IMAGE_EXTENSIONS and ext_bare not in PDF_EXTENSIONS:
                return (
                    f"This tool cannot read binary files. The file appears "
                    f"to be a binary {ext} file. Please use appropriate tools "
                    f"for binary file analysis."
                )

        # Blocked device paths
        if _is_blocked_device_path(full_path):
            return (
                f"Cannot read '{file_path}': this device file would block "
                f"or produce infinite output."
            )

        return None

    def set_context(self, context: ToolUseContext) -> None:
        """Inject ToolUseContext — called by run_tool_use pipeline."""
        self._current_context = context

    # --- _arun ------------------------------------------------------------------------

    async def _arun(
        self,
        file_path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> ToolResult:
        full_path = _resolve_tool_file_path(
            file_path,
            session=self._session,
            context=self._current_context,
        )
        ext = os.path.splitext(full_path)[1].lower().lstrip(".")
        ctx = self._current_context

        # ── Dedup check ─────────────────────────────────────────────────────────
        if ctx is not None:
            existing = ctx.read_file_state.get(full_path)
            existing_offset = _read_state_field(existing, "offset")
            existing_limit = _read_state_field(existing, "limit")
            if (
                existing is not None
                and not _read_state_field(existing, "is_partial_view", False)
                and existing_offset is not None
            ):
                range_match = (
                    existing_offset == offset and existing_limit == limit
                )
                if range_match:
                    try:
                        if ext == "ipynb":
                            current_mtime_ns = _get_file_mtime_ns(full_path)
                            stored_mtime_ns = _normalize_read_timestamp_ns(
                                _read_state_field(existing, "timestamp")
                            )
                            if current_mtime_ns == stored_mtime_ns:
                                return ToolResult(
                                    status=ToolStatus.SUCCESS,
                                    content=FILE_UNCHANGED_STUB,
                                )
                        elif not _has_file_changed_since_read(full_path, existing):
                            return ToolResult(
                                status=ToolStatus.SUCCESS,
                                content=FILE_UNCHANGED_STUB,
                            )
                    except OSError:
                        pass  # stat failed — fall through to full read

        # ── Image branch ────────────────────────────────────────────────────────
        if ext in IMAGE_EXTENSIONS:
            try:
                img_result = _read_image_file(full_path)
                # Images do not update readFileState, but they still trigger
                # nested memory discovery after a successful read.
                _add_nested_memory_trigger(ctx, full_path)
                _add_skill_path_trigger(ctx, full_path)
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    content=[
                        make_image_block(
                            img_result["base64"],
                            img_result["media_type"],
                        ),
                    ],
                    metadata={
                        "type": "image",
                        "file_path": full_path,
                        "media_type": img_result["media_type"],
                        "original_size": img_result["original_size"],
                    },
                )
            except Exception as exc:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content=f"Failed to read image: {exc}",
                )

        # ── PDF branch (basic text extraction) ───────────────────────
        if ext in PDF_EXTENSIONS:
            result = await self._read_pdf(file_path, full_path)
            if ctx is not None and result.status == ToolStatus.SUCCESS:
                _add_nested_memory_trigger(ctx, full_path)
                _add_skill_path_trigger(ctx, full_path)
            return result

        # ── Notebook branch ─────────────────────────────────────────────────────
        if ext == "ipynb":
            try:
                cells = read_notebook(full_path)
                cells_state_json = notebook_cells_json(cells)
                cells_json_bytes = len(cells_state_json.encode("utf-8"))
                if cells_json_bytes > DEFAULT_MAX_SIZE_BYTES:
                    return ToolResult(
                        status=ToolStatus.ERROR,
                        content=(
                            f"Notebook content ({_format_file_size(cells_json_bytes)}) "
                            f"exceeds maximum allowed size "
                            f"({_format_file_size(DEFAULT_MAX_SIZE_BYTES)}). "
                            "Use bash with jq to read specific portions:\n"
                            f"  cat \"{file_path}\" | jq '.cells[:20]' # First 20 cells\n"
                            f"  cat \"{file_path}\" | jq '.cells[100:120]' # Cells 100-120\n"
                            f"  cat \"{file_path}\" | jq '.cells | length' # Count total cells\n"
                            f"  cat \"{file_path}\" | jq '.cells[] | select(.cell_type==\"code\") | .source' # All code sources"
                        ),
                    )
                estimated_tokens = len(cells_state_json) // CHARS_PER_TOKEN_ESTIMATE
                if estimated_tokens > DEFAULT_MAX_TOKEN_ESTIMATE:
                    return ToolResult(
                        status=ToolStatus.ERROR,
                        content=(
                            f"File content (~{estimated_tokens} tokens) exceeds "
                            f"maximum allowed tokens ({DEFAULT_MAX_TOKEN_ESTIMATE}). "
                            "Use bash with jq to read specific notebook portions."
                        ),
                    )

                if ctx is not None:
                    _update_read_file_state(
                        ctx,
                        full_path,
                        content=cells_state_json,
                        timestamp_ns=_get_file_mtime_ns(full_path),
                        offset=offset,
                        limit=limit,
                        is_partial_view=False,
                    )
                    _add_nested_memory_trigger(ctx, full_path)
                    _add_skill_path_trigger(ctx, full_path)

                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    content=notebook_cells_to_content_blocks(cells),
                    metadata={
                        "type": "notebook",
                        "file_path": full_path,
                        "cells": cells,
                    },
                )
            except json.JSONDecodeError:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content="Notebook is not valid JSON.",
                )
            except FileNotFoundError:
                similar = _find_similar_file(full_path)
                cwd = _resolve_tool_working_dir(self._session, ctx)
                msg = (
                    f"File does not exist. "
                    f"Note: your current working directory is {cwd}."
                )
                cwd_suggestion = _suggest_path_under_cwd(full_path, cwd)
                if cwd_suggestion:
                    msg += f" Did you mean {cwd_suggestion}?"
                elif similar:
                    msg += f" Did you mean {similar}?"
                return ToolResult(status=ToolStatus.ERROR, content=msg)
            except Exception as exc:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content=f"Failed to read notebook: {exc}",
                )

        # ── Text file branch ────────────────────────────────────────────────────
        try:
            line_offset = 0 if offset == 0 else offset - 1
            max_size = DEFAULT_MAX_SIZE_BYTES if limit is None else None
            result = read_file_in_range(
                full_path,
                offset=line_offset,
                max_lines=limit,
                max_bytes=max_size,
            )
        except FileNotFoundError:
            similar = _find_similar_file(full_path)
            cwd = _resolve_tool_working_dir(self._session, ctx)
            msg = (
                f"File does not exist. "
                f"Note: your current working directory is {cwd}."
            )
            cwd_suggestion = _suggest_path_under_cwd(full_path, cwd)
            if cwd_suggestion:
                msg += f" Did you mean {cwd_suggestion}?"
            elif similar:
                msg += f" Did you mean {similar}?"
            return ToolResult(status=ToolStatus.ERROR, content=msg)
        except IsADirectoryError:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=(
                    f"'{file_path}' is a directory, not a file. "
                    f"Use bash with 'ls' to list directory contents."
                ),
            )
        except FileTooLargeError as exc:
            return ToolResult(status=ToolStatus.ERROR, content=str(exc))
        except OSError as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Cannot read file: {exc}",
            )

        content = result["content"]
        line_count = result["line_count"]
        total_lines = result["total_lines"]
        mtime_ns = result["mtime_ns"]

        # Token estimate gate.
        estimated_tokens = len(content) // CHARS_PER_TOKEN_ESTIMATE
        if estimated_tokens > DEFAULT_MAX_TOKEN_ESTIMATE:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=(
                    f"File content (~{estimated_tokens} tokens) exceeds "
                    f"maximum allowed tokens ({DEFAULT_MAX_TOKEN_ESTIMATE}). "
                    f"Use offset and limit parameters to read specific "
                    f"portions of the file, or search for specific content "
                    f"instead of reading the whole file."
                ),
            )

        # ── Update readFileState ────────────────────────────────────────────────
        if ctx is not None:
            _update_read_file_state(
                ctx,
                full_path,
                content=content,
                timestamp_ns=mtime_ns,
                offset=offset,
                limit=limit,
                is_partial_view=bool(limit is not None or offset not in (0, 1)),
            )
            _add_nested_memory_trigger(ctx, full_path)
            _add_skill_path_trigger(ctx, full_path)

        # ── Format output ───────────────────────────────────────────────────────
        if content:
            formatted = add_line_numbers(content, start_line=offset)
            formatted += CYBER_RISK_MITIGATION_REMINDER
        elif total_lines == 0:
            formatted = (
                "<system-reminder>Warning: the file exists but the "
                "contents are empty.</system-reminder>"
            )
        else:
            formatted = (
                f"<system-reminder>Warning: the file exists but is "
                f"shorter than the provided offset ({offset}). The file "
                f"has {total_lines} lines.</system-reminder>"
            )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=formatted,
            metadata={
                "type": "text",
                "file_path": full_path,
                "num_lines": line_count,
                "start_line": offset,
                "total_lines": total_lines,
            },
        )

    async def _read_pdf(self, file_path: str, full_path: str) -> ToolResult:
        """Read a PDF as a document block plus best-effort extracted text."""
        try:
            raw = Path(full_path).read_bytes()
        except Exception as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Failed to read PDF: {exc}",
            )

        if len(raw) > DEFAULT_MAX_SIZE_BYTES:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=(
                    f"PDF file ({_format_file_size(len(raw))}) exceeds maximum "
                    f"inline document size ({_format_file_size(DEFAULT_MAX_SIZE_BYTES)})."
                ),
            )

        text = f"PDF file read: {file_path} ({_format_file_size(len(raw))})"

        try:
            pdf_b64 = base64.b64encode(raw).decode("ascii")
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=text,
                metadata={
                    "type": "pdf",
                    "file_path": full_path,
                    "media_type": "application/pdf",
                    "original_size": len(raw),
                },
                additional_messages=[{
                    "role": "user",
                    "content": [
                        make_document_block(pdf_b64, "application/pdf"),
                    ],
                }],
            )
        except Exception as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Failed to read PDF: {exc}",
            )


def _suggest_path_under_cwd(requested_path: str, cwd: str) -> str | None:
    """Detect a missing cwd prefix and suggest the existing workspace path.

    If /Users/x/src/foobar doesn't exist but /Users/x/src/repo/foobar does,
    suggest the corrected path.
    """
    cwd_parent = os.path.dirname(cwd)
    if not requested_path.startswith(cwd_parent):
        return None

    relative_to_parent = os.path.relpath(requested_path, cwd_parent)
    parts = relative_to_parent.split(os.sep)
    if len(parts) < 2:
        return None

    # Replace the first path component with the cwd's basename
    candidate = os.path.join(cwd, *parts[1:])
    if os.path.exists(candidate):
        return candidate
    return None


# =====================================================================
# Edit constants
# =====================================================================

MAX_EDIT_FILE_SIZE: int = 1024 * 1024 * 1024  # 1 GiB

FILE_UNEXPECTEDLY_MODIFIED_ERROR: str = (
    "File has been unexpectedly modified. "
    "Read it again before attempting to write it."
)

CONTEXT_LINES: int = 4

LEFT_SINGLE_CURLY_QUOTE = "\u2018"
RIGHT_SINGLE_CURLY_QUOTE = "\u2019"
LEFT_DOUBLE_CURLY_QUOTE = "\u201c"
RIGHT_DOUBLE_CURLY_QUOTE = "\u201d"

# Desanitization replacements used before applying edit strings.
DESANITIZATIONS: dict[str, str] = {
    '<fnr>': '<function_results>',
    '<n>': '<name>',
    '</n>': '</name>',
    '<o>': '<output>',
    '</o>': '</output>',
    '<e>': '<error>',
    '</e>': '</error>',
    '<s>': '<system>',
    '</s>': '</system>',
    '<r>': '<result>',
    '</r>': '</result>',
    '< META_START >': '<META_START>',
    '< META_END >': '<META_END>',
    '< EOT >': '<EOT>',
    '< META >': '<META>',
    '< SOS >': '<SOS>',
    '\n\nH:': '\n\nHuman:',
    '\n\nA:': '\n\nAssistant:',
}


# =====================================================================
# Quote normalization utilities
# =====================================================================

def normalize_quotes(s: str) -> str:
    """Convert curly quotes to straight quotes."""
    return (
        s.replace(LEFT_SINGLE_CURLY_QUOTE, "'")
        .replace(RIGHT_SINGLE_CURLY_QUOTE, "'")
        .replace(LEFT_DOUBLE_CURLY_QUOTE, '"')
        .replace(RIGHT_DOUBLE_CURLY_QUOTE, '"')
    )


def find_actual_string(file_content: str, search_string: str) -> str | None:
    """Find the real substring in *file_content* that matches *search_string*.

    First tries exact match, then falls back to quote-normalized match.
    Returns the actual substring from the file, or None.
    """
    if search_string in file_content:
        return search_string

    normalized_search = normalize_quotes(search_string)
    normalized_file = normalize_quotes(file_content)

    idx = normalized_file.find(normalized_search)
    if idx != -1:
        return file_content[idx: idx + len(search_string)]

    return None


def _is_opening_context(chars: list[str], index: int) -> bool:
    """Return whether a quote at *index* is in opening punctuation context."""
    if index == 0:
        return True
    prev = chars[index - 1]
    return prev in (" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013")


def _apply_curly_double_quotes(s: str) -> str:
    chars = list(s)
    result: list[str] = []
    for i, ch in enumerate(chars):
        if ch == '"':
            result.append(
                LEFT_DOUBLE_CURLY_QUOTE
                if _is_opening_context(chars, i)
                else RIGHT_DOUBLE_CURLY_QUOTE
            )
        else:
            result.append(ch)
    return "".join(result)


def _apply_curly_single_quotes(s: str) -> str:
    chars = list(s)
    result: list[str] = []
    for i, ch in enumerate(chars):
        if ch == "'":
            prev = chars[i - 1] if i > 0 else None
            nxt = chars[i + 1] if i < len(chars) - 1 else None
            prev_is_letter = prev is not None and prev.isalpha()
            nxt_is_letter = nxt is not None and nxt.isalpha()
            if prev_is_letter and nxt_is_letter:
                result.append(RIGHT_SINGLE_CURLY_QUOTE)
            else:
                result.append(
                    LEFT_SINGLE_CURLY_QUOTE
                    if _is_opening_context(chars, i)
                    else RIGHT_SINGLE_CURLY_QUOTE
                )
        else:
            result.append(ch)
    return "".join(result)


def preserve_quote_style(
    old_string: str,
    actual_old_string: str,
    new_string: str,
) -> str:
    """Preserve curly-quote typography from the original file in *new_string*.
    """
    if old_string == actual_old_string:
        return new_string

    has_double = (
        LEFT_DOUBLE_CURLY_QUOTE in actual_old_string
        or RIGHT_DOUBLE_CURLY_QUOTE in actual_old_string
    )
    has_single = (
        LEFT_SINGLE_CURLY_QUOTE in actual_old_string
        or RIGHT_SINGLE_CURLY_QUOTE in actual_old_string
    )

    if not has_double and not has_single:
        return new_string

    result = new_string
    if has_double:
        result = _apply_curly_double_quotes(result)
    if has_single:
        result = _apply_curly_single_quotes(result)
    return result


# =====================================================================
# Edit application
# =====================================================================

def apply_edit_to_file(
    original: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Apply a single string replacement edit."""
    if replace_all:
        replacer = lambda s, o, n: s.replace(o, n)
    else:
        replacer = lambda s, o, n: s.replace(o, n, 1)

    if new_string != "":
        return replacer(original, old_string, new_string)

    # When deleting and old_string doesn't end with newline but a trailing
    # newline follows in the file, strip that trailing newline too.
    strip_trailing_nl = (
        not old_string.endswith("\n")
        and (old_string + "\n") in original
    )
    if strip_trailing_nl:
        return replacer(original, old_string + "\n", new_string)
    return replacer(original, old_string, new_string)


def get_patch_for_edit(
    file_path: str,
    file_contents: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Apply edit and produce a structured patch.

    Returns ``(hunks, updated_file)`` where each hunk is a dict with
    ``old_start``, ``old_lines``, ``new_start``, ``new_lines``, ``lines``.
    """
    if not file_contents and old_string == "" and new_string == "":
        return [], ""

    if old_string == "":
        updated = new_string
    else:
        updated = apply_edit_to_file(file_contents, old_string, new_string, replace_all)

    if updated == file_contents:
        raise ValueError("Original and edited file match exactly. Failed to apply edit.")

    hunks = _structured_patch(file_path, file_contents, updated)
    return hunks, updated


def _structured_patch(
    file_path: str,
    old_content: str,
    new_content: str,
    context_lines: int = 3,
) -> list[dict[str, Any]]:
    """Generate structured patch hunks using difflib.

    Produces structured patch output using Python's ``difflib.unified_diff``.
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=file_path,
        tofile=file_path,
        n=context_lines,
    ))

    if not diff_lines:
        return []

    hunks: list[dict[str, Any]] = []
    current_hunk: dict[str, Any] | None = None

    for line in diff_lines:
        if line.startswith("@@"):
            if current_hunk is not None:
                hunks.append(current_hunk)
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                current_hunk = {
                    "old_start": int(m.group(1)),
                    "old_lines": int(m.group(2) or 1),
                    "new_start": int(m.group(3)),
                    "new_lines": int(m.group(4) or 1),
                    "lines": [],
                }
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if current_hunk is not None:
            clean = line.rstrip("\n")
            current_hunk["lines"].append(clean)

    if current_hunk is not None:
        hunks.append(current_hunk)

    return hunks


def format_patch_as_text(
    file_path: str,
    hunks: list[dict[str, Any]],
) -> str:
    """Render hunks as a unified-diff string for the tool result message."""
    if not hunks:
        return ""

    parts: list[str] = [f"--- {file_path}", f"+++ {file_path}"]
    for h in hunks:
        header = f"@@ -{h['old_start']},{h['old_lines']} +{h['new_start']},{h['new_lines']} @@"
        parts.append(header)
        for ln in h.get("lines", []):
            parts.append(ln)
    return "\n".join(parts)


def get_snippet_for_patch(
    hunks: list[dict[str, Any]],
    new_file: str,
    context_lines: int = CONTEXT_LINES,
) -> tuple[str, int]:
    """Return a snippet around the changed region with line numbers.

    Returns ``(formatted_snippet, start_line)``.
    """
    if not hunks:
        return ("", 1)

    min_line = min(h["old_start"] for h in hunks)
    max_line = max(h["old_start"] + h.get("new_lines", 0) - 1 for h in hunks)

    start = max(1, min_line - context_lines)
    end = max_line + context_lines

    file_lines = new_file.splitlines()
    snippet_lines = file_lines[start - 1: end]

    numbered = []
    for i, ln in enumerate(snippet_lines, start=start):
        numbered.append(f"{i:6d}|{ln}")

    return ("\n".join(numbered), start)


# =====================================================================
# Desanitization
# =====================================================================

def desanitize_match_string(match_string: str) -> tuple[str, list[tuple[str, str]]]:
    """Try to reverse API sanitization on *match_string*.

    Returns ``(result, applied_replacements)``.
    """
    result = match_string
    applied: list[tuple[str, str]] = []
    for short, long in DESANITIZATIONS.items():
        before = result
        result = result.replace(short, long)
        if result != before:
            applied.append((short, long))
    return result, applied


# =====================================================================
# File I/O helpers
# =====================================================================

def _read_file_for_edit(path: str) -> tuple[str, bool]:
    """Read file content.  Returns (content, file_exists).

    Handles UTF-8 and UTF-16-LE (BOM detection).  Normalizes CRLF to LF.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return ("", False)

    # BOM detection
    if len(raw) >= 2 and raw[0] == 0xFF and raw[1] == 0xFE:
        text = raw.decode("utf-16-le")
    else:
        text = raw.decode("utf-8", errors="replace")

    text = text.replace("\r\n", "\n")
    return (text, True)


def _get_file_mtime_ns(path: str) -> int:
    """Return file mtime in nanoseconds for stale-read comparisons."""
    return os.stat(path).st_mtime_ns


def _read_state_field(entry: Any, field: str, default: Any = None) -> Any:
    """Read a read-file-state field from dataclass or legacy dict entries."""
    if entry is None:
        return default
    if isinstance(entry, dict):
        return entry.get(field, default)
    return getattr(entry, field, default)


def _normalize_read_timestamp_ns(timestamp: float | int | None) -> int:
    """Normalize legacy read-file timestamps (s/ms/us/ns) to nanoseconds."""
    if timestamp is None:
        return 0

    if isinstance(timestamp, int):
        if timestamp <= 0:
            return 0
        if timestamp >= 1e17:
            return timestamp
        if timestamp >= 1e14:
            return timestamp * 1_000
        if timestamp >= 1e11:
            return timestamp * 1_000_000
        return timestamp * 1_000_000_000

    value = float(timestamp)
    if value <= 0:
        return 0

    if value >= 1e17:
        return int(value)
    if value >= 1e14:
        return int(value * 1_000)
    if value >= 1e11:
        return int(value * 1_000_000)
    return int(value * 1_000_000_000)


def _is_full_read_snapshot(entry: Any) -> bool:
    """True when a read-state entry represents the whole file contents."""
    if entry is None:
        return False
    if _read_state_field(entry, "is_partial_view", False):
        return False
    if _read_state_field(entry, "limit", None) is not None:
        return False
    return _read_state_field(entry, "offset", None) in (None, 0, 1)


def _has_file_changed_since_read(
    path: str,
    entry: Any,
    *,
    current_content: str | None = None,
) -> bool:
    """Detect whether a file changed since its last tracked read snapshot."""
    if entry is None:
        return True

    current_timestamp_ns = _get_file_mtime_ns(path)
    stored_timestamp_ns = _normalize_read_timestamp_ns(
        _read_state_field(entry, "timestamp")
    )
    if current_timestamp_ns <= stored_timestamp_ns:
        if _is_full_read_snapshot(entry):
            if current_content is None:
                try:
                    current_content, _ = _read_file_for_edit(path)
                except OSError:
                    return True
            return current_content != _read_state_field(entry, "content", "")
        return False

    if _is_full_read_snapshot(entry):
        if current_content is None:
            current_content, _ = _read_file_for_edit(path)
        if current_content == _read_state_field(entry, "content", ""):
            return False

    return True


def _update_read_file_state(
    ctx: ToolUseContext | Any | None,
    full_path: str,
    *,
    content: str,
    timestamp_ns: int,
    offset: int | None,
    limit: int | None,
    is_partial_view: bool,
) -> None:
    """Store a normalized read snapshot in ``ctx.read_file_state``."""
    if ctx is None or not hasattr(ctx, "read_file_state"):
        return

    from openspace.services.tooling.context import ReadFileEntry

    ctx.read_file_state[full_path] = ReadFileEntry(
        content=content,
        timestamp=timestamp_ns,
        offset=offset,
        limit=limit,
        is_partial_view=is_partial_view,
    )


def _add_nested_memory_trigger(ctx: ToolUseContext | Any | None, full_path: str) -> None:
    """Register a FileRead target for nested OPENSPACE.md discovery."""
    if ctx is None or not hasattr(ctx, "nested_memory_triggers"):
        return
    triggers = getattr(ctx, "nested_memory_triggers")
    if isinstance(triggers, set):
        triggers.add(full_path)
    source_paths = getattr(ctx, "nested_memory_source_paths", None)
    if isinstance(source_paths, set):
        source_paths.add(full_path)


def _add_skill_path_trigger(ctx: ToolUseContext | Any | None, full_path: str) -> None:
    """Register a touched path for dynamic skill discovery."""
    if ctx is None:
        return
    marker = getattr(ctx, "mark_dynamic_skill_path", None)
    if callable(marker):
        marker(full_path)
        return
    triggers = getattr(ctx, "dynamic_skill_path_triggers", None)
    if isinstance(triggers, set):
        triggers.add(full_path)


def _find_similar_file(file_path: str) -> str | None:
    """Suggest a similar file when the target doesn't exist.

    Simple heuristic: check common extension swaps.
    """
    p = Path(file_path)
    parent = p.parent
    if not parent.exists():
        return None

    existing = {f.name for f in parent.iterdir() if f.is_file()}
    extensions = (".py", ".ts", ".js", ".tsx", ".jsx", ".md", ".json", ".yaml", ".yml", ".toml")
    for ext in extensions:
        candidate = p.stem + ext
        if candidate in existing and candidate != p.name:
            return str(parent / candidate)
    return None


def _format_file_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# =====================================================================
# FileEditTool
# =====================================================================

class FileEditTool(BaseTool):
    """Exact string replacement editor.

    Input schema:
        file_path:   str   — absolute path to the file
        old_string:  str   — text to replace (empty = new file / empty file overwrite)
        new_string:  str   — replacement text (must differ from old_string)
        replace_all: bool  — replace all occurrences (default False)

    The tool requires that the file has been read first (tracked via
    ``ToolUseContext.read_file_state``).
    """

    _name = "edit"
    _description = (
        "Performs exact string replacements in files. "
        "You must read the file first before editing."
    )
    backend_type = BackendType.SHELL
    _is_read_only = False
    _is_concurrency_safe = False

    search_hint: str = "modify file contents in place"
    parameter_descriptions = {
        "file_path": "The absolute path to the file to modify",
        "old_string": "The text to replace",
        "new_string": "The text to replace it with (must be different from old_string)",
        "replace_all": "Replace all occurrences of old_string (default false)",
    }

    def __init__(self, session: ShellSession | None = None):
        self._session = session
        self._current_context: ToolUseContext | None = None
        super().__init__()

    # --- check_permissions ------------------------------------------------------------

    async def check_permissions(self, input: Dict[str, Any], context: Any):
        """Delegate to filesystem.check_write_permission_for_tool.

        The path is resolved before delegating to the shared permission helper.
        """
        from openspace.grounding.core.permissions import (
            check_write_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        file_path = input.get("file_path", "") or ""
        full_path = (
            _resolve_tool_file_path(file_path, session=self._session, context=context)
            if file_path
            else ""
        )
        return check_write_permission_for_tool(
            tool_name=self._name,
            input_path=full_path,
            context=perm_ctx,
        )

    async def pre_permission_validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Validate only input-local constraints before permission checks."""
        old_string = input.get("old_string", "")
        new_string = input.get("new_string", "")
        if old_string == new_string:
            return "No changes to make: old_string and new_string are exactly the same."
        return None

    async def post_permission_validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Run filesystem/read-state validation after write permission passes."""
        return await self.validate_input(input, context)

    # --- validate_input ---------------------------------------------------------------

    async def validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Comprehensive pre-execution validation.

        Performs the file-state checks needed before editing:
        1. old_string == new_string
        2. File size limit (1 GiB)
        3. File encoding + CRLF normalization
        4. File doesn't exist -> allow only if old_string == '' (new file)
        5. File exists + old_string == '' -> only if file empty
        6. .ipynb -> redirect to NotebookEditTool
        7. readFileState check (must have read, not partial)
        8. mtime check (file modified since read?)
        9. findActualString (quote normalization)
        10. Multiple matches + !replace_all -> error
        """
        file_path = input.get("file_path", "")
        old_string = input.get("old_string", "")
        new_string = input.get("new_string", "")
        replace_all = bool(input.get("replace_all", False))

        full_path = _resolve_tool_file_path(
            file_path,
            session=self._session,
            context=context,
        )

        # 1. old_string == new_string
        if old_string == new_string:
            return "No changes to make: old_string and new_string are exactly the same."

        # 2. File size check
        try:
            stat = os.stat(full_path)
            if stat.st_size > MAX_EDIT_FILE_SIZE:
                return (
                    f"File is too large to edit ({_format_file_size(stat.st_size)}). "
                    f"Maximum editable file size is {_format_file_size(MAX_EDIT_FILE_SIZE)}."
                )
        except FileNotFoundError:
            pass
        except OSError as exc:
            return f"Cannot access file: {exc}"

        # 3. Read file content
        file_content, file_exists = _read_file_for_edit(full_path)

        # 4. File doesn't exist
        if not file_exists:
            if old_string == "":
                try:
                    from openspace.services.runtime_support.settings import validate_settings_edit

                    settings_error = validate_settings_edit(
                        full_path,
                        new_string,
                        old_content="{}",
                        cwd=getattr(context, "cwd", None),
                    )
                    if settings_error:
                        return settings_error
                except Exception:
                    pass
                return None  # new file creation
            similar = _find_similar_file(full_path)
            msg = f"File does not exist: {full_path}."
            if similar:
                msg += f" Did you mean {similar}?"
            return msg

        # 5. File exists + old_string empty
        if old_string == "":
            if file_content.strip() != "":
                return "Cannot create new file - file already exists."
            try:
                from openspace.services.runtime_support.settings import validate_settings_edit

                settings_error = validate_settings_edit(
                    full_path,
                    new_string,
                    old_content="{}",
                    cwd=getattr(context, "cwd", None),
                )
                if settings_error:
                    return settings_error
            except Exception:
                pass
            return None  # empty file overwrite

        # 6. .ipynb check
        if full_path.endswith(".ipynb"):
            return (
                "File is a Jupyter Notebook. "
                "Use the notebook_edit tool to edit this file."
            )

        # 7. readFileState check
        ctx = context
        if ctx is not None and hasattr(ctx, "read_file_state"):
            entry = ctx.read_file_state.get(full_path)
            if entry is None:
                return (
                    "File has not been read yet. "
                    "Read it first before writing to it."
                )
            if _read_state_field(entry, "is_partial_view", False):
                return (
                    "File has not been read yet. "
                    "Read it first before writing to it."
                )

            # 8. mtime check
            try:
                if _has_file_changed_since_read(
                    full_path,
                    entry,
                    current_content=file_content,
                ):
                    return (
                        "File has been modified since read, either by "
                        "the user or by a linter. Read it again before "
                        "attempting to write it."
                    )
            except OSError:
                pass

        # 9. findActualString
        actual = find_actual_string(file_content, old_string)
        if actual is None:
            return f"String to replace not found in file.\nString: {old_string}"

        # 10. Multiple matches
        matches = file_content.count(actual)
        if matches > 1 and not replace_all:
            return (
                f"Found {matches} matches of the string to replace, but "
                f"replace_all is false. To replace all occurrences, set "
                f"replace_all to true. To replace only one occurrence, "
                f"please provide more context to uniquely identify the instance.\n"
                f"String: {old_string}"
            )

        try:
            from openspace.services.runtime_support.settings import validate_settings_edit

            updated_content = (
                file_content.replace(actual, new_string)
                if replace_all
                else file_content.replace(actual, new_string, 1)
            )
            settings_error = validate_settings_edit(
                full_path,
                updated_content,
                old_content=file_content,
                cwd=getattr(context, "cwd", None),
            )
            if settings_error:
                return settings_error
        except Exception:
            pass

        return None

    # --- _arun ------------------------------------------------------------------------

    async def _arun(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        full_path = _resolve_tool_file_path(
            file_path,
            session=self._session,
            context=self._current_context,
        )

        tracker = getattr(self._current_context, "diagnostic_tracker", None)
        if tracker is not None:
            try:
                await tracker.before_file_edited(full_path)
            except Exception:
                logger.debug("diagnostic_tracker.before_file_edited failed for %s", full_path, exc_info=True)

        # Ensure parent directory exists.
        parent = os.path.dirname(full_path)
        os.makedirs(parent, exist_ok=True)
        await record_snapshot(full_path, context=self._current_context)

        # Read current content + mtime re-check (critical section)
        original, file_exists = _read_file_for_edit(full_path)

        if file_exists:
            try:
                ctx = self._current_context
                if ctx is not None and hasattr(ctx, "read_file_state"):
                    last_read = ctx.read_file_state.get(full_path)
                    if last_read is None or _has_file_changed_since_read(
                        full_path,
                        last_read,
                        current_content=original,
                    ):
                        raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)
            except (OSError, AttributeError):
                pass

        # Apply quote normalization
        actual_old = find_actual_string(original, old_string) or old_string
        actual_new = preserve_quote_style(old_string, actual_old, new_string)

        # Generate patch + apply edit
        try:
            hunks, updated = get_patch_for_edit(
                full_path, original, actual_old, actual_new, replace_all,
            )
        except ValueError as exc:
            return ToolResult(status=ToolStatus.ERROR, error=str(exc))

        # Write to disk — preserve original encoding
        raw_original = b""
        try:
            raw_original = Path(full_path).read_bytes() if file_exists else b""
        except OSError:
            pass

        is_utf16le = len(raw_original) >= 2 and raw_original[0] == 0xFF and raw_original[1] == 0xFE
        has_crlf = b"\r\n" in raw_original

        write_content = updated
        if has_crlf:
            write_content = write_content.replace("\n", "\r\n")

        if is_utf16le:
            Path(full_path).write_bytes(write_content.encode("utf-16-le"))
        else:
            Path(full_path).write_text(write_content, encoding="utf-8")

        _notify_lsp_file_written(self._current_context, full_path, updated)

        # Update read_file_state.
        ctx = self._current_context
        if ctx is not None and hasattr(ctx, "read_file_state"):
            try:
                new_mtime_ns = _get_file_mtime_ns(full_path)
            except OSError:
                new_mtime_ns = 0
            _update_read_file_state(
                ctx,
                full_path,
                content=updated,
                timestamp_ns=new_mtime_ns,
                offset=None,
                limit=None,
                is_partial_view=False,
            )
        _add_skill_path_trigger(ctx, full_path)

        # Build result message.
        if replace_all:
            result_text = (
                f"The file {file_path} has been updated. "
                f"All occurrences were successfully replaced."
            )
        else:
            result_text = f"The file {file_path} has been updated successfully."

        # Append diff snippet for model context
        patch_text = format_patch_as_text(full_path, hunks)
        if patch_text:
            snippet, start_line = get_snippet_for_patch(hunks, updated)
            if snippet:
                result_text += f"\n\nHere\'s the result of running the edit command:\n{snippet}"

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=result_text,
            metadata={
                "file_path": full_path,
                "old_string": actual_old,
                "new_string": actual_new,
                "replace_all": replace_all,
                "hunks": hunks,
            },
        )

    def set_context(self, context: ToolUseContext) -> None:
        """Inject the ToolUseContext for the current execution.

        Called by ``run_tool_use`` pipeline before ``_execute_raw()``.
        Stored as ``_current_context`` so ``_arun`` can access
        ``read_file_state`` for mtime re-check and post-write update.
        """
        self._current_context = context


# =====================================================================
# WriteFileTool
# =====================================================================
#
# Writes create or replace local files after write permission has been granted.
# Existing files must have a complete, current read_file_state entry before
# overwrite. Successful writes record history, notify diagnostics/LSP services,
# update read_file_state, and return the create/update result text.

FILE_WRITE_TOOL_NAME = "write"

_WRITE_TOOL_DESCRIPTION = """\
Writes a file to the local filesystem.

Usage:
- This tool will overwrite the existing file if there is one at the provided path.
- If this is an existing file, you MUST use the read tool first to read \
the file's contents. This tool will fail if you did not read the file first.
- Prefer the edit tool for modifying existing files — it only \
sends the diff. Only use this tool to create new files or for complete rewrites.
- NEVER create documentation files (*.md) or README files unless explicitly \
requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to \
files unless asked."""


class WriteFileTool(BaseTool):
    """Write or create a file.

    Input schema:
        file_path:  str  — absolute path to the file to write
        content:    str  — the content to write to the file

    Validates that existing files have been read first (tracked via
    ``ToolUseContext.read_file_state``) and checks mtime for concurrent
    modification before writing.
    """

    _name = FILE_WRITE_TOOL_NAME
    _description = "Write a file to the local filesystem."
    backend_type = BackendType.SHELL

    _is_read_only = False
    _is_concurrency_safe = False

    search_hint: str = "create or overwrite files"
    parameter_descriptions = {
        "file_path": "The absolute path to the file to write (must be absolute, not relative)",
        "content": "The content to write to the file",
    }

    def __init__(self, session: ShellSession | None = None):
        self._session = session
        self._current_context: ToolUseContext | None = None
        super().__init__()

    def get_prompt(self) -> str:
        """Return the dynamic tool description."""
        return _WRITE_TOOL_DESCRIPTION

    # --- check_permissions ------------------------------------------------------------

    async def check_permissions(self, input: Dict[str, Any], context: Any):
        from openspace.grounding.core.permissions import (
            check_write_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        file_path = input.get("file_path", "") or ""
        full_path = (
            _resolve_tool_file_path(file_path, session=self._session, context=context)
            if file_path
            else ""
        )
        return check_write_permission_for_tool(
            tool_name=self._name,
            input_path=full_path,
            context=perm_ctx,
        )

    async def pre_permission_validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Write has no filesystem-state validation before permission checks."""
        return None

    async def post_permission_validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Run existence/read-state/stale validation after permission passes."""
        return await self.validate_input(input, context)

    # --- validate_input ---------------------------------------------------------------

    async def validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Pre-execution validation for write filesystem state.

        Checks:
        1. File doesn't exist -> allow new file creation.
        2. readFileState: existing file must have been fully read first.
        3. mtime: existing file must not have been modified since last read.
        """
        file_path = input.get("file_path", "")
        full_path = _resolve_tool_file_path(
            file_path,
            session=self._session,
            context=context,
        )

        # -- Check if file exists --
        try:
            os.stat(full_path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"Cannot access file: {exc}"

        # -- readFileState check --
        ctx = context
        if ctx is not None and hasattr(ctx, "read_file_state"):
            entry = ctx.read_file_state.get(full_path)
            if entry is None:
                return (
                    "File has not been read yet. "
                    "Read it first before writing to it."
                )
            if _read_state_field(entry, "is_partial_view", False):
                return (
                    "File has not been read yet. "
                    "Read it first before writing to it."
                )

            # -- mtime check --
            if _has_file_changed_since_read(full_path, entry):
                return (
                    "File has been modified since read, either by the user "
                    "or by a linter. Read it again before attempting to "
                    "write it."
                )

        return None

    # --- _arun ------------------------------------------------------------------------

    async def _arun(self, file_path: str, content: str) -> ToolResult:
        full_path = _resolve_tool_file_path(
            file_path,
            session=self._session,
            context=self._current_context,
        )
        parent_dir = os.path.dirname(full_path)

        tracker = getattr(self._current_context, "diagnostic_tracker", None)
        if tracker is not None:
            try:
                await tracker.before_file_edited(full_path)
            except Exception:
                logger.debug("diagnostic_tracker.before_file_edited failed for %s", full_path, exc_info=True)

        # Ensure parent directory exists.
        os.makedirs(parent_dir, exist_ok=True)
        await record_snapshot(full_path, context=self._current_context)

        # -- Critical section: read current state + mtime re-check --
        # Read current state and guard against stale overwrites.
        meta_content: str | None = None
        meta_encoding: str = "utf-8"
        file_exists = False

        try:
            raw = Path(full_path).read_bytes()
            file_exists = True
            # BOM detection (same as _read_file_for_edit)
            if len(raw) >= 2 and raw[0] == 0xFF and raw[1] == 0xFE:
                meta_content = raw.decode("utf-16-le")
                meta_encoding = "utf-16-le"
            else:
                meta_content = raw.decode("utf-8", errors="replace")
            meta_content = meta_content.replace("\r\n", "\n")
        except FileNotFoundError:
            pass
        except OSError as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Cannot read file for staleness check: {exc}",
            )

        # Re-check mtime with content fallback.
        if file_exists:
            ctx = self._current_context
            if ctx is not None and hasattr(ctx, "read_file_state"):
                try:
                    last_read = ctx.read_file_state.get(full_path)
                    if last_read is None or _has_file_changed_since_read(
                        full_path,
                        last_read,
                        current_content=meta_content,
                    ):
                        raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)
                except RuntimeError:
                    raise
                except (OSError, AttributeError):
                    pass

        old_content = meta_content

        # Write content with LF line endings and preserve original encoding.
        try:
            if meta_encoding == "utf-16-le":
                Path(full_path).write_bytes(content.encode("utf-16-le"))
            else:
                Path(full_path).write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Failed to write file: {exc}",
            )

        _notify_lsp_file_written(self._current_context, full_path, content)

        # Update read_file_state.
        ctx = self._current_context
        if ctx is not None and hasattr(ctx, "read_file_state"):
            try:
                new_mtime_ns = _get_file_mtime_ns(full_path)
            except OSError:
                new_mtime_ns = 0
            _update_read_file_state(
                ctx,
                full_path,
                content=content,
                timestamp_ns=new_mtime_ns,
                offset=None,
                limit=None,
                is_partial_view=False,
            )
        _add_skill_path_trigger(ctx, full_path)

        # Build result text.
        if old_content is not None:
            result_text = (
                f"The file {file_path} has been updated successfully."
            )
        else:
            result_text = (
                f"File created successfully at: {file_path}"
            )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=result_text,
            metadata={
                "file_path": full_path,
                "type": "update" if old_content is not None else "create",
            },
        )

    def set_context(self, context: ToolUseContext) -> None:
        """Inject the ToolUseContext for the current execution.

        Called by ``run_tool_use`` pipeline before ``_execute_raw()``.
        """
        self._current_context = context
