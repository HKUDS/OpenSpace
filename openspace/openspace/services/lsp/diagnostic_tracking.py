from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from openspace.services.lsp.diagnostic_registry import check_for_lsp_diagnostics
from openspace.services.lsp.types import Diagnostic, DiagnosticFile

MAX_DIAGNOSTICS_SUMMARY_CHARS = 4000


class DiagnosticTrackingService:
    """OpenSpace diagnostic tracker.

    OpenSpace's tracker primarily queries IDE diagnostics through MCP and compares
    against a pre-edit baseline.  OpenSpace 22.1 has no real IDE diagnostics
    RPC, so this service records dirty files and consumes passive LSP
    diagnostics from ``LSPDiagnosticRegistry``.  The public methods keep OpenSpace's
    ``beforeFileEdited`` / ``trackDirtyFile`` / ``getNewDiagnostics`` shape for
    file tools and attachments.
    """

    def __init__(self) -> None:
        self._baselines: dict[str, list[Diagnostic]] = {}
        self._dirty_files: set[str] = set()

    async def before_file_edited(self, file_path: str) -> None:
        self._baselines.setdefault(self._normalize(file_path), [])

    def track_dirty_file(self, file_path: str) -> None:
        self._dirty_files.add(self._normalize(file_path))

    async def get_new_diagnostics(self) -> list[DiagnosticFile]:
        diagnostic_sets = check_for_lsp_diagnostics()
        files: list[DiagnosticFile] = []
        dirty = set(self._dirty_files)
        for diagnostic_set in diagnostic_sets:
            for raw_file in diagnostic_set.get("files", []):  # type: ignore[union-attr]
                file = raw_file if isinstance(raw_file, DiagnosticFile) else self._coerce_file(raw_file)
                if file is None:
                    continue
                if dirty and self._normalize(file.uri) not in dirty:
                    continue
                baseline = self._baselines.get(self._normalize(file.uri), [])
                new_diagnostics = [
                    diagnostic
                    for diagnostic in file.diagnostics
                    if not any(self._diagnostics_equal(diagnostic, old) for old in baseline)
                ]
                if new_diagnostics:
                    files.append(DiagnosticFile(uri=file.uri, diagnostics=new_diagnostics))
                self._baselines[self._normalize(file.uri)] = list(file.diagnostics)
        return files

    def reset(self) -> None:
        self._baselines.clear()
        self._dirty_files.clear()

    @staticmethod
    def format_diagnostics_summary(files: Iterable[DiagnosticFile]) -> str:
        parts: list[str] = []
        for file in files:
            filename = file.uri.rsplit("/", 1)[-1] or file.uri
            diagnostics = "\n".join(
                "  "
                + DiagnosticTrackingService._severity_symbol(diagnostic.severity)
                + f" [Line {int((diagnostic.range.get('start') or {}).get('line', 0)) + 1}:"
                + f"{int((diagnostic.range.get('start') or {}).get('character', 0)) + 1}] "
                + diagnostic.message
                + (f" [{diagnostic.code}]" if diagnostic.code else "")
                + (f" ({diagnostic.source})" if diagnostic.source else "")
                for diagnostic in file.diagnostics
            )
            parts.append(f"{filename}:\n{diagnostics}")
        result = "\n\n".join(parts)
        marker = "...[truncated]"
        if len(result) > MAX_DIAGNOSTICS_SUMMARY_CHARS:
            return result[: MAX_DIAGNOSTICS_SUMMARY_CHARS - len(marker)] + marker
        return result

    @staticmethod
    def _severity_symbol(severity: str) -> str:
        return {"Error": "x", "Warning": "!", "Info": "i", "Hint": "*"}.get(severity, "-")

    @staticmethod
    def _normalize(file_uri: str) -> str:
        for prefix in ("file://", "_claude_fs_right:", "_claude_fs_left:"):
            if file_uri.startswith(prefix):
                file_uri = file_uri[len(prefix) :]
                break
        return file_uri.replace("\\", "/").lower()

    @staticmethod
    def _diagnostics_equal(a: Diagnostic, b: Diagnostic) -> bool:
        return (
            a.message == b.message
            and a.severity == b.severity
            and a.source == b.source
            and a.code == b.code
            and json.dumps(a.range, sort_keys=True, default=str)
            == json.dumps(b.range, sort_keys=True, default=str)
        )

    @staticmethod
    def _coerce_file(raw: Any) -> DiagnosticFile | None:
        if not isinstance(raw, dict):
            return None
        diagnostics: list[Diagnostic] = []
        for item in raw.get("diagnostics") or []:
            if isinstance(item, Diagnostic):
                diagnostics.append(item)
            elif isinstance(item, dict):
                diagnostics.append(
                    Diagnostic(
                        message=str(item.get("message") or ""),
                        severity=str(item.get("severity") or "Error"),  # type: ignore[arg-type]
                        range=dict(item.get("range") or {}),
                        source=str(item["source"]) if item.get("source") is not None else None,
                        code=str(item["code"]) if item.get("code") is not None else None,
                    )
                )
        return DiagnosticFile(uri=str(raw.get("uri") or ""), diagnostics=diagnostics)


diagnostic_tracker = DiagnosticTrackingService()
