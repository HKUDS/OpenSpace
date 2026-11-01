from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from openspace.services.lsp.config import get_all_lsp_servers
from openspace.services.lsp.server_instance import LSPServerInstance, _file_uri
from openspace.services.lsp.types import LSPServerConfig
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class LSPServerManager:
    """Routes files to configured language servers and lazily starts them."""

    def __init__(self, *, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self._servers: dict[str, LSPServerInstance] = {}
        self._extension_map: dict[str, list[str]] = {}
        self._opened_files: dict[str, str] = {}

    async def initialize(self) -> None:
        configs = get_all_lsp_servers(self.cwd)
        for name, config in configs.items():
            try:
                if not config.command or not config.extension_to_language:
                    continue
                for ext in config.extension_to_language:
                    self._extension_map.setdefault(ext.lower(), []).append(name)
                self._servers[name] = LSPServerInstance(name, config)
                self._servers[name].on_request("workspace/configuration", self._handle_workspace_configuration)
            except Exception as exc:
                logger.debug("Failed to initialize LSP server %s: %s", name, exc)

    async def shutdown(self) -> None:
        results = await asyncio.gather(
            *(server.stop() for server in self._servers.values() if server.state in {"running", "error"}),
            return_exceptions=True,
        )
        self._servers.clear()
        self._extension_map.clear()
        self._opened_files.clear()
        errors = [str(result) for result in results if isinstance(result, Exception)]
        if errors:
            raise RuntimeError(f"Failed to stop {len(errors)} LSP server(s): {'; '.join(errors)}")

    def get_server_for_file(self, file_path: str) -> LSPServerInstance | None:
        ext = Path(file_path).suffix.lower()
        names = self._extension_map.get(ext) or []
        return self._servers.get(names[0]) if names else None

    async def ensure_server_started(self, file_path: str) -> LSPServerInstance | None:
        server = self.get_server_for_file(file_path)
        if server is None:
            return None
        if server.state in {"stopped", "error"}:
            await server.start()
        return server

    async def send_request(self, file_path: str, method: str, params: Any) -> Any | None:
        server = await self.ensure_server_started(file_path)
        if server is None:
            return None
        return await server.send_request(method, params)

    def get_all_servers(self) -> dict[str, LSPServerInstance]:
        return self._servers

    async def open_file(self, file_path: str, content: str) -> None:
        server = await self.ensure_server_started(file_path)
        if server is None:
            return
        file_uri = _file_uri(file_path)
        if self._opened_files.get(file_uri) == server.name:
            return
        ext = Path(file_path).suffix.lower()
        language_id = server.config.extension_to_language.get(ext, "plaintext")
        await server.send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": file_uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": content,
                }
            },
        )
        self._opened_files[file_uri] = server.name

    async def change_file(self, file_path: str, content: str) -> None:
        server = self.get_server_for_file(file_path)
        if server is None or server.state != "running":
            await self.open_file(file_path, content)
            return
        file_uri = _file_uri(file_path)
        if self._opened_files.get(file_uri) != server.name:
            await self.open_file(file_path, content)
            return
        await server.send_notification(
            "textDocument/didChange",
            {
                "textDocument": {"uri": file_uri, "version": 1},
                "contentChanges": [{"text": content}],
            },
        )

    async def save_file(self, file_path: str) -> None:
        server = self.get_server_for_file(file_path)
        if server is None or server.state != "running":
            return
        await server.send_notification("textDocument/didSave", {"textDocument": {"uri": _file_uri(file_path)}})

    async def close_file(self, file_path: str) -> None:
        server = self.get_server_for_file(file_path)
        if server is None or server.state != "running":
            return
        file_uri = _file_uri(file_path)
        await server.send_notification("textDocument/didClose", {"textDocument": {"uri": file_uri}})
        self._opened_files.pop(file_uri, None)

    def is_file_open(self, file_path: str) -> bool:
        return _file_uri(file_path) in self._opened_files

    @staticmethod
    def _handle_workspace_configuration(params: Any) -> list[None]:
        items = []
        if isinstance(params, dict):
            raw_items = params.get("items")
            if isinstance(raw_items, list):
                items = raw_items
        return [None for _ in items]


__all__ = ["LSPServerManager", "LSPServerConfig"]
