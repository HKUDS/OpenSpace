from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import requests
from aiohttp import web

from openspace.communication.adapters.base import BaseChannelAdapter
from openspace.communication.attachment_cache import AttachmentCache
from openspace.communication.config import CommunicationConfig, load_communication_config
from openspace.communication.gateway_runtime import RuntimeStatusStore, ScopedLock, ScopedLockManager
from openspace.communication.policy import (
    build_attachment_instruction,
    is_authorized,
    should_accept_message,
)
from openspace.communication.runtime_manager import SessionRuntimeManager
from openspace.communication.session_store import SessionStore
from openspace.communication.types import ChannelMessage, ChannelPlatform, ChannelSession
from openspace.host_detection import build_grounding_config_path, build_llm_kwargs, load_runtime_env
from openspace.services.runtime_support.low_latency import (
    LowLatencyProfiler,
    aggregate_low_latency_spans,
    get_capability_profile,
    new_correlation_id,
)
from openspace.services.runtime_support.warm_core import WarmCore
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace import OpenSpace

logger = Logger.get_logger(__name__)


def _append_no_proxy_hosts(*hosts: str) -> None:
    for env_name in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(env_name, "")
        entries = [entry.strip() for entry in current.split(",") if entry.strip()]
        updated = False
        for host in hosts:
            if host not in entries:
                entries.append(host)
                updated = True
        if updated:
            os.environ[env_name] = ",".join(entries)


def _configure_ollama_process_env(model: str) -> None:
    if not model.lower().startswith("ollama/"):
        return

    _append_no_proxy_hosts("127.0.0.1", "localhost")
    for env_name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        if os.environ.get(env_name):
            logger.info("Clearing %s for local Ollama access", env_name)
            os.environ.pop(env_name, None)


