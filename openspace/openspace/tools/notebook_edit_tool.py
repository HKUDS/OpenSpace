"""Notebook editing tool.

The canonical tool name is ``notebook_edit`` with ``NotebookEdit`` as an
alias. Tool results, permission asks, file history, and LSP notifications use
the same runtime paths as the other local file tools.
"""
from __future__ import annotations

import json
import os
import random
import string
import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus
from openspace.services.conversation.content_blocks import make_image_block, make_text_block
from openspace.persistence.file_history import record_snapshot
from openspace.utils.logging import Logger

NOTEBOOK_EDIT_TOOL_NAME = "notebook_edit"
NOTEBOOK_EDIT_TOOL_ALIAS = "NotebookEdit"
NOTEBOOK_EDIT_MAX_RESULT_SIZE_CHARS = 100_000
logger = Logger.get_logger(__name__)


def _notify_lsp_file_written(context: Any, file_path: str, content: str) -> None:
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

DESCRIPTION = "Replace the contents of a specific cell in a Jupyter notebook."
PROMPT = (
    "Completely replaces the contents of a specific cell in a Jupyter notebook "
    "(.ipynb file) with new source. Jupyter notebooks are interactive documents "
    "that combine code, text, and visualizations, commonly used for data analysis "
    "and scientific computing. The notebook_path parameter must be an absolute "
    "path, not a relative path. The cell_id identifies the cell to edit. Use "
    "edit_mode=insert to add a new cell after the cell specified by cell_id, or "
    "at the beginning if cell_id is not specified. Use edit_mode=delete to delete "
    "the cell specified by cell_id."
)

LARGE_OUTPUT_THRESHOLD = 10_000
OUTPUT_TEXT_MAX_CHARS = 30_000


def make_input_schema() -> dict[str, Any]:
    """Return the NotebookEdit tool input schema."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": (
                    "The absolute path to the Jupyter notebook file to edit "
                    "(must be absolute, not relative)"
                ),
            },
            "cell_id": {
                "type": "string",
                "description": (
                    "The ID of the cell to edit. When inserting a new cell, "
                    "the new cell will be inserted after the cell with this ID, "
                    "or at the beginning if not specified."
                ),
            },
            "new_source": {
                "type": "string",
                "description": "The new source for the cell",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": (
                    "The type of the cell (code or markdown). If not specified, "
                    "it defaults to the current cell type. If using "
                    "edit_mode=insert, this is required."
                ),
            },
            "edit_mode": {
                "type": "string",
                "enum": ["replace", "insert", "delete"],
                "description": (
                    "The type of edit to make (replace, insert, delete). "
                    "Defaults to replace."
                ),
            },
        },
        "required": ["notebook_path", "new_source"],
    }


def make_output_schema() -> dict[str, Any]:
    """Return the NotebookEdit tool output schema."""
    return {
        "type": "object",
        "properties": {
            "new_source": {
                "type": "string",
                "description": "The new source code that was written to the cell",
            },
            "cell_id": {
                "type": "string",
                "description": "The ID of the cell that was edited",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": "The type of the cell",
            },
            "language": {
                "type": "string",
                "description": "The programming language of the notebook",
            },
            "edit_mode": {
                "type": "string",
                "description": "The edit mode that was used",
            },
            "error": {
                "type": "string",
                "description": "Error message if the operation failed",
            },
            "notebook_path": {
                "type": "string",
                "description": "The path to the notebook file",
            },
            "original_file": {
                "type": "string",
                "description": "The original notebook content before modification",
            },
            "updated_file": {
                "type": "string",
                "description": "The updated notebook content after modification",
            },
        },
        "required": [
            "new_source",
            "cell_type",
            "language",
            "edit_mode",
            "notebook_path",
            "original_file",
            "updated_file",
        ],
    }


def parse_cell_id(cell_id: str) -> int | None:
    """Parse fallback ``cell-N`` identifiers.

    Only ``cell-N`` strings are treated as fallback numeric cell indexes.
    Plain numbers are not accepted.
    """
    if not isinstance(cell_id, str) or not cell_id.startswith("cell-"):
        return None
    suffix = cell_id[5:]
    if not suffix.isdigit():
        return None
    return int(suffix)


def read_notebook(notebook_path: str, cell_id: str | None = None) -> list[dict[str, Any]]:
    """Read and normalize a Jupyter notebook into cell data."""
    content = Path(os.path.expanduser(notebook_path)).read_text(encoding="utf-8")
    notebook = json.loads(content)
    if not isinstance(notebook, Mapping):
        raise ValueError("Notebook is not valid JSON.")
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        cells = []
    language = _notebook_language(notebook)

    if cell_id:
        for index, cell in enumerate(cells):
            if isinstance(cell, Mapping) and cell.get("id") == cell_id:
                return [_process_cell(dict(cell), index, language, True)]
        raise ValueError(f'Cell with ID "{cell_id}" not found in notebook')

    return [
        _process_cell(dict(cell), index, language, False)
        for index, cell in enumerate(cells)
        if isinstance(cell, Mapping)
    ]


def notebook_cells_to_content_blocks(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map notebook cells to tool result content blocks."""
    blocks: list[dict[str, Any]] = []
    for cell in cells:
        for block in _blocks_from_cell(cell):
            if (
                blocks
                and blocks[-1].get("type") == "text"
                and block.get("type") == "text"
            ):
                blocks[-1]["text"] = f"{blocks[-1].get('text', '')}\n{block.get('text', '')}"
            else:
                blocks.append(block)
    return blocks


