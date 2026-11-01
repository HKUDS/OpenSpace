from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from openspace.communication.adapters.base import BaseChannelAdapter
from openspace.communication.attachment_cache import AttachmentCache
from openspace.communication.config import FeishuConfig
from openspace.communication.policy import is_authorized
from openspace.communication.types import (
    AttachmentKind,
    ChannelMessage,
    ChannelPlatform,
    ChannelSource,
    SendResult,
)
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_FEISHU_WEBHOOK_MAX_BODY_BYTES = 1 * 1024 * 1024
_FEISHU_WEBHOOK_READ_TIMEOUT_SECONDS = 30
_FEISHU_WEBHOOK_RATE_WINDOW_SECONDS = 60
_FEISHU_WEBHOOK_RATE_LIMIT_MAX = 120
_FEISHU_WEBHOOK_RATE_MAX_KEYS = 4096
_FEISHU_WEBHOOK_ANOMALY_TTL_SECONDS = 6 * 60 * 60
_FEISHU_DEDUP_CACHE_SIZE = 2048
_FEISHU_DEDUP_TTL_SECONDS = 24 * 60 * 60

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        GetMessageRequest,
        GetMessageResourceRequest,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None  # type: ignore[assignment]
    CreateMessageRequest = None  # type: ignore[assignment]
    CreateMessageRequestBody = None  # type: ignore[assignment]
    GetMessageRequest = None  # type: ignore[assignment]
    GetMessageResourceRequest = None  # type: ignore[assignment]
    ReplyMessageRequest = None  # type: ignore[assignment]
    ReplyMessageRequestBody = None  # type: ignore[assignment]
    FEISHU_DOMAIN = None  # type: ignore[assignment]
    LARK_DOMAIN = None  # type: ignore[assignment]


