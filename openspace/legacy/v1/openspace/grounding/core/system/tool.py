from ..tool.local_tool import LocalTool
from ..types import BackendType, ToolResult, ToolStatus
from ..grounding_client import GroundingClient


class _BaseSystemTool(LocalTool):
    backend_type = BackendType.SYSTEM

    def __init__(self, client: GroundingClient):
        super().__init__(verbose=False, handle_errors=True)
        self._client = client

    @property
    def client(self) -> GroundingClient:
        return self._client


class ListProvidersTool(_BaseSystemTool):
    _name = "list_providers"
    _description = "List all registered backend providers"

    async def _arun(self) -> ToolResult:
        prov = list(self.client.list_providers().keys())
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=", ".join(prov),
        )


class ListBackendToolsTool(_BaseSystemTool):
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


class ListSessionToolsTool(_BaseSystemTool):
    _name = "list_session_tools"
    _description = "List tools (incl. dynamic) for a session"

    async def _arun(self, session_id: str) -> ToolResult:
        tools = await self.client.list_session_tools(session_id)
        names = [t.schema.name for t in tools]
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=", ".join(names),
        )


class ListAllBackendToolsTool(_BaseSystemTool):
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


SYSTEM_TOOLS: list[type[_BaseSystemTool]] = [
    ListProvidersTool,
    ListBackendToolsTool,
    ListSessionToolsTool,
    ListAllBackendToolsTool,
]