from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from openspace.entrypoints.tui.resume_controller import (
    _send_bridge_command_result,
    _send_bridge_notification,
    _sync_runtime_status,
    handle_resume_event,
)
from openspace.entrypoints.tui.settings_controller import handle_settings_update
from openspace.entrypoints.tui.slash_controller import handle_slash_command
from openspace.core.background_orchestrator import BackgroundOrchestrator
from openspace.core.background_runtime import BackgroundRuntimeManager
from openspace.core.mcp_interaction import MCPInteraction
from openspace.core.tui_bridge import TUIBridge
from openspace.protocol import CoreToTuiEvent, StreamEvent
from openspace.runtime import ExecutionRequest
from openspace import OpenSpace
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

def _build_tui_args(args) -> list[str]:
    tui_args: list[str] = []
    if getattr(args, "doctor", False):
        tui_args.append("--doctor")
    elif getattr(args, "resume", False):
        tui_args.append("--resume")
    return tui_args


def _suppress_console_logs_for_tui() -> list[tuple[logging.Handler, int]]:
    """Keep file logs enabled while the fullscreen TUI owns stdout."""
    suppressed: list[tuple[logging.Handler, int]] = []
    for logger_name in ("", "openspace"):
        current_logger = logging.getLogger(logger_name)
        for handler in current_logger.handlers:
            if isinstance(handler, logging.FileHandler):
                continue
            if not isinstance(handler, logging.StreamHandler):
                continue
            suppressed.append((handler, handler.level))
            handler.setLevel(logging.CRITICAL + 1)
    return suppressed


def _restore_console_logs(suppressed: list[tuple[logging.Handler, int]]) -> None:
    for handler, level in suppressed:
        handler.setLevel(level)


StdioRedirect = tuple[int, int, int]


def _redirect_stdio_to_file(log_file: str) -> StdioRedirect | None:
    """Capture direct stdout/stderr writes while the fullscreen TUI owns the tty."""
    saved_stdout: int | None = None
    saved_stderr: int | None = None
    log_fd: int | None = None
    try:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        return (saved_stdout, saved_stderr, log_fd)
    except Exception:
        for fd in (saved_stdout, saved_stderr, log_fd):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        logger.debug("Failed to redirect TUI stdio to %s", log_file, exc_info=True)
        return None


