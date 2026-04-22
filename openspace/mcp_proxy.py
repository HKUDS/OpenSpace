from __future__ import annotations

import asyncio
import argparse
import contextlib
import inspect
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from openspace.mcp_stdio import maybe_redirect_stderr_to_file
from openspace.grounding.backends.mcp.client import MCPClient
from openspace.mcp_tool_registration import (
    register_evolution_tools,
    register_main_tools,
)
from openspace.shared_mcp_runtime import ServerKind, ensure_daemon


_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
maybe_redirect_stderr_to_file(_LOG_DIR, "mcp_proxy_stderr.log")

_proxy_activity_lock = threading.Lock()
_proxy_active_request_count = 0
_proxy_last_activity_at = time.monotonic()
_proxy_idle_watchdog_started = False


def _proxy_mode_for(server_kind: ServerKind) -> str:
    raw = os.environ.get("OPENSPACE_MCP_PROXY_MODE", "").strip().lower()
    if raw in {"daemon", "direct"}:
        return raw
    return "daemon"


def _proxy_idle_timeout_seconds() -> int:
    timeout_raw = os.environ.get("OPENSPACE_MCP_PROXY_IDLE_TIMEOUT_SECONDS", "").strip()
    if not timeout_raw:
        timeout_raw = os.environ.get("OPENSPACE_MCP_IDLE_TIMEOUT_SECONDS", "").strip()
    if timeout_raw:
        try:
            return int(timeout_raw)
        except ValueError:
            return 0
    return 180


def _mark_proxy_request_start() -> None:
    global _proxy_active_request_count, _proxy_last_activity_at
    with _proxy_activity_lock:
        _proxy_active_request_count += 1
        _proxy_last_activity_at = time.monotonic()


def _mark_proxy_request_end() -> None:
    global _proxy_active_request_count, _proxy_last_activity_at
    with _proxy_activity_lock:
        _proxy_active_request_count = max(0, _proxy_active_request_count - 1)
        _proxy_last_activity_at = time.monotonic()


def _begin_proxy_shutdown(reason: str) -> None:
    with contextlib.suppress(Exception):
        os.kill(os.getpid(), signal.SIGTERM)