def notebook_cells_json(cells: list[dict[str, Any]]) -> str:
    """String used as readFileState content for notebook reads."""
    return json.dumps(cells, ensure_ascii=False, separators=(",", ":"))


def _blocks_from_cell(cell: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [_cell_content_to_text_block(cell)]
    for output in cell.get("outputs") or []:
        if not isinstance(output, Mapping):
            continue
        blocks.extend(_cell_output_to_blocks(dict(output)))
    return blocks


def _cell_content_to_text_block(cell: dict[str, Any]) -> dict[str, Any]:
    metadata: list[str] = []
    cell_type = cell.get("cellType")
    if cell_type != "code":
        metadata.append(f"<cell_type>{cell_type}</cell_type>")
    if cell_type == "code" and cell.get("language") != "python":
        metadata.append(f"<language>{cell.get('language')}</language>")
    cell_id = str(cell.get("cell_id") or "")
    source = str(cell.get("source") or "")
    return make_text_block(
        f'<cell id="{cell_id}">{"".join(metadata)}{source}</cell id="{cell_id}">'
    )


def _cell_output_to_blocks(output: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = output.get("text")
    if text:
        blocks.append(make_text_block(f"\n{text}"))
    image = output.get("image")
    if isinstance(image, Mapping):
        data = str(image.get("image_data") or "")
        media_type = str(image.get("media_type") or "image/png")
        if data:
            blocks.append(make_image_block(data, media_type))
    return blocks


def _process_cell(
    cell: dict[str, Any],
    index: int,
    code_language: str,
    include_large_outputs: bool,
) -> dict[str, Any]:
    cell_type = str(cell.get("cell_type") or "")
    source = cell.get("source")
    cell_data: dict[str, Any] = {
        "cellType": cell_type,
        "source": "".join(str(item) for item in source) if isinstance(source, list) else str(source or ""),
        "cell_id": str(cell.get("id") or f"cell-{index}"),
    }
    if cell_type == "code":
        if cell.get("execution_count") is not None:
            cell_data["execution_count"] = cell.get("execution_count")
        cell_data["language"] = code_language

    outputs = cell.get("outputs")
    if cell_type == "code" and isinstance(outputs, list) and outputs:
        processed_outputs = [
            _process_output(dict(output))
            for output in outputs
            if isinstance(output, Mapping)
        ]
        processed_outputs = [output for output in processed_outputs if output is not None]
        if not include_large_outputs and _is_large_outputs(processed_outputs):
            cell_data["outputs"] = [{
                "output_type": "stream",
                "text": (
                    "Outputs are too large to include. Use bash with: "
                    f"cat <notebook_path> | jq '.cells[{index}].outputs'"
                ),
            }]
        elif processed_outputs:
            cell_data["outputs"] = processed_outputs
    return cell_data


def _process_output(output: dict[str, Any]) -> dict[str, Any] | None:
    output_type = output.get("output_type")
    if output_type == "stream":
        return {
            "output_type": output_type,
            "text": _process_output_text(output.get("text")),
        }
    if output_type in {"execute_result", "display_data"}:
        data = output.get("data")
        data_map = data if isinstance(data, Mapping) else {}
        result: dict[str, Any] = {
            "output_type": output_type,
            "text": _process_output_text(data_map.get("text/plain")),
        }
        image = _extract_output_image(data_map)
        if image is not None:
            result["image"] = image
        return result
    if output_type == "error":
        traceback = output.get("traceback")
        tb_text = "\n".join(str(v) for v in traceback) if isinstance(traceback, list) else ""
        return {
            "output_type": output_type,
            "text": _process_output_text(
                f"{output.get('ename')}: {output.get('evalue')}\n{tb_text}"
            ),
        }
    return None


def _process_output_text(text: Any) -> str:
    if text is None:
        return ""
    raw = "".join(str(v) for v in text) if isinstance(text, list) else str(text)
    return _format_output(raw)


def _format_output(content: str) -> str:
    """Format notebook output text with truncation."""
    if len(content) <= OUTPUT_TEXT_MAX_CHARS:
        return content
    truncated_part = content[:OUTPUT_TEXT_MAX_CHARS]
    remaining_lines = content[OUTPUT_TEXT_MAX_CHARS:].count("\n") + 1
    return f"{truncated_part}\n\n... [{remaining_lines} lines truncated] ..."


def _extract_output_image(data: Mapping[str, Any]) -> dict[str, str] | None:
    png = data.get("image/png")
    if isinstance(png, str):
        return {"image_data": "".join(png.split()), "media_type": "image/png"}
    jpeg = data.get("image/jpeg")
    if isinstance(jpeg, str):
        return {"image_data": "".join(jpeg.split()), "media_type": "image/jpeg"}
    return None


def _is_large_outputs(outputs: list[dict[str, Any]]) -> bool:
    size = 0
    for output in outputs:
        size += len(str(output.get("text") or ""))
        image = output.get("image")
        if isinstance(image, Mapping):
            size += len(str(image.get("image_data") or ""))
        if size > LARGE_OUTPUT_THRESHOLD:
            return True
    return False


def _read_text_with_metadata(path: str) -> tuple[str, str, str]:
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
        content = raw.decode("utf-16-le", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
        content = raw.decode("utf-8-sig", errors="replace")
    else:
        encoding = "utf-8"
        content = raw.decode("utf-8", errors="replace")

    line_endings = "CRLF" if b"\r\n" in raw else "LF"
    content = content.replace("\r\n", "\n")
    return content, encoding, line_endings


def _write_text_content(path: str, content: str, encoding: str, line_endings: str) -> None:
    write_content = content.replace("\n", "\r\n") if line_endings == "CRLF" else content
    if encoding == "utf-16-le":
        Path(path).write_bytes(write_content.encode("utf-16-le"))
    else:
        Path(path).write_text(write_content, encoding=encoding)


def _notebook_language(notebook: Mapping[str, Any]) -> str:
    metadata = notebook.get("metadata")
    if not isinstance(metadata, Mapping):
        return "python"
    language_info = metadata.get("language_info")
    if not isinstance(language_info, Mapping):
        return "python"
    language = language_info.get("name")
    return str(language) if language else "python"


def _notebook_cells(notebook: Mapping[str, Any]) -> list[Any]:
    cells = notebook.get("cells")
    return cells if isinstance(cells, list) else []


def _find_cell_index(cells: list[Any], cell_id: str) -> tuple[int, str | None]:
    for index, cell in enumerate(cells):
        if isinstance(cell, Mapping) and cell.get("id") == cell_id:
            return index, None
    parsed_index = parse_cell_id(cell_id)
    if parsed_index is not None:
        if parsed_index < 0 or parsed_index >= len(cells):
            return -1, f"Cell with index {parsed_index} does not exist in notebook."
        return parsed_index, None
    return -1, f'Cell with ID "{cell_id}" not found in notebook.'


def _random_cell_id() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(13))


def _supports_cell_ids(notebook: Mapping[str, Any]) -> bool:
    nbformat = notebook.get("nbformat")
    minor = notebook.get("nbformat_minor")
    try:
        nbformat_int = int(nbformat)
        minor_int = int(minor)
    except Exception:
        return False
    return nbformat_int > 4 or (nbformat_int == 4 and minor_int >= 5)


def _resolve_path(notebook_path: str, context: Any | None, session: Any | None) -> str:
    from openspace.grounding.backends.shell.file_tools import _resolve_tool_file_path

    return _resolve_tool_file_path(notebook_path, session=session, context=context)


def _read_state_timestamp_is_stale(path: str, entry: Any) -> bool:
    from openspace.grounding.backends.shell.file_tools import (
        _get_file_mtime_ns,
        _normalize_read_timestamp_ns,
        _read_state_field,
    )

    return _get_file_mtime_ns(path) > _normalize_read_timestamp_ns(
        _read_state_field(entry, "timestamp")
    )


def _update_notebook_read_state(context: Any | None, path: str, content: str) -> None:
    if context is None or not hasattr(context, "read_file_state"):
        return
    from openspace.grounding.backends.shell.file_tools import (
        _get_file_mtime_ns,
        _update_read_file_state,
    )

    _update_read_file_state(
        context,
        path,
        content=content,
        timestamp_ns=_get_file_mtime_ns(path),
        offset=None,
        limit=None,
        is_partial_view=False,
    )


def _make_data(
    *,
    new_source: str,
    cell_type: str | None,
    language: str,
    edit_mode: str,
    cell_id: str | None,
    notebook_path: str,
    original_file: str,
    updated_file: str,
    error: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "new_source": new_source,
        "cell_type": cell_type or "code",
        "language": language,
        "edit_mode": edit_mode,
        "error": error,
        "notebook_path": notebook_path,
        "original_file": original_file,
        "updated_file": updated_file,
    }
    if cell_id is not None:
        data["cell_id"] = cell_id
    return data


def _js_display(value: Any) -> str:
    return "undefined" if value is None else str(value)


def _format_tool_result_content(data: Mapping[str, Any]) -> str:
    error = data.get("error")
    if error:
        return str(error)
    edit_mode = data.get("edit_mode")
    cell_id = _js_display(data.get("cell_id"))
    new_source = str(data.get("new_source") or "")
    if edit_mode == "replace":
        return f"Updated cell {cell_id} with {new_source}"
    if edit_mode == "insert":
        return f"Inserted cell {cell_id} with {new_source}"
    if edit_mode == "delete":
        return f"Deleted cell {cell_id}"
    return "Unknown edit mode"


class NotebookEditTool(BaseTool):
    _name = NOTEBOOK_EDIT_TOOL_NAME
    _description = DESCRIPTION
    backend_type = BackendType.SHELL
    aliases = [NOTEBOOK_EDIT_TOOL_ALIAS]
    should_defer = True
    search_hint = "edit Jupyter notebook cells (.ipynb)"
    max_result_size_chars = NOTEBOOK_EDIT_MAX_RESULT_SIZE_CHARS
    _is_read_only = False
    _is_concurrency_safe = False

    def __init__(self, session: Any | None = None) -> None:
        self._session = session
        self._current_context: Any | None = None
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=DESCRIPTION,
                parameters=make_input_schema(),
                return_schema=make_output_schema(),
                backend_type=self.backend_type,
            )
        )

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def get_prompt(self, context: Any = None) -> str:
        return PROMPT

    def user_facing_name(self) -> str:
        return "Edit Notebook"

    def get_path(self, input_data: Mapping[str, Any]) -> str:
        return str(input_data.get("notebook_path") or "")

    async def check_permissions(self, input: dict[str, Any], context: Any = None):
        from openspace.grounding.core.permissions import (
            check_write_permission_for_tool,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        notebook_path = str(input.get("notebook_path") or "")
        full_path = (
            _resolve_path(notebook_path, context, self._session)
            if notebook_path
            else ""
        )
        return check_write_permission_for_tool(
            tool_name=self._name,
            input_path=full_path,
            context=perm_ctx,
        )

    async def pre_permission_validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> str | None:
        return self._validate_input_without_io(input, context)

    async def post_permission_validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> str | None:
        return await self.validate_input(input, context)

    async def validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> str | None:
        basic_error = self._validate_input_without_io(input, context)
        if basic_error is not None:
            return basic_error

        notebook_path = str(input.get("notebook_path") or "")
        edit_mode = str(input.get("edit_mode") or "replace")
        cell_id = input.get("cell_id")
        full_path = _resolve_path(notebook_path, context, self._session)

        if full_path.startswith("\\\\") or full_path.startswith("//"):
            return None

        entry = None
        if context is not None and hasattr(context, "read_file_state"):
            entry = context.read_file_state.get(full_path)
        if entry is None:
            return "File has not been read yet. Read it first before writing to it."

        try:
            if _read_state_timestamp_is_stale(full_path, entry):
                return (
                    "File has been modified since read, either by the user or "
                    "by a linter. Read it again before attempting to write it."
                )
        except FileNotFoundError:
            return "Notebook file does not exist."

        try:
            content, _, _ = _read_text_with_metadata(full_path)
        except FileNotFoundError:
            return "Notebook file does not exist."
        notebook = self._parse_notebook_for_validation(content)
        if notebook is None:
            return "Notebook is not valid JSON."

        cells = _notebook_cells(notebook)
        if not cell_id:
            if edit_mode != "insert":
                return "Cell ID must be specified when not inserting a new cell."
            return None

        cell_index, cell_error = _find_cell_index(cells, str(cell_id))
        if cell_index == -1:
            return cell_error
        return None

    def _validate_input_without_io(
        self,
        input: Mapping[str, Any],
        context: Any = None,
    ) -> str | None:
        notebook_path = str(input.get("notebook_path") or "")
        cell_type = input.get("cell_type")
        edit_mode = str(input.get("edit_mode") or "replace")
        full_path = _resolve_path(notebook_path, context, self._session)

        if full_path.startswith("\\\\") or full_path.startswith("//"):
            return None
        if Path(full_path).suffix != ".ipynb":
            return (
                "File must be a Jupyter notebook (.ipynb file). For editing "
                "other file types, use the FileEdit tool."
            )
        if edit_mode not in {"replace", "insert", "delete"}:
            return "Edit mode must be replace, insert, or delete."
        if edit_mode == "insert" and not cell_type:
            return "Cell type is required when using edit_mode=insert."
        return None

    @staticmethod
    def _parse_notebook_for_validation(content: str) -> Mapping[str, Any] | None:
        try:
            parsed = json.loads(content)
        except Exception:
            return None
        return parsed if isinstance(parsed, Mapping) else None

    async def _arun(
        self,
        notebook_path: str,
        new_source: str,
        cell_id: str | None = None,
        cell_type: str | None = None,
        edit_mode: str = "replace",
    ) -> ToolResult:
        full_path = _resolve_path(notebook_path, self._current_context, self._session)
        tracker = getattr(self._current_context, "diagnostic_tracker", None)
        if tracker is not None:
            try:
                await tracker.before_file_edited(full_path)
            except Exception:
                logger.debug("diagnostic_tracker.before_file_edited failed for %s", full_path, exc_info=True)
        await record_snapshot(full_path, context=self._current_context)

        try:
            content, encoding, line_endings = _read_text_with_metadata(full_path)
            try:
                notebook = json.loads(content)
                if not isinstance(notebook, dict):
                    raise ValueError("notebook root must be an object")
            except Exception:
                data = _make_data(
                    new_source=new_source,
                    cell_type=cell_type,
                    language="python",
                    edit_mode="replace",
                    cell_id=cell_id,
                    notebook_path=full_path,
                    original_file="",
                    updated_file="",
                    error="Notebook is not valid JSON.",
                )
                return self._result_from_data(data)

            cells = _notebook_cells(notebook)
            if not cell_id:
                if edit_mode != "insert":
                    data = _make_data(
                        new_source=new_source,
                        cell_type=cell_type,
                        language="python",
                        edit_mode="replace",
                        cell_id=cell_id,
                        notebook_path=full_path,
                        original_file="",
                        updated_file="",
                        error="Cell ID must be specified when not inserting a new cell.",
                    )
                    return self._result_from_data(data)
                if not cell_type:
                    data = _make_data(
                        new_source=new_source,
                        cell_type=cell_type,
                        language="python",
                        edit_mode="replace",
                        cell_id=cell_id,
                        notebook_path=full_path,
                        original_file="",
                        updated_file="",
                        error="Cell type is required when using edit_mode=insert.",
                    )
                    return self._result_from_data(data)
                cell_index = 0
            else:
                cell_index, cell_error = _find_cell_index(cells, cell_id)
                if cell_index == -1:
                    data = _make_data(
                        new_source=new_source,
                        cell_type=cell_type,
                        language="python",
                        edit_mode="replace",
                        cell_id=cell_id,
                        notebook_path=full_path,
                        original_file="",
                        updated_file="",
                        error=cell_error or f'Cell with ID "{cell_id}" not found in notebook.',
                    )
                    return self._result_from_data(data)
                if edit_mode == "insert":
                    if not cell_type:
                        data = _make_data(
                            new_source=new_source,
                            cell_type=cell_type,
                            language="python",
                            edit_mode="replace",
                            cell_id=cell_id,
                            notebook_path=full_path,
                            original_file="",
                            updated_file="",
                            error="Cell type is required when using edit_mode=insert.",
                        )
                        return self._result_from_data(data)
                    cell_index += 1

            effective_edit_mode = edit_mode
            if effective_edit_mode == "replace" and cell_index == len(cells):
                effective_edit_mode = "insert"
                if not cell_type:
                    cell_type = "code"

            language = _notebook_language(notebook)
            new_cell_id: str | None = None
            if _supports_cell_ids(notebook):
                if effective_edit_mode == "insert":
                    new_cell_id = _random_cell_id()
                elif cell_id is not None:
                    new_cell_id = cell_id

            if effective_edit_mode == "delete":
                cells.pop(cell_index)
            elif effective_edit_mode == "insert":
                if cell_type == "markdown":
                    new_cell: dict[str, Any] = {
                        "cell_type": "markdown",
                        "id": new_cell_id,
                        "source": new_source,
                        "metadata": {},
                    }
                else:
                    new_cell = {
                        "cell_type": "code",
                        "id": new_cell_id,
                        "source": new_source,
                        "metadata": {},
                        "execution_count": None,
                        "outputs": [],
                    }
                cells.insert(cell_index, new_cell)
            else:
                target_cell = cells[cell_index]
                if not isinstance(target_cell, dict):
                    target_cell = {}
                    cells[cell_index] = target_cell
                original_cell_type = target_cell.get("cell_type")
                target_cell["source"] = new_source
                if original_cell_type == "code":
                    target_cell["execution_count"] = None
                    target_cell["outputs"] = []
                if cell_type and cell_type != original_cell_type:
                    target_cell["cell_type"] = cell_type

            updated_content = json.dumps(notebook, ensure_ascii=False, indent=1)
            _write_text_content(full_path, updated_content, encoding, line_endings)
            _notify_lsp_file_written(self._current_context, full_path, updated_content)
            _update_notebook_read_state(self._current_context, full_path, updated_content)

            data = _make_data(
                new_source=new_source,
                cell_type=cell_type,
                language=language,
                edit_mode=effective_edit_mode or "replace",
                cell_id=new_cell_id,
                notebook_path=full_path,
                original_file=content,
                updated_file=updated_content,
            )
            return self._result_from_data(data)
        except Exception as exc:
            data = _make_data(
                new_source=new_source,
                cell_type=cell_type,
                language="python",
                edit_mode="replace",
                cell_id=cell_id,
                notebook_path=full_path,
                original_file="",
                updated_file="",
                error=str(exc) if isinstance(exc, Exception) else "Unknown error occurred while editing notebook",
            )
            return self._result_from_data(data)

    def _result_from_data(self, data: Mapping[str, Any]) -> ToolResult:
        error = str(data.get("error") or "")
        content = _format_tool_result_content(data)
        return ToolResult(
            status=ToolStatus.ERROR if error else ToolStatus.SUCCESS,
            content=content,
            error=error or None,
            metadata={
                "tool": self.name,
                "data": dict(data),
            },
        )


__all__ = [
    "NOTEBOOK_EDIT_TOOL_ALIAS",
    "DESCRIPTION",
    "NOTEBOOK_EDIT_MAX_RESULT_SIZE_CHARS",
    "NOTEBOOK_EDIT_TOOL_NAME",
    "NotebookEditTool",
    "PROMPT",
    "make_input_schema",
    "make_output_schema",
    "notebook_cells_json",
    "notebook_cells_to_content_blocks",
    "parse_cell_id",
    "read_notebook",
]
