from __future__ import annotations

import argparse
import inspect
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from openspace.grounding.backends.mcp.client import MCPClient
from openspace.mcp_tool_registration import (
    register_evolution_tools,
    register_main_tools,
)
from openspace.shared_mcp_runtime import ServerKind, ensure_daemon


def _proxy_mode_for(server_kind: ServerKind) -> str:
    raw = os.environ.get("OPENSPACE_MCP_PROXY_MODE", "").strip().lower()
    if raw in {"daemon", "direct"}:
        return raw
    return "daemon"


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
        self._client: MCPClient | None = None
        self._current_url: str | None = None

    async def _get_client(self) -> MCPClient:
        record = await ensure_daemon(self._server_kind)
        if self._client is not None and self._current_url == record.url:
            return self._client

        await self._reset_client()

        self._client = MCPClient(
            config={"mcpServers": {"daemon": {"url": record.url}}},
            timeout=10.0,
            sse_read_timeout=60 * 60.0,
            check_dependencies=False,
        )
        self._current_url = record.url
        return self._client

    async def _reset_client(self) -> None:
        if self._client is not None:
            await self._client.close_all_sessions()
        self._client = None
        self._current_url = None

    async def _call_remote_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        for attempt in range(2):
            try:
                client = await self._get_client()
                session = await client.create_session("daemon", auto_initialize=True)
                if session is None:
                    raise RuntimeError("Failed to create daemon MCP session")
                result = await session.connector.call_tool(tool_name, args)
                return _extract_text_payload(result)
            except Exception as exc:
                if attempt == 0:
                    await self._reset_client()
                    continue
                return _json_error(exc, status="error")
        return _json_error("Unreachable proxy retry path", status="error")


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