class CommunicationGateway:
    def __init__(self, config: CommunicationConfig):
        self.config = config
        workspace_root = (
            Path(config.agent.workspace_root).expanduser().resolve()
            if config.agent.workspace_root
            else None
        )
        self.session_store = SessionStore(
            config.sessions_dir,
            workspace_root=workspace_root,
        )
        self.attachment_cache = AttachmentCache(
            config.sessions_dir,
            max_attachment_bytes=config.sessions.max_attachment_bytes,
            max_session_attachment_bytes=config.sessions.max_session_attachment_bytes,
        )
        self.runtime_manager = SessionRuntimeManager(
            config,
            self._create_openspace_runtime,
            session_store=self.session_store,
        )
        self._session_queues: Dict[str, asyncio.Queue[ChannelMessage]] = {}
        self._session_workers: Dict[str, asyncio.Task] = {}
        self._adapters: Dict[ChannelPlatform, BaseChannelAdapter] = {}
        self._web_app: Optional[web.Application] = None
        self._web_runner: Optional[web.AppRunner] = None
        self._web_site: Optional[web.TCPSite] = None
        self._running = False
        self._runtime_manager_started = False
        self._runtime_status = RuntimeStatusStore(self._runtime_status_path)
        self._lock_manager = ScopedLockManager(self._locks_dir)
        self._acquired_locks: list[ScopedLock] = []
        self._warm_core = self._init_warm_core()

    async def start(self) -> None:
        if self._running:
            return

        self.config.data_path.mkdir(parents=True, exist_ok=True)
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        self._bridge_tokens_dir.mkdir(parents=True, exist_ok=True)
        self._outbound_media_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._build_adapters()
            for adapter in self._adapters.values():
                validate_configuration = getattr(adapter, "validate_configuration", None)
                if callable(validate_configuration):
                    validate_configuration()
            self._acquire_adapter_locks()
            self._write_runtime_status("starting")
            await self.runtime_manager.start()
            self._runtime_manager_started = True

            self._web_app = web.Application()
            self._web_app.router.add_get(self.config.server.health_path, self._handle_health)
            for adapter in self._adapters.values():
                adapter.register_http_routes(self._web_app)

            self._web_runner = web.AppRunner(self._web_app)
            await self._web_runner.setup()
            self._web_site = web.TCPSite(
                self._web_runner,
                self.config.server.host,
                self.config.server.port,
            )
            await self._web_site.start()

            for adapter in self._adapters.values():
                connected = await adapter.connect()
                if not connected:
                    raise RuntimeError(
                        f"Communication adapter failed to connect: {adapter.platform.value}"
                    )

            self._running = True
            self._write_runtime_status("running")
            logger.info(
                "Communication gateway started on %s:%s for platforms=%s",
                self.config.server.host,
                self.config.server.port,
                ",".join(self.config.enabled_platforms) or "(none)",
            )
        except Exception as exc:
            await self._rollback_start(exc)
            raise

    async def stop(self) -> None:
        if not self._running and not self._has_live_resources():
            return

        self._write_runtime_status("stopping")
        self._running = False
        await self._stop_session_workers()
        await self._disconnect_adapters()
        await self._cleanup_web_runner()
        await self._stop_runtime_manager()
        self._release_locks()
        self._write_runtime_status("stopped")
        logger.info("Communication gateway stopped")

    def _build_adapters(self) -> None:
        adapters: Dict[ChannelPlatform, BaseChannelAdapter] = {}
        if self.config.whatsapp.enabled:
            from openspace.communication.adapters.whatsapp import WhatsAppAdapter

            adapter = WhatsAppAdapter(
                self.config.whatsapp,
                self.attachment_cache,
                runtime_dir=self.config.data_path,
                poll_interval_seconds=self.config.sessions.whatsapp_poll_interval_seconds,
            )
            adapter.set_message_handler(self.handle_message)
            adapters[ChannelPlatform.WHATSAPP] = adapter
        if self.config.feishu.enabled:
            from openspace.communication.adapters.feishu import FeishuAdapter

            adapter = FeishuAdapter(
                self.config.feishu,
                self.attachment_cache,
                runtime_dir=self.config.data_path,
            )
            adapter.set_message_handler(self.handle_message)
            adapters[ChannelPlatform.FEISHU] = adapter
        self._adapters = adapters

    def _acquire_adapter_locks(self) -> None:
        self._release_locks()
        for adapter in self._adapters.values():
            get_lock_identity = getattr(adapter, "get_lock_identity", None)
            binding = get_lock_identity() if callable(get_lock_identity) else None
            if binding is None:
                continue
            scope, identity = binding
            lock = self._lock_manager.acquire(
                scope=scope,
                identity=identity,
                metadata={"platform": adapter.platform.value},
            )
            self._acquired_locks.append(lock)

    def _release_locks(self) -> None:
        while self._acquired_locks:
            self._lock_manager.release(self._acquired_locks.pop())

    def _write_runtime_status(
        self,
        gateway_state: str,
        *,
        fatal_error: Optional[str] = None,
    ) -> None:
        platform_states = {
            adapter.platform.value: {"connected": adapter.is_connected}
            for adapter in self._adapters.values()
        }
        self._runtime_status.write(
            gateway_state=gateway_state,
            platforms=platform_states,
            config_path=str(self.config.data_path),
            fatal_error=fatal_error,
        )

    async def _rollback_start(self, exc: Exception) -> None:
        logger.error("Communication gateway startup failed: %s", exc, exc_info=True)
        self._running = False
        await self._disconnect_adapters()
        await self._cleanup_web_runner()
        try:
            await self._stop_runtime_manager()
        finally:
            self._release_locks()
            self._write_runtime_status("failed", fatal_error=str(exc))

    async def _stop_session_workers(self) -> None:
        worker_tasks = list(self._session_workers.values())
        self._session_workers.clear()
        for task in worker_tasks:
            task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        self._session_queues.clear()

    async def _disconnect_adapters(self) -> None:
        adapters = list(self._adapters.values())
        self._adapters.clear()
        for adapter in adapters:
            try:
                await adapter.disconnect()
            except Exception:
                logger.warning(
                    "Failed to disconnect adapter during cleanup: %s",
                    getattr(adapter.platform, "value", "unknown"),
                    exc_info=True,
                )

    async def _cleanup_web_runner(self) -> None:
        if self._web_runner is None:
            return
        try:
            await self._web_runner.cleanup()
        finally:
            self._web_runner = None
            self._web_site = None
            self._web_app = None

    async def _stop_runtime_manager(self) -> None:
        if not self._runtime_manager_started:
            return
        try:
            await self.runtime_manager.stop()
        finally:
            self._runtime_manager_started = False

    def _has_live_resources(self) -> bool:
        return any(
            (
                self._runtime_manager_started,
                bool(self._adapters),
                self._web_runner is not None,
                bool(self._acquired_locks),
                bool(self._session_workers),
                bool(self._session_queues),
            )
        )

    @property
    def _runtime_status_path(self) -> Path:
        return getattr(self.config, "runtime_status_path", self.config.data_path / "runtime_status.json")

    @property
    def _locks_dir(self) -> Path:
        return getattr(self.config, "locks_dir", self.config.data_path / "locks")

    @property
    def _bridge_tokens_dir(self) -> Path:
        return getattr(self.config, "bridge_tokens_dir", self.config.data_path / "bridge_tokens")

    @property
    def _outbound_media_dir(self) -> Path:
        return getattr(self.config, "outbound_media_dir", self.config.data_path / "outbound_media")

    async def handle_message(self, message: ChannelMessage) -> None:
        message.metadata.setdefault(
            "openspace_received_monotonic",
            asyncio.get_running_loop().time(),
        )
        session = self.session_store.get_or_create_session(message.source)
        queue = self._session_queues.get(session.session_key)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.config.sessions.per_session_queue_size)
            self._session_queues[session.session_key] = queue
        worker = self._session_workers.get(session.session_key)
        if worker is None or worker.done():
            self._session_workers[session.session_key] = asyncio.create_task(
                self._session_worker(session, queue)
            )
        await queue.put(message)

    async def _session_worker(
        self,
        session: ChannelSession,
        queue: asyncio.Queue[ChannelMessage],
    ) -> None:
        session_key = session.session_key
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(),
                        timeout=self.config.sessions.idle_ttl_seconds,
                    )
                except asyncio.TimeoutError:
                    if queue.empty():
                        logger.info("Retiring idle communication worker: %s", session_key)
                        return
                    continue

                try:
                    await self._process_message(session, message)
                except Exception as exc:
                    logger.error(
                        "Failed to process %s message for session %s: %s",
                        message.source.platform.value,
                        session.session_key,
                        exc,
                        exc_info=True,
                    )
                    adapter = self._adapters.get(message.source.platform)
                    if adapter:
                        await adapter.send_text(
                            message.source.chat_id,
                            f"OpenSpace communication error: {exc}",
                        )
                finally:
                    queue.task_done()
        finally:
            current_task = asyncio.current_task()
            if self._session_workers.get(session_key) is current_task:
                self._session_workers.pop(session_key, None)
            if queue.empty():
                if (
                    self._session_queues.get(session_key) is queue
                    and session_key not in self._session_workers
                ):
                    self._session_queues.pop(session_key, None)
            elif self._running and session_key not in self._session_workers:
                self._session_workers[session_key] = asyncio.create_task(
                    self._session_worker(session, queue)
                )

    async def _process_message(self, session: ChannelSession, message: ChannelMessage) -> None:
        correlation_id = new_correlation_id(
            platform=message.source.platform.value,
            session_key=session.session_key,
            message_id=message.message_id,
        )
        profile = get_capability_profile(self._effective_capability_profile_name())
        profiler = LowLatencyProfiler(
            enabled=self.config.low_latency_profiler_enabled,
            correlation_id=correlation_id,
            session_key=session.session_key,
            profile=profile.name,
            backend_scope=self._effective_backend_scope(profile),
            base_metadata={
                "platform": message.source.platform.value,
                "chat_type": message.source.chat_type,
            },
        )
        profiler.mark("gateway.receive")
        self._record_queue_wait_span(message, profiler)

        platform_config = self._get_platform_config(message.source.platform)
        if not is_authorized(message, platform_config):
            logger.info(
                "Rejected %s message from unauthorized user %s",
                message.source.platform.value,
                message.source.user_id,
            )
            return

        reply_to_bot = self.session_store.is_reply_to_assistant(
            session,
            message.reply_to_message_id,
        )
        if not should_accept_message(message, platform_config, reply_to_bot):
            logger.debug(
                "Skipped %s group message that did not satisfy policy",
                message.source.platform.value,
            )
            return

        history = self.session_store.load_history(
            session,
            self.config.sessions.history_max_turns,
        )
        if not message.text.strip():
            message.text = build_attachment_instruction(message)

        self.session_store.append_user_message(session, message)

        adapter = self._adapters.get(message.source.platform)
        if adapter is None:
            raise RuntimeError(f"No adapter registered for {message.source.platform.value}")

        early_feedback_task = self._start_early_feedback_timer(
            adapter=adapter,
            session=session,
            message=message,
            correlation_id=correlation_id,
            profiler=profiler,
        )
        partial_text_parts: list[str] = []
        partial_sent = False
        partial_sent_len = 0

        async def runtime_event_handler(event_type: str, data: Dict[str, Any]) -> None:
            nonlocal partial_sent, partial_sent_len
            if event_type != "llm_token":
                return
            if not self._adapter_supports_partial_text(adapter):
                return
            token = data.get("token")
            if not isinstance(token, str) or not token:
                return
            partial_text_parts.append(token)
            content = "".join(partial_text_parts)
            if partial_sent and len(content) - partial_sent_len < 80:
                return
            send_result = await adapter.send_partial_text(
                message.source.chat_id,
                content,
                reply_to_message_id=message.message_id,
                metadata={
                    "openspace_message_type": "partial_response",
                    "transient": True,
                    "low_latency_correlation_id": correlation_id,
                },
            )
            if send_result is not None and send_result.success:
                partial_sent = True
                partial_sent_len = len(content)
                self._mark_first_visible_event_if_missing(
                    profiler,
                    method="partial_response",
                )

        try:
            channel_context = message.to_channel_context(session.session_key)
            result = await self.runtime_manager.execute_turn(
                session=session,
                message=message,
                conversation_history=history,
                channel_context=channel_context,
                low_latency_profiler=profiler,
                runtime_event_handler=runtime_event_handler,
            )
        finally:
            await self._cancel_early_feedback_timer(early_feedback_task)

        result_session_id = str(result.get("session_id", "")).strip()
        if self.session_store.bind_openspace_session_id(session, result_session_id):
            logger.info(
                "Bound communication session %s to OpenSpace session %s",
                session.session_key,
                result_session_id,
            )
        response_text = self._extract_response_text(result)

        with profiler.span("reply.sent"):
            send_result = await adapter.send_text(
                message.source.chat_id,
                response_text,
                reply_to_message_id=message.message_id,
            )
        if send_result.success:
            self._mark_first_visible_event_if_missing(
                profiler,
                method="final_reply",
            )
        if not send_result.success:
            logger.warning(
                "Failed to send %s response for session %s: %s",
                message.source.platform.value,
                session.session_key,
                send_result.error,
            )
        self.session_store.append_assistant_message(
            session,
            content=response_text,
            platform_message_id=send_result.message_id,
            metadata={
                "task_id": result.get("task_id"),
                "status": result.get("status"),
                "send_success": send_result.success,
                "send_error": send_result.error,
                "low_latency_correlation_id": correlation_id,
                "low_latency_profile": profiler.profile or profile.name,
                "low_latency_spans": profiler.as_dicts(),
            },
        )

    @staticmethod
    def _adapter_supports_partial_text(adapter: BaseChannelAdapter) -> bool:
        return type(adapter).send_partial_text is not BaseChannelAdapter.send_partial_text

    def _record_queue_wait_span(
        self,
        message: ChannelMessage,
        profiler: LowLatencyProfiler,
    ) -> None:
        if not profiler.enabled:
            return
        started = message.metadata.get("openspace_received_monotonic")
        try:
            started_ms = float(started) * 1000.0
        except (TypeError, ValueError):
            return
        ended_ms = asyncio.get_running_loop().time() * 1000.0
        profiler.record(
            "gateway.queue_wait",
            started_at_ms=started_ms,
            ended_at_ms=ended_ms,
        )

    def _start_early_feedback_timer(
        self,
        *,
        adapter: BaseChannelAdapter,
        session: ChannelSession,
        message: ChannelMessage,
        correlation_id: str,
        profiler: LowLatencyProfiler,
    ) -> Optional[asyncio.Task[None]]:
        low_latency = self.config.low_latency
        if not (
            self.config.low_latency_behavior_enabled
            and low_latency.early_feedback_enabled
        ):
            return None

        return asyncio.create_task(
            self._send_early_feedback_after_delay(
                adapter=adapter,
                session=session,
                message=message,
                correlation_id=correlation_id,
                profiler=profiler,
            ),
            name=f"openspace-early-feedback-{session.session_key}",
        )

    async def _cancel_early_feedback_timer(
        self,
        task: Optional[asyncio.Task[None]],
    ) -> None:
        if task is None:
            return
        if task.done():
            with contextlib.suppress(Exception):
                task.result()
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _send_early_feedback_after_delay(
        self,
        *,
        adapter: BaseChannelAdapter,
        session: ChannelSession,
        message: ChannelMessage,
        correlation_id: str,
        profiler: LowLatencyProfiler,
    ) -> None:
        delay_s = max(0, self.config.low_latency.early_feedback_delay_ms) / 1000.0
        await asyncio.sleep(delay_s)
        metadata = {
            "openspace_message_type": "early_feedback",
            "transient": True,
            "exclude_from_transcript": True,
            "low_latency_correlation_id": correlation_id,
            "session_key": session.session_key,
        }
        try:
            typing_sender = getattr(adapter, "send_typing", None)
            if callable(typing_sender):
                result = await typing_sender(message.source.chat_id, metadata=metadata)
                if result is not None and getattr(result, "success", False):
                    profiler.mark("first_visible_event", method="typing")
                    return

            if self._adapter_supports_partial_text(adapter):
                result = await adapter.send_partial_text(
                    message.source.chat_id,
                    self.config.low_latency.early_feedback_text,
                    reply_to_message_id=message.message_id,
                    metadata=metadata,
                )
                visible_method = "partial_text"
            else:
                result = await adapter.send_text(
                    message.source.chat_id,
                    self.config.low_latency.early_feedback_text,
                    reply_to_message_id=message.message_id,
                    metadata=metadata,
                )
                visible_method = "text"
            if result is not None and getattr(result, "success", False):
                profiler.mark("first_visible_event", method=visible_method)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Early feedback send failed", exc_info=True)

    def _mark_first_visible_event_if_missing(
        self,
        profiler: LowLatencyProfiler,
        *,
        method: str,
    ) -> None:
        if not profiler.enabled:
            return
        if any(event.name == "first_visible_event" for event in profiler.events):
            return
        profiler.mark("first_visible_event", method=method)

    async def _handle_health(self, request: web.Request) -> web.Response:
        runtime_status = await self.runtime_manager.status()
        gateway_status = self._runtime_status.read() or {}
        return web.json_response(
            {
                "status": "ok" if self._running else "starting",
                "gateway": gateway_status,
                "platforms": {
                    platform.value: {
                        "connected": adapter.is_connected,
                    }
                    for platform, adapter in self._adapters.items()
                },
                "runtime": runtime_status,
                "sessions": len(self.session_store.list_sessions()),
                "low_latency": self._low_latency_metrics_summary(),
            }
        )

    def _low_latency_metrics_summary(self, *, max_groups: int = 50) -> dict[str, Any]:
        try:
            records = self.session_store.load_low_latency_span_records()
            aggregates = aggregate_low_latency_spans(records)
        except Exception:
            logger.debug("Failed to aggregate low latency metrics", exc_info=True)
            return {"available": False}
        return {
            "available": True,
            "span_records": len(records),
            "groups": [item.to_dict() for item in aggregates[:max(0, max_groups)]],
            "truncated": len(aggregates) > max(0, max_groups),
        }

    async def _create_openspace_runtime(
        self,
        session: ChannelSession,
        *,
        low_latency_profiler: LowLatencyProfiler | None = None,
    ) -> "OpenSpace":
        from openspace import OpenSpace, OpenSpaceConfig

        load_runtime_env()
        env_model = os.environ.get("OPENSPACE_MODEL", "")
        model, llm_kwargs = build_llm_kwargs(env_model)
        llm_kwargs = dict(llm_kwargs)
        if model.lower().startswith("ollama/"):
            llm_kwargs["api_base"] = os.environ.get("OLLAMA_API_BASE", "").strip() or "http://127.0.0.1:11434"
            llm_kwargs["api_key"] = os.environ.get("OLLAMA_API_KEY", "").strip() or llm_kwargs.get("api_key") or "ollama"
            llm_kwargs.pop("extra_headers", None)
        backend_scope = self.config.agent.backend_scope
        profile = get_capability_profile(self._effective_capability_profile_name())
        effective_backend_scope = self._effective_backend_scope(profile)
        low_latency_runtime_enabled = self.config.low_latency_runtime_enabled
        grounding_config_path = (
            self.config.agent.grounding_config_path
            or build_grounding_config_path()
        )
        recording_dir = self.config.data_path / "recordings"
        openspace_config = OpenSpaceConfig(
            llm_model=model,
            llm_kwargs=llm_kwargs,
            workspace_dir=session.workspace_dir,
            session_storage_dir=str(self.config.data_path / "openspace_sessions"),
            grounding_max_iterations=self.config.agent.max_iterations,
            enable_recording=self.config.agent.enable_recording,
            recording_backends=self.config.agent.recording_backends,
            recording_log_dir=str(recording_dir),
            post_execution_mode=(
                profile.post_execution_mode
                if low_latency_runtime_enabled
                else "background"
            ),
            memory_drain_timeout_s=(
                profile.memory_drain_timeout_s
                if low_latency_runtime_enabled
                else 0.0
            ),
            backend_scope=effective_backend_scope if effective_backend_scope else backend_scope,
            grounding_config_path=grounding_config_path,
            llm_timeout=self.config.agent.llm_timeout,
            capability_profile=profile.name,
            low_latency_enabled=self.config.low_latency.enabled,
            low_latency_profiler_only=self.config.low_latency.profiler_only,
            hard_active_tool_limit=self.config.low_latency.hard_active_tool_limit,
            fast_tool_policy_enabled=(
                self.config.low_latency.fast_tool_policy_enabled
                if low_latency_runtime_enabled
                else False
            ),
            disable_fast_auto_preselection=(
                self.config.low_latency.disable_fast_auto_preselection
                if low_latency_runtime_enabled
                else False
            ),
            disable_turn0_llm_skill_selector=(
                self.config.low_latency.disable_turn0_llm_skill_selector
                if low_latency_runtime_enabled
                else False
            ),
            disable_fast_skill_body_ranking=(
                self.config.low_latency.disable_fast_skill_body_ranking
                if low_latency_runtime_enabled
                else False
            ),
            skill_metadata_only_discovery=(
                self.config.low_latency.skill_metadata_only_discovery
                if low_latency_runtime_enabled
                else False
            ),
            tool_schema_cache_telemetry=self.config.low_latency.tool_schema_cache_telemetry,
            lsp_sync_start=(
                self.config.low_latency.lsp_sync_start
                if low_latency_runtime_enabled
                else True
            ),
            scheduler_sync_start=(
                self.config.low_latency.scheduler_sync_start
                if low_latency_runtime_enabled
                else True
            ),
            scheduler_execute_sync_start=(
                self.config.low_latency.scheduler_execute_sync_start
                if low_latency_runtime_enabled
                else True
            ),
            skill_store_sync_start=not low_latency_runtime_enabled,
            execution_analysis_sync_start=not low_latency_runtime_enabled,
            warm_core=self._warm_core,
        )
        runtime = OpenSpace(openspace_config)
        await runtime.initialize(low_latency_profiler=low_latency_profiler)
        runtime.register_event_sink(
            lambda event_type, data: self._handle_runtime_event(session, event_type, data)
        )
        return runtime

    def _effective_capability_profile_name(self) -> str:
        if self.config.low_latency_runtime_enabled:
            return self.config.low_latency.profile
        return "batch_full"

    def _init_warm_core(self) -> WarmCore | None:
        try:
            return WarmCore.from_config(self.config)
        except Exception:
            logger.debug("WarmCore initialization failed; continuing without it", exc_info=True)
            return None

    def _effective_backend_scope(self, profile: Any) -> Optional[list[str]]:
        if self.config.agent.backend_scope:
            return list(self.config.agent.backend_scope)
        if self.config.low_latency_runtime_enabled:
            return list(getattr(profile, "backend_scope", ()) or ())
        return None

    async def _handle_runtime_event(
        self,
        session: ChannelSession,
        event_type: str,
        data: Dict[str, Any],
    ) -> None:
        if not event_type.startswith("cron_"):
            return
        adapter = self._adapters.get(session.source.platform)
        if adapter is None:
            return
        target, metadata = self._resolve_cron_target(session, data)
        if not target:
            return
        message = self._format_cron_runtime_message(event_type, data)
        if not message:
            return
        send_result = await adapter.send_text(target, message, metadata=metadata)
        if not send_result.success:
            logger.warning(
                "Failed to send %s runtime event for session %s: %s",
                event_type,
                session.session_key,
                send_result.error,
            )

    @staticmethod
    def _resolve_cron_target(session: ChannelSession, data: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        run = data.get("run") if isinstance(data.get("run"), dict) else {}
        target = run.get("notification_target") if isinstance(run, dict) else {}
        identity = target.get("identity") if isinstance(target, dict) else None
        metadata = target.get("metadata") if isinstance(target, dict) else {}
        return str(identity or session.source.chat_id), dict(metadata if isinstance(metadata, dict) else {})

    @staticmethod
    def _format_cron_runtime_message(event_type: str, data: Dict[str, Any]) -> str:
        run = data.get("run") if isinstance(data.get("run"), dict) else {}
        schedule = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
        name = str(schedule.get("name") or schedule.get("id") or "scheduled task")
        payload = run.get("task_payload") if isinstance(run, dict) else {}
        prompt = str((payload or {}).get("prompt") or schedule.get("description") or "").strip()
        if event_type == "cron_notification":
            return f"OpenSpace reminder: {name}\n{prompt}".strip()
        if event_type == "cron_approval_requested":
            approval = data.get("approval") if isinstance(data.get("approval"), dict) else {}
            approval_id = str(approval.get("id") or "")
            suffix = f"\nApproval request: {approval_id}" if approval_id else ""
            return (
                f"OpenSpace scheduled task requires approval: {name}\n"
                f"{prompt}{suffix}"
            ).strip()
        if event_type == "cron_task_started":
            task_id = str(data.get("task_id") or run.get("task_id") or "")
            return f"OpenSpace scheduled task started: {name}" + (f"\nTask ID: {task_id}" if task_id else "")
        if event_type == "cron_task_failed":
            return f"OpenSpace scheduled task failed: {name}\n{data.get('error') or run.get('error') or 'unknown error'}"
        return ""

    def _get_platform_config(self, platform: ChannelPlatform) -> Any:
        if platform == ChannelPlatform.WHATSAPP:
            return self.config.whatsapp
        if platform == ChannelPlatform.FEISHU:
            return self.config.feishu
        raise ValueError(f"Unsupported platform: {platform}")

    @staticmethod
    def _extract_response_text(result: Dict[str, Any]) -> str:
        response = str(result.get("response", "")).strip()
        if response:
            return response
        error = str(result.get("error", "")).strip()
        if error:
            return f"OpenSpace error: {error}"
        return "OpenSpace completed the task but returned no response."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenSpace communication gateway",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to the communication JSON config file",
    )
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="Start the communication gateway")
    run_parser.add_argument(
        "--config",
        dest="command_config",
        type=str,
        help="Path to the communication JSON config file",
    )
    run_parser.add_argument("--host", type=str, default=None)
    run_parser.add_argument("--port", type=int, default=None)
    health_parser = subparsers.add_parser("health", help="Check the running gateway health endpoint")
    health_parser.add_argument(
        "--config",
        dest="command_config",
        type=str,
        help="Path to the communication JSON config file",
    )
    health_parser.add_argument("--host", type=str, default=None)
    health_parser.add_argument("--port", type=int, default=None)
    return parser


async def _run_gateway(
    config_path: Optional[str],
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> int:
    config = load_communication_config(config_path)
    if host is not None or port is not None:
        server_config = config.server.model_copy(
            update={
                key: value
                for key, value in {"host": host, "port": port}.items()
                if value is not None
            }
        )
        config = config.model_copy(update={"server": server_config})
    _configure_ollama_process_env(os.environ.get("OPENSPACE_MODEL", ""))
    gateway = CommunicationGateway(config)
    try:
        await gateway.start()
    except Exception as exc:
        logger.error("Failed to start communication gateway: %s", exc)
        return 1

    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await gateway.stop()
    return 0


def _check_health(config_path: Optional[str], host: Optional[str], port: Optional[int]) -> int:
    config = load_communication_config(config_path)
    url = f"http://{host or config.server.host}:{port or config.server.port}{config.server.health_path}"
    response = requests.get(url, timeout=5)
    response.raise_for_status()
    print(response.text)
    return 0


async def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    config_path = getattr(args, "command_config", None) or args.config
    if command == "health":
        return _check_health(config_path, args.host, args.port)
    return await _run_gateway(
        config_path,
        getattr(args, "host", None),
        getattr(args, "port", None),
    )


def run_main() -> None:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    run_main()
