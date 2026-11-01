from enum import Enum
from datetime import datetime
from typing import Any, Dict, Generic, List, TypeVar, Optional
import jsonschema
from pydantic import BaseModel, Field, ConfigDict

# Pydantic v2 compatibility
try:
    from pydantic import RootModel
    PYDANTIC_V2 = True
except ImportError:
    PYDANTIC_V2 = False


class BackendType(str, Enum):
    MCP = "mcp"
    SHELL = "shell"
    WEB = "web"
    GUI = "gui"
    SYSTEM = "system"
    NOT_SET = "not_set"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class SessionStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    
    
ProgressToken = str | int
RequestId = str | int

RequestParamsT = TypeVar("RequestParamsT", bound=BaseModel | Dict[str, Any] | None)
NotificationParamsT = TypeVar("NotificationParamsT", bound=BaseModel | Dict[str, Any] | None)
MethodT = TypeVar("MethodT", bound=str)


class BaseEntity(BaseModel):
    metadata: Dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class JsonRpcBase(BaseEntity):
    jsonrpc: str = "2.0"


class RpcMessage(JsonRpcBase, Generic[MethodT, RequestParamsT]):
    method: MethodT
    params: RequestParamsT


class Request(RpcMessage[MethodT, RequestParamsT]):
    id: RequestId | None = None  # id is None means Notification


class Notification(RpcMessage[MethodT, NotificationParamsT]):
    pass


class Result(JsonRpcBase):
    pass


class ErrorData(BaseEntity):
    code: int
    message: str
    data: Any | None = None


class ToolResult(Result):
    """Tool execution result"""
    status: ToolStatus
    content: Any = ""
    error: ErrorData | str | None = None
    execution_time: float | None = None

    @property
    def is_success(self) -> bool: return self.status == ToolStatus.SUCCESS
    
    @property
    def is_error(self) -> bool: return self.status == ToolStatus.ERROR


class SecurityPolicy(BaseEntity):
    allow_shell_commands: bool = True
    allow_network_access: bool = True
    allow_file_access: bool = True
    allowed_domains: List[str] = Field(default_factory=list)
    blocked_commands: List[str] = Field(default_factory=list)
    sandbox_enabled: bool = False
    
    @classmethod
    def from_dict(cls, data: Dict) -> "SecurityPolicy":
        """
        Create SecurityPolicy from configuration dict.
        
        Supports two formats for blocked_commands:
        1. List format (applies to all OS): ["cmd1", "cmd2"]
        2. Dict format (OS-specific):
           {
               "common": ["cmd1", "cmd2"],
               "linux": ["cmd3"],
               "darwin": ["cmd4"],
               "windows": ["cmd5"]
           }
        
        When using dict format, merges 'common' commands with current OS-specific commands.
        """
        import sys
        import platform
        
        processed_data = {}
        for k, v in data.items():
            if k not in cls.model_fields:
                continue
            
            # Special handling for blocked_commands
            if k == "blocked_commands":
                if isinstance(v, dict):
                    # Dict format: merge common + OS-specific
                    blocked_list = list(v.get("common", []))
                    
                    # Determine current OS
                    system = sys.platform
                    if system.startswith("linux"):
                        os_key = "linux"
                    elif system == "darwin":
                        os_key = "darwin"
                    elif system.startswith("win"):
                        os_key = "windows"
                    else:
                        os_key = None
                    
                    # Merge OS-specific commands
                    if os_key and os_key in v:
                        blocked_list.extend(v[os_key])
                    
                    processed_data[k] = blocked_list
                elif isinstance(v, list):
                    # List format: use as-is
                    processed_data[k] = v
                else:
                    # Invalid format, use empty list
                    processed_data[k] = []
            else:
                processed_data[k] = v
        
        return cls(**processed_data)

    def check(self, *, command: str | None = None, domain: str | None = None) -> bool:
        """
        return True if allowed, False if denied.
        Command check uses token-level matching to prevent simple space/escape bypasses.
        """
        import shlex

        # Shell / Python command check
        if command:
            if not self.allow_shell_commands:
                return False

            tokens = [t.lower() for t in shlex.split(command, posix=True)]
            blocked_set = {b.lower() for b in self.blocked_commands}
            if any(tok in blocked_set for tok in tokens):
                return False

        # Network access check
        if domain:
            if not self.allow_network_access:
                return False
            if self.allowed_domains and domain not in self.allowed_domains:
                return False

        return True

    def find_dangerous_tokens(self, command: str) -> List[str]:
        """
        Find and return all dangerous tokens in the command.
        Returns empty list if no dangerous tokens found.
        """
        import shlex
        
        if not command:
            return []
        
        try:
            tokens = [t.lower() for t in shlex.split(command, posix=True)]
        except ValueError:
            # If shlex.split fails, fall back to simple split
            tokens = [t.lower() for t in command.split()]
        
        blocked_set = {b.lower() for b in self.blocked_commands}
        dangerous = [tok for tok in tokens if tok in blocked_set]
        
        return dangerous


class ToolSchema(BaseEntity):
    name: str
    description: str | None = None
    parameters: Dict[str, Any] = Field(default_factory=dict)  # JSON Schema, optional
    return_schema: Dict[str, Any] = Field(default_factory=dict)
    examples: List[dict] = Field(default_factory=list)
    usage_hint: str | None = None
    latency_hint: str | None = None
    backend_type: BackendType
    security_policy: SecurityPolicy | None = None

    def validate_parameters(self, params: Dict[str, Any], *, raise_exc: bool = False) -> bool:
        """use jsonschema to validate parameters
        
        Returns True if parameters are valid or if tool has no parameters.
        """
        # If tool has no parameters defined and no parameters are provided, validation passes
        if not self.parameters and not params:
            return True
        
        # If tool has no parameters defined but parameters are provided, validation fails
        if not self.parameters and params:
            if raise_exc:
                raise ValueError(f"Tool '{self.name}' does not accept any parameters, but got: {list(params.keys())}")
            return False
        
        try:
            jsonschema.validate(params, self.parameters)
            return True
        except jsonschema.ValidationError:
            if raise_exc:
                raise
            return False

    def is_allowed(self, *, command: str | None = None, domain: str | None = None) -> bool:
        """check security policy"""
        return self.security_policy.check(command=command, domain=domain) if self.security_policy else True


class SessionConfig(BaseEntity):
    session_name: str
    backend_type: BackendType
    connection_params: Dict[str, Any] = Field(default_factory=dict)
    timeout: int = 30
    max_retries: int = 3
    auto_reconnect: bool = True
    auto_connect: bool = True
    health_check_interval: int = 5
    custom_settings: Dict[str, Any] = Field(default_factory=dict)


class SessionInfo(SessionConfig):
    status: SessionStatus
    created_at: datetime
    last_activity: datetime


class SandboxOptions(BaseEntity):
    api_key: str
    """Direct API key for sandbox provider (e.g., E2B API key).
    If not provided, will use E2B_API_KEY environment variable."""
    
    sandbox_template_id: Optional[str] = None
    """Template ID for the sandbox environment.
    Default: 'base'"""

    supergateway_command: Optional[str] = None
    """Command to run supergateway.
    Default: 'npx -y supergateway'"""


# ClientMessage: Only available in Pydantic v2
if PYDANTIC_V2:
    class ClientMessage(
        RootModel[
            Request[Any, str] | Notification[Any, str]
        ]
    ):
        """
        Unified deserialization entry: `ClientMessage.model_validate_json(raw_bytes)`
        """
else:
    # Pydantic v1 fallback: not used in current codebase
    ClientMessage = None  # type: ignore