"""
RemoteTool.
Wrapper around a connector that calls a remote tool.
"""
from typing import Optional
from openspace.utils.logging import Logger
from ..types import BackendType, ToolResult, ToolSchema, ToolStatus
from .base import BaseTool
from openspace.grounding.core.transport.connectors import BaseConnector

logger = Logger.get_logger(__name__)


class RemoteTool(BaseTool):
    backend_type = BackendType.NOT_SET

    def __init__(
        self,
        schema: ToolSchema | None = None,
        connector: Optional[BaseConnector] = None,
        remote_name: str = "",
        *,
        verbose: bool = False,
        backend: BackendType = BackendType.NOT_SET,
    ):
        self._conn = connector
        self._remote_name = remote_name or (schema.name if schema else "")
        self.backend_type = backend
        super().__init__(schema=schema, verbose=verbose)

    async def _arun(self, **kwargs):
        # If no connector, tool must be invoked via grounding_client (on-demand startup)
        if self._conn is None:
            raise RuntimeError(
                f"Tool '{self.name}' has no connector. "
                "Use grounding_client.invoke_tool() to execute it with on-demand server startup."
            )
        
        raw = await self._conn.invoke(self._remote_name, kwargs)
        
        if hasattr(raw, 'content') and hasattr(raw, 'isError'):
            content_parts = []
            for item in (raw.content or []):
                # Extract text from TextContent
                if hasattr(item, 'text') and item.text:
                    content_parts.append(item.text)
                # Handle ImageContent (just note its presence)
                elif hasattr(item, 'data'):
                    content_parts.append(f"[Image data: {len(item.data) if item.data else 0} bytes]")
                # Handle EmbeddedResource
                elif hasattr(item, 'resource'):
                    content_parts.append(f"[Embedded resource: {getattr(item.resource, 'uri', 'unknown')}]")
            
            content = "\n".join(content_parts) if content_parts else ""
            is_error = getattr(raw, 'isError', False)
            
            return ToolResult(
                status=ToolStatus.ERROR if is_error else ToolStatus.SUCCESS,
                content=content,
                error=content if is_error else None,
            )
        
        # Handle dict response
        if isinstance(raw, dict):
            import json
            try:
                content = json.dumps(raw, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                content = str(raw)
        # Handle list/tuple response
        elif isinstance(raw, (list, tuple)):
            import json
            try:
                content = json.dumps(raw, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                content = str(raw)
        # Handle primitive types
        elif isinstance(raw, (int, float, bool)):
            content = str(raw)
        elif isinstance(raw, str):
            content = raw
        # Fallback for unknown types
        else:
            content = str(raw)
        
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
        )