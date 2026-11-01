from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LSPServerState = Literal["stopped", "starting", "running", "stopping", "error"]
LSPSeverity = Literal["Error", "Warning", "Info", "Hint"]


@dataclass(slots=True)
class LSPServerConfig:
    """Configured language server.

    Implementation notes: ``services/lsp/types.ts`` + plugin LSP config.  OpenSpace loads
    the same shape from explicit local config/env instead of OpenSpace plugins.
    """

    command: str
    args: list[str] = field(default_factory=list)
    extension_to_language: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    workspace_folder: str | None = None
    initialization_options: dict[str, Any] = field(default_factory=dict)
    startup_timeout_ms: int = 10_000
    request_timeout_ms: int = 10_000
    max_restarts: int = 3


@dataclass(slots=True)
class Diagnostic:
    message: str
    severity: LSPSeverity = "Error"
    range: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    code: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "message": self.message,
            "severity": self.severity,
            "range": self.range,
        }
        if self.source is not None:
            data["source"] = self.source
        if self.code is not None:
            data["code"] = self.code
        return data


@dataclass(slots=True)
class DiagnosticFile:
    uri: str
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "diagnostics": [diagnostic.to_json() for diagnostic in self.diagnostics],
        }
