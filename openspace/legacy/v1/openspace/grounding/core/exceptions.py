"""
Unified exception & error-code definitions for the grounding framework
"""
from enum import Enum, auto
from typing import Any, Dict


class ErrorCode(str, Enum):
    # generic
    UNKNOWN = auto()
    CONFIG_INVALID = auto()

    # provider / session / connector
    PROVIDER_ERROR = auto()
    SESSION_NOT_FOUND = auto()

    # connection
    CONNECTION_FAILED = auto()
    CONNECTION_TIMEOUT = auto()

    # tool
    TOOL_NOT_FOUND = auto()
    TOOL_EXECUTION_FAIL = auto()
    AMBIGUOUS_TOOL = auto()


class GroundingError(Exception):
    """
    Framework-wide base exception.

    Args:
        message: Human readable error message.
        code: One of the error codes defined above.
        retryable: Whether the caller may retry the operation automatically.
        context: Extra key-value pairs (e.g. tool_name, session_id) for logging / metrics.
    """

    __slots__ = ("message", "code", "retryable", "context")

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.UNKNOWN,
        retryable: bool = False,
        **context: Any,
    ):
        super().__init__(f"[{code}] {message}")
        self.message: str = message
        self.code: ErrorCode = code
        self.retryable: bool = retryable
        self.context: Dict[str, Any] = context

    def to_dict(self) -> Dict[str, Any]:
        """Serialize error for structured logging / JSON response."""
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "context": self.context,
        }

    def __str__(self) -> str:  
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str: 
        return f"GroundingError(code={self.code}, msg={self.message!r})"