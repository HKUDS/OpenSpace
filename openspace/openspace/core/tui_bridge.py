"""
IPC bridge between Python Core (this process) and TS TUI (child process).

Communication is bidirectional NDJSON over the child's stdin/stdout.
The Python side is the *parent*: it spawns the TS TUI via
``asyncio.create_subprocess_exec`` and talks through pipes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Any, AsyncGenerator

from openspace.protocol import CoreToTuiEvent, StreamEvent

logger = logging.getLogger(__name__)

# U+2028 / U+2029 must be escaped in NDJSON to prevent line-split issues
_JS_LINE_TERMINATORS = str.maketrans({"\u2028": "\\u2028", "\u2029": "\\u2029"})


def _ndjson_dumps(obj: Any) -> str:
    """Serialize *obj* to a single NDJSON line, safe for JS receivers."""
    return json.dumps(obj, ensure_ascii=False).translate(_JS_LINE_TERMINATORS)


class TUIBridge:
    """Manages the lifecycle of the TS TUI child process and IPC messaging."""

    def __init__(
        self,
        tui_entry: str | None = None,
        node_bin: str | None = None,
        tui_args: list[str] | None = None,
    ) -> None:
        self._tui_entry = tui_entry or self._default_tui_entry()
        self._node_bin = node_bin or self._find_node()
        self._tui_args = list(tui_args or [])
        self._process: asyncio.subprocess.Process | None = None
        self._pending_permissions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_prompts: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tool_permission_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled = False
        self._shutdown_event = asyncio.Event()

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the TUI child process."""
        if not os.path.exists(self._tui_entry):
            searched = "\n".join(
                f"  - {path}" for path in self.default_tui_entry_candidates()
            )
            raise FileNotFoundError(
                f"TUI entry not found: {self._tui_entry}. "
                f"{self.default_tui_missing_hint()}\n"
                "Searched default locations:\n"
                f"{searched}"
            )

        self._shutdown_event.clear()
        env = dict(os.environ)
        env["OPENSPACE_TUI_IPC"] = "1"
        self._process = await asyncio.create_subprocess_exec(
            self._node_bin,
            self._tui_entry,
            *self._tui_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        logger.info("TUI process started (pid=%s)", self._process.pid)

    async def shutdown(self) -> None:
        """Gracefully shut down the TUI child process."""
        self._shutdown_event.set()
        active_tool_permission_ids = self._active_tool_permission_ask_ids()
        tool_permission_tasks = self._reject_active_tool_permission_asks(
            "TUI bridge shut down"
        )
        self._reject_pending_permissions("TUI bridge shut down")
        self._reject_pending_prompts("TUI bridge shut down")
        await self._notify_tool_permission_cancelled(
            active_tool_permission_ids,
            "TUI bridge shut down",
        )
        if tool_permission_tasks:
            await asyncio.gather(*tool_permission_tasks, return_exceptions=True)
        proc = self._process
        if proc is None:
            return

        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("TUI process did not exit in time, killing")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

        logger.info("TUI process exited (code=%s)", proc.returncode)
        self._process = None

    # ── Sending (Core → TUI) ────────────────────────────────────

    async def send(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Send an IPC event to the TUI child process."""
        await self.send_event(StreamEvent(type=event_type, data=data or {}))

    async def send_event(self, event: StreamEvent) -> None:
        """Send a pre-built StreamEvent."""
        if event.type == "tool_permission_ask":
            await self._forward_tool_permission_ask(event)
            return
        if event.type == "tool_permission_cancel":
            tool_use_id = str(event.data.get("tool_use_id") or "").strip()
            if tool_use_id:
                self._cancel_tool_permission_prompt(
                    tool_use_id,
                    str(event.data.get("reason") or "Permission prompt cancelled."),
                )
            await self._write_event_to_child(event)
            return

        await self._write_event_to_child(event)

    async def _write_event_to_child(self, event: StreamEvent) -> None:
        """Write an event directly to the child TUI process when available."""
        proc = self._process
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            logger.debug("send() skipped: TUI process not available")
            return

        line = _ndjson_dumps(event.to_dict()) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()

    # ── Receiving (TUI → Core) ───────────────────────────────────

    async def receive(self) -> AsyncGenerator[StreamEvent, None]:
        """Async generator that yields events from the TUI child process."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return

        while not self._shutdown_event.is_set():
            try:
                raw_line = await proc.stdout.readline()
            except (asyncio.CancelledError, ConnectionError):
                break

            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON line from TUI: %.100s", line)
                continue

            event = StreamEvent.from_dict(parsed)

            if event.type == "permission_response":
                self._handle_permission_response(event)
            elif event.type == "tool_permission_response":
                self._handle_tool_permission_response(event)
            elif event.type == "prompt_response":
                self._handle_prompt_response(event)
            elif event.type == "cancel":
                self._cancelled = True
                self._reject_active_tool_permission_asks("Cancelled by TUI")
                self._reject_pending_permissions("Cancelled by TUI")
                self._reject_pending_prompts("Cancelled by TUI")

            yield event

        if self._active_tool_permission_ask_ids():
            self._reject_active_tool_permission_asks(
                "TUI process exited before permission prompt completed"
            )
        if self._pending_permissions:
            self._reject_pending_permissions(
                "TUI process exited before permission responses were received"
            )
        if self._pending_prompts:
            self._reject_pending_prompts(
                "TUI process exited before prompt responses were received"
            )

    # ── Permission round-trip ────────────────────────────────────

    async def request_permission(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        risk_level: str = "medium",
        description: str | None = None,
        request_kind: str | None = None,
        host: str | None = None,
        origin: str | None = None,
        agent_id: str | None = None,
        agent_name: str | None = None,
        agent_color: str | None = None,
        allow_always_pattern: str | None = None,
    ) -> dict[str, Any]:
        """
        Send a permission_request to TUI and wait for the user's decision.

        Returns the permission_response data dict with keys:
        ``tool_use_id``, ``decision``, and optionally ``pattern``.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_permissions[tool_use_id] = future

        payload = {
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "risk_level": risk_level,
            "description": description or f"Allow {tool_name}?",
            "request_kind": request_kind or "tool",
            "origin": origin or "primary",
        }
        if host is not None:
            payload["host"] = host
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if agent_name is not None:
            payload["agent_name"] = agent_name
        if agent_color is not None:
            payload["agent_color"] = agent_color
        if allow_always_pattern is not None:
            payload["allow_always_pattern"] = allow_always_pattern

        await self.send(
            CoreToTuiEvent.PERMISSION_REQUEST.value,
            payload,
        )

        try:
            return await future
        finally:
            self._pending_permissions.pop(tool_use_id, None)

    def _handle_permission_response(self, event: StreamEvent) -> None:
        tool_use_id = event.data.get("tool_use_id")
        if not tool_use_id:
            return
        future = self._pending_permissions.get(tool_use_id)
        if future and not future.done():
            future.set_result(event.data)

    def _handle_tool_permission_response(self, event: StreamEvent) -> None:
        if not isinstance(event.data, dict):
            self._fail_malformed_tool_permission_response(
                "Malformed tool_permission_response had non-object data."
            )
            return

        tool_use_id = str(event.data.get("tool_use_id") or "").strip()
        if not tool_use_id:
            self._fail_malformed_tool_permission_response(
                "Malformed tool_permission_response missing tool_use_id."
            )
            return

        if (
            tool_use_id not in self._tool_permission_tasks
            and not self._is_tool_permission_ask_pending(tool_use_id)
        ):
            self._fail_malformed_tool_permission_response(
                f"Malformed tool_permission_response referenced unknown tool_use_id={tool_use_id!r}.",
                preferred_tool_use_id=tool_use_id,
            )
            return

        self._resolve_tool_permission_ask(tool_use_id, dict(event.data))
        self._cancel_tool_permission_task(tool_use_id)

    def _reject_pending_permissions(self, reason: str) -> None:
        for tool_use_id, future in list(self._pending_permissions.items()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
            self._pending_permissions.pop(tool_use_id, None)

    async def request_prompt(
        self,
        prompt_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        default_value: str | None = None,
        placeholder: str | None = None,
        multiline: bool = False,
    ) -> dict[str, Any]:
        """Send a prompt_request to TUI and wait for the user's response."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_prompts[prompt_id] = future

        await self.send(
            CoreToTuiEvent.PROMPT_REQUEST.value,
            {
                "prompt_id": prompt_id,
                "title": title,
                "description": description,
                "default_value": default_value,
                "placeholder": placeholder,
                "multiline": multiline,
            },
        )

        try:
            return await future
        finally:
            self._pending_prompts.pop(prompt_id, None)

    def _handle_prompt_response(self, event: StreamEvent) -> None:
        prompt_id = event.data.get("prompt_id")
        if not prompt_id:
            return
        future = self._pending_prompts.get(prompt_id)
        if future and not future.done():
            future.set_result(event.data)

    def _reject_pending_prompts(self, reason: str) -> None:
        for prompt_id, future in list(self._pending_prompts.items()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
            self._pending_prompts.pop(prompt_id, None)

    def _cancel_tool_permission_prompt(
        self,
        tool_use_id: str,
        reason: str,
    ) -> asyncio.Task[None] | None:
        logger.debug("Cancelling tool permission prompt %s: %s", tool_use_id, reason)
        return self._cancel_tool_permission_task(tool_use_id)

    def _cancel_tool_permission_prompt_waiters(self, tool_use_id: str) -> None:
        prompt_ids = {
            self._tool_permission_prompt_id(tool_use_id, "choice"),
            self._tool_permission_prompt_id(tool_use_id, "edit"),
        }
        for prompt_id, future in list(self._pending_prompts.items()):
            is_ask_prompt = (
                prompt_id.startswith(
                    self._tool_permission_prompt_id(tool_use_id, "ask-")
                )
                or prompt_id.startswith(
                    self._tool_permission_prompt_id(tool_use_id, "ask-other-")
                )
            )
            if prompt_id not in prompt_ids and not is_ask_prompt:
                continue
            if not future.done():
                future.cancel()
            self._pending_prompts.pop(prompt_id, None)

    def _fail_malformed_tool_permission_response(
        self,
        message: str,
        *,
        preferred_tool_use_id: str | None = None,
    ) -> None:
        active_ids = self._active_tool_permission_ask_ids()

        # Deterministic fail-closed policy for malformed permission responses:
        # if a usable tool_use_id maps to exactly one live ask, deny only that
        # ask so unrelated concurrent prompts survive. If the malformed event
        # cannot be tied to one ask, deny every active ask instead of letting
        # any wait for the full permission timeout.
        if preferred_tool_use_id and preferred_tool_use_id in active_ids:
            target_ids = [preferred_tool_use_id]
        else:
            target_ids = active_ids

        if not target_ids:
            logger.warning("%s", message)
            return

        logger.warning("%s", message)
        for pending_tool_use_id in target_ids:
            self._resolve_tool_permission_ask(
                pending_tool_use_id,
                {
                    "option_id": "deny",
                    "message": message,
                },
            )
            self._cancel_tool_permission_task(pending_tool_use_id)

    def _active_tool_permission_ask_ids(self) -> list[str]:
        from openspace.tool_runtime.permissions import pending_permission_ask_ids

        active_ids = list(self._tool_permission_tasks)
        for tool_use_id in pending_permission_ask_ids():
            if tool_use_id not in active_ids:
                active_ids.append(tool_use_id)
        return active_ids

    def _start_tool_permission_prompt(self, event: StreamEvent) -> None:
        tool_use_id = str(event.data.get("tool_use_id") or "").strip()
        if not tool_use_id:
            logger.warning("Ignoring tool_permission_ask without tool_use_id")
            return
        permission_ask_id = str(event.data.get("permission_ask_id") or "").strip()

        proc = self._process
        if (
            self._shutdown_event.is_set()
            or proc is None
            or proc.stdin is None
            or proc.stdin.is_closing()
        ):
            self._resolve_tool_permission_ask(
                tool_use_id,
                {
                    "option_id": "deny",
                    "permission_ask_id": permission_ask_id,
                    "message": "Interactive permission prompt unavailable because the TUI bridge is not running.",
                },
            )
            return

        existing = self._tool_permission_tasks.get(tool_use_id)
        if existing is not None and not existing.done():
            self._resolve_tool_permission_ask(
                tool_use_id,
                {
                    "option_id": "deny",
                    "permission_ask_id": permission_ask_id,
                    "message": "Permission prompt was superseded by a newer request.",
                },
            )
            self._cancel_tool_permission_task(tool_use_id)

        task = asyncio.create_task(self._drive_tool_permission_prompt(dict(event.data)))
        self._tool_permission_tasks[tool_use_id] = task

        def _discard(done_task: asyncio.Task[None], ask_id: str = tool_use_id) -> None:
            if self._tool_permission_tasks.get(ask_id) is done_task:
                self._tool_permission_tasks.pop(ask_id, None)

        task.add_done_callback(_discard)

    async def _forward_tool_permission_ask(self, event: StreamEvent) -> None:
        tool_use_id = str(event.data.get("tool_use_id") or "").strip()
        if not tool_use_id:
            logger.warning("Ignoring tool_permission_ask without tool_use_id")
            return
        permission_ask_id = str(event.data.get("permission_ask_id") or "").strip()

        proc = self._process
        if (
            self._shutdown_event.is_set()
            or proc is None
            or proc.stdin is None
            or proc.stdin.is_closing()
        ):
            self._resolve_tool_permission_ask(
                tool_use_id,
                {
                    "option_id": "deny",
                    "permission_ask_id": permission_ask_id,
                    "message": "Interactive permission prompt unavailable because the TUI bridge is not running.",
                },
            )
            return

        payload = dict(event.data)
        payload.setdefault("response_channel", "tool_permission_response")
        payload.setdefault("request_kind", "tool")
        await self._write_event_to_child(
            StreamEvent(type=event.type, data=payload)
        )

    async def _drive_tool_permission_prompt(self, payload: dict[str, Any]) -> None:
        tool_use_id = str(payload.get("tool_use_id") or "").strip()
        if not tool_use_id:
            return
        permission_ask_id = str(payload.get("permission_ask_id") or "").strip()

        try:
            response = await self._collect_tool_permission_response(payload)
        except asyncio.CancelledError:
            self._resolve_tool_permission_ask(
                tool_use_id,
                {
                    "option_id": "deny",
                    "permission_ask_id": permission_ask_id,
                    "message": "Permission prompt was cancelled.",
                },
            )
            raise
        except Exception as exc:
            logger.warning(
                "Tool permission prompt failed for %s: %s",
                tool_use_id,
                exc,
            )
            response = {
                "option_id": "deny",
                "permission_ask_id": permission_ask_id,
                "message": f"Interactive permission prompt failed: {exc}",
            }

        if permission_ask_id:
            response.setdefault("permission_ask_id", permission_ask_id)
        self._resolve_tool_permission_ask(tool_use_id, response)

    def _reject_active_tool_permission_asks(
        self,
        reason: str,
    ) -> list[asyncio.Task[None]]:
        task_map = dict(self._tool_permission_tasks)
        active_ids = self._active_tool_permission_ask_ids()
        for tool_use_id in active_ids:
            self._resolve_tool_permission_ask(
                tool_use_id,
                {
                    "option_id": "deny",
                    "message": reason,
                },
            )
            self._cancel_tool_permission_prompt_waiters(tool_use_id)
            task = self._tool_permission_tasks.pop(tool_use_id, None)
            if task is not None and not task.done():
                task.cancel()
        return [task for tool_use_id, task in task_map.items() if tool_use_id in active_ids]

    async def _notify_tool_permission_cancelled(
        self,
        tool_use_ids: list[str],
        reason: str,
    ) -> None:
        """Best-effort child notification to dismiss rendered permission prompts."""
        for tool_use_id in tool_use_ids:
            try:
                await self._write_event_to_child(
                    StreamEvent(
                        type="tool_permission_cancel",
                        data={
                            "tool_use_id": tool_use_id,
                            "reason": reason,
                        },
                    )
                )
            except Exception:
                logger.debug(
                    "Failed to notify TUI about cancelled tool permission prompt %s",
                    tool_use_id,
                    exc_info=True,
                )

    def _cancel_tool_permission_task(
        self,
        tool_use_id: str,
    ) -> asyncio.Task[None] | None:
        self._cancel_tool_permission_prompt_waiters(tool_use_id)
        task = self._tool_permission_tasks.pop(tool_use_id, None)
        if task is not None and not task.done():
            task.cancel()
        return task

    async def _collect_tool_permission_response(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        tool_use_id = str(payload.get("tool_use_id") or "").strip()
        tool_name = str(payload.get("tool_name") or "tool").strip() or "tool"
        tool_input = payload.get("tool_input")
        if payload.get("interaction") == "ask_user_question":
            return await self._collect_ask_user_question_response(payload)

        options_raw = payload.get("options")
        options = [
            option
            for option in options_raw
            if isinstance(option, dict) and option.get("option_id")
        ] if isinstance(options_raw, list) else []

        if not options:
            options = [
                {"option_id": "allow_once", "label": "Allow once"},
                {"option_id": "deny", "label": "Deny"},
            ]

        choice_error: str | None = None
        while self._is_tool_permission_ask_pending(tool_use_id):
            prompt_response = await self.request_prompt(
                self._tool_permission_prompt_id(tool_use_id, "choice"),
                title=f"Permission Required: {tool_name}",
                description=self._build_tool_permission_choice_description(
                    payload,
                    options,
                    choice_error,
                ),
                default_value="1",
                placeholder="Enter option number or name",
                multiline=False,
            )
            if prompt_response.get("decision") != "submit":
                return {
                    "option_id": "deny",
                    "message": "Permission prompt cancelled by user.",
                }

            selection = str(prompt_response.get("value") or "").strip()
            selected_option = self._select_tool_permission_option(selection, options)
            if selected_option is None:
                choice_error = f"Invalid selection: {selection or '<empty>'}"
                continue

            option_id = str(selected_option.get("option_id") or "").strip()
            if option_id != "provide_input":
                return {
                    "option_id": option_id,
                    "suggestion_index": selected_option.get("suggestion_index"),
                }

            edit_error: str | None = None
            while self._is_tool_permission_ask_pending(tool_use_id):
                edit_response = await self.request_prompt(
                    self._tool_permission_prompt_id(tool_use_id, "edit"),
                    title=f"Edit Tool Input: {tool_name}",
                    description=self._build_tool_permission_edit_description(
                        payload,
                        edit_error,
                    ),
                    default_value=self._format_tool_input_json(tool_input),
                    placeholder='{"key": "value"}',
                    multiline=True,
                )
                if edit_response.get("decision") != "submit":
                    return {
                        "option_id": "deny",
                        "message": "Input edit cancelled by user.",
                    }

                try:
                    edited_input = self._parse_edited_tool_input(
                        edit_response.get("value"),
                    )
                except ValueError as exc:
                    edit_error = str(exc)
                    continue

                return {
                    "option_id": "provide_input",
                    "edited_input": edited_input,
                }

        return {"option_id": "deny", "message": "Permission prompt expired."}

    async def _collect_ask_user_question_response(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        tool_use_id = str(payload.get("tool_use_id") or "").strip()
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        questions_raw = payload.get("questions")
        if not isinstance(questions_raw, list):
            questions_raw = tool_input.get("questions")
        if not isinstance(questions_raw, list) or not questions_raw:
            return {
                "option_id": "deny",
                "message": "AskUserQuestion payload did not include questions.",
            }

        answers: dict[str, str] = {}
        existing_annotations = tool_input.get("annotations")
        annotations: dict[str, Any] = (
            dict(existing_annotations) if isinstance(existing_annotations, dict) else {}
        )
        total = len(questions_raw)
        for index, question in enumerate(questions_raw, start=1):
            if not isinstance(question, dict):
                return {
                    "option_id": "deny",
                    "message": "AskUserQuestion question payload was malformed.",
                }
            question_text = str(question.get("question") or "").strip()
            options = self._ask_user_question_options(question)
            if not question_text or len(options) < 2:
                return {
                    "option_id": "deny",
                    "message": "AskUserQuestion question payload was incomplete.",
                }

            answer_error: str | None = None
            while self._is_tool_permission_ask_pending(tool_use_id):
                prompt_response = await self.request_prompt(
                    self._tool_permission_prompt_id(tool_use_id, f"ask-{index}"),
                    title=self._ask_user_question_title(question, index, total),
                    description=self._build_ask_user_question_description(
                        question,
                        index,
                        total,
                        answer_error,
                    ),
                    default_value="",
                    placeholder=(
                        "Enter option numbers separated by commas, or custom text"
                        if self._ask_user_question_multi_select(question)
                        else "Enter option number, label, or custom text"
                    ),
                    multiline=False,
                )
                if prompt_response.get("decision") != "submit":
                    return {
                        "option_id": "deny",
                        "message": "Question prompt cancelled by user.",
                    }

                try:
                    answer, annotation = self._parse_ask_user_question_answer(
                        prompt_response.get("value"),
                        question,
                    )
                except ValueError as exc:
                    answer_error = str(exc)
                    continue

                if "__OPENSPACE_OTHER_ANSWER__" in answer:
                    other_response = await self.request_prompt(
                        self._tool_permission_prompt_id(
                            tool_use_id,
                            f"ask-other-{index}",
                        ),
                        title=self._ask_user_question_title(question, index, total),
                        description="Enter a custom answer.",
                        default_value="",
                        placeholder="Custom answer",
                        multiline=False,
                    )
                    if other_response.get("decision") != "submit":
                        return {
                            "option_id": "deny",
                            "message": "Custom answer prompt cancelled by user.",
                        }
                    custom_answer = str(other_response.get("value") or "").strip()
                    if not custom_answer:
                        answer_error = "Custom answer cannot be empty."
                        continue
                    answer = answer.replace(
                        "__OPENSPACE_OTHER_ANSWER__",
                        custom_answer,
                    )

                answers[question_text] = answer
                if annotation:
                    current_annotation = annotations.get(question_text)
                    if isinstance(current_annotation, dict):
                        annotations[question_text] = {
                            **current_annotation,
                            **annotation,
                        }
                    elif current_annotation is not None:
                        annotations[question_text] = {
                            "value": current_annotation,
                            **annotation,
                        }
                    else:
                        annotations[question_text] = annotation
                break

        if len(answers) != total:
            return {"option_id": "deny", "message": "Question prompt expired."}

        updated_input = dict(tool_input)
        updated_input["questions"] = questions_raw
        updated_input["answers"] = answers
        if annotations:
            updated_input["annotations"] = annotations
        return {
            "option_id": "allow_once",
            "updated_input": updated_input,
        }

    def _resolve_tool_permission_ask(
        self,
        tool_use_id: str,
        response: dict[str, Any],
    ) -> None:
        from openspace.tool_runtime.permissions import resolve_permission_ask

        if not resolve_permission_ask(tool_use_id, response):
            logger.debug(
                "tool_permission_ask response dropped for %s; no pending ask",
                tool_use_id,
            )

    @staticmethod
    def _ask_user_question_options(question: dict[str, Any]) -> list[dict[str, Any]]:
        options_raw = question.get("options")
        return [
            option
            for option in options_raw
            if isinstance(option, dict) and option.get("label")
        ] if isinstance(options_raw, list) else []

    @staticmethod
    def _ask_user_question_multi_select(question: dict[str, Any]) -> bool:
        return bool(question.get("multiSelect", question.get("multi_select", False)))

    @staticmethod
    def _ask_user_question_title(
        question: dict[str, Any],
        index: int,
        total: int,
    ) -> str:
        header = str(question.get("header") or "").strip()
        prefix = f"Question {index}/{total}"
        return f"{prefix}: {header}" if header else prefix

    @classmethod
    def _build_ask_user_question_description(
        cls,
        question: dict[str, Any],
        index: int,
        total: int,
        error_message: str | None,
    ) -> str:
        options = cls._ask_user_question_options(question)
        multi_select = cls._ask_user_question_multi_select(question)
        lines: list[str] = []
        if error_message:
            lines.append(error_message)
        lines.append(str(question.get("question") or f"Question {index}/{total}"))
        if multi_select:
            lines.append(
                "Select one or more options by number or label. Use commas "
                "for multiple selections."
            )
        else:
            lines.append(
                "Select one option by number or label. Type custom text for Other."
            )
        for option_index, option in enumerate(options, start=1):
            label = str(option.get("label") or option_index)
            description = str(option.get("description") or "").strip()
            suffix = f" - {description}" if description else ""
            lines.append(f"{option_index}. {label}{suffix}")
            preview = option.get("preview")
            if isinstance(preview, str) and preview.strip():
                lines.append(
                    "   Preview:\n"
                    + cls._indent_preview(cls._truncate_preview(preview.strip()))
                )
        lines.append(f"{len(options) + 1}. Other - Provide a custom answer")
        return "\n".join(lines)

    @classmethod
    def _parse_ask_user_question_answer(
        cls,
        raw_value: Any,
        question: dict[str, Any],
    ) -> tuple[str, dict[str, str] | None]:
        raw_text = str(raw_value or "").strip()
        if not raw_text:
            raise ValueError("Answer cannot be empty.")

        options = cls._ask_user_question_options(question)
        if cls._ask_user_question_multi_select(question):
            parts = [part.strip() for part in raw_text.split(",") if part.strip()]
            if not parts:
                raise ValueError("Answer cannot be empty.")
            labels: list[str] = []
            for part in parts:
                selected = cls._select_ask_user_question_option(part, options)
                if selected == "__other__":
                    labels.append("__OPENSPACE_OTHER_ANSWER__")
                elif isinstance(selected, dict):
                    labels.append(str(selected.get("label") or part))
                else:
                    labels.append(part)
            return ", ".join(labels), None

        selected = cls._select_ask_user_question_option(raw_text, options)
        if selected == "__other__":
            return "__OPENSPACE_OTHER_ANSWER__", None
        if isinstance(selected, dict):
            label = str(selected.get("label") or raw_text)
            preview = selected.get("preview")
            annotation = (
                {"preview": preview}
                if isinstance(preview, str) and preview
                else None
            )
            return label, annotation
        return raw_text, None

    @staticmethod
    def _select_ask_user_question_option(
        selection: str,
        options: list[dict[str, Any]],
    ) -> dict[str, Any] | str | None:
        normalized = selection.strip().lower()
        if normalized.isdigit():
            index = int(normalized) - 1
            if 0 <= index < len(options):
                return options[index]
            if index == len(options):
                return "__other__"
            return None
        if normalized == "other":
            return "__other__"
        for option in options:
            label = str(option.get("label") or "").strip().lower()
            if normalized == label:
                return option
        return None

    @staticmethod
    def _truncate_preview(preview: str, limit: int = 1200) -> str:
        if len(preview) <= limit:
            return preview
        return preview[:limit] + "\n[preview truncated]"

    @staticmethod
    def _indent_preview(preview: str) -> str:
        return "\n".join(f"   {line}" for line in preview.splitlines())

    @staticmethod
    def _tool_permission_prompt_id(tool_use_id: str, stage: str) -> str:
        return f"tool-permission-{tool_use_id}-{stage}"

    @staticmethod
    def _select_tool_permission_option(
        selection: str,
        options: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        normalized = selection.strip().lower()
        if normalized.isdigit():
            index = int(normalized) - 1
            if 0 <= index < len(options):
                return options[index]

        aliases = {
            "allow": "allow_once",
            "always": "allow_always",
            "edit": "provide_input",
            "input": "provide_input",
            "no": "deny",
        }
        normalized = aliases.get(normalized, normalized)
        for option in options:
            option_id = str(option.get("option_id") or "").strip().lower()
            label = str(option.get("label") or "").strip().lower()
            if normalized in {option_id, label}:
                return option
        return None

    @staticmethod
    def _build_tool_permission_choice_description(
        payload: dict[str, Any],
        options: list[dict[str, Any]],
        error_message: str | None,
    ) -> str:
        lines: list[str] = []
        message = str(payload.get("message") or "").strip()
        if message:
            lines.append(message)
        blocked_path = str(payload.get("blocked_path") or "").strip()
        if blocked_path:
            lines.append(f"Blocked path: {blocked_path}")
        if error_message:
            lines.append(error_message)
        lines.append("Choose an option by number or option_id:")
        for index, option in enumerate(options, start=1):
            label = str(option.get("label") or option.get("option_id") or index)
            lines.append(f"{index}. {label}")
        return "\n".join(lines)

    @staticmethod
    def _build_tool_permission_edit_description(
        payload: dict[str, Any],
        error_message: str | None,
    ) -> str:
        lines = [
            "Edit the JSON tool input and submit a JSON object to continue.",
        ]
        message = str(payload.get("message") or "").strip()
        if message:
            lines.insert(0, message)
        if error_message:
            lines.append(error_message)
        return "\n".join(lines)

    @staticmethod
    def _format_tool_input_json(tool_input: Any) -> str:
        return json.dumps(tool_input, ensure_ascii=False, indent=2, sort_keys=True)

    @staticmethod
    def _parse_edited_tool_input(raw_value: Any) -> dict[str, Any]:
        if not isinstance(raw_value, str):
            raise ValueError("Edited input must be submitted as JSON text.")
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Edited input must be a JSON object.")
        return parsed

    @staticmethod
    def _is_tool_permission_ask_pending(tool_use_id: str) -> bool:
        from openspace.tool_runtime.permissions import is_permission_ask_pending

        return is_permission_ask_pending(tool_use_id)

    # ── Cancel / interrupt ───────────────────────────────────────

    async def cancel(self) -> None:
        """Send a cancel event to the TUI (triggered by Ctrl+C in Core)."""
        self._cancelled = True
        active_tool_permission_ids = self._active_tool_permission_ask_ids()
        tool_permission_tasks = self._reject_active_tool_permission_asks(
            "Cancelled by user interrupt"
        )
        self._reject_pending_permissions("Cancelled by user interrupt")
        self._reject_pending_prompts("Cancelled by user interrupt")
        await self._notify_tool_permission_cancelled(
            active_tool_permission_ids,
            "Cancelled by user interrupt",
        )
        if tool_permission_tasks:
            await asyncio.gather(*tool_permission_tasks, return_exceptions=True)
        await self.send("cancel", {"reason": "user_interrupt"})

    def reset_cancel(self) -> None:
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    # ── Stderr monitoring ────────────────────────────────────────

    async def drain_stderr(self) -> None:
        """Read and log TUI stderr until the process exits."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            logger.info("[TUI stderr] %s", line.decode("utf-8", errors="replace").rstrip())

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _default_tui_entry() -> str:
        candidates = TUIBridge.default_tui_entry_candidates()
        for entry in candidates:
            if os.path.exists(entry):
                return entry
        return candidates[0]

    @staticmethod
    def _tui_entry_paths() -> tuple[str, str]:
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        repo_root = os.path.dirname(package_root)
        source_entry = os.path.join(
            repo_root,
            "apps",
            "tui",
            "dist",
            "index.js",
        )
        packaged_entry = os.path.join(
            package_root,
            "packaged",
            "tui",
            "index.js",
        )
        return source_entry, packaged_entry

    @staticmethod
    def _source_checkout_root() -> str:
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.dirname(package_root)

    @staticmethod
    def running_from_source_checkout() -> bool:
        """Return True when the imported package is inside this repo checkout."""
        repo_root = TUIBridge._source_checkout_root()
        return os.path.isfile(
            os.path.join(repo_root, "pyproject.toml")
        ) and os.path.isdir(
            os.path.join(repo_root, "apps", "tui")
        )

    @staticmethod
    def default_tui_entry_candidates() -> list[str]:
        source_entry, packaged_entry = TUIBridge._tui_entry_paths()
        if TUIBridge.running_from_source_checkout():
            return [source_entry]
        return [packaged_entry]

    @staticmethod
    def default_tui_available() -> bool:
        return any(
            os.path.exists(entry)
            for entry in TUIBridge.default_tui_entry_candidates()
        )

    @staticmethod
    def interactive_terminal_available() -> bool:
        """Return whether the TUI can attach to a user terminal.

        The TUI reserves stdin/stdout for NDJSON IPC with Python, so the
        rendered Ink UI must open the controlling terminal separately.
        """
        if os.name == "nt":
            return bool(sys.stdin.isatty() and sys.stderr.isatty())

        try:
            fd = os.open("/dev/tty", os.O_RDWR)
        except OSError:
            return False
        else:
            os.close(fd)
            return True

    @staticmethod
    def default_tui_missing_hint() -> str:
        if TUIBridge.running_from_source_checkout():
            return (
                "Build the source TUI with `npm --prefix apps/tui run build`, "
                "or run without --tui."
            )
        return (
            "The packaged TUI artifact is missing. Reinstall OpenSpace from a "
            "package built with `npm --prefix apps/tui run build:packaged`, or "
            "run without --tui."
        )

    @staticmethod
    def _find_node() -> str:
        node = shutil.which("node")
        if node is None:
            raise FileNotFoundError(
                "Node.js not found on PATH. The TS TUI requires Node.js to run."
            )
        return node
