from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from openspace.services.lsp.client import LSPClient
from openspace.services.lsp.types import LSPServerConfig, LSPServerState
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

LSP_ERROR_CONTENT_MODIFIED = -32801
MAX_RETRIES_FOR_TRANSIENT_ERRORS = 3
RETRY_BASE_DELAY_MS = 500


def _file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


class LSPServerInstance:
    """Lifecycle wrapper for one configured language server."""

    def __init__(self, name: str, config: LSPServerConfig) -> None:
        self.name = name
        self.config = config
        self.state: LSPServerState = "stopped"
        self.start_time: float | None = None
        self.last_error: Exception | None = None
        self.restart_count = 0
        self._crash_recovery_count = 0
        self._client = LSPClient(name, self._on_crash)

    def _on_crash(self, error: Exception) -> None:
        self.state = "error"
        self.last_error = error
        self._crash_recovery_count += 1

    async def start(self) -> None:
        if self.state in {"running", "starting"}:
            return
        if self.state == "error" and self._crash_recovery_count > self.config.max_restarts:
            error = RuntimeError(
                f"LSP server '{self.name}' exceeded max crash recovery attempts ({self.config.max_restarts})"
            )
            self.last_error = error
            raise error

        self.state = "starting"
        try:
            await self._client.start(
                self.config.command,
                self.config.args,
                env=self.config.env,
                cwd=self.config.workspace_folder,
            )
            workspace = self.config.workspace_folder or os.getcwd()
            workspace_uri = Path(workspace).resolve().as_uri()
            init_params = {
                "processId": os.getpid(),
                "initializationOptions": self.config.initialization_options or {},
                "workspaceFolders": [{"uri": workspace_uri, "name": Path(workspace).name}],
                "rootPath": workspace,
                "rootUri": workspace_uri,
                "capabilities": {
                    "workspace": {"configuration": False, "workspaceFolders": False},
                    "textDocument": {
                        "synchronization": {
                            "dynamicRegistration": False,
                            "willSave": False,
                            "willSaveWaitUntil": False,
                            "didSave": True,
                        },
                        "publishDiagnostics": {
                            "relatedInformation": True,
                            "tagSupport": {"valueSet": [1, 2]},
                            "versionSupport": False,
                            "codeDescriptionSupport": True,
                            "dataSupport": False,
                        },
                        "hover": {"dynamicRegistration": False, "contentFormat": ["markdown", "plaintext"]},
                        "definition": {"dynamicRegistration": False, "linkSupport": True},
                        "references": {"dynamicRegistration": False},
                        "documentSymbol": {
                            "dynamicRegistration": False,
                            "hierarchicalDocumentSymbolSupport": True,
                        },
                        "callHierarchy": {"dynamicRegistration": False},
                    },
                    "general": {"positionEncodings": ["utf-16"]},
                },
            }
            await asyncio.wait_for(
                self._client.initialize(init_params),
                timeout=self.config.startup_timeout_ms / 1000,
            )
            self.state = "running"
            self.start_time = asyncio.get_running_loop().time()
            self._crash_recovery_count = 0
        except Exception as exc:
            await self._client.stop()
            self.state = "error"
            self.last_error = exc
            raise

    async def stop(self) -> None:
        if self.state in {"stopped", "stopping"}:
            return
        self.state = "stopping"
        try:
            await self._client.stop()
            self.state = "stopped"
        except Exception as exc:
            self.state = "error"
            self.last_error = exc
            raise

    async def restart(self) -> None:
        await self.stop()
        self.restart_count += 1
        if self.restart_count > self.config.max_restarts:
            raise RuntimeError(
                f"Max restart attempts ({self.config.max_restarts}) exceeded for server '{self.name}'"
            )
        await self.start()

    def is_healthy(self) -> bool:
        return self.state == "running" and self._client.is_initialized

    async def send_request(self, method: str, params: Any) -> Any:
        if not self.is_healthy():
            detail = f", last error: {self.last_error}" if self.last_error else ""
            raise RuntimeError(f"Cannot send request to LSP server '{self.name}': server is {self.state}{detail}")

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES_FOR_TRANSIENT_ERRORS + 1):
            try:
                return await asyncio.wait_for(
                    self._client.send_request(method, params),
                    timeout=self.config.request_timeout_ms / 1000,
                )
            except Exception as exc:
                last_error = exc
                code = getattr(exc, "code", None)
                if code == LSP_ERROR_CONTENT_MODIFIED and attempt < MAX_RETRIES_FOR_TRANSIENT_ERRORS:
                    await asyncio.sleep((RETRY_BASE_DELAY_MS * (2**attempt)) / 1000)
                    continue
                break
        raise RuntimeError(
            f"LSP request '{method}' failed for server '{self.name}': {last_error or 'unknown error'}"
        )

    async def send_notification(self, method: str, params: Any) -> None:
        if not self.is_healthy():
            raise RuntimeError(f"Cannot send notification to LSP server '{self.name}': server is {self.state}")
        await self._client.send_notification(method, params)

    def on_notification(self, method: str, handler: Callable[[Any], Any]) -> None:
        self._client.on_notification(method, handler)

    def on_request(self, method: str, handler: Callable[[Any], Any]) -> None:
        self._client.on_request(method, handler)


__all__ = ["LSPServerInstance", "_file_uri"]
