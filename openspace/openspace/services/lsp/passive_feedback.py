from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

from openspace.services.lsp.diagnostic_registry import register_pending_lsp_diagnostic
from openspace.services.lsp.types import Diagnostic, DiagnosticFile
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def _map_lsp_severity(value: Any) -> str:
    return {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}.get(value, "Error")


def _uri_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path)
        if parsed.netloc:
            return f"//{parsed.netloc}{path}"
        return path
    return uri


def format_diagnostics_for_attachment(params: dict[str, Any]) -> list[DiagnosticFile]:
    uri = _uri_to_path(str(params.get("uri") or ""))
    diagnostics: list[Diagnostic] = []
    for raw in params.get("diagnostics") or []:
        if not isinstance(raw, dict):
            continue
        code = raw.get("code")
        diagnostics.append(
            Diagnostic(
                message=str(raw.get("message") or ""),
                severity=_map_lsp_severity(raw.get("severity")),  # type: ignore[arg-type]
                range=dict(raw.get("range") or {}),
                source=str(raw["source"]) if raw.get("source") is not None else None,
                code=str(code) if code is not None else None,
            )
        )
    return [DiagnosticFile(uri=uri, diagnostics=diagnostics)]


def register_lsp_notification_handlers(manager: Any) -> dict[str, Any]:
    """Register publishDiagnostics handlers on all configured servers."""

    registration_errors: list[dict[str, str]] = []
    success_count = 0
    diagnostic_failures: dict[str, dict[str, Any]] = {}
    servers = manager.get_all_servers()
    for server_name, server in servers.items():
        try:
            def _handler(params: Any, *, _server_name: str = server_name) -> None:
                try:
                    if not isinstance(params, dict) or "uri" not in params or "diagnostics" not in params:
                        return
                    files = format_diagnostics_for_attachment(params)
                    first = files[0] if files else None
                    if first is None or not first.diagnostics:
                        return
                    register_pending_lsp_diagnostic(server_name=_server_name, files=files)
                    diagnostic_failures.pop(_server_name, None)
                except Exception as exc:
                    state = diagnostic_failures.setdefault(_server_name, {"count": 0, "lastError": ""})
                    state["count"] += 1
                    state["lastError"] = str(exc)
                    logger.debug("LSP diagnostic handler failed for %s: %s", _server_name, exc)

            server.on_notification("textDocument/publishDiagnostics", _handler)
            success_count += 1
        except Exception as exc:
            registration_errors.append({"serverName": server_name, "error": str(exc)})

    return {
        "totalServers": len(servers),
        "successCount": success_count,
        "registrationErrors": registration_errors,
        "diagnosticFailures": diagnostic_failures,
    }
