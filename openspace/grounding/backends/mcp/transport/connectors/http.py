"""
HTTP connector for MCP implementations.

This module provides a connector for communicating with MCP implementations
through HTTP APIs with SSE, Streamable HTTP, or simple JSON-RPC for transport.
"""

import asyncio
import anyio
import httpx
from typing import Any, Dict, List
from mcp import ClientSession
from mcp.types import (
    CallToolResult,
    TextContent,
    ImageContent,
    EmbeddedResource,
    Tool,
    Resource,
    Prompt,
    GetPromptResult,
    ReadResourceResult,
)

from openspace.utils.logging import Logger
from openspace.grounding.core.transport.task_managers.base import BaseConnectionManager
from openspace.grounding.backends.mcp.transport.task_managers import SseConnectionManager, StreamableHttpConnectionManager
from openspace.grounding.backends.mcp.transport.connectors.base import MCPBaseConnector, DEFAULT_TOOL_CALL_MAX_RETRIES, DEFAULT_TOOL_CALL_RETRY_DELAY

logger = Logger.get_logger(__name__)


def _build_sse_candidate_urls(base_url: str) -> list[str]:
    """Try the common FastMCP `/sse` endpoint before the raw base URL."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/sse"):
        return [normalized]
    return [f"{normalized}/sse", normalized]


class HttpConnector(MCPBaseConnector):
    """Connector for MCP implementations using HTTP transport.

    This connector uses HTTP/SSE or streamable HTTP to communicate with remote MCP implementations,
    using a connection manager to handle the proper lifecycle management.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5,
        sse_read_timeout: float = 60 * 5,
        tool_call_max_retries: int = DEFAULT_TOOL_CALL_MAX_RETRIES,
        tool_call_retry_delay: float = DEFAULT_TOOL_CALL_RETRY_DELAY,
    ):
        """Initialize a new HTTP connector.

        Args:
            base_url: The base URL of the MCP HTTP API.
            auth_token: Optional authentication token.
            headers: Optional additional headers.
            timeout: Timeout for HTTP operations in seconds.
            sse_read_timeout: Timeout for SSE read operations in seconds.
            tool_call_max_retries: Maximum number of retries for tool calls (default: 3)
            tool_call_retry_delay: Initial delay between retries in seconds (default: 1.0)
        """
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.headers = headers or {}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        
        # JSON-RPC HTTP mode fields
        self._use_jsonrpc = False
        self._jsonrpc_client: httpx.AsyncClient | None = None
        self._jsonrpc_request_id = 0
        
        # Create a placeholder connection manager (will be set up later in connect())
        # We use a placeholder here because the actual transport type (SSE vs Streamable HTTP)
        # can only be determined at runtime through server negotiation as per MCP specification
        from openspace.grounding.core.transport.task_managers import PlaceholderConnectionManager
        connection_manager = PlaceholderConnectionManager()
        super().__init__(
            connection_manager, 
            tool_call_max_retries=tool_call_max_retries,
            tool_call_retry_delay=tool_call_retry_delay,
        )

    async def connect(self) -> None:
        """Create the underlying session/connection.
        
        For JSON-RPC mode, we don't use a connection manager.
        """
        if self._connected:
            return
        
        try:
            # Hook: before connection - this sets up transport type
            await self._before_connect()
            
            if self._use_jsonrpc:
                # JSON-RPC mode doesn't use connection manager
                # Just call _after_connect to set up the HTTP client
                await self._after_connect()
                self._connected = True
            else:
                # Use normal connection flow with connection manager
                # If _before_connect() already established a connection, reuse it
                if self._connection is None:
                    self._connection = await self._connection_manager.start()
                await self._after_connect()
                self._connected = True
        except Exception:
            await self._cleanup_on_connect_failure()
            raise

    async def disconnect(self) -> None:
        """Close the session/connection and reset state."""
        if not self._connected:
            return
        
        # Hook: before disconnection
        await self._before_disconnect()
        
        if not self._use_jsonrpc:
            # Stop the connection manager only for non-JSON-RPC modes
            if self._connection_manager:
                await self._connection_manager.stop()
                self._connection = None
        
        # Hook: after disconnection
        await self._after_disconnect()
        
        self._connected = False

    async def _before_connect(self) -> None:
        """Negotiate transport type and set up the appropriate connection manager.
        
        Tries transports in order:
        1. Streamable HTTP (new MCP transport)
        2. SSE (legacy MCP transport)
        3. Simple JSON-RPC HTTP (for custom servers)
        
        This implements backwards compatibility per MCP specification.
        """
        self.transport_type = None
        self._use_jsonrpc = False
        connection_manager = None
        streamable_error = None
        sse_error = None

        # First, try the new streamable HTTP transport
        try:
            logger.debug(f"Attempting streamable HTTP connection to: {self.base_url}")
            connection_manager = StreamableHttpConnectionManager(
                self.base_url, self.headers, self.timeout, self.sse_read_timeout
            )

            # Test the connection by starting it with built-in timeout
            read_stream, write_stream = await connection_manager.start(timeout=self.timeout)

            # Create and verify ClientSession
            test_client = ClientSession(read_stream, write_stream, sampling_callback=None)
            
            # Add timeout to __aenter__ - use asyncio.wait_for instead of anyio.fail_after
            # to avoid cancel scope conflicts with background tasks
            try:
                await asyncio.wait_for(test_client.__aenter__(), timeout=self.timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"ClientSession enter timed out after {self.timeout}s")

            try:
                # Add timeout to initialize() using asyncio.wait_for to prevent hanging
                try:
                    await asyncio.wait_for(test_client.initialize(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"initialize() timed out after {self.timeout}s")
                    
                try:
                    await asyncio.wait_for(test_client.list_tools(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"list_tools() timed out after {self.timeout}s")
                
                # SUCCESS! Keep the client session (don't close it, closing destroys the streams)
                # Store it directly as the client_session for later use
                self.transport_type = "streamable HTTP"
                self._connection_manager = connection_manager
                self._connection = connection_manager.get_streams()
                self.client_session = test_client  # Reuse the working session
                logger.debug("Streamable HTTP transport selected")
                return
            except TimeoutError:
                try:
                    await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                except (asyncio.TimeoutError, Exception):
                    pass
                raise
            except Exception as init_error:
                # Clean up the test client only on error
                try:
                    await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                except (asyncio.TimeoutError, Exception):
                    pass
                raise init_error

        except Exception as e:
            streamable_error = e
            logger.debug(f"Streamable HTTP failed: {e}")

            # Clean up the failed connection manager
            if connection_manager:
                try:
                    await asyncio.wait_for(connection_manager.stop(), timeout=2)
                except (asyncio.TimeoutError, Exception):
                    pass

        # Try SSE fallback. FastMCP commonly exposes legacy SSE on `/sse`,
        # but some callers may already pass the full endpoint.
        for sse_url in _build_sse_candidate_urls(self.base_url):
            connection_manager = None
            try:
                logger.debug(f"Attempting SSE fallback connection to: {sse_url}")
                connection_manager = SseConnectionManager(
                    sse_url, self.headers, self.timeout, self.sse_read_timeout
                )

                # Test the connection by starting it with built-in timeout
                read_stream, write_stream = await connection_manager.start(timeout=self.timeout)

                # Create and verify ClientSession
                test_client = ClientSession(read_stream, write_stream, sampling_callback=None)

                # Add timeout to __aenter__ - use asyncio.wait_for instead of anyio.fail_after
                # to avoid cancel scope conflicts with background tasks
                try:
                    await asyncio.wait_for(test_client.__aenter__(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"ClientSession enter timed out after {self.timeout}s")

                try:
                    try:
                        await asyncio.wait_for(test_client.initialize(), timeout=self.timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"initialize() timed out after {self.timeout}s")

                    try:
                        await asyncio.wait_for(test_client.list_tools(), timeout=self.timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"list_tools() timed out after {self.timeout}s")

                    # SUCCESS! Keep the client session (don't close it, closing destroys the streams)
                    # Store it directly as the client_session for later use
                    self.transport_type = "SSE"
                    self._connection_manager = connection_manager
                    self._connection = connection_manager.get_streams()
                    self.client_session = test_client  # Reuse the working session
                    logger.debug("SSE transport selected")
                    return
                except TimeoutError:
                    try:
                        await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    raise
                except Exception as init_error:
                    # Clean up the test client only on error
                    try:
                        await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    raise init_error

            except Exception as e:
                sse_error = e
                logger.debug(f"SSE failed for {sse_url}: {e}")

                # Clean up the failed connection manager
                if connection_manager:
                    try:
                        await asyncio.wait_for(connection_manager.stop(), timeout=2)
                    except (asyncio.TimeoutError, Exception):
                        pass

        # Both MCP transports failed, try simple JSON-RPC HTTP as last resort
        # This is useful for custom MCP servers that don't implement proper MCP transports
        logger.debug(f"Attempting JSON-RPC HTTP fallback to: {self.base_url}")
        try:
            # Test JSON-RPC connection
            await self._try_jsonrpc_connection()
            
            self.transport_type = "JSON-RPC HTTP"
            self._use_jsonrpc = True
            logger.info(f"JSON-RPC HTTP transport selected for: {self.base_url}")
            return
            
        except Exception as jsonrpc_error:
            # All transports failed
            logger.error(
                f"All transport methods failed for {self.base_url}. "
                f"Streamable HTTP: {streamable_error}, SSE: {sse_error}, JSON-RPC: {jsonrpc_error}"
            )
            # Raise the most relevant error - prefer the original streamable error
            raise streamable_error or sse_error or jsonrpc_error

    async def _try_jsonrpc_connection(self) -> None:
        """Test JSON-RPC HTTP connection by sending an initialize request."""
        headers = {**self.headers, "Content-Type": "application/json"}
        
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout), headers=headers) as client:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "OpenSpace", "version": "1.0.0"},
                }
            }
            
            response = await client.post(self.base_url, json=payload)
            response.raise_for_status()
            
            data = response.json()
            
            # Check for JSON-RPC error
            if "error" in data:
                error = data["error"]
                raise RuntimeError(f"JSON-RPC error: {error.get('message', str(error))}")
            
            # Success - server supports JSON-RPC
            logger.debug(f"JSON-RPC test succeeded: {data.get('result', {})}")

    async def _after_connect(self) -> None:
        """Create ClientSession (or set up JSON-RPC client) and log success."""
        if self._use_jsonrpc:
            # Set up JSON-RPC HTTP client
            headers = {**self.headers, "Content-Type": "application/json"}
            self._jsonrpc_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers=headers,
            )
            logger.debug(f"JSON-RPC HTTP client set up for: {self.base_url}")
        else:
            # Skip creating ClientSession if _before_connect() already created one
            if self.client_session is None:
                await super()._after_connect()
            else:
                logger.debug("Reusing ClientSession from _before_connect()")
        
        logger.debug(f"Successfully connected to MCP implementation via {self.transport_type}: {self.base_url}")

    async def _before_disconnect(self) -> None:
        """Clean up resources before disconnection."""
        # Clean up JSON-RPC client if used
        if self._jsonrpc_client:
            try:
                await self._jsonrpc_client.aclose()
            except Exception as e:
                logger.warning(f"Error closing JSON-RPC client: {e}")
            finally:
                self._jsonrpc_client = None
        
        # Call parent cleanup for MCP resources
        await super()._before_disconnect()

    @property
    def public_identifier(self) -> str:
        """Get the identifier for the connector."""
        return {"type": self.transport_type, "base_url": self.base_url}

    # =====================
    # JSON-RPC HTTP Methods
    # =====================

    def _next_jsonrpc_id(self) -> int:
        """Get next JSON-RPC request ID."""
        self._jsonrpc_request_id += 1
        return self._jsonrpc_request_id

    async def _send_jsonrpc_request(
        self, 
        method: str, 
        params: Dict[str, Any] = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> Any:
        """Send a JSON-RPC request and return the result.
        
        Args:
            method: The JSON-RPC method name (e.g., "tools/list", "tools/call")
            params: The method parameters
            max_retries: Maximum number of retries for transient errors (400, 503, etc.)
            retry_delay: Initial delay between retries (doubles each retry)
            
        Returns:
            The result field from the JSON-RPC response
        """
        if not self._jsonrpc_client:
            raise RuntimeError("JSON-RPC client not initialized")

        last_error = None
        
        for attempt in range(max_retries):
            request_id = self._next_jsonrpc_id()
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }

            logger.debug(f"Sending JSON-RPC request: {method} (id={request_id}, attempt {attempt + 1}/{max_retries})")
            
            try:
                response = await self._jsonrpc_client.post(self.base_url, json=payload)
                response.raise_for_status()
                
                data = response.json()
                
                if "error" in data:
                    error = data["error"]
                    error_msg = error.get("message", str(error))
                    raise RuntimeError(f"JSON-RPC error: {error_msg}")
                
                return data.get("result", {})
                
            except httpx.HTTPStatusError as e:
                last_error = e
                status_code = e.response.status_code
                
                # Retry on 400 (Bad Request) and 5xx errors
                # 400 can happen when MCP server is temporarily not ready
                if status_code in (400, 500, 502, 503, 504) and attempt < max_retries - 1:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(
                        f"HTTP {status_code} error on {method}, retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                    continue
                    
                raise RuntimeError(f"HTTP error: {status_code}") from e
                
            except httpx.RequestError as e:
                last_error = e
                # Retry on connection errors
                if attempt < max_retries - 1:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(
                        f"Request error on {method}: {e}, retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                    continue
                    
                raise RuntimeError(f"Request error: {e}") from e
        
        # Should not reach here, but just in case
        raise RuntimeError(f"Max retries exceeded for {method}") from last_error

    def _parse_tools_from_json(self, tools_data: List[Dict]) -> List[Tool]:
        """Parse tool data into Tool objects."""
        tools = []
        for tool_dict in tools_data:
            try:
                tool = Tool(
                    name=tool_dict.get("name", ""),
                    description=tool_dict.get("description", ""),
                    inputSchema=tool_dict.get("inputSchema", {}),
                )
                tools.append(tool)
            except Exception as e:
                logger.warning(f"Failed to parse tool: {e}")
        return tools

    def _parse_resources_from_json(self, resources_data: List[Dict]) -> List[Resource]:
        """Parse resource data into Resource objects."""
        resources = []
        for res_dict in resources_data:
            try:
                resource = Resource(
                    uri=res_dict.get("uri", ""),
                    name=res_dict.get("name", ""),
                    description=res_dict.get("description"),
                    mimeType=res_dict.get("mimeType"),
                )
                resources.append(resource)
            except Exception as e:
                logger.warning(f"Failed to parse resource: {e}")
        return resources

    def _parse_prompts_from_json(self, prompts_data: List[Dict]) -> List[Prompt]:
        """Parse prompt data into Prompt objects."""
        prompts = []
        for prompt_dict in prompts_data:
            try:
                prompt = Prompt(
                    name=prompt_dict.get("name", ""),
                    description=prompt_dict.get("description"),
                    arguments=prompt_dict.get("arguments"),
                )
                prompts.append(prompt)
            except Exception as e:
                logger.warning(f"Failed to parse prompt: {e}")
        return prompts

    # =====================
    # Override MCP Methods for JSON-RPC Support
    # =====================

    async def initialize(self) -> Dict[str, Any]:
        """Initialize the MCP session."""
        if not self._use_jsonrpc:
            return await super().initialize()
        
        # JSON-RPC mode
        logger.debug("Initializing JSON-RPC HTTP MCP session")
        
        result = await self._send_jsonrpc_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "OpenSpace", "version": "1.0.0"},
        })
        
        capabilities = result.get("capabilities", {})
        
        # List tools
        if capabilities.get("tools"):
            try:
                tools_result = await self._send_jsonrpc_request("tools/list", {})
                self._tools = self._parse_tools_from_json(tools_result.get("tools", []))
            except Exception:
                self._tools = []
        else:
            # Try anyway - some servers don't advertise capabilities correctly
            try:
                tools_result = await self._send_jsonrpc_request("tools/list", {})
                self._tools = self._parse_tools_from_json(tools_result.get("tools", []))
            except Exception:
                self._tools = []
        
        # List resources
        if capabilities.get("resources"):
            try:
                resources_result = await self._send_jsonrpc_request("resources/list", {})
                self._resources = self._parse_resources_from_json(resources_result.get("resources", []))
            except Exception:
                self._resources = []
        else:
            self._resources = []
        
        # List prompts
        if capabilities.get("prompts"):
            try:
                prompts_result = await self._send_jsonrpc_request("prompts/list", {})
                self._prompts = self._parse_prompts_from_json(prompts_result.get("prompts", []))
            except Exception:
                self._prompts = []
        else:
            self._prompts = []
        
        logger.info(
            f"JSON-RPC HTTP MCP session initialized with {len(self._tools)} tools, "
            f"{len(self._resources)} resources, {len(self._prompts)} prompts"
        )
        
        return result

    @property
    def is_connected(self) -> bool:
        """Check if the connector is connected."""
        if self._use_jsonrpc:
            return self._connected and self._jsonrpc_client is not None
        return super().is_connected

    async def _ensure_connected(self) -> None:
        """Ensure the connector is connected."""
        if self._use_jsonrpc:
            if not self._connected or not self._jsonrpc_client:
                raise RuntimeError("JSON-RPC HTTP connector is not connected")
        else:
            await super()._ensure_connected()

    async def list_tools(self) -> List[Tool]:
        """List all available tools."""
        if not self._use_jsonrpc:
            return await super().list_tools()
        
        await self._ensure_connected()
        try:
            tools_result = await self._send_jsonrpc_request("tools/list", {})
            self._tools = self._parse_tools_from_json(tools_result.get("tools", []))
            return self._tools
        except Exception as e:
            logger.error(f"Error listing tools: {e}")
            return []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> CallToolResult:
        """Call an MCP tool."""
        if not self._use_jsonrpc:
            return await super().call_tool(name, arguments)
        
        await self._ensure_connected()
        logger.debug(f"Calling tool '{name}' with arguments: {arguments}")
        
        result = await self._send_jsonrpc_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        
        # Parse the result into CallToolResult
        content = []
        for item in result.get("content", []):
            item_type = item.get("type", "text")
            if item_type == "text":
                content.append(TextContent(type="text", text=item.get("text", "")))
            elif item_type == "image":
                content.append(ImageContent(
                    type="image",
                    data=item.get("data", ""),
                    mimeType=item.get("mimeType", "image/png"),
                ))
            elif item_type == "resource":
                content.append(EmbeddedResource(
                    type="resource",
                    resource=item.get("resource", {}),
                ))
        
        if not content and result:
            content.append(TextContent(type="text", text=str(result)))
        
        return CallToolResult(
            content=content,
            isError=result.get("isError", False),
        )

    async def list_resources(self) -> List[Resource]:
        """List all available resources."""
        if not self._use_jsonrpc:
            return await super().list_resources()
        
        await self._ensure_connected()
        try:
            resources_result = await self._send_jsonrpc_request("resources/list", {})
            self._resources = self._parse_resources_from_json(resources_result.get("resources", []))
            return self._resources
        except Exception as e:
            logger.error(f"Error listing resources: {e}")
            return []

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """Read a resource by URI."""
        if not self._use_jsonrpc:
            return await super().read_resource(uri)
        
        await self._ensure_connected()
        result = await self._send_jsonrpc_request("resources/read", {"uri": uri})
        return ReadResourceResult(**result)

    async def list_prompts(self) -> List[Prompt]:
        """List all available prompts."""
        if not self._use_jsonrpc:
            return await super().list_prompts()
        
        await self._ensure_connected()
        try:
            prompts_result = await self._send_jsonrpc_request("prompts/list", {})
            self._prompts = self._parse_prompts_from_json(prompts_result.get("prompts", []))
            return self._prompts
        except Exception as e:
            logger.error(f"Error listing prompts: {e}")
            return []

    async def get_prompt(self, name: str, arguments: Dict[str, Any] | None = None) -> GetPromptResult:
        """Get a prompt by name."""
        if not self._use_jsonrpc:
            return await super().get_prompt(name, arguments)
        
        await self._ensure_connected()
        result = await self._send_jsonrpc_request("prompts/get", {
            "name": name,
            "arguments": arguments or {},
        })
        return GetPromptResult(**result)

    async def request(self, method: str, params: Dict[str, Any] | None = None) -> Any:
        """Send a raw request to the MCP implementation."""
        if not self._use_jsonrpc:
            return await super().request(method, params)
        
        await self._ensure_connected()
        return await self._send_jsonrpc_request(method, params or {})

    async def invoke(self, name: str, params: Dict[str, Any]) -> Any:
        """Invoke a tool or special method."""
        if not self._use_jsonrpc:
            return await super().invoke(name, params)
        
        await self._ensure_connected()

        if not name.startswith("__"):
            return await self.call_tool(name, params)

        if name == "__read_resource__":
            return await self.read_resource(params["uri"])
        if name == "__list_prompts__":
            return await self.list_prompts()
        if name == "__get_prompt__":
            return await self.get_prompt(params["name"], params.get("args"))

        raise ValueError(f"Unsupported MCP invoke name: {name}")