class FeishuAdapter(BaseChannelAdapter):
    MAX_MESSAGE_LENGTH = 8000
    _REPLY_CONTEXT_MAX_LEN = 200

    def __init__(
        self,
        config: FeishuConfig,
        attachment_cache: AttachmentCache,
        *,
        runtime_dir: Optional[Path] = None,
    ):
        super().__init__(ChannelPlatform.FEISHU)
        self.config = config
        self.attachment_cache = attachment_cache
        self.runtime_dir = (
            Path(runtime_dir).expanduser().resolve()
            if runtime_dir is not None
            else attachment_cache.base_dir.parent.resolve()
        )
        self._client: Any = None
        self._bot_open_id = config.bot_open_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_client: Any = None
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False
        self._dedup_state_path = self.runtime_dir / "feishu_seen_message_ids.json"
        self._seen_message_ids: OrderedDict[str, float] = OrderedDict()
        self._recent_sent_message_ids: OrderedDict[str, None] = OrderedDict()
        self._rate_windows: dict[str, deque[float]] = {}
        self._webhook_anomalies: dict[str, tuple[int, str, float]] = {}
        self._dedup_dirty = False
        self._load_seen_message_ids()

    def register_http_routes(self, app: Any) -> None:
        if self.config.connection_mode == "webhook":
            app.router.add_post(self.config.webhook_path, self._handle_webhook)

    def validate_configuration(self) -> None:
        if self.config.connection_mode == "webhook" and not _optional_str(self.config.verification_token):
            raise ValueError("Feishu webhook mode requires verification_token")

    def get_lock_identity(self) -> Optional[tuple[str, str]]:
        app_id = _optional_str(self.config.app_id)
        if not app_id:
            return None
        return ("feishu-app", app_id)

    async def connect(self) -> bool:
        self.validate_configuration()
        if not FEISHU_AVAILABLE:
            logger.error("Feishu adapter requires lark-oapi")
            return False
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu adapter missing app_id/app_secret")
            return False
        domain = FEISHU_DOMAIN if self.config.domain != "lark" else LARK_DOMAIN
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        self._running = True
        self._loop = asyncio.get_running_loop()
        if not self._bot_open_id:
            self._bot_open_id = await asyncio.to_thread(self._fetch_bot_open_id)
        if self.config.connection_mode == "websocket":
            self._start_websocket_client()
            self._connected = False
        else:
            self._connected = True
        logger.info(
            "Feishu adapter connected via %s mode",
            self.config.connection_mode,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._connected = False
        if self._ws_thread is not None and self._ws_thread.is_alive():
            await asyncio.to_thread(self._ws_thread.join, 5)
        self._ws_thread = None
        self._persist_seen_message_ids()
        self._client = None

    async def send_text(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Feishu client not initialized")

        last_message_id: Optional[str] = None
        for chunk in _split_text(content, self.MAX_MESSAGE_LENGTH):
            payload = json.dumps({"text": chunk}, ensure_ascii=False)
            if reply_to_message_id:
                body = (
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(payload)
                    .build()
                )
                request = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to_message_id)
                    .request_body(body)
                    .build()
                )
                response = await asyncio.to_thread(self._client.im.v1.message.reply, request)
            else:
                body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(payload)
                    .build()
                )
                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .request_body(body)
                    .build()
                )
                response = await asyncio.to_thread(self._client.im.v1.message.create, request)

            if not response.success():
                return SendResult(
                    success=False,
                    error=f"[{response.code}] {response.msg}",
                    raw_response=response,
                )
            last_message_id = getattr(getattr(response, "data", None), "message_id", None)
            if last_message_id:
                self._remember_sent_message_id(last_message_id)
        return SendResult(success=True, message_id=last_message_id)

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        remote_ip = _client_ip_from_request(request)
        rate_key = f"{self.config.app_id}:{self.config.webhook_path}:{remote_ip}"
        if not self._check_webhook_rate_limit(rate_key):
            self._record_webhook_anomaly(remote_ip, "429")
            return web.Response(status=429, text="Rate limit exceeded")

        content_length = request.content_length or 0
        if content_length > _FEISHU_WEBHOOK_MAX_BODY_BYTES:
            self._record_webhook_anomaly(remote_ip, "413")
            return web.Response(status=413, text="Payload too large")

        try:
            async with asyncio.timeout(_FEISHU_WEBHOOK_READ_TIMEOUT_SECONDS):
                body_bytes = await request.read()
        except TimeoutError:
            self._record_webhook_anomaly(remote_ip, "408")
            return web.Response(status=408, text="Request timeout")

        if len(body_bytes) > _FEISHU_WEBHOOK_MAX_BODY_BYTES:
            self._record_webhook_anomaly(remote_ip, "413")
            return web.Response(status=413, text="Payload too large")

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._record_webhook_anomaly(remote_ip, "400")
            return web.json_response({"code": 400, "msg": "invalid json"}, status=400)

        incoming_token = str((payload.get("header") or {}).get("token") or payload.get("token") or "")
        if not incoming_token or not hmac.compare_digest(incoming_token, self.config.verification_token or ""):
            self._record_webhook_anomaly(remote_ip, "401-token")
            return web.Response(status=401, text="Invalid verification token")

        if self.config.encrypt_key and not _is_webhook_signature_valid(
            encrypt_key=self.config.encrypt_key,
            headers=request.headers,
            body_bytes=body_bytes,
        ):
            self._record_webhook_anomaly(remote_ip, "401-signature")
            return web.Response(status=401, text="Invalid signature")
        if payload.get("encrypt"):
            self._record_webhook_anomaly(remote_ip, "400-encrypted")
            return web.json_response(
                {"code": 400, "msg": "encrypted webhook payloads are not supported"},
                status=400,
            )

        self._clear_webhook_anomaly(remote_ip)

        if payload.get("type") == "url_verification":
            return web.json_response({"challenge": payload.get("challenge", "")})

        event_type = str((payload.get("header") or {}).get("event_type") or "")
        if event_type == "im.message.receive_v1":
            await self._handle_message_event(payload)
        return web.json_response({"code": 0, "msg": "ok"})

    def _start_websocket_client(self) -> None:
        assert lark is not None

        handler = (
            lark.EventDispatcherHandler.builder(
                self.config.encrypt_key or "",
                self.config.verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        def _run_ws_forever() -> None:
            import lark_oapi.ws.client as lark_ws_client

            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        self._ws_client.start()
                    except Exception as exc:
                        self._connected = False
                        logger.warning("Feishu WebSocket client error: %s", exc)
                    if self._running:
                        time.sleep(5)
            finally:
                self._connected = False
                ws_loop.close()

        self._ws_thread = threading.Thread(
            target=_run_ws_forever,
            daemon=True,
            name="openspace-feishu-ws",
        )
        self._ws_thread.start()

    def _on_message_sync(self, data: Any) -> None:
        if not self._loop or not self._running:
            return
        self._connected = True
        asyncio.run_coroutine_threadsafe(
            self._handle_websocket_message(data),
            self._loop,
        )

    async def _handle_message_event(self, payload: dict[str, Any]) -> None:
        normalized = await self._normalize_webhook_payload(payload)
        if normalized is not None:
            await self.dispatch_message(normalized)

    async def _handle_websocket_message(self, data: Any) -> None:
        normalized = await self._normalize_websocket_event(data)
        if normalized is not None:
            await self.dispatch_message(normalized)

    async def _normalize_webhook_payload(self, payload: dict[str, Any]) -> Optional[ChannelMessage]:
        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        if str(sender.get("sender_type", "")).lower() == "bot":
            return None
        return await self._normalize_inbound_message(
            message_id=_optional_str(message.get("message_id")),
            chat_id=_optional_str(message.get("chat_id")),
            chat_type=_optional_str(message.get("chat_type")) or "p2p",
            sender_uid=(
                _optional_str(sender_id.get("open_id"))
                or _optional_str(sender_id.get("user_id"))
                or _optional_str(sender_id.get("union_id"))
            ),
            sender_name=(
                _optional_str(sender_id.get("name"))
                or _optional_str(sender.get("sender_name"))
            ),
            thread_id=_optional_str(message.get("thread_id")),
            message_type=_optional_str(message.get("message_type")) or "",
            content=_safe_json_loads(message.get("content", "{}")),
            mentions=list(message.get("mentions") or []),
            reply_to_message_id=(
                _optional_str(message.get("parent_id"))
                or _optional_str(message.get("upper_message_id"))
            ),
            metadata={"webhook_payload": payload},
            resolve_mentions=False,
        )

    async def _normalize_websocket_event(self, data: Any) -> Optional[ChannelMessage]:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if message is None or sender is None:
            return None
        if str(getattr(sender, "sender_type", "")).lower() == "bot":
            return None

        sender_id = getattr(sender, "sender_id", None)
        return await self._normalize_inbound_message(
            message_id=_optional_str(getattr(message, "message_id", None)),
            chat_id=_optional_str(getattr(message, "chat_id", None)),
            chat_type=_optional_str(getattr(message, "chat_type", None)) or "p2p",
            sender_uid=(
                _optional_str(getattr(sender_id, "open_id", None))
                or _optional_str(getattr(sender_id, "user_id", None))
                or _optional_str(getattr(sender_id, "union_id", None))
            ),
            sender_name=_optional_str(getattr(sender, "sender_name", None)),
            thread_id=_optional_str(getattr(message, "thread_id", None)),
            message_type=_optional_str(getattr(message, "message_type", None)) or "",
            content=_safe_json_loads(getattr(message, "content", "{}")),
            mentions=list(getattr(message, "mentions", None) or []),
            reply_to_message_id=(
                _optional_str(getattr(message, "parent_id", None))
                or _optional_str(getattr(message, "upper_message_id", None))
            ),
            metadata={"websocket_event": True},
            resolve_mentions=True,
        )

    async def _normalize_inbound_message(
        self,
        *,
        message_id: Optional[str],
        chat_id: Optional[str],
        chat_type: str,
        sender_uid: Optional[str],
        sender_name: Optional[str],
        thread_id: Optional[str],
        message_type: str,
        content: dict[str, Any],
        mentions: list[Any],
        reply_to_message_id: Optional[str],
        metadata: dict[str, Any],
        resolve_mentions: bool,
    ) -> Optional[ChannelMessage]:
        if not message_id or not chat_id:
            return None
        if self._is_message_seen(message_id):
            logger.debug("Skipping duplicate Feishu message %s", message_id)
            return None

        source = ChannelSource(
            platform=ChannelPlatform.FEISHU,
            chat_id=chat_id,
            chat_type="dm" if str(chat_type).lower() == "p2p" else "group",
            user_id=sender_uid,
            user_name=sender_name,
            chat_name=chat_id,
            thread_id=thread_id,
        )
        session_key = _build_session_key_hint(source)
        normalized_type = str(message_type or "").strip().lower()
        mentions_bot = self._mentions_bot(mentions)

        text = ""
        if normalized_type == "text":
            text = str(content.get("text", "")).strip()
            if resolve_mentions:
                text = _resolve_mentions(text, mentions)
        elif normalized_type == "post":
            text = _extract_post_text(content)

        prefilter_message = ChannelMessage(
            source=source,
            text=text,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            mentions_bot=mentions_bot,
            metadata=metadata,
        )
        if not self._passes_prefilter(prefilter_message):
            self._remember_message_seen(message_id)
            return None

        attachments = []
        if normalized_type == "image":
            attachment = await self._download_attachment(
                session_key=session_key,
                message_id=message_id,
                file_key=str(content.get("image_key", "")).strip(),
                file_name=str(content.get("image_key", "image")).strip() + ".png",
                kind=AttachmentKind.IMAGE,
                resource_type="image",
            )
            if attachment is not None:
                attachments.append(attachment)
        elif normalized_type == "file":
            attachment = await self._download_attachment(
                session_key=session_key,
                message_id=message_id,
                file_key=str(content.get("file_key", "")).strip(),
                file_name=str(content.get("file_name", "document")).strip(),
                kind=AttachmentKind.DOCUMENT,
                resource_type="file",
            )
            if attachment is not None:
                attachments.append(attachment)

        prefilter_message.attachments = attachments
        prefilter_message.reply_to_text = await self._fetch_message_text(reply_to_message_id)
        self._remember_message_seen(message_id)
        return prefilter_message

    def _passes_prefilter(self, message: ChannelMessage) -> bool:
        if not is_authorized(message, self.config):
            logger.info("Rejected Feishu message from unauthorized user %s", message.source.user_id)
            return False
        if message.source.chat_type == "dm":
            return self.config.allow_dm
        if not self.config.allow_groups:
            return False
        if self.config.group_policy == "disabled":
            return False
        if self.config.group_policy == "mention_only":
            return message.mentions_bot
        if self.config.group_policy == "reply_or_mention":
            return message.mentions_bot or self._is_reply_to_recent_bot_message(
                message.reply_to_message_id
            )
        return True

    def _is_reply_to_recent_bot_message(self, message_id: Optional[str]) -> bool:
        if not message_id:
            return False
        return message_id in self._recent_sent_message_ids

    def _remember_sent_message_id(self, message_id: str) -> None:
        self._recent_sent_message_ids.pop(message_id, None)
        self._recent_sent_message_ids[message_id] = None
        while len(self._recent_sent_message_ids) > _FEISHU_DEDUP_CACHE_SIZE:
            self._recent_sent_message_ids.popitem(last=False)

    async def _download_attachment(
        self,
        *,
        session_key: str,
        message_id: str,
        file_key: str,
        file_name: str,
        kind: AttachmentKind,
        resource_type: str,
    ):
        if not self._client or not file_key:
            return None
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        response = await asyncio.to_thread(self._client.im.v1.message_resource.get, request)
        if not response.success():
            logger.warning(
                "Failed to download Feishu attachment: code=%s msg=%s",
                response.code,
                response.msg,
            )
            return None
        try:
            file_data = await asyncio.to_thread(
                _read_attachment_body,
                response.file,
                self.attachment_cache.max_attachment_bytes,
            )
        except ValueError as exc:
            logger.warning(
                "Rejected Feishu attachment for session %s: %s",
                session_key,
                exc,
            )
            return None
        return self.attachment_cache.save_bytes(
            session_key=session_key,
            data=file_data,
            filename=file_name,
            kind=kind,
        )

    def _fetch_bot_open_id(self) -> Optional[str]:
        if not self._client or not lark:
            return None
        try:
            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.APP})
                .build()
            )
            response = self._client.request(request)
            if not response.success():
                logger.warning(
                    "Failed to fetch Feishu bot info: code=%s msg=%s",
                    response.code,
                    response.msg,
                )
                return None
            payload = json.loads(response.raw.content)
            bot = (payload.get("data") or payload).get("bot") or {}
            return _optional_str(bot.get("open_id"))
        except Exception as exc:
            logger.warning("Failed to resolve Feishu bot open_id: %s", exc)
            return None

    async def _fetch_message_text(self, message_id: Optional[str]) -> Optional[str]:
        if not self._client or not message_id or GetMessageRequest is None:
            return None

        request = GetMessageRequest.builder().message_id(message_id).build()
        try:
            response = await asyncio.to_thread(self._client.im.v1.message.get, request)
        except Exception as exc:
            logger.debug("Failed to fetch Feishu parent message %s: %s", message_id, exc)
            return None
        if not response.success():
            return None

        data = getattr(response, "data", None)
        message_obj = None
        items = getattr(data, "items", None)
        if items:
            message_obj = items[0]
        elif data is not None:
            message_obj = getattr(data, "message", None) or data
        if message_obj is None:
            return None

        body = getattr(message_obj, "body", None)
        raw_content = getattr(body, "content", None) if body is not None else getattr(message_obj, "content", None)
        message_type = (
            getattr(message_obj, "msg_type", None)
            or getattr(message_obj, "message_type", None)
            or ""
        )
        content = _safe_json_loads(raw_content)
        text = ""
        if str(message_type).lower() == "text":
            text = str(content.get("text", "")).strip()
        elif str(message_type).lower() == "post":
            text = _extract_post_text(content)
        if not text:
            return None
        if len(text) > self._REPLY_CONTEXT_MAX_LEN:
            text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
        return text

    def _mentions_bot(self, mentions: list[Any]) -> bool:
        if not mentions:
            return False
        if not self._bot_open_id:
            return False

        for mention in mentions:
            mention_id = (mention.get("id") or {}) if isinstance(mention, dict) else getattr(mention, "id", None)
            open_id = (
                _optional_str(mention_id.get("open_id"))
                if isinstance(mention_id, dict)
                else _optional_str(getattr(mention_id, "open_id", None))
            )
            if open_id == self._bot_open_id:
                return True
        return False

    def _load_seen_message_ids(self) -> None:
        if not self._dedup_state_path.exists():
            return
        try:
            payload = json.loads(self._dedup_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to load Feishu dedup cache from %s", self._dedup_state_path)
            return
        entries = payload.get("message_ids", {}) if isinstance(payload, dict) else {}
        now = time.time()
        valid: list[tuple[str, float]] = []
        if isinstance(entries, dict):
            for message_id, seen_at in entries.items():
                normalized_id = _optional_str(message_id)
                if not normalized_id:
                    continue
                try:
                    timestamp = float(seen_at)
                except (TypeError, ValueError):
                    continue
                if now - timestamp <= _FEISHU_DEDUP_TTL_SECONDS:
                    valid.append((normalized_id, timestamp))
        for message_id, seen_at in sorted(valid, key=lambda item: item[1])[-_FEISHU_DEDUP_CACHE_SIZE:]:
            self._seen_message_ids[message_id] = seen_at

    def _persist_seen_message_ids(self) -> None:
        if not self._dedup_dirty:
            return
        self._dedup_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"message_ids": dict(self._seen_message_ids)}
        try:
            self._dedup_state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Failed to persist Feishu dedup cache to %s", self._dedup_state_path)
            return
        self._dedup_dirty = False

    def _is_message_seen(self, message_id: str) -> bool:
        now = time.time()
        self._prune_seen_message_ids(now)
        return message_id in self._seen_message_ids

    def _remember_message_seen(self, message_id: str) -> None:
        now = time.time()
        self._prune_seen_message_ids(now)
        if message_id in self._seen_message_ids:
            self._seen_message_ids.move_to_end(message_id)
            return
        self._seen_message_ids[message_id] = now
        self._seen_message_ids.move_to_end(message_id)
        while len(self._seen_message_ids) > _FEISHU_DEDUP_CACHE_SIZE:
            self._seen_message_ids.popitem(last=False)
        self._dedup_dirty = True
        self._persist_seen_message_ids()

    def _mark_message_seen(self, message_id: str) -> bool:
        if self._is_message_seen(message_id):
            return True
        self._remember_message_seen(message_id)
        return False

    def _prune_seen_message_ids(self, now: Optional[float] = None) -> None:
        current = now or time.time()
        stale = [
            message_id
            for message_id, seen_at in self._seen_message_ids.items()
            if current - seen_at > _FEISHU_DEDUP_TTL_SECONDS
        ]
        for message_id in stale:
            self._seen_message_ids.pop(message_id, None)
            self._dedup_dirty = True

    def _check_webhook_rate_limit(self, rate_key: str) -> bool:
        now = time.time()
        window = self._rate_windows.get(rate_key)
        if window is None:
            if len(self._rate_windows) >= _FEISHU_WEBHOOK_RATE_MAX_KEYS:
                stale_keys = [
                    key
                    for key, timestamps in self._rate_windows.items()
                    if not timestamps or now - timestamps[-1] > _FEISHU_WEBHOOK_RATE_WINDOW_SECONDS
                ]
                for key in stale_keys:
                    self._rate_windows.pop(key, None)
                if rate_key not in self._rate_windows and len(self._rate_windows) >= _FEISHU_WEBHOOK_RATE_MAX_KEYS:
                    return False
            window = deque()
            self._rate_windows[rate_key] = window
        cutoff = now - _FEISHU_WEBHOOK_RATE_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= _FEISHU_WEBHOOK_RATE_LIMIT_MAX:
            return False
        window.append(now)
        return True

    def _record_webhook_anomaly(self, remote_ip: str, status: str) -> None:
        now = time.time()
        current = self._webhook_anomalies.get(remote_ip)
        if current and now - current[2] < _FEISHU_WEBHOOK_ANOMALY_TTL_SECONDS:
            self._webhook_anomalies[remote_ip] = (current[0] + 1, status, current[2])
            return
        self._webhook_anomalies[remote_ip] = (1, status, now)

    def _clear_webhook_anomaly(self, remote_ip: str) -> None:
        self._webhook_anomalies.pop(remote_ip, None)


