from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from aiohttp import web

from openspace.communication.adapters import FeishuAdapter, WhatsAppAdapter
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
from openspace.tool_layer import OpenSpace, OpenSpaceConfig
from openspace.utils.logging import Logger

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
        self.runtime_manager = SessionRuntimeManager(config, self._create_openspace_runtime)
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
            adapter = self._instantiate_adapter(
                WhatsAppAdapter,
                self.config.whatsapp,
                self.attachment_cache,
                runtime_dir=self.config.data_path,
                poll_interval_seconds=self.config.sessions.whatsapp_poll_interval_seconds,
            )
            adapter.set_message_handler(self.handle_message)
            adapters[ChannelPlatform.WHATSAPP] = adapter
        if self.config.feishu.enabled:
            adapter = self._instantiate_adapter(
                FeishuAdapter,
                self.config.feishu,
                self.attachment_cache,
                runtime_dir=self.config.data_path,
            )
            adapter.set_message_handler(self.handle_message)
            adapters[ChannelPlatform.FEISHU] = adapter
        self._adapters = adapters

    @staticmethod
    def _instantiate_adapter(adapter_cls: Any, *args: Any, **kwargs: Any) -> BaseChannelAdapter:
        try:
            return adapter_cls(*args, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            compatibility_kwargs = dict(kwargs)
            compatibility_kwargs.pop("runtime_dir", None)
            return adapter_cls(*args, **compatibility_kwargs)

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

        result = await self.runtime_manager.execute_turn(
            session=session,
            message=message,
            conversation_history=history,
            channel_context=message.to_channel_context(session.session_key),
        )
        response_text = self._extract_response_text(result)

        adapter = self._adapters.get(message.source.platform)
        if adapter is None:
            raise RuntimeError(f"No adapter registered for {message.source.platform.value}")

        send_result = await adapter.send_text(
            message.source.chat_id,
            response_text,
            reply_to_message_id=message.message_id,
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
            },
        )

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
            }
        )

    async def _create_openspace_runtime(self, session: ChannelSession) -> OpenSpace:
        load_runtime_env()
        env_model = os.environ.get("OPENSPACE_MODEL", "")
        model, llm_kwargs = build_llm_kwargs(env_model)
        llm_kwargs = dict(llm_kwargs)
        if model.lower().startswith("ollama/"):
            llm_kwargs["api_base"] = os.environ.get("OLLAMA_API_BASE", "").strip() or "http://127.0.0.1:11434"
            llm_kwargs["api_key"] = os.environ.get("OLLAMA_API_KEY", "").strip() or llm_kwargs.get("api_key") or "ollama"
            llm_kwargs.pop("extra_headers", None)
        backend_scope = self.config.agent.backend_scope
        grounding_config_path = (
            self.config.agent.grounding_config_path
            or build_grounding_config_path()
        )
        recording_dir = self.config.data_path / "recordings"
        openspace_config = OpenSpaceConfig(
            llm_model=model,
            llm_kwargs=llm_kwargs,
            workspace_dir=session.workspace_dir,
            grounding_max_iterations=self.config.agent.max_iterations,
            enable_recording=self.config.agent.enable_recording,
            recording_backends=self.config.agent.recording_backends,
            recording_log_dir=str(recording_dir),
            backend_scope=backend_scope,
            grounding_config_path=grounding_config_path,
            llm_timeout=self.config.agent.llm_timeout,
        )
        runtime = OpenSpace(openspace_config)
        await runtime.initialize()
        return runtime

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
        type=str,
        help="Path to the communication JSON config file",
    )
    health_parser = subparsers.add_parser("health", help="Check the running gateway health endpoint")
    health_parser.add_argument(
        "--config",
        type=str,
        help="Path to the communication JSON config file",
    )
    health_parser.add_argument("--host", type=str, default=None)
    health_parser.add_argument("--port", type=int, default=None)
    return parser


async def _run_gateway(config_path: Optional[str]) -> int:
    config = load_communication_config(config_path)
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
    if command == "health":
        return _check_health(args.config, args.host, args.port)
    return await _run_gateway(args.config)


def run_main() -> None:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    run_main()
