"""
MCP server interaction utilities — reconnection and elicitation handling.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from openspace.protocol import CoreToTuiEvent
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.core.tui_bridge import TUIBridge
    from openspace.grounding.core.grounding_client import GroundingClient

logger = Logger.get_logger(__name__)

_ELICITATION_TIMEOUT = 300  # seconds


class MCPInteraction:
    """Manages MCP server reconnection and elicitation workflows."""

    def __init__(self, tui_bridge: TUIBridge | None = None) -> None:
        self._bridge = tui_bridge
        self.grounding_client: GroundingClient | None = None
        self._pending_elicitations: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def bind_grounding_client(
        self,
        grounding_client: GroundingClient | None,
    ) -> None:
        self.grounding_client = grounding_client

    # ── Reconnection ──────────────────────────────────────────────

    async def reconnect(self, server_name: str) -> dict[str, Any]:
        """Attempt to reconnect to an MCP server by name."""
        try:
            if self.grounding_client is None:
                raise RuntimeError("Grounding client not available")

            from openspace.grounding.core.types import BackendType

            provider = self.grounding_client.get_provider(BackendType.MCP)
            if hasattr(provider, "list_servers") and server_name not in provider.list_servers():
                raise ValueError(f"MCP server '{server_name}' is not configured")

            session_name = f"{BackendType.MCP.value}-{server_name}"
            if session_name in self.grounding_client.list_sessions():
                await self.grounding_client.close_session(session_name)

            await self.grounding_client.create_session(
                backend=BackendType.MCP,
                name=session_name,
                server=server_name,
            )

            result: dict[str, Any] = {
                "status": "connected",
                "server_name": server_name,
                "message": f"Reconnected to {server_name}",
            }
        except KeyError:
            result = {
                "status": "error",
                "server_name": server_name,
                "message": "MCP provider is not registered",
            }
            logger.warning("MCP provider unavailable for reconnect")
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "error",
                "server_name": server_name,
                "message": str(exc),
            }
            logger.error("MCP reconnect failed for %s: %s", server_name, exc)

        if self._bridge is not None:
            await self._bridge.send(CoreToTuiEvent.MCP_STATUS.value, result)

        return result

    async def emit_status_snapshot(self) -> list[dict[str, Any]]:
        """Emit the current MCP surface area to the TUI."""
        if self._bridge is None or self.grounding_client is None:
            return []

        try:
            from openspace.grounding.core.types import BackendType

            provider = self.grounding_client.get_provider(BackendType.MCP)
            server_names = (
                provider.list_servers() if hasattr(provider, "list_servers") else []
            )
            session_names = set(self.grounding_client.list_sessions())
            tools_by_server: dict[str, list[dict[str, Any]]] = {}

            if hasattr(provider, "list_tools"):
                tools = await provider.list_tools(use_cache=True)
                for tool in tools:
                    schema = getattr(tool, "schema", None)
                    runtime_info = getattr(tool, "runtime_info", None)
                    server_name = getattr(runtime_info, "server_name", None)
                    if not server_name:
                        continue
                    tools_by_server.setdefault(server_name, []).append(
                        {
                            "name": getattr(schema, "name", "unknown"),
                            "description": getattr(schema, "description", ""),
                            "server_name": server_name,
                        }
                    )

            resources: dict[str, list[str]] = {}
            session_map = getattr(provider, "_server_sessions", {})
            if isinstance(session_map, dict):
                for server_name, session in session_map.items():
                    connector = getattr(session, "connector", None)
                    try:
                        raw_resources = getattr(connector, "resources", [])
                    except RuntimeError:
                        raw_resources = []
                    rendered: list[str] = []
                    for resource in raw_resources:
                        uri = getattr(resource, "uri", None)
                        name = getattr(resource, "name", None)
                        if uri is not None:
                            rendered.append(str(uri))
                        elif name is not None:
                            rendered.append(str(name))
                    resources[server_name] = rendered

            payloads: list[dict[str, Any]] = []
            for server_name in server_names:
                session_name = f"mcp-{server_name}"
                payload = {
                    "server_name": server_name,
                    "status": (
                        "connected"
                        if session_name in session_names
                        else "disconnected"
                    ),
                    "message": f"MCP snapshot for {server_name}",
                    "tools": tools_by_server.get(server_name, []),
                    "commands": [],
                    "resources": resources,
                }
                await self._bridge.send(CoreToTuiEvent.MCP_STATUS.value, payload)
                payloads.append(payload)

            return payloads
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to emit MCP status snapshot: %s", exc)
            return []

    # ── Elicitation ───────────────────────────────────────────────

    async def handle_elicitation(self, request: dict[str, Any]) -> dict[str, Any]:
        """Forward an elicitation request to TUI and wait for the user's response.

        *request* must contain ``elicitation_id``, ``server_name``,
        ``message``, and ``schema``.
        """
        if self._bridge is None:
            return {"status": "error", "message": "No TUI bridge available"}

        elicitation_id: str = request["elicitation_id"]

        await self._bridge.send(
            CoreToTuiEvent.ELICITATION_REQUEST.value,
            {
                "elicitation_id": elicitation_id,
                "server_name": request.get("server_name", ""),
                "message": request.get("message", ""),
                "schema": request.get("schema", {}),
            },
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_elicitations[elicitation_id] = future

        try:
            return await asyncio.wait_for(future, timeout=_ELICITATION_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Elicitation %s timed out", elicitation_id)
            return {"status": "error", "message": "Elicitation timed out"}
        finally:
            self._pending_elicitations.pop(elicitation_id, None)

    def receive_elicitation_response(
        self,
        elicitation_id: str,
        response: dict[str, Any],
    ) -> None:
        """Resolve a pending elicitation future with the TUI-supplied response."""
        future = self._pending_elicitations.get(elicitation_id)
        if future is None:
            logger.warning(
                "Received response for unknown elicitation %s", elicitation_id,
            )
            return
        if not future.done():
            future.set_result(response)

    # ── Validation ────────────────────────────────────────────────

    @staticmethod
    def validate_elicitation_response(
        response: dict[str, Any],
        schema: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        """Basic JSON-Schema-style validation (no external dependency).

        Checks ``required`` fields and top-level ``properties`` types.
        Returns ``(is_valid, errors)``.
        """
        errors: list[str] = []

        required = schema.get("required", [])
        for field in required:
            if field not in response:
                errors.append(f"Missing required field: {field}")

        _TYPE_MAP: dict[str, type | tuple[type, ...]] = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }

        properties: dict[str, Any] = schema.get("properties", {})
        for key, prop_schema in properties.items():
            if key not in response:
                continue
            expected_type = prop_schema.get("type")
            if expected_type and expected_type in _TYPE_MAP:
                if not isinstance(response[key], _TYPE_MAP[expected_type]):
                    errors.append(
                        f"Field '{key}': expected {expected_type}, "
                        f"got {type(response[key]).__name__}"
                    )

        return (len(errors) == 0, errors)

    # ── Submit ────────────────────────────────────────────────────

    async def submit_elicitation(
        self,
        server_name: str,
        elicitation_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit a validated elicitation response back to the MCP server."""
        if elicitation_id not in self._pending_elicitations:
            return {
                "status": "error",
                "message": f"Unknown elicitation id: {elicitation_id}",
            }

        self.receive_elicitation_response(elicitation_id, response)
        return {
            "status": "ok",
            "server_name": server_name,
            "elicitation_id": elicitation_id,
        }
