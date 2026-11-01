from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

from openspace.services.lsp.types import DiagnosticFile

MAX_DIAGNOSTICS_PER_FILE = 10
MAX_TOTAL_DIAGNOSTICS = 30
MAX_DELIVERED_FILES = 500


@dataclass(slots=True)
class PendingLSPDiagnostic:
    server_name: str
    files: list[DiagnosticFile]
    timestamp: float
    attachment_sent: bool = False


class LSPDiagnosticRegistry:
    """OpenSpace ``LSPDiagnosticRegistry.ts`` equivalent for passive diagnostics."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingLSPDiagnostic] = {}
        self._delivered: OrderedDict[str, set[str]] = OrderedDict()

    def register_pending_lsp_diagnostic(self, *, server_name: str, files: list[DiagnosticFile]) -> None:
        self._pending[str(uuid4())] = PendingLSPDiagnostic(
            server_name=server_name,
            files=files,
            timestamp=time.time(),
        )

    def check_for_lsp_diagnostics(self) -> list[dict[str, object]]:
        all_files: list[DiagnosticFile] = []
        server_names: set[str] = set()
        to_mark: list[PendingLSPDiagnostic] = []
        for diagnostic in self._pending.values():
            if not diagnostic.attachment_sent:
                all_files.extend(diagnostic.files)
                server_names.add(diagnostic.server_name)
                to_mark.append(diagnostic)
        if not all_files:
            return []

        deduped = self._deduplicate_files(all_files)
        for diagnostic in to_mark:
            diagnostic.attachment_sent = True
        self._pending = {
            key: value
            for key, value in self._pending.items()
            if not value.attachment_sent
        }

        total = 0
        limited: list[DiagnosticFile] = []
        severity_order = {"Error": 1, "Warning": 2, "Info": 3, "Hint": 4}
        for file in deduped:
            file.diagnostics.sort(key=lambda d: severity_order.get(d.severity, 4))
            remaining = MAX_TOTAL_DIAGNOSTICS - total
            if remaining <= 0:
                break
            file.diagnostics = file.diagnostics[: min(MAX_DIAGNOSTICS_PER_FILE, remaining)]
            if file.diagnostics:
                limited.append(file)
                total += len(file.diagnostics)

        for file in limited:
            delivered = self._delivered.setdefault(file.uri, set())
            self._delivered.move_to_end(file.uri)
            for diagnostic in file.diagnostics:
                delivered.add(self._diagnostic_key(diagnostic.to_json()))
        while len(self._delivered) > MAX_DELIVERED_FILES:
            self._delivered.popitem(last=False)

        if total == 0:
            return []
        return [{"serverName": ", ".join(sorted(server_names)), "files": limited}]

    def clear_all_lsp_diagnostics(self) -> None:
        self._pending.clear()

    def reset_all_lsp_diagnostic_state(self) -> None:
        self._pending.clear()
        self._delivered.clear()

    def clear_delivered_diagnostics_for_file(self, file_uri: str) -> None:
        self._delivered.pop(file_uri, None)

    def pending_count(self) -> int:
        return len(self._pending)

    def _deduplicate_files(self, files: list[DiagnosticFile]) -> list[DiagnosticFile]:
        seen_by_file: dict[str, set[str]] = {}
        merged: dict[str, DiagnosticFile] = {}
        for file in files:
            seen = seen_by_file.setdefault(file.uri, set())
            previously = self._delivered.get(file.uri, set())
            target = merged.setdefault(file.uri, DiagnosticFile(uri=file.uri, diagnostics=[]))
            for diagnostic in file.diagnostics:
                key = self._diagnostic_key(diagnostic.to_json())
                if key in seen or key in previously:
                    continue
                seen.add(key)
                target.diagnostics.append(diagnostic)
        return [file for file in merged.values() if file.diagnostics]

    @staticmethod
    def _diagnostic_key(diagnostic: dict[str, object]) -> str:
        return json.dumps(
            {
                "message": diagnostic.get("message"),
                "severity": diagnostic.get("severity"),
                "range": diagnostic.get("range"),
                "source": diagnostic.get("source"),
                "code": diagnostic.get("code"),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )


diagnostic_registry = LSPDiagnosticRegistry()


def register_pending_lsp_diagnostic(*, server_name: str, files: list[DiagnosticFile]) -> None:
    diagnostic_registry.register_pending_lsp_diagnostic(server_name=server_name, files=files)


def check_for_lsp_diagnostics() -> list[dict[str, object]]:
    return diagnostic_registry.check_for_lsp_diagnostics()


def clear_all_lsp_diagnostics() -> None:
    diagnostic_registry.clear_all_lsp_diagnostics()


def reset_all_lsp_diagnostic_state() -> None:
    diagnostic_registry.reset_all_lsp_diagnostic_state()


def clear_delivered_diagnostics_for_file(file_uri: str) -> None:
    diagnostic_registry.clear_delivered_diagnostics_for_file(file_uri)
