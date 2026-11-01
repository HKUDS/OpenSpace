from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

from openspace.utils.logging import Logger

from openspace.communication.types import ChannelMessage, ChannelPlatform, SendResult

logger = Logger.get_logger(__name__)

MessageHandler = Callable[[ChannelMessage], Awaitable[None]]


class BaseChannelAdapter(ABC):
    platform: ChannelPlatform

    def __init__(self, platform: ChannelPlatform):
        self.platform = platform
        self._message_handler: Optional[MessageHandler] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    async def dispatch_message(self, message: ChannelMessage) -> None:
        if self._message_handler is None:
            logger.warning("Dropping %s message because no handler is attached", self.platform.value)
            return
        await self._message_handler(message)

    def register_http_routes(self, app: Any) -> None:
        """Optional hook for adapters that need inbound HTTP routes."""

    def validate_configuration(self) -> None:
        """Optional hook for adapter-specific startup validation."""

    def get_lock_identity(self) -> Optional[tuple[str, str]]:
        """Return an optional (scope, identity) tuple for gateway-scoped locking."""
        return None

    @abstractmethod
    async def connect(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_text(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        raise NotImplementedError

    async def send_typing(
        self,
        chat_id: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult | None:
        """Optional out-of-band typing/ack capability."""
        return None

    async def send_partial_text(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult | None:
        """Optional partial response capability; defaults to a normal send."""
        return await self.send_text(
            chat_id,
            content,
            reply_to_message_id=reply_to_message_id,
            metadata=metadata,
        )

    async def update_message(
        self,
        message_id: str,
        content: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult | None:
        """Optional in-place message update capability."""
        return None
