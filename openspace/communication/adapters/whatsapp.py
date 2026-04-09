from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp

from openspace.communication.adapters.base import BaseChannelAdapter
from openspace.communication.attachment_cache import AttachmentCache
from openspace.communication.config import WhatsAppConfig
from openspace.communication.types import (
    AttachmentKind,
    ChannelMessage,
    ChannelPlatform,
    ChannelSource,
    SendResult,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class WhatsAppAdapter(BaseChannelAdapter):
    def __init__(
        self,
        config: WhatsAppConfig,
        attachment_cache: AttachmentCache,
        *,
        runtime_dir: Optional[Path] = None,
        poll_interval_seconds: float = 1.0,
    ):
        super().__init__(ChannelPlatform.WHATSAPP)
        self.config = config
        self.attachment_cache = attachment_cache
        self.runtime_dir = (
            Path(runtime_dir).expanduser().resolve()
            if runtime_dir is not None
            else attachment_cache.base_dir.parent.resolve()
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._receiver_task: Optional[asyncio.Task] = None
        self._bridge_process: Optional[subprocess.Popen] = None
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._auth_event = asyncio.Event()
        self._status_event = asyncio.Event()
        self._bridge_state = "disconnected"

    def validate_configuration(self) -> None:
        if self.config.bridge.enforce_loopback and self.config.bridge.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("WhatsApp bridge host must stay on loopback")

    def get_lock_identity(self) -> Optional[tuple[str, str]]:
        return ("whatsapp-session", str(self._session_dir().resolve()))

    async def connect(self) -> bool:
        self.validate_configuration()
        if self._http_session is None:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
            )

        for attempt in range(2):
            try:
                await self._open_control_socket()
            except Exception as exc:
                logger.info("WhatsApp bridge connection attempt %s failed: %s", attempt + 1, exc)
                if attempt == 0:
                    await self._start_bridge_process()
                    await asyncio.sleep(1)
                    continue
                return False
            break

        for _ in range(20):
            if self._bridge_state == "connected":
                self._connected = True
                return True
            await asyncio.sleep(1)
        logger.error("WhatsApp bridge control channel opened but WhatsApp session did not connect")
        return False

    async def disconnect(self) -> None:
        self._connected = False
        self._bridge_state = "disconnected"
        self._status_event.clear()
        self._auth_event.clear()

        if self._receiver_task is not None:
            self._receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receiver_task
            self._receiver_task = None

        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RuntimeError("WhatsApp bridge disconnected"))
        self._pending_requests.clear()

        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

        if self._bridge_process is not None and self._bridge_process.poll() is None:
            self._bridge_process.terminate()
            try:
                self._bridge_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._bridge_process.kill()
        self._bridge_process = None

    async def send_text(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if self._ws is None:
            return SendResult(success=False, error="WhatsApp bridge not initialized")

        last_message_id: Optional[str] = None
        for chunk in _split_text(content, 60000):
            try:
                payload = await self._send_command(
                    {
                        "type": "send",
                        "to": chat_id,
                        "text": chunk,
                        "replyToMessageId": reply_to_message_id,
                    }
                )
            except Exception as exc:
                return SendResult(success=False, error=str(exc))
            last_message_id = _optional_str(payload.get("messageId")) or last_message_id
        return SendResult(success=True, message_id=last_message_id)

    async def send_media(
        self,
        chat_id: str,
        *,
        file_path: str,
        mimetype: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> SendResult:
        if self._ws is None:
            return SendResult(success=False, error="WhatsApp bridge not initialized")
        try:
            payload = await self._send_command(
                {
                    "type": "send_media",
                    "to": chat_id,
                    "filePath": file_path,
                    "mimetype": mimetype,
                    "caption": caption,
                    "fileName": file_name,
                    "replyToMessageId": reply_to_message_id,
                }
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=_optional_str(payload.get("messageId")))

    async def _open_control_socket(self) -> None:
        if self._http_session is None:
            raise RuntimeError("WhatsApp bridge HTTP session is not initialized")
        if self._receiver_task is not None:
            self._receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receiver_task
            self._receiver_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._auth_event.clear()
        self._status_event.clear()
        ws = await self._http_session.ws_connect(
            self.config.bridge.ws_url,
            heartbeat=20,
            autoping=True,
            max_msg_size=4 * 1024 * 1024,
        )
        self._ws = ws
        self._receiver_task = asyncio.create_task(self._receive_loop(ws))
        await self._send_ws_json({"type": "auth", "token": self._effective_bridge_token()})
        await asyncio.wait_for(self._auth_event.wait(), timeout=5)

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        break
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    logger.warning("Ignoring invalid WhatsApp bridge JSON: %r", msg.data[:200])
                    continue
                await self._handle_ws_payload(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("WhatsApp bridge receive loop stopped: %s", exc)
        finally:
            if self._ws is ws:
                self._ws = None
            self._connected = False
            self._bridge_state = "disconnected"
            self._status_event.clear()
            for request_id, future in list(self._pending_requests.items()):
                if not future.done():
                    future.set_exception(RuntimeError("WhatsApp bridge disconnected"))
                self._pending_requests.pop(request_id, None)

    async def _handle_ws_payload(self, payload: dict[str, Any]) -> None:
        message_type = str(payload.get("type", "")).strip().lower()
        if message_type == "auth_ok":
            self._auth_event.set()
            return
        if message_type == "status":
            self._bridge_state = str(payload.get("status", "")).strip().lower() or "disconnected"
            self._connected = self._bridge_state == "connected"
            self._status_event.set()
            return
        if message_type == "qr":
            logger.info("WhatsApp bridge is waiting for QR scan")
            return
        if message_type == "ack":
            request_id = _optional_str(payload.get("requestId"))
            if request_id and request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)
                if not future.done():
                    future.set_result(payload)
            return
        if message_type == "error":
            request_id = _optional_str(payload.get("requestId"))
            error = _optional_str(payload.get("error")) or "Unknown bridge error"
            if request_id and request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)
                if not future.done():
                    future.set_exception(RuntimeError(error))
            else:
                logger.warning("WhatsApp bridge error: %s", error)
            return
        if message_type == "message":
            message = await self._normalize_event(payload)
            if message is not None:
                await self.dispatch_message(message)

    async def _send_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("WhatsApp bridge control channel is not connected")
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        try:
            await self._send_ws_json({**payload, "requestId": request_id})
            return await asyncio.wait_for(future, timeout=20)
        finally:
            self._pending_requests.pop(request_id, None)

    async def _send_ws_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("WhatsApp bridge control channel is not connected")
        await self._ws.send_str(json.dumps(payload, ensure_ascii=False))

    async def _normalize_event(self, event: dict[str, Any]) -> Optional[ChannelMessage]:
        chat_id = str(event.get("chatId", "")).strip()
        message_id = str(event.get("messageId", "")).strip()
        sender_id = str(event.get("senderId", "")).strip()
        if not chat_id or not message_id:
            return None
        normalized_sender_id = _normalize_whatsapp_identifier(sender_id)

        source = ChannelSource(
            platform=ChannelPlatform.WHATSAPP,
            chat_id=chat_id,
            chat_type="group" if event.get("isGroup") else "dm",
            user_id=normalized_sender_id or sender_id or None,
            user_name=_optional_str(event.get("senderName")),
            chat_name=_optional_str(event.get("chatName")),
        )

        session_key = _build_session_key_hint(source)
        attachments = []
        media_type = str(event.get("mediaType", "")).strip().lower()
        attachment_kind = AttachmentKind.IMAGE if media_type == "image" else AttachmentKind.DOCUMENT
        for media_path in event.get("mediaUrls") or []:
            attachment = self.attachment_cache.copy_local_file(
                session_key=session_key,
                source_path=str(media_path),
                kind=attachment_kind,
            )
            if attachment is not None:
                attachments.append(attachment)

        body = str(event.get("body", "") or "").strip()
        return ChannelMessage(
            source=source,
            text=body,
            message_id=message_id,
            attachments=attachments,
            reply_to_message_id=_optional_str(event.get("replyToMessageId")),
            mentions_bot=bool(event.get("mentionsBot")),
            metadata={
                "bridge_event": event,
                "raw_user_id": sender_id or None,
                "auth_candidates": [
                    candidate
                    for candidate in (
                        sender_id or None,
                        normalized_sender_id or None,
                        f"+{normalized_sender_id}" if normalized_sender_id else None,
                    )
                    if candidate
                ],
            },
        )

    async def _start_bridge_process(self) -> None:
        if self._bridge_process is not None and self._bridge_process.poll() is None:
            return

        bridge_script = self._resolve_bridge_script()
        bridge_dir = bridge_script.parent
        session_dir = self._session_dir()
        session_dir.mkdir(parents=True, exist_ok=True)
        self._outbound_media_root().mkdir(parents=True, exist_ok=True)

        if self.config.bridge.auto_install_dependencies and not (bridge_dir / "node_modules").exists():
            subprocess.run(
                ["npm", "install", "--silent"],
                cwd=bridge_dir,
                check=True,
            )

        env = os.environ.copy()
        env["BRIDGE_TOKEN"] = self._effective_bridge_token()
        env["BRIDGE_MEDIA_ROOT"] = str(self._outbound_media_root())
        if self.config.allowed_users:
            env["WHATSAPP_ALLOWED_USERS"] = ",".join(self.config.allowed_users)
        if self.config.reply_prefix is not None:
            env["WHATSAPP_REPLY_PREFIX"] = self.config.reply_prefix

        self._bridge_process = subprocess.Popen(
            [
                "node",
                str(bridge_script),
                "--host",
                self.config.bridge.host,
                "--port",
                str(self.config.bridge.port),
                "--session",
                str(session_dir),
                "--mode",
                self.config.bridge.mode,
            ],
            cwd=str(bridge_dir),
            env=env,
        )

    def _resolve_bridge_script(self) -> Path:
        if self.config.bridge.script_path:
            custom_path = Path(self.config.bridge.script_path).expanduser().resolve()
            return custom_path / "bridge.js" if custom_path.is_dir() else custom_path

        source_dir = Path(__file__).resolve().parent.parent / "bridges" / "whatsapp"
        target_dir = self.runtime_dir / "whatsapp-bridge"
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("bridge.js", "allowlist.js", "package.json"):
            shutil.copy2(source_dir / filename, target_dir / filename)
        return target_dir / "bridge.js"

    def _effective_bridge_token(self) -> str:
        configured = _optional_str(self.config.bridge.token)
        if configured:
            return configured

        token_path = self.runtime_dir / "bridge_tokens" / "whatsapp.token"
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        token_path.write_text(token, encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
        return token

    def _session_dir(self) -> Path:
        if self.config.bridge.session_dir:
            return Path(self.config.bridge.session_dir).expanduser().resolve()
        return (self.runtime_dir / "whatsapp" / "session").resolve()

    def _outbound_media_root(self) -> Path:
        return (self.runtime_dir / "outbound_media").resolve()


def _split_text(content: str, limit: int) -> list[str]:
    text = content.strip()
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        chunk = remaining[:limit]
        if len(remaining) > limit:
            split_at = chunk.rfind("\n")
            if split_at < limit // 3:
                split_at = chunk.rfind(" ")
            if split_at >= limit // 3:
                chunk = chunk[:split_at]
        chunks.append(chunk.strip())
        remaining = remaining[len(chunk):].lstrip()
    return [chunk for chunk in chunks if chunk]


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _build_session_key_hint(source: ChannelSource) -> str:
    parts = [source.platform.value, source.chat_id]
    if source.thread_id:
        parts.append(source.thread_id)
    return "__".join(part.replace("/", "_") for part in parts if part)


def _normalize_whatsapp_identifier(value: Any) -> str:
    normalized = re.sub(r":.*@", "@", str(value or "").strip())
    normalized = re.sub(r"@.*", "", normalized)
    return normalized.lstrip("+")
