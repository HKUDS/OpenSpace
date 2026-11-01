from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

NotificationHandler = Callable[[Any], None | Awaitable[None]]
RequestHandler = Callable[[Any], Any | Awaitable[Any]]


class LSPClient:
    """Stdio JSON-RPC client for a language server process."""

    def __init__(self, server_name: str, on_crash: Callable[[Exception], None] | None = None) -> None:
        self.server_name = server_name
        self._on_crash = on_crash
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._capabilities: dict[str, Any] | None = None
        self._initialized = False
        self._stopping = False

    @property
    def capabilities(self) -> dict[str, Any] | None:
        return self._capabilities

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def start(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if self._process is not None:
            return
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                command,
                *(args or []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
                cwd=cwd,
            )
        except Exception:
            self._process = None
            raise
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self.send_request("initialize", params)
        if isinstance(result, dict):
            capabilities = result.get("capabilities")
            if isinstance(capabilities, dict):
                self._capabilities = capabilities
        await self.send_notification("initialized", {})
        self._initialized = True
        return result if isinstance(result, dict) else {}

    async def send_request(self, method: str, params: Any) -> Any:
        if self._process is None:
            raise RuntimeError(f"LSP server {self.server_name} is not started")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        await self._write_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await future

    async def send_notification(self, method: str, params: Any) -> None:
        if self._process is None:
            raise RuntimeError(f"LSP server {self.server_name} is not started")
        await self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers.setdefault(method, []).append(handler)

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    async def stop(self) -> None:
        self._stopping = True
        try:
            if self._process is not None and self._initialized:
                try:
                    await asyncio.wait_for(self.send_request("shutdown", None), timeout=2)
                    await self.send_notification("exit", None)
                except Exception:
                    pass
            if self._process is not None and self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
        finally:
            for task in (self._reader_task, self._stderr_task):
                if task is not None:
                    task.cancel()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(f"LSP server {self.server_name} stopped"))
            self._pending.clear()
            self._process = None
            self._initialized = False
            self._stopping = False

    async def _write_message(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError(f"LSP server {self.server_name} stdin is not available")
        body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        process.stdin.write(header + body)
        await process.stdin.drain()

    async def _read_message(self) -> dict[str, Any] | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        headers: dict[str, str] = {}
        while True:
            line = await process.stdout.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            text = line.decode("ascii", errors="replace").strip()
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.lower()] = value.strip()
        length = int(headers.get("content-length") or "0")
        if length <= 0:
            return None
        body = await process.stdout.readexactly(length)
        return json.loads(body.decode("utf-8"))

    async def _reader_loop(self) -> None:
        try:
            while True:
                message = await self._read_message()
                if message is None:
                    break
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._stopping:
                logger.debug("LSP reader failed for %s: %s", self.server_name, exc)
        finally:
            if not self._stopping:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError(f"LSP server {self.server_name} connection closed"))
                self._pending.clear()
                self._initialized = False
                if self._on_crash is not None:
                    self._on_crash(RuntimeError(f"LSP server {self.server_name} connection closed"))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = message.get("id")
            future = self._pending.pop(int(request_id), None) if isinstance(request_id, int) else None
            if future is None or future.done():
                return
            if "error" in message:
                error = message.get("error") or {}
                exc = RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
                if isinstance(error, dict) and isinstance(error.get("code"), int):
                    setattr(exc, "code", error["code"])
                future.set_exception(exc)
            else:
                future.set_result(message.get("result"))
            return

        method = str(message.get("method") or "")
        if "id" in message:
            await self._handle_server_request(message, method)
            return
        for handler in self._notification_handlers.get(method, []):
            result = handler(message.get("params"))
            if asyncio.iscoroutine(result):
                await result

    async def _handle_server_request(self, message: dict[str, Any], method: str) -> None:
        handler = self._request_handlers.get(method)
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
        try:
            result = handler(message.get("params")) if handler else None
            if asyncio.iscoroutine(result):
                result = await result
            response["result"] = result
        except Exception as exc:
            response["error"] = {"code": -32603, "message": str(exc)}
        await self._write_message(response)

    async def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                logger.debug("[LSP SERVER %s] %s", self.server_name, line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
