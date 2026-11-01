from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.services.lsp.manager import (
    get_initialization_status,
    get_lsp_server_manager,
    wait_for_initialization,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

LSP_TOOL_NAME = "lsp"
MAX_LSP_FILE_SIZE_BYTES = 10_000_000


class LSPTool(BaseTool):
    """Read-only code intelligence tool backed by optional LSP servers."""

    _name = LSP_TOOL_NAME
    _description = (
        "Perform Language Server Protocol code intelligence operations. "
        "Supports diagnostics, go_to_definition, and find_references when a "
        "language server is explicitly configured."
    )
    backend_type = BackendType.META
    _is_read_only = True
    _is_concurrency_safe = True
    should_defer = True
    search_hint = "code intelligence definitions references diagnostics"
    max_result_size_chars = 100_000
    aliases = ["LSP", "LSPTool"]
    parameter_descriptions = {
        "operation": "diagnostics, go_to_definition, or find_references",
        "file_path": "Absolute or relative path to the source file",
        "line": "1-based line number for position-based operations",
        "character": "1-based character offset for position-based operations",
    }

    def __init__(self) -> None:
        self._current_context: Any | None = None
        super().__init__()

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def validate_input(self, input: Dict[str, Any], context: Any = None) -> str | None:
        operation = str(input.get("operation") or "")
        if operation not in {"diagnostics", "go_to_definition", "find_references"}:
            return "Invalid operation. Use diagnostics, go_to_definition, or find_references."
        file_path = str(input.get("file_path") or "")
        if not file_path:
            return "file_path is required."
        path = _resolve_path(file_path, context)
        if path.startswith("\\\\") or path.startswith("//"):
            return None
        if not os.path.exists(path):
            return f"File does not exist: {file_path}"
        if not os.path.isfile(path):
            return f"Path is not a file: {file_path}"
        if operation != "diagnostics":
            try:
                line = int(input.get("line") or 0)
                character = int(input.get("character") or 0)
            except (TypeError, ValueError):
                return "line and character must be positive integers."
            if line <= 0 or character <= 0:
                return "line and character must be positive integers."
        return None

    async def check_permissions(self, input: Dict[str, Any], context: Any):
        from openspace.grounding.core.permissions import (
            check_read_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)
        return check_read_permission_for_tool(
            tool_name=self._name,
            input_path=_resolve_path(str(input.get("file_path") or ""), context),
            context=perm_ctx,
        )

    async def _arun(
        self,
        operation: str,
        file_path: str,
        line: int | None = None,
        character: int | None = None,
    ) -> ToolResult:
        absolute_path = _resolve_path(file_path, self._current_context)
        status = get_initialization_status()
        if status.get("status") == "pending":
            await wait_for_initialization()

        manager = get_lsp_server_manager()
        if manager is None:
            return _success_output(
                operation,
                file_path,
                "LSP server manager not initialized. Configure LSP servers explicitly to use this tool.",
            )

        try:
            if operation == "diagnostics":
                return await self._diagnostics(manager, absolute_path, file_path)

            if not manager.is_file_open(absolute_path):
                size = os.path.getsize(absolute_path)
                if size > MAX_LSP_FILE_SIZE_BYTES:
                    return _success_output(
                        operation,
                        file_path,
                        f"File too large for LSP analysis ({(size + 999_999) // 1_000_000}MB exceeds 10MB limit)",
                    )
                content = Path(absolute_path).read_text(encoding="utf-8", errors="replace")
                await manager.open_file(absolute_path, content)

            method, params = _method_and_params(operation, absolute_path, int(line or 1), int(character or 1))
            result = await manager.send_request(absolute_path, method, params)
            if result is None:
                return _success_output(
                    operation,
                    file_path,
                    f"No LSP server available for file type: {Path(absolute_path).suffix}",
                )
            formatted, result_count, file_count = _format_result(operation, result, os.getcwd())
            return _success_output(
                operation,
                file_path,
                formatted,
                result_count=result_count,
                file_count=file_count,
            )
        except Exception as exc:
            logger.debug("LSP tool request failed for %s on %s: %s", operation, file_path, exc)
            return _success_output(operation, file_path, f"Error performing {operation}: {exc}")

    async def _diagnostics(self, manager: Any, absolute_path: str, file_path: str) -> ToolResult:
        server = manager.get_server_for_file(absolute_path)
        if server is None:
            return _success_output("diagnostics", file_path, f"No LSP server available for file type: {Path(absolute_path).suffix}")
        if not manager.is_file_open(absolute_path):
            content = Path(absolute_path).read_text(encoding="utf-8", errors="replace")
            await manager.open_file(absolute_path, content)
        # Diagnostics arrive passively through publishDiagnostics.  Requesting a
        # save nudges servers like tsserver/pyright to publish without failing
        # the operation when unsupported.
        try:
            await manager.save_file(absolute_path)
        except Exception:
            pass
        from openspace.services.lsp.diagnostic_registry import check_for_lsp_diagnostics

        matching = []
        normalized = os.path.normcase(os.path.abspath(absolute_path))
        for diagnostic_set in check_for_lsp_diagnostics():
            for file in diagnostic_set.get("files", []):  # type: ignore[union-attr]
                uri = getattr(file, "uri", "")
                if os.path.normcase(os.path.abspath(str(uri).replace("file://", ""))) == normalized:
                    matching.append(file)
        if not matching:
            return _success_output("diagnostics", file_path, "No new LSP diagnostics available for this file.", result_count=0, file_count=0)
        lines: list[str] = []
        total = 0
        for file in matching:
            lines.append(f"{file.uri}:")
            for diagnostic in getattr(file, "diagnostics", []) or []:
                start = (diagnostic.range.get("start") or {}) if isinstance(diagnostic.range, dict) else {}
                lines.append(
                    f"  [{diagnostic.severity}] Line {int(start.get('line', 0)) + 1}:"
                    f"{int(start.get('character', 0)) + 1} {diagnostic.message}"
                )
                total += 1
        return _success_output("diagnostics", file_path, "\n".join(lines), result_count=total, file_count=len(matching))


def _resolve_path(file_path: str, context: Any) -> str:
    expanded = os.path.expanduser(file_path)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    cwd = getattr(context, "cwd", None) if context is not None else None
    return os.path.abspath(os.path.join(str(cwd or os.getcwd()), expanded))


def _success_output(
    operation: str,
    file_path: str,
    result: str,
    *,
    result_count: int | None = None,
    file_count: int | None = None,
) -> ToolResult:
    data: dict[str, Any] = {"operation": operation, "result": result, "filePath": file_path}
    if result_count is not None:
        data["resultCount"] = result_count
    if file_count is not None:
        data["fileCount"] = file_count
    return ToolResult(status=ToolStatus.SUCCESS, content=result, metadata={"data": data})


def _method_and_params(operation: str, absolute_path: str, line: int, character: int) -> tuple[str, dict[str, Any]]:
    uri = Path(absolute_path).resolve().as_uri()
    position = {"line": line - 1, "character": character - 1}
    if operation == "go_to_definition":
        return "textDocument/definition", {"textDocument": {"uri": uri}, "position": position}
    if operation == "find_references":
        return (
            "textDocument/references",
            {"textDocument": {"uri": uri}, "position": position, "context": {"includeDeclaration": True}},
        )
    raise ValueError(f"Unsupported operation: {operation}")


def _location_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        return unquote(parsed.path)
    return uri


def _format_range(range_value: dict[str, Any]) -> str:
    start = range_value.get("start") or {}
    return f"{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1}"


def _as_locations(result: Any) -> list[dict[str, Any]]:
    raw = result if isinstance(result, list) else [result] if result else []
    locations: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if "targetUri" in item:
            locations.append({"uri": item.get("targetUri"), "range": item.get("targetSelectionRange") or item.get("targetRange") or {}})
        else:
            locations.append({"uri": item.get("uri"), "range": item.get("range") or {}})
    return [loc for loc in locations if loc.get("uri")]


def _format_result(operation: str, result: Any, cwd: str) -> tuple[str, int, int]:
    locations = _as_locations(result)
    if not locations:
        return ("No definition found" if operation == "go_to_definition" else "No references found", 0, 0)
    lines: list[str] = []
    files: set[str] = set()
    for loc in locations:
        path = _location_to_path(str(loc.get("uri") or ""))
        files.add(path)
        try:
            display = os.path.relpath(path, cwd)
        except Exception:
            display = path
        lines.append(f"{display}:{_format_range(dict(loc.get('range') or {}))}")
    heading = "Definitions:" if operation == "go_to_definition" else "References:"
    return heading + "\n" + "\n".join(lines), len(locations), len(files)


__all__ = ["LSPTool", "LSP_TOOL_NAME"]