def _restore_stdio_redirect(redirect: StdioRedirect | None) -> None:
    if redirect is None:
        return
    saved_stdout, saved_stderr, log_fd = redirect
    try:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
    finally:
        for fd in (saved_stdout, saved_stderr, log_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _session_start_source_for_slash_command(command: str) -> str | None:
    normalized = command.lstrip("/").lower()
    if normalized in {"resume", "load"}:
        return "resume"
    if normalized == "compact":
        return "compact"
    return None


async def _handle_agent_input_event(
    background_runtime: BackgroundRuntimeManager,
    payload: dict[str, Any],
    bridge: TUIBridge,
) -> None:
    normalized_payload = dict(payload) if isinstance(payload, dict) else {}
    if "text" in normalized_payload and "content" not in normalized_payload:
        normalized_payload["content"] = normalized_payload.get("text")

    try:
        await background_runtime.dispatch("agent_input", normalized_payload)
    except Exception as exc:  # noqa: BLE001
        await _send_bridge_notification(
            bridge,
            "warn",
            "Agent Input",
            str(exc),
        )


async def _handle_background_control_event(
    openspace: OpenSpace,
    background_runtime: BackgroundRuntimeManager,
    payload: dict[str, Any],
    bridge: TUIBridge,
) -> None:
    normalized_payload = dict(payload) if isinstance(payload, dict) else {}
    action = str(normalized_payload.get("action", "")).strip().lower()
    if action == "background_all_foreground_tasks":
        backgrounded = await openspace.background_all_foreground_tasks()
        await _send_bridge_command_result(
            bridge,
            "background",
            (
                f"Backgrounded {len(backgrounded)} foreground task"
                f"{'' if len(backgrounded) == 1 else 's'}"
                if backgrounded
                else "No foreground tasks to background"
            ),
        )
        return
    if action == "start":
        title = str(normalized_payload.get("title") or "").strip()
        if not title:
            try:
                prompt_response = await bridge.request_prompt(
                    f"background-title-{int(asyncio.get_running_loop().time() * 1000)}",
                    title="Name Background Session",
                    description="Provide a short label for this background session.",
                    placeholder="Investigate login regression",
                    default_value="",
                    multiline=False,
                )
            except Exception as exc:  # noqa: BLE001
                await _send_bridge_notification(
                    bridge,
                    "warn",
                    "Background Control",
                    f"Background start cancelled: {exc}",
                )
                await _send_bridge_command_result(
                    bridge,
                    "background",
                    "Background start cancelled",
                )
                return

            if prompt_response.get("decision") != "submit":
                await _send_bridge_command_result(
                    bridge,
                    "background",
                    "Background start cancelled",
                )
                return

            prompted_title = str(prompt_response.get("value") or "").strip()
            if prompted_title:
                normalized_payload["title"] = prompted_title

        metadata: dict[str, Any] = {}
        if normalized_payload.get("task_id"):
            metadata["task_id"] = normalized_payload.get("task_id")
        if normalized_payload.get("session_id"):
            metadata["requested_session_id"] = normalized_payload.get("session_id")
        if normalized_payload.get("title"):
            metadata["title"] = normalized_payload.get("title")
        if metadata:
            normalized_payload["metadata"] = metadata

    try:
        await background_runtime.dispatch("background_control", normalized_payload)
    except Exception as exc:  # noqa: BLE001
        await _send_bridge_notification(
            bridge,
            "warn",
            "Background Control",
            str(exc),
        )


async def tui_mode(
    openspace: OpenSpace,
    args,
    *,
    console_log_handlers: list[tuple[logging.Handler, int]] | None = None,
) -> None:
    console_log_handlers = (
        console_log_handlers
        if console_log_handlers is not None
        else _suppress_console_logs_for_tui()
    )
    tui_log_file = Logger.ensure_file_logging()
    logger.info("TUI file log enabled: %s", tui_log_file)
    stdio_redirect = _redirect_stdio_to_file(tui_log_file)
    bridge = TUIBridge(tui_args=_build_tui_args(args))
    try:
        await bridge.start()
    except Exception:
        _restore_stdio_redirect(stdio_redirect)
        _restore_console_logs(console_log_handlers)
        raise
    openspace.set_tui_bridge(bridge)

    mcp_interaction = MCPInteraction(bridge)
    mcp_interaction.bind_grounding_client(openspace.get_grounding_client())
    event_queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
    active_session_id: str | None = None
    pending_session_start_source: str | None = None
    current_query_task: asyncio.Task | None = None
    current_query_abort_event: asyncio.Event | None = None

    def _serialize_background_transcript(
        transcript: Any,
        *,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(transcript, list):
            return []

        messages: list[dict[str, Any]] = []
        for index, entry in enumerate(transcript):
            if not isinstance(entry, dict):
                continue
            if agent_id is not None and entry.get("agent_id") != agent_id:
                continue

            content = entry.get("content")
            if isinstance(content, str):
                text = content
                normalized_content: str | list[dict[str, Any]] = content
            else:
                text = str(content) if content is not None else ""
                normalized_content = (
                    [{"type": "text", "text": text}] if text else []
                )

            messages.append(
                {
                    "id": entry.get("entry_id") or f"background-{index}",
                    "role": entry.get("role") or "assistant",
                    "text": text,
                    "content": normalized_content,
                    "timestamp": entry.get("timestamp"),
                    "meta": {
                        "agent_id": entry.get("agent_id"),
                        "kind": entry.get("kind"),
                        "background": True,
                    },
                }
            )

        return messages

    async def _emit_background_snapshot(
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type == "agent_list":
            await bridge.send(
                CoreToTuiEvent.AGENT_LIST.value,
                {
                    "session_id": payload.get("session_id"),
                    "agents": payload.get("agents", []),
                },
            )
            return

        if event_type == "agent_event":
            latest_event = payload.get("latest_event", {})
            if not isinstance(latest_event, dict):
                latest_event = {}
            raw_payload = latest_event.get("payload")
            transformed_payload = (
                raw_payload
                if isinstance(raw_payload, dict)
                else {"value": raw_payload} if raw_payload is not None else {}
            )
            await bridge.send(
                CoreToTuiEvent.AGENT_EVENT.value,
                {
                    "session_id": payload.get("session_id"),
                    "run_index": payload.get("run_index"),
                    "event_count": payload.get("event_count"),
                    "event_id": latest_event.get("event_id"),
                    "agent_id": str(latest_event.get("agent_id") or "primary"),
                    "event": str(latest_event.get("kind") or "update"),
                    "timestamp": latest_event.get("timestamp"),
                    "source": latest_event.get("source"),
                    "payload": transformed_payload,
                },
            )
            return

        if event_type == "agent_transcript":
            transcript = payload.get("transcript", [])
            agent_ids: list[str] = []
            if isinstance(transcript, list):
                seen_agent_ids: set[str] = set()
                for entry in transcript:
                    if not isinstance(entry, dict):
                        continue
                    current_agent_id = entry.get("agent_id")
                    if (
                        isinstance(current_agent_id, str)
                        and current_agent_id.strip()
                        and current_agent_id not in seen_agent_ids
                    ):
                        seen_agent_ids.add(current_agent_id)
                        agent_ids.append(current_agent_id)

            if not agent_ids:
                agent_ids = ["primary"]

            for current_agent_id in agent_ids:
                await bridge.send(
                    CoreToTuiEvent.AGENT_TRANSCRIPT.value,
                    {
                        "session_id": payload.get("session_id"),
                        "agent_id": current_agent_id,
                        "messages": _serialize_background_transcript(
                            transcript,
                            agent_id=current_agent_id,
                        ),
                    },
                )
            return

        if event_type == "status_update":
            await bridge.send(CoreToTuiEvent.STATUS_UPDATE.value, payload)
            return

        if event_type == "background_session_update":
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            await bridge.send(
                CoreToTuiEvent.BACKGROUND_SESSION_UPDATE.value,
                {
                    "session_id": payload.get("session_id"),
                    "task_id": metadata.get("task_id"),
                    "status": payload.get("status"),
                    "title": payload.get("title"),
                    "run_index": payload.get("run_index"),
                    "created_at": payload.get("created_at"),
                    "started_at": payload.get("started_at"),
                    "finished_at": payload.get("finished_at"),
                    "updated_at": payload.get("updated_at"),
                    "primary_agent_id": payload.get("primary_agent_id"),
                    "active_agent_id": payload.get("active_agent_id"),
                    "agent_count": payload.get("agent_count"),
                    "event_count": payload.get("event_count"),
                    "transcript_count": payload.get("transcript_count"),
                    "metadata": metadata,
                },
            )

    background_runtime = BackgroundRuntimeManager(
        emit_callback=_emit_background_snapshot,
    )
    background_orchestrator = BackgroundOrchestrator(openspace=openspace)
    background_runtime.bind_runtime(background_orchestrator)

    async def _pump_bridge_events() -> None:
        try:
            async for event in bridge.receive():
                await event_queue.put(event)
        finally:
            await event_queue.put(None)

    async def _run_query(text: str, attachments: list[str] | None = None) -> None:
        nonlocal active_session_id
        nonlocal current_query_task
        nonlocal current_query_abort_event
        nonlocal pending_session_start_source

        abort_event = asyncio.Event()
        current_query_abort_event = abort_event
        context = {"session_id": active_session_id} if active_session_id else {}
        if pending_session_start_source:
            context["session_start_source"] = pending_session_start_source
            pending_session_start_source = None
        if attachments:
            context["attachments"] = attachments
            normalized_attachments = []
            for attachment in attachments:
                if not isinstance(attachment, str) or not attachment.strip():
                    continue
                normalized_attachments.append(
                    {
                        "path": attachment,
                        "kind": "file",
                        "name": os.path.basename(attachment),
                    }
                )
            if normalized_attachments:
                channel_context = context.get("channel_context", {})
                if not isinstance(channel_context, dict):
                    channel_context = {}
                channel_context["attachments"] = normalized_attachments
                context["channel_context"] = channel_context

        terminal_phase = "query_complete"
        try:
            await openspace.execute(
                ExecutionRequest(
                    prompt=text,
                    context=context or {},
                    abort_event=abort_event,
                )
            )
            active_session_id = openspace.current_session_id
        except asyncio.CancelledError:
            terminal_phase = "query_cancelled"
            logger.info("TUI query cancelled")
            await _send_bridge_notification(
                bridge,
                "warn",
                "Cancelled",
                "Query cancelled",
            )
        except Exception as exc:  # noqa: BLE001
            terminal_phase = "query_error"
            logger.error("TUI query failed: %s", exc, exc_info=True)
            await _send_bridge_notification(
                bridge,
                "error",
                "Query Failed",
                str(exc),
            )
        finally:
            if current_query_abort_event is abort_event:
                current_query_abort_event = None
            current_query_task = None
            await bridge.send(
                "status_update",
                {
                    "phase": terminal_phase,
                    "session_id": active_session_id,
                },
            )

    reader_task = asyncio.create_task(_pump_bridge_events())
    stderr_task = asyncio.create_task(bridge.drain_stderr())

    try:
        await _sync_runtime_status(openspace, bridge)
        await mcp_interaction.emit_status_snapshot()
        await background_runtime.handle_background_control({"action": "status"})

        if getattr(args, "resume", False):
            resumed = await handle_resume_event(
                openspace,
                bridge,
                {"action": "list"},
            )
            if resumed is not None:
                active_session_id = resumed
                pending_session_start_source = "resume"

        while True:
            event = await event_queue.get()
            if event is None:
                break

            if event.type == "query":
                if current_query_task is not None and not current_query_task.done():
                    await _send_bridge_notification(
                        bridge,
                        "warn",
                        "Busy",
                        "A query is already running",
                    )
                    continue

                text = str(event.data.get("text", "")).strip()
                if not text:
                    continue

                attachments = event.data.get("attachments")
                current_query_task = asyncio.create_task(
                    _run_query(
                        text,
                        attachments if isinstance(attachments, list) else None,
                    )
                )
                continue

            if event.type == "cancel":
                if current_query_task is not None and not current_query_task.done():
                    if current_query_abort_event is not None:
                        current_query_abort_event.set()
                    current_query_task.cancel()
                continue

            if event.type == "tui_unavailable":
                message = str(
                    event.data.get("message")
                    or "The TUI could not attach to an interactive terminal."
                )
                raise RuntimeError(message)

            if event.type == "permission_response":
                # Resolved inside TUIBridge.receive(); nothing to route here.
                continue

            if event.type == "tool_permission_response":
                # Resolved inside TUIBridge.receive(); nothing to route here.
                continue

            if event.type == "prompt_response":
                # Resolved inside TUIBridge.receive(); nothing to route here.
                continue

            if event.type == "slash_command":
                command = str(event.data.get("command", "")).lstrip("/").lower()
                if current_query_task is not None and not current_query_task.done():
                    if command not in {"cost", "effort"}:
                        await _send_bridge_notification(
                            bridge,
                            "warn",
                            "Busy",
                            "Wait for the active query to finish first",
                        )
                        continue

                resumed = await handle_slash_command(openspace, bridge, event.data)
                if resumed is not None:
                    active_session_id = resumed
                    pending_session_start_source = (
                        _session_start_source_for_slash_command(command)
                    )
                continue

            if event.type == "resume_session":
                if current_query_task is not None and not current_query_task.done():
                    await _send_bridge_notification(
                        bridge,
                        "warn",
                        "Busy",
                        "Wait for the active query to finish before changing sessions",
                    )
                    continue
                resumed = await handle_resume_event(openspace, bridge, event.data)
                if resumed is not None:
                    active_session_id = resumed
                    pending_session_start_source = "resume"
                continue

            if event.type == "settings_update":
                await handle_settings_update(openspace, bridge, event.data)
                continue

            if event.type == "mcp_reconnect":
                server_name = str(event.data.get("server_name", "")).strip()
                if server_name:
                    await mcp_interaction.reconnect(server_name)
                    await mcp_interaction.emit_status_snapshot()
                continue

            if event.type == "elicitation_response":
                elicitation_id = str(event.data.get("elicitation_id", "")).strip()
                values = event.data.get("values", {})
                if elicitation_id:
                    mcp_interaction.receive_elicitation_response(
                        elicitation_id,
                        values if isinstance(values, dict) else {},
                    )
                continue

            if event.type == "agent_input":
                payload = dict(event.data) if isinstance(event.data, dict) else {}
                await _handle_agent_input_event(
                    background_runtime,
                    payload,
                    bridge,
                )
                continue

            if event.type == "background_control":
                payload = dict(event.data) if isinstance(event.data, dict) else {}
                await _handle_background_control_event(
                    openspace,
                    background_runtime,
                    payload,
                    bridge,
                )
                continue

    except KeyboardInterrupt:
        await bridge.cancel()
        await bridge.shutdown()
        raise
    finally:
        if current_query_task is not None and not current_query_task.done():
            if current_query_abort_event is not None:
                current_query_abort_event.set()
            current_query_task.cancel()
            try:
                await current_query_task
            except asyncio.CancelledError:
                pass

        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

        try:
            await bridge.shutdown()
        finally:
            _restore_stdio_redirect(stdio_redirect)
            _restore_console_logs(console_log_handlers)



handle_agent_input_event = _handle_agent_input_event
handle_background_control_event = _handle_background_control_event
