"""
Configuration loader for MCP session.

This module provides functionality to load MCP configuration from JSON files.
"""

from typing import Any, Optional

from openspace.grounding.core.types import SandboxOptions
from openspace.config.utils import get_config_value
from .transport.connectors import (
    MCPBaseConnector,
    HttpConnector,
    SandboxConnector,
    StdioConnector,
    WebSocketConnector,
)
from .transport.connectors.utils import is_stdio_server
from .installer import MCPInstallerManager

# Import E2BSandbox
try:
    from openspace.grounding.core.security import E2BSandbox
    E2B_AVAILABLE = True
except ImportError:
    E2BSandbox = None
    E2B_AVAILABLE = False

async def create_connector_from_config(
    server_config: dict[str, Any],
    server_name: str = "unknown",
    sandbox: bool = False,
    sandbox_options: SandboxOptions | None = None,
    timeout: float = 30.0,
    sse_read_timeout: float = 300.0,
    installer: Optional[MCPInstallerManager] = None,
    check_dependencies: bool = True,
    tool_call_max_retries: int = 3,
    tool_call_retry_delay: float = 1.0,
) -> MCPBaseConnector:
    """Create a connector based on server configuration.
    
    Args:
        server_config: The server configuration section
        server_name: Name of the MCP server (for display purposes)
        sandbox: Whether to use sandboxed execution mode for running MCP servers.
        sandbox_options: Optional sandbox configuration options.
        timeout: Timeout for operations in seconds (default: 30.0)
        sse_read_timeout: SSE read timeout in seconds (default: 300.0)
        installer: Optional installer manager for dependency installation
        check_dependencies: Whether to check and install dependencies (default: True)
        tool_call_max_retries: Maximum number of retries for tool calls (default: 3)
        tool_call_retry_delay: Initial delay between retries in seconds (default: 1.0)

    Returns:
        A configured connector instance
        
    Raises:
        RuntimeError: If dependencies are not installed and user declines installation
    """
    
    # Get original command and args from config
    original_command = get_config_value(server_config, "command")
    original_args = get_config_value(server_config, "args", [])

    # Check and install dependencies if needed (only for stdio servers)
    if is_stdio_server(server_config) and check_dependencies:
        # Use provided installer or get global instance
        if installer is None:
            from .installer import get_global_installer
            installer = get_global_installer()

        # Ensure dependencies are installed (using original command/args)
        await installer.ensure_dependencies(server_name, original_command, original_args)

    # Stdio connector (command-based)
    if is_stdio_server(server_config) and not sandbox:
        return StdioConnector(
            command=get_config_value(server_config, "command"),
            args=get_config_value(server_config, "args"),
            env=get_config_value(server_config, "env", None),
        )

    # Sandboxed connector
    elif is_stdio_server(server_config) and sandbox:
        if not E2B_AVAILABLE:
            raise ImportError(
                "E2B sandbox support not available. Please install e2b-code-interpreter: "
                "'pip install e2b-code-interpreter'"
            )
        
        # Create E2B sandbox instance
        _sandbox_options = sandbox_options or {}
        e2b_sandbox = E2BSandbox(_sandbox_options)
        
        # Extract timeout values from sandbox_options or use defaults
        connector_timeout = _sandbox_options.get("timeout", timeout)
        connector_sse_timeout = _sandbox_options.get("sse_read_timeout", sse_read_timeout)
        
        # Create and return sandbox connector
        return SandboxConnector(
            sandbox=e2b_sandbox,
            command=get_config_value(server_config, "command"),
            args=get_config_value(server_config, "args"),
            env=get_config_value(server_config, "env", None),
            supergateway_command=_sandbox_options.get("supergateway_command", "npx -y supergateway"),
            port=_sandbox_options.get("port", 3000),
            timeout=connector_timeout,
            sse_read_timeout=connector_sse_timeout,
        )

    # HTTP connector
    elif "url" in server_config:
        return HttpConnector(
            base_url=get_config_value(server_config, "url"),
            headers=get_config_value(server_config, "headers", None),
            auth_token=get_config_value(server_config, "auth_token", None),
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
            tool_call_max_retries=tool_call_max_retries,
            tool_call_retry_delay=tool_call_retry_delay,
        )

    # WebSocket connector
    elif "ws_url" in server_config:
        return WebSocketConnector(
            url=get_config_value(server_config, "ws_url"),
            headers=get_config_value(server_config, "headers", None),
            auth_token=get_config_value(server_config, "auth_token", None),
        )

    raise ValueError("Cannot determine connector type from config")