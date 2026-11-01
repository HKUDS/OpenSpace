from typing import TYPE_CHECKING

from ..tool.local_tool import LocalTool
from ..types import BackendType, ToolResult, ToolStatus

if TYPE_CHECKING:
    from ..grounding_client import GroundingClient


class _BaseMetaTool(LocalTool):
    """Base for internal meta introspection tools."""

    backend_type = BackendType.META

    _is_read_only = True
    _is_concurrency_safe = True
    should_defer = True

    def __init__(self, client: "GroundingClient"):
        super().__init__(verbose=False, handle_errors=True)
        self._client = client

    @property
    def client(self) -> "GroundingClient":
        return self._client


class ListProvidersTool(_BaseMetaTool):
    _name = "list_providers"
    _description = "List all registered backend providers"

    async def _arun(self) -> ToolResult:
        prov = list(self.client.list_providers().keys())
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=", ".join(prov),
        )


class ListBackendToolsTool(_BaseMetaTool):
    _name = "list_backend_tools"
    _description = "List static tools for a backend"

    async def _arun(self, backend: str) -> ToolResult:
        try:
            be = BackendType(backend.lower())
        except ValueError:
            return ToolResult(ToolStatus.ERROR, error=f"Unknown backend '{backend}'")

        tools = await self.client.list_backend_tools(be)
        names = [t.schema.name for t in tools]
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=", ".join(names),
        )


class ListSessionToolsTool(_BaseMetaTool):
    _name = "list_session_tools"
    _description = "List tools (incl. dynamic) for a session"

    async def _arun(self, session_id: str) -> ToolResult:
        tools = await self.client.list_session_tools(session_id)
        names = [t.schema.name for t in tools]
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=", ".join(names),
        )


class ListAllBackendToolsTool(_BaseMetaTool):
    _name = "list_all_backend_tools"
    _description = "List static tools for every registered backend"

    async def _arun(self, use_cache: bool = False) -> ToolResult:
        all_tools = await self.client.list_all_backend_tools(use_cache=use_cache)
        lines = [
            f"{backend.value}: {', '.join(t.schema.name for t in tools)}"
            for backend, tools in all_tools.items()
        ]
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content="\n".join(lines),
        )


META_TOOLS: list[type[_BaseMetaTool]] = [
    ListProvidersTool,
    ListBackendToolsTool,
    ListSessionToolsTool,
    ListAllBackendToolsTool,
]