def _proxy_idle_watchdog_loop(idle_timeout_seconds: int) -> None:
    check_interval = max(1, min(max(idle_timeout_seconds // 3, 1), 60))
    while True:
        time.sleep(check_interval)
        with _proxy_activity_lock:
            active = _proxy_active_request_count
            idle_for = time.monotonic() - _proxy_last_activity_at
        if active == 0 and idle_for >= idle_timeout_seconds:
            _begin_proxy_shutdown(f"idle timeout after {idle_for:.1f}s")
            return


def _maybe_start_proxy_idle_watchdog() -> None:
    global _proxy_idle_watchdog_started
    if _proxy_idle_watchdog_started:
        return

    idle_timeout_seconds = _proxy_idle_timeout_seconds()
    if idle_timeout_seconds <= 0:
        return

    watchdog = threading.Thread(
        target=_proxy_idle_watchdog_loop,
        args=(idle_timeout_seconds,),
        name="openspace-mcp-proxy-idle-watchdog",
        daemon=True,
    )
    watchdog.start()
    _proxy_idle_watchdog_started = True


def _json_error(error: Any, **extra: Any) -> str:
    return json.dumps({"error": str(error), **extra}, ensure_ascii=False)


def _extract_text_payload(result: Any) -> str:
    text_parts: list[str] = []
    for item in getattr(result, "content", []):
        if isinstance(item, TextContent):
            text_parts.append(item.text)
            continue
        text = getattr(item, "text", None)
        if text is not None:
            text_parts.append(text)
    if not text_parts:
        raise RuntimeError("Remote MCP tool returned no text payload")
    return "\n".join(text_parts)


class _RemoteProxyBase:
    def __init__(self, server_kind: ServerKind):
        self._server_kind = server_kind

    async def _call_remote_tool_once(self, tool_name: str, args: dict[str, Any]) -> str:
        record = await ensure_daemon(self._server_kind)
        client = MCPClient(
            config={"mcpServers": {"daemon": {"url": record.url}}},
            timeout=10.0,
            sse_read_timeout=60 * 60.0,
            check_dependencies=False,
        )
        try:
            session = await client.create_session("daemon", auto_initialize=True)
            if session is None:
                raise RuntimeError("Failed to create daemon MCP session")
            result = await session.connector.call_tool(tool_name, args)
            return _extract_text_payload(result)
        finally:
            await client.close_all_sessions()

    def _call_remote_tool_blocking(self, tool_name: str, args: dict[str, Any]) -> str:
        return asyncio.run(self._call_remote_tool_once(tool_name, args))

    async def _call_remote_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        _mark_proxy_request_start()
        try:
            for attempt in range(2):
                try:
                    return await asyncio.to_thread(
                        self._call_remote_tool_blocking,
                        tool_name,
                        args,
                    )
                except Exception as exc:
                    if attempt == 1:
                        return _json_error(exc, status="error")
            return _json_error("Unreachable proxy retry path", status="error")
        finally:
            _mark_proxy_request_end()


class _MainProxyImplementation(_RemoteProxyBase):
    def __init__(self):
        super().__init__("main")

    async def execute_task(
        self,
        task: str,
        workspace_dir: str | None = None,
        max_iterations: int | None = None,
        skill_dirs: list[str] | None = None,
        search_scope: str = "all",
    ) -> str:
        return await self._call_remote_tool(
            "execute_task",
            {
                "task": task,
                "workspace_dir": workspace_dir,
                "max_iterations": max_iterations,
                "skill_dirs": skill_dirs,
                "search_scope": search_scope,
            },
        )

    async def search_skills(
        self,
        query: str,
        source: str = "all",
        limit: int = 20,
        auto_import: bool = True,
    ) -> str:
        return await self._call_remote_tool(
            "search_skills",
            {
                "query": query,
                "source": source,
                "limit": limit,
                "auto_import": auto_import,
            },
        )

    async def fix_skill(
        self,
        skill_dir: str,
        direction: str,
    ) -> str:
        return await self._call_remote_tool(
            "fix_skill",
            {
                "skill_dir": skill_dir,
                "direction": direction,
            },
        )

    async def upload_skill(
        self,
        skill_dir: str,
        visibility: str = "public",
        origin: str | None = None,
        parent_skill_ids: list[str] | None = None,
        tags: list[str] | None = None,
        created_by: str | None = None,
        change_summary: str | None = None,
    ) -> str:
        return await self._call_remote_tool(
            "upload_skill",
            {
                "skill_dir": skill_dir,
                "visibility": visibility,
                "origin": origin,
                "parent_skill_ids": parent_skill_ids,
                "tags": tags,
                "created_by": created_by,
                "change_summary": change_summary,
            },
        )


class _EvolutionProxyImplementation(_RemoteProxyBase):
    def __init__(self):
        super().__init__("evolution")

    async def evolve_from_context(
        self,
        task: str,
        summary: str,
        workspace_dir: str | None = None,
        file_paths: list[str] | None = None,
        max_skills: int = 3,
        skill_dirs: list[str] | None = None,
        output_dir: str | None = None,
    ) -> str:
        return await self._call_remote_tool(
            "evolve_from_context",
            {
                "task": task,
                "summary": summary,
                "workspace_dir": workspace_dir,
                "file_paths": file_paths,
                "max_skills": max_skills,
                "skill_dirs": skill_dirs,
                "output_dir": output_dir,
            },
        )


def _build_fastmcp(server_kind: ServerKind) -> FastMCP:
    kwargs: dict[str, Any] = {}
    if "description" in inspect.signature(FastMCP.__init__).parameters:
        if server_kind == "main":
            kwargs["description"] = (
                "OpenSpace: Unite the Agents. Evolve the Mind. Rebuild the World."
            )
        else:
            kwargs["description"] = (
                "OpenSpace evolution sidecar: capture reusable skills from host-agent work."
            )
    name = "OpenSpace" if server_kind == "main" else "OpenSpace Evolution"
    return FastMCP(name, **kwargs)


def _run_proxy(server_kind: ServerKind) -> None:
    if _proxy_mode_for(server_kind) == "direct":
        if server_kind == "main":
            from openspace.mcp_server import run_mcp_server
        else:
            from openspace.evolution_mcp_server import run_mcp_server
        run_mcp_server()
        return

    parser = argparse.ArgumentParser(description="OpenSpace MCP proxy")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.parse_args()

    mcp = _build_fastmcp(server_kind)
    if server_kind == "main":
        register_main_tools(mcp, _MainProxyImplementation())
    else:
        register_evolution_tools(mcp, _EvolutionProxyImplementation())
    _maybe_start_proxy_idle_watchdog()
    mcp.run(transport="stdio")


def run_main_mcp_proxy() -> None:
    _run_proxy("main")


def run_evolution_mcp_proxy() -> None:
    _run_proxy("evolution")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenSpace MCP proxy")
    parser.add_argument("--kind", choices=["main", "evolution"], required=True)
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    args = parser.parse_args()

    # Rebuild argv for the generic runner so direct fallback can reuse legacy entrypoints.
    transport = args.transport
    os.environ.setdefault("OPENSPACE_MCP_PROXY_MODE", _proxy_mode_for(args.kind))
    import sys

    sys.argv = [sys.argv[0], "--transport", transport]
    _run_proxy(args.kind)


if __name__ == "__main__":
    main()