def _safe_json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _client_ip_from_request(request: web.Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return request.remote or "unknown"


def _resolve_mentions(text: str, mentions: list[Any]) -> str:
    if not text or not mentions:
        return text

    resolved = text
    for mention in mentions:
        key = _optional_str(
            mention.get("key") if isinstance(mention, dict) else getattr(mention, "key", None)
        )
        if not key or key not in resolved:
            continue
        name = _optional_str(
            mention.get("name") if isinstance(mention, dict) else getattr(mention, "name", None)
        ) or "user"
        resolved = resolved.replace(key, f"@{name}")
    return resolved


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


def _is_webhook_signature_valid(*, encrypt_key: str, headers: Any, body_bytes: bytes) -> bool:
    timestamp = str(headers.get("x-lark-request-timestamp", "") or "")
    nonce = str(headers.get("x-lark-request-nonce", "") or "")
    signature = str(headers.get("x-lark-signature", "") or "")
    if not timestamp or not nonce or not signature:
        return False
    content = f"{timestamp}{nonce}{encrypt_key}{body_bytes.decode('utf-8', errors='replace')}"
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hmac.compare_digest(signature, expected)


def _build_session_key_hint(source: ChannelSource) -> str:
    parts = [source.platform.value, source.chat_id]
    if source.thread_id:
        parts.append(source.thread_id)
    return "__".join(part.replace("/", "_") for part in parts if part)


def _extract_post_text(content: dict[str, Any]) -> str:
    texts: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            title = _optional_str(value.get("title"))
            if title:
                texts.append(title)
            tag = _optional_str(value.get("tag"))
            if tag == "at":
                user_name = _optional_str(value.get("user_name")) or "user"
                texts.append(f"@{user_name}")
            else:
                text = _optional_str(value.get("text"))
                if text:
                    texts.append(text)
            for nested in value.values():
                _walk(nested)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(content)
    deduped: list[str] = []
    for text in texts:
        if text not in deduped:
            deduped.append(text)
    return "\n".join(deduped).strip()


def _read_attachment_body(raw_file: Any, max_bytes: int) -> bytes:
    if raw_file is None:
        return b""

    if isinstance(raw_file, bytes):
        data = raw_file
    elif isinstance(raw_file, bytearray):
        data = bytes(raw_file)
    elif hasattr(raw_file, "read"):
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = raw_file.read(65536)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                elif isinstance(chunk, bytearray):
                    chunk = bytes(chunk)
                elif not isinstance(chunk, bytes):
                    chunk = bytes(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"attachment size {total} exceeds limit {max_bytes}"
                    )
                chunks.append(chunk)
        finally:
            close = getattr(raw_file, "close", None)
            if callable(close):
                close()
        data = b"".join(chunks)
    else:
        data = bytes(raw_file)

    if len(data) > max_bytes:
        raise ValueError(f"attachment size {len(data)} exceeds limit {max_bytes}")
    return data
