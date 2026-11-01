import datetime
import json
import ast
import base64
import hashlib
import re
import shutil
from typing import Any, Dict, List, Optional
from pathlib import Path

from openspace.utils.logging import Logger
from .recorder import TrajectoryRecorder
from .action_recorder import ActionRecorder

logger = Logger.get_logger(__name__)

_PERSISTED_OUTPUT_PATH_RE = re.compile(r"(Full output saved to:\s*)[^\n]+")


class RecordingManager:
    # Global instance management (singleton pattern)
    _global_instance: Optional['RecordingManager'] = None
    
    def __init__(
        self,
        enabled: bool = True,
        task_id: str = "",
        log_dir: str = "./logs/recordings",
        backends: Optional[List[str]] = None,
        enable_screenshot: bool = True,
        enable_video: bool = False,
        enable_conversation_log: bool = True,
        auto_save_interval: int = 10,
        server_url: Optional[str] = None,
        agent_name: str = "GroundingAgent",
    ):
        """
        Initialize automatic recording manager
        
        Args:
            enabled: whether to enable recording
            task_id: task ID (for naming recording directory)
            log_dir: log directory path
            backends: list of backends to record (None = all)
                    (optional: "mcp", "gui", "shell", "meta", "web")
            enable_screenshot: whether to enable screenshot (through platform.ScreenshotClient)
            enable_video: whether to enable video recording (through platform.RecordingClient)
            enable_conversation_log: whether to save LLM conversations to conversations.jsonl (default: True)
            auto_save_interval: automatic save interval (steps)
            server_url: local server address (None = read from config/environment variables)
            agent_name: name of the agent performing the recording (default: "GroundingAgent")
        """
        self.enabled = enabled
        self.task_id = task_id
        self.log_dir = log_dir
        self.backends = set(backends) if backends else {"mcp", "gui", "shell", "meta", "web"}
        self.enable_screenshot = enable_screenshot
        self.enable_video = enable_video
        self.enable_conversation_log = enable_conversation_log
        self.auto_save_interval = auto_save_interval
        self.server_url = server_url
        self.agent_name = agent_name
        
        # internal state
        self._recorder: Optional[TrajectoryRecorder] = None
        self._action_recorder: Optional[ActionRecorder] = None
        self._is_started = False
        self._step_counter = 0
        
        # video/screenshot clients (internal management)
        self._recording_client = None
        self._screenshot_client = None
        
        # Register as global instance
        RecordingManager._global_instance = self

    @classmethod
    def is_recording(cls) -> bool:
        """
        Check if there is an active recording session
        
        Returns:
            bool: True if recording is active
        """
        return cls._global_instance is not None and cls._global_instance._is_started
    
    @classmethod
    async def record_retrieved_tools(
        cls,
        task_instruction: str,
        tools: List[Any],
        preselection_debug_info: Optional[Dict[str, Any]] = None,
    ):
        """
        Record the tools retrieved for a task
        
        Args:
            task_instruction: The task instruction used for retrieval
            tools: List of retrieved tools
            preselection_debug_info: Debug info from tool preselection (similarity scores, LLM selections)
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        
        # Extract tool info
        tool_info = []
        for tool in tools:
            info = {
                "name": getattr(tool, "name", str(tool)),
            }
            # Prefer runtime_info.backend
            # over backend_type (may be NOT_SET for cached RemoteTools)
            runtime_info = getattr(tool, "_runtime_info", None)
            if runtime_info and hasattr(runtime_info, "backend"):
                info["backend"] = runtime_info.backend.value if hasattr(runtime_info.backend, "value") else str(runtime_info.backend)
                info["server_name"] = runtime_info.server_name
            elif hasattr(tool, "backend_type"):
                info["backend"] = tool.backend_type.value if hasattr(tool.backend_type, "value") else str(tool.backend_type)
            tool_info.append(info)
        
        # Build metadata
        metadata = {
            "instruction": task_instruction[:500],  # Truncate long instructions
            "count": len(tools),
            "tools": tool_info,
        }
        
        # Add preselection debug info if available
        if preselection_debug_info:
            metadata["preselection_debug"] = {
                "search_mode": preselection_debug_info.get("search_mode", ""),
                "total_candidates": preselection_debug_info.get("total_candidates", 0),
                "deferred_count": preselection_debug_info.get("deferred_count", 0),
                "non_deferred_count": preselection_debug_info.get("non_deferred_count", 0),
                "llm_filter": preselection_debug_info.get("llm_filter", {}),
                "tool_scores": preselection_debug_info.get("tool_scores", []),
            }
        
        # Save to metadata
        await instance._recorder.add_metadata("retrieved_tools", metadata)
        
        logger.info(f"Recorded {len(tools)} retrieved tools (with preselection debug info: {preselection_debug_info is not None})")
    
    @classmethod
    async def record_skill_selection(
        cls,
        selection_record: Dict[str, Any],
    ):
        """
        Record skill selection decision to metadata.json.
        
        This captures the pre-execution skill matching conversation:
        - Which skills were available
        - The LLM prompt and response (or keyword fallback)
        - Which skills were selected
        
        Args:
            selection_record: Structured record from SkillRegistry.select_skills_with_llm()
                Keys: method, task, available_skills, prompt, llm_response, selected, error
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return

        selection_record = cls._merge_skill_selection_record(
            instance._recorder.metadata.get("skill_selection"),
            selection_record,
        )

        # Save to metadata alongside retrieved_tools
        await instance._recorder.add_metadata("skill_selection", selection_record)

        selected = selection_record.get("selected", [])
        method = selection_record.get("method", "unknown")
        logger.info(
            f"Recorded skill selection: {len(selected)} selected via {method} "
            f"(from {len(selection_record.get('available_skills', []))} available)"
        )

    @classmethod
    def record_skill_selection_now(
        cls,
        selection_record: Dict[str, Any],
    ) -> None:
        """Synchronous metadata variant for non-async skill discovery paths."""

        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        selection_record = cls._merge_skill_selection_record(
            instance._recorder.metadata.get("skill_selection"),
            selection_record,
        )
        add_metadata_now = getattr(instance._recorder, "add_metadata_now", None)
        if callable(add_metadata_now):
            add_metadata_now("skill_selection", selection_record)
        else:
            instance._recorder.metadata["skill_selection"] = selection_record
            instance._recorder._save_metadata()

    @staticmethod
    def _merge_skill_selection_record(
        existing: Any,
        current: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Preserve cumulative selected/available skill ids across turns."""

        def _ordered_strings(values: Any) -> list[str]:
            if not isinstance(values, list):
                return []
            return [str(value) for value in values if str(value or "").strip()]

        if not isinstance(existing, dict):
            return dict(current or {})

        merged = dict(existing)
        merged.update(dict(current or {}))

        for key in ("selected", "available_skills", "filtered_out"):
            merged[key] = list(
                dict.fromkeys(
                    _ordered_strings(existing.get(key))
                    + _ordered_strings((current or {}).get(key))
                )
            )

        history = list(existing.get("selection_history") or [])
        compact_current = {
            key: (current or {}).get(key)
            for key in (
                "method",
                "source",
                "task",
                "selected",
                "available_skills",
                "filtered_out",
                "error",
            )
            if key in (current or {})
        }
        if compact_current:
            history.append(compact_current)
            merged["selection_history"] = history[-20:]

        return merged

    @staticmethod
    def _truncate_messages(
        messages: List[Dict[str, Any]],
        max_content_length: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Truncate message content to avoid huge log files."""
        result = []
        for msg in messages:
            msg = RecordingManager._archive_message_persisted_result(msg)
            new_msg = {"role": msg.get("role", "unknown")}
            content = msg.get("content", "")

            if isinstance(content, str):
                if len(content) > max_content_length:
                    new_msg["content"] = content[:max_content_length] + f"... [truncated, total {len(content)} chars]"
                else:
                    new_msg["content"] = content
            elif isinstance(content, list):
                new_msg["content"] = [
                    RecordingManager._sanitize_content_block(item, max_content_length)
                    for item in content
                ]
            else:
                new_msg["content"] = str(content)[:max_content_length]

            if "tool_calls" in msg:
                new_msg["tool_calls"] = msg["tool_calls"]
            if "tool_call_id" in msg:
                new_msg["tool_call_id"] = msg["tool_call_id"]
            if "name" in msg:
                new_msg["name"] = msg["name"]
            if "_meta" in msg:
                meta = RecordingManager._sanitize_message_meta(
                    msg.get("_meta"),
                    max_content_length,
                )
                if meta:
                    new_msg["_meta"] = meta

            result.append(new_msg)
        return result

    @staticmethod
    def _sanitize_message_meta(
        meta: Any,
        max_content_length: int,
    ) -> Dict[str, Any]:
        """Keep compact metadata needed for analysis and quality attribution."""

        if not isinstance(meta, dict):
            return {}
        sanitized: Dict[str, Any] = {}
        for key in (
            "type",
            "attachment_type",
            "uuid",
            "timestamp",
            "tool_name",
            "tool_call_id",
            "status",
            "execution_time",
            "truncated_chars",
            "has_multimodal_content",
        ):
            if key in meta:
                sanitized[key] = meta[key]

        attachment = meta.get("attachment")
        if isinstance(attachment, dict):
            attachment_type = attachment.get("type")
            if attachment_type in {
                "skill_listing",
                "skill_discovery",
                "dynamic_skill",
                "skill_state",
                "invoked_skill_content",
                "invoked_skills",
            }:
                sanitized["attachment"] = RecordingManager._sanitize_skill_attachment(
                    attachment,
                    max_content_length,
                )

        tr_meta = meta.get("tool_result_metadata")
        if isinstance(tr_meta, dict):
            safe_tool_meta = {
                key: tr_meta[key]
                for key in (
                    "tool",
                    "tool_call_id",
                    "tool_use_id",
                    "persisted",
                    "persisted_path",
                    "original_length",
                    "persist_error",
                    "persisted_size",
                    "content_type",
                    "url",
                )
                if key in tr_meta
            }
            if tr_meta.get("tool") == "Skill":
                safe_tool_meta.update({
                    key: tr_meta[key]
                    for key in (
                        "skill_id",
                        "skill_name",
                        "execution_context",
                        "error_type",
                    )
                    if key in tr_meta
                })
            if safe_tool_meta:
                sanitized["tool_result_metadata"] = safe_tool_meta
        return sanitized

    @staticmethod
    def _sanitize_skill_attachment(
        attachment: Dict[str, Any],
        max_content_length: int,
    ) -> Dict[str, Any]:
        copied = dict(attachment)
        if isinstance(copied.get("content"), str):
            copied["content"] = RecordingManager._truncate_text_value(
                copied["content"],
                max_content_length,
            )
        skills = copied.get("skills")
        if isinstance(skills, list):
            sanitized_skills = []
            for item in skills:
                if not isinstance(item, dict):
                    continue
                skill_item = dict(item)
                if isinstance(skill_item.get("content"), str):
                    skill_item["content"] = RecordingManager._truncate_text_value(
                        skill_item["content"],
                        max_content_length,
                    )
                sanitized_skills.append(skill_item)
            copied["skills"] = sanitized_skills
        return copied

    @staticmethod
    def _truncate_text_value(text: str, max_content_length: int) -> str:
        if len(text) > max_content_length:
            return text[:max_content_length] + f"... [truncated, total {len(text)} chars]"
        return text

    @staticmethod
    def _recording_dir() -> Path | None:
        instance = RecordingManager._global_instance
        recorder = getattr(instance, "_recorder", None) if instance else None
        trajectory_dir = getattr(recorder, "trajectory_dir", None)
        return Path(trajectory_dir) if trajectory_dir else None

    @staticmethod
    def _recording_tool_results_dir() -> Path | None:
        recording_dir = RecordingManager._recording_dir()
        if not recording_dir:
            return None
        tool_results_dir = recording_dir / "tool-results"
        tool_results_dir.mkdir(parents=True, exist_ok=True)
        return tool_results_dir

    @staticmethod
    def _recording_relative_path(path: Path) -> str | None:
        recording_dir = RecordingManager._recording_dir()
        if not recording_dir:
            return None
        try:
            return path.relative_to(recording_dir).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _safe_tool_result_filename(
        *,
        source_path: Path,
        tool_call_id: Any = None,
        tool_name: Any = None,
    ) -> str:
        seed = str(tool_call_id or tool_name or "").strip()
        if not seed:
            seed = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:16]
        safe_seed = re.sub(r"[^A-Za-z0-9_.-]+", "_", seed).strip("._-")
        if not safe_seed:
            safe_seed = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:16]
        safe_seed = safe_seed[:96]

        suffix = source_path.suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix or ""):
            suffix = ".txt"
        return f"{safe_seed}{suffix}"

    @classmethod
    def _archive_persisted_tool_result(
        cls,
        persisted_path: Any,
        *,
        tool_call_id: Any = None,
        tool_name: Any = None,
    ) -> str | None:
        """Copy a persisted tool result into the recording and return a relative path."""
        if not persisted_path:
            return None
        path_text = str(persisted_path).strip()
        if not path_text:
            return None

        recording_dir = cls._recording_dir()
        if not recording_dir:
            return None

        source = Path(path_text).expanduser()
        if not source.is_absolute():
            candidate = recording_dir / source
            if candidate.is_file():
                return source.as_posix()
            return None

        try:
            if source.is_file():
                relative_existing = cls._recording_relative_path(source)
                if relative_existing:
                    return relative_existing
            else:
                return None
        except OSError:
            return None

        tool_results_dir = cls._recording_tool_results_dir()
        if tool_results_dir is None:
            return None

        filename = cls._safe_tool_result_filename(
            source_path=source,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        target = tool_results_dir / filename
        try:
            if target.exists() and target.stat().st_size != source.stat().st_size:
                digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
                target = tool_results_dir / (
                    f"{target.stem}-{digest}{target.suffix}"
                )

            if not target.exists() or target.stat().st_size != source.stat().st_size:
                shutil.copy2(source, target)
            return cls._recording_relative_path(target)
        except Exception as exc:
            logger.debug(
                "Failed to archive persisted tool result %s into recording: %s",
                source,
                exc,
            )
            return None

    @staticmethod
    def _replace_persisted_output_path(content: str, persisted_path: str) -> str:
        return _PERSISTED_OUTPUT_PATH_RE.sub(
            lambda match: f"{match.group(1)}{persisted_path}",
            content,
            count=1,
        )

    @classmethod
    def _archive_message_persisted_result(
        cls,
        msg: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Rewrite recorded persisted-output evidence to the recording archive."""
        if not isinstance(msg, dict):
            return msg

        meta = msg.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        tr_meta = meta.get("tool_result_metadata")
        if not isinstance(tr_meta, dict):
            tr_meta = {}

        content = msg.get("content", "")
        persisted_path = tr_meta.get("persisted_path")
        if not persisted_path and isinstance(content, str):
            match = _PERSISTED_OUTPUT_PATH_RE.search(content)
            if match:
                persisted_path = match.group(0).split(":", 1)[-1].strip()
        if not persisted_path:
            return msg

        tool_call_id = (
            meta.get("tool_call_id")
            or msg.get("tool_call_id")
            or tr_meta.get("tool_call_id")
            or tr_meta.get("tool_use_id")
        )
        tool_name = meta.get("tool_name") or msg.get("name") or tr_meta.get("tool")

        archived_path = cls._archive_persisted_tool_result(
            persisted_path,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        if not archived_path:
            return msg

        updated = dict(msg)
        updated_meta = dict(meta)
        updated_tr_meta = dict(tr_meta)
        updated_tr_meta["persisted"] = True
        updated_tr_meta["persisted_path"] = archived_path
        if tool_name:
            updated_meta["tool_name"] = str(tool_name)
            updated_tr_meta.setdefault("tool", str(tool_name))
        if tool_call_id:
            updated_meta["tool_call_id"] = str(tool_call_id)
            updated_tr_meta.setdefault("tool_call_id", str(tool_call_id))
        updated_meta["tool_result_metadata"] = updated_tr_meta
        updated["_meta"] = updated_meta
        if isinstance(content, str):
            updated["content"] = cls._replace_persisted_output_path(
                content,
                archived_path,
            )
        return updated

    @staticmethod
    def _recording_assets_dir() -> Path | None:
        recording_dir = RecordingManager._recording_dir()
        if not recording_dir:
            return None
        assets_dir = recording_dir / "multimodal"
        assets_dir.mkdir(parents=True, exist_ok=True)
        return assets_dir

    @staticmethod
    def _persist_base64_asset(
        *,
        data: str,
        media_type: str,
        extension: str,
    ) -> Dict[str, Any]:
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:
            return {"note": "[invalid base64 media omitted]", "media_type": media_type}

        digest = hashlib.sha256(raw).hexdigest()
        assets_dir = RecordingManager._recording_assets_dir()
        if assets_dir is None:
            return {
                "note": "[media data omitted]",
                "media_type": media_type,
                "sha256": digest,
                "bytes": len(raw),
            }

        filename = f"{digest[:16]}.{extension}"
        path = assets_dir / filename
        if not path.exists():
            path.write_bytes(raw)
        return {
            "path": f"multimodal/{filename}",
            "media_type": media_type,
            "sha256": digest,
            "bytes": len(raw),
        }

    @staticmethod
    def _extension_for_media_type(media_type: str) -> str:
        normalized = media_type.lower()
        if normalized == "image/jpeg":
            return "jpg"
        if normalized == "image/png":
            return "png"
        if normalized == "image/webp":
            return "webp"
        if normalized == "image/gif":
            return "gif"
        if normalized == "application/pdf":
            return "pdf"
        return "bin"

    @staticmethod
    def _sanitize_content_block(item: Any, max_content_length: int) -> Any:
        if not isinstance(item, dict):
            return item

        block_type = item.get("type")
        if block_type == "text":
            return {
                **item,
                "text": RecordingManager._truncate_text_value(
                    str(item.get("text", "")),
                    max_content_length,
                ),
            }

        if block_type == "tool_result" and isinstance(item.get("content"), list):
            return {
                **item,
                "content": [
                    RecordingManager._sanitize_content_block(block, max_content_length)
                    for block in item["content"]
                ],
            }

        if block_type == "image":
            source = item.get("source")
            if isinstance(source, dict) and source.get("data"):
                media_type = str(source.get("media_type") or "image/png")
                ref = RecordingManager._persist_base64_asset(
                    data=str(source["data"]),
                    media_type=media_type,
                    extension=RecordingManager._extension_for_media_type(media_type),
                )
                return {"type": "image", "source": {"type": "file", **ref}}
            return {"type": "image", "note": "[image data omitted]"}

        if block_type == "image_url":
            image_url = item.get("image_url")
            url = str(image_url.get("url") if isinstance(image_url, dict) else "")
            if ";base64," in url and url.startswith("data:"):
                header, data = url.split(";base64,", 1)
                media_type = header.replace("data:", "", 1) or "image/png"
                ref = RecordingManager._persist_base64_asset(
                    data=data,
                    media_type=media_type,
                    extension=RecordingManager._extension_for_media_type(media_type),
                )
                return {"type": "image_url", "image_url": {"type": "file", **ref}}
            return {"type": "image_url", "note": "[external image url omitted]"}

        if block_type == "document":
            source = item.get("source")
            if isinstance(source, dict) and source.get("data"):
                media_type = str(source.get("media_type") or "application/pdf")
                ref = RecordingManager._persist_base64_asset(
                    data=str(source["data"]),
                    media_type=media_type,
                    extension=RecordingManager._extension_for_media_type(media_type),
                )
                return {"type": "document", "source": {"type": "file", **ref}}
            return {"type": "document", "note": "[document data omitted]"}

        return item

    @classmethod
    async def record_conversation_setup(
        cls,
        setup_messages: List[Dict[str, Any]],
        tools: Optional[List] = None,
        max_content_length: int = 5000,
        agent_name: str = "GroundingAgent",
        extra: Optional[Dict[str, Any]] = None,
    ):
        """
        Record initial conversation context to conversations.jsonl (called once before iterations).

        Writes a ``type: "setup"`` line containing all system messages, the user
        instruction, **and** the tool schemas exposed to the LLM so the log
        gives a complete picture of what the model sees.

        Args:
            setup_messages: The initial messages list (system prompts + user instruction).
            tools: BaseTool list passed to the LLM (optional).  Each tool's
                   name, backend, and description are recorded.
            max_content_length: Max length for message content truncation.
            agent_name: Agent/phase identifier. Used to distinguish conversations
                from different pipeline stages during replay.
                Common values: "GroundingAgent", "ExecutionAnalyzer",
                "SkillEvolver", "SkillEvolver.retry".
            extra: Optional dict of additional context (e.g. evolution_type,
                trigger, target_skills) merged into the record.
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        if not getattr(instance, 'enable_conversation_log', True):
            return

        record: Dict[str, Any] = {
            "type": "setup",
            "agent_name": agent_name,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "messages": cls._truncate_messages(setup_messages, max_content_length),
        }
        if extra:
            record["extra"] = extra

        # Record tool definitions so the log shows what the LLM can call.
        # Description includes the [Backend] tag that the LLM actually sees.
        if tools:
            _BACKEND_LABELS = {
                "mcp": "MCP", "shell": "Shell", "gui": "GUI",
                "web": "Web", "meta": "Meta",
            }
            tool_defs = []
            for t in tools:
                schema = getattr(t, "schema", None)
                if schema:
                    backend_val = getattr(schema, "backend_type", None)
                    backend_str = (
                        backend_val.value
                        if hasattr(backend_val, "value")
                        else str(backend_val) if backend_val else None
                    )
                    entry: Dict[str, Any] = {
                        "name": schema.name,
                        "backend": backend_str,
                    }
                    if schema.description:
                        desc = schema.description
                        # Mirror the [Backend] tag that _prepare_tools_for_llmclient
                        # adds so the recording matches what the LLM sees.
                        if backend_str and backend_str not in ("not_set",):
                            label = _BACKEND_LABELS.get(backend_str, backend_str)
                            desc = f"[{label}] {desc}"
                        if len(desc) > 200:
                            desc = desc[:200] + "..."
                        entry["description"] = desc
                else:
                    entry = {"name": getattr(t, "name", str(t))}
                tool_defs.append(entry)
            record["tools"] = tool_defs

        conv_file = instance._recorder.trajectory_dir / "conversations.jsonl"
        try:
            with open(conv_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write("\n")
        except Exception as e:
            logger.debug(f"Failed to write conversation setup: {e}")

    @classmethod
    async def record_iteration_context(
        cls,
        iteration: int,
        delta_messages: List[Dict[str, Any]],
        response_metadata: Dict[str, Any],
        max_content_length: int = 5000,
        agent_name: str = "GroundingAgent",
        extra: Optional[Dict[str, Any]] = None,
    ):
        """
        Record a single iteration's delta messages to conversations.jsonl.

        Only the messages produced during this iteration are stored (assistant
        response, tool results, inter-iteration guidance), avoiding repetition
        of system prompts and initial user instruction.  The initial context is
        stored once via ``record_conversation_setup``.  The full conversation
        can be reconstructed by concatenating the setup with all deltas in order.

        Args:
            iteration: Iteration number (1-based).
            delta_messages: Messages added during this iteration (assistant + tool results).
            response_metadata: Lightweight metadata about the LLM response
                (has_tool_calls, tool_calls_count).
            max_content_length: Max length for message content truncation.
            agent_name: Agent/phase identifier (must match the corresponding
                ``record_conversation_setup`` call).
            extra: Optional dict of additional context merged into the record.
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        if not getattr(instance, 'enable_conversation_log', True):
            return

        record = {
            "type": "iteration",
            "agent_name": agent_name,
            "iteration": iteration,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "response_metadata": response_metadata,
            "delta_messages": cls._truncate_messages(delta_messages, max_content_length),
        }
        if extra:
            record["extra"] = extra

        # Append to conversations.jsonl (real-time)
        conv_file = instance._recorder.trajectory_dir / "conversations.jsonl"
        try:
            with open(conv_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write("\n")
        except Exception as e:
            logger.debug(f"Failed to write conversation log: {e}")
    
    @classmethod
    async def record_tool_execution(
        cls,
        tool_name: str,
        backend: str,
        parameters: Dict[str, Any],
        result: Any,
        server_name: Optional[str] = None,
        is_success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Record tool execution (internal method, called by BaseTool automatically)
        
        Args:
            tool_name: Name of the tool
            backend: Backend type (gui, shell, mcp, etc.)
            parameters: Tool parameters
            result: Tool execution result (content or error message)
            server_name: Server name for MCP backend
            is_success: Whether the tool execution was successful (default: True for backward compatibility)
            metadata: Tool result metadata (e.g. intermediate_steps for GUI)
        """
        if not cls._global_instance or not cls._global_instance._is_started:
            return
        
        instance = cls._global_instance
        
        # Infer backend if not_set or not in allowed backends
        if backend == "not_set" or backend not in instance.backends:
            inferred = cls._infer_backend_from_tool_name(tool_name)
            if inferred and inferred in instance.backends:
                backend = inferred
            elif backend not in instance.backends:
                logger.debug(
                    f"Backend '{backend}' not in recording backends {instance.backends}, "
                    f"skipping recording for tool '{tool_name}'"
                )
                return
        
        # Create mock tool_call and result objects for compatibility with existing _record_* methods
        class MockFunctionCall:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments
        
        class MockToolCall:
            def __init__(self, name, arguments):
                self.function = MockFunctionCall(name, arguments)
        
        class MockResult:
            def __init__(self, content, is_success=True, metadata=None):
                self.content = content
                self.is_success = is_success
                self.is_error = not is_success
                self.error = content if not is_success else None
                self.metadata = metadata or {}
        
        tool_call = MockToolCall(tool_name, parameters)
        mock_result = MockResult(result, is_success=is_success, metadata=metadata)
        
        try:
            if backend == "mcp":
                server = server_name or "unknown"
                await instance._record_mcp(tool_call, mock_result, server)
            elif backend == "gui":
                await instance._record_gui(tool_call, mock_result)
            elif backend == "shell":
                await instance._record_shell(tool_call, mock_result)
            elif backend == "meta":
                await instance._record_meta(tool_call, mock_result)
            elif backend == "web":
                await instance._record_web(tool_call, mock_result)
            else:
                logger.warning(f"No recording handler for backend '{backend}', tool '{tool_name}'")
                return
            
            instance._step_counter += 1
        except Exception as e:
            logger.warning(f"Failed to record tool execution for {tool_name}: {e}")
    
    @staticmethod
    def _parse_arguments(arg_data):
        """Safely parse tool_call.function.arguments which may be JSON string.

        Handles:
        1. Proper JSON strings with true/false/null
        2. Python literal strings (produced by OpenAI) using ast.literal_eval
        3. Already-dict objects (returned by SDK)
        """
        if not isinstance(arg_data, str):
            return arg_data or {}

        # First, try JSON
        try:
            return json.loads(arg_data)
        except json.JSONDecodeError:
            pass

        # Fallback to Python literal
        try:
            return ast.literal_eval(arg_data)
        except Exception:
            logger.debug("Failed to parse arguments, returning raw string")
            return {"raw": arg_data}
    
    async def start(self, task_id: Optional[str] = None):
        """Start automatic recording
        Args:
            task_id: If provided, override the current task_id for this recording session. This allows
                     external callers (e.g. Coordinator) to specify a meaningful task identifier without
                     having to recreate the RecordingManager instance.
        """
        # Allow dynamic update of task_id before recording actually starts
        if task_id:
            self.task_id = task_id
        if not self.enabled or self._is_started:
            return
        
        try:
            # check server availability (only when video or screenshot is enabled)
            if self.enable_video or self.enable_screenshot:
                await self._check_server_availability()
            
            self._recorder = TrajectoryRecorder(
                task_name=self.task_id,
                log_dir=self.log_dir,
                enable_screenshot=self.enable_screenshot,
                enable_video=self.enable_video,
                server_url=self.server_url,
            )
            
            # create action recorder for agent decision tracking
            self._action_recorder = ActionRecorder(
                trajectory_dir=Path(self._recorder.get_trajectory_dir())
            )
            
            
            # create video client (internal management)
            if self.enable_video:
                from openspace.platforms import RecordingClient
                self._recording_client = RecordingClient(base_url=self.server_url)
                success = await self._recording_client.start_recording()
                if success:
                    logger.info("Video recording started")
                else:
                    logger.warning("Video recording failed to start")
            
            # create screenshot client (internal management)
            if self.enable_screenshot:
                from openspace.platforms import ScreenshotClient
                self._screenshot_client = ScreenshotClient(base_url=self.server_url)
                logger.debug("Screenshot client ready")
            
            # save initial metadata
            await self._recorder.add_metadata("task_id", self.task_id)
            await self._recorder.add_metadata("backends", list(self.backends))
            await self._recorder.add_metadata("start_time", datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))

            # Capture and save initial screenshot if enabled
            if self.enable_screenshot and self._screenshot_client:
                try:
                    init_shot = await self._screenshot_client.capture()
                    if init_shot:
                        await self._recorder.save_init_screenshot(init_shot)
                        logger.debug("Initial screenshot saved")
                except Exception as e:
                    logger.debug(f"Failed to capture initial screenshot: {e}")
            
            self._is_started = True
            logger.info(f"Recording started: {self._recorder.get_trajectory_dir()}")
            
        except Exception as e:
            logger.error(f"Recording failed to start: {e}")
            raise
    
    async def _check_server_availability(self):
        """Check if local server is available"""
        try:
            from openspace.platforms import SystemInfoClient

            # Use context manager to ensure aiohttp session is closed, avoiding warning of unclosed session
            async with SystemInfoClient(base_url=self.server_url) as client:
                info = await client.get_system_info()

            if info:
                logger.info(f"Server connected ({info.get('platform', 'unknown')})")
            else:
                logger.warning("Server not responding, video/screenshot functionality unavailable")
        
        except Exception:
            logger.warning("Cannot connect to server, video/screenshot functionality unavailable")
    
    async def save_execution_outcome(
        self,
        status: str,
        iterations: int,
        execution_time: float = 0,
    ) -> None:
        """Persist task-level execution outcome into metadata.json.

        Should be called **before** ``stop()`` so the data is included in the
        finalized recording.  The saved dict has the structure::

            {"status": "success"|"incomplete"|"error",
             "iterations": int,
             "execution_time": float}
        """
        if self._recorder:
            await self._recorder.add_metadata("execution_outcome", {
                "status": status,
                "iterations": iterations,
                "execution_time": round(execution_time, 2),
            })

    async def stop(self):
        """Stop automatic recording"""
        if not self.enabled or not self._is_started:
            return
        
        try:
            # stop video recording and save
            if self._recording_client:
                try:
                    video_path = None
                    if self._recorder:
                        video_path = str(Path(self._recorder.get_trajectory_dir()) / "screen_recording.mp4")
                    
                    video_bytes = await self._recording_client.end_recording(dest=video_path)
                    if video_bytes and video_path:
                        video_size_mb = len(video_bytes) / (1024 * 1024)
                        logger.info(f"Video recording saved: {video_path} ({video_size_mb:.2f} MB)")
                except Exception as e:
                    logger.warning(f"Video recording failed to save: {e}")

            # close RecordingClient session, avoid unclosed session warning
            try:
                if self._recording_client:
                    await self._recording_client.close()
            except Exception as e:
                logger.debug(f"Failed to close RecordingClient session: {e}")
            
            # close screenshot client
            if self._screenshot_client:
                try:
                    await self._screenshot_client.close()
                except Exception as e:
                    logger.debug(f"Screenshot client failed to close: {e}")
                finally:
                    self._screenshot_client = None
            
            # finalize trajectory recording
            if self._recorder:
                # save final metadata
                await self._recorder.add_metadata("end_time", datetime.datetime.now().isoformat())
                await self._recorder.add_metadata("total_steps", self._step_counter)
                
                # generate summary
                await self.generate_summary()
                
                # finalize recording
                await self._recorder.finalize()
                
                logger.info(f"Recording completed: {self._recorder.get_trajectory_dir()}")
            
            self._is_started = False
            self._recorder = None
            self._action_recorder = None
            
        except Exception as e:
            logger.error(f"Recording failed to stop: {e}")
    
    @staticmethod
    def _infer_backend_from_tool_name(tool_name: str) -> Optional[str]:
        """Infer backend from tool name when tool_results lack backend."""
        if not tool_name or not isinstance(tool_name, str):
            return None
        name = tool_name.strip()
        # Use rsplit to handle server names that themselves contain "__".
        if "__" in name:
            name = name.rsplit("__", 1)[-1]
        shell_tools = {"read", "write", "ls", "bash", "edit", "grep", "glob"}
        if name in shell_tools:
            return "shell"
        if name in ("gui_agent",) or "gui" in name.lower():
            return "gui"
        if "mcp" in name.lower() or ("." in name and "__" not in name):
            return "mcp"
        if name in ("web_search", "web_fetch", "WebSearch", "WebFetch", "deep_research_agent", "deep_research"):
            return "web"
        if name in (
            "list_providers",
            "list_backend_tools",
            "list_session_tools",
            "list_all_backend_tools",
            "tool_search",
        ):
            return "meta"
        return None
    
    async def _record_mcp(self, tool_call, result, server: str):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        command = f"{server}.{tool_name}"
        if result.is_success:
            result_str = self._format_recorded_content(result.content)
        else:
            result_str = str(result.error)
        result_brief = result_str[:200] + "..." if len(result_str) > 200 else result_str
        
        is_actual_success = result.is_success and not result_str.startswith("ERROR:")
        
        step_info = await self._recorder.record_step(
            backend="mcp",
            tool=tool_name,
            command=command,
            result=self._result_with_evidence(
                {
                    "status": "success" if is_actual_success else "error",
                    "output": result_brief,
                },
                tool_name=tool_name,
                tool_result=result,
            ),
            parameters=parameters,
            extra={
                "server": server,
            },
            auto_screenshot=self.enable_screenshot
        )
        
        # Add agent_name to step_info
        step_info["agent_name"] = self.agent_name

    def _format_recorded_content(self, content: Any) -> str:
        """Format tool output for trajectory logs without raw media payloads."""
        if isinstance(content, list):
            sanitized = [
                self._sanitize_content_block(item, max_content_length=5000)
                for item in content
            ]
            return json.dumps(sanitized, ensure_ascii=False)
        return str(content)

    @staticmethod
    def _tool_result_evidence(tool_name: str, result: Any) -> Dict[str, Any]:
        """Extract compact structured identity and persistence evidence."""
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}

        evidence: Dict[str, Any] = {"tool_name": tool_name}
        tool_call_id = metadata.get("tool_call_id") or metadata.get("tool_use_id")
        if tool_call_id:
            evidence["tool_call_id"] = str(tool_call_id)

        for key in (
            "persisted",
            "persisted_path",
            "original_length",
            "persist_error",
            "persisted_size",
            "content_type",
            "url",
        ):
            if key in metadata and metadata[key] is not None:
                evidence[key] = metadata[key]

        if evidence.get("persisted_path"):
            archived_path = RecordingManager._archive_persisted_tool_result(
                evidence["persisted_path"],
                tool_call_id=evidence.get("tool_call_id"),
                tool_name=tool_name,
            )
            if archived_path:
                evidence["persisted_path"] = archived_path

        if len(evidence) == 1:
            return {}
        return evidence

    @classmethod
    def _result_with_evidence(
        cls,
        result: Dict[str, Any],
        *,
        tool_name: str,
        tool_result: Any,
    ) -> Dict[str, Any]:
        evidence = cls._tool_result_evidence(tool_name, tool_result)
        if evidence:
            result = dict(result)
            persisted_path = evidence.get("persisted_path")
            if persisted_path:
                result = {
                    key: (
                        cls._replace_persisted_output_path(value, str(persisted_path))
                        if isinstance(value, str)
                        else value
                    )
                    for key, value in result.items()
                }
            result["evidence"] = evidence
        return result

    async def _record_gui(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        # Extract actual pyautogui command (from action_history)
        command = "gui_agent"
        if result.is_success and hasattr(result, 'metadata') and result.metadata:
            action_history = result.metadata.get("action_history", [])
            if action_history:
                # Get last successful execution action
                for action in reversed(action_history):
                    planned_action = action.get("planned_action", {})
                    execution_result = action.get("execution_result", {})
                    
                    if planned_action.get("action_type") == "PYAUTOGUI_COMMAND":
                        cmd = planned_action.get("command", "")
                        if cmd and execution_result.get("status") == "success":
                            command = cmd
                            break
                    elif execution_result.get("status") == "success":
                        action_type = planned_action.get("action_type", "")
                        if action_type and action_type not in ["WAIT", "DONE", "FAIL"]:
                            params = planned_action.get("parameters", {})
                            if params:
                                param_str = ", ".join([f"{k}={v}" for k, v in list(params.items())[:2]])
                                command = f"{action_type}({param_str})"
                            else:
                                command = action_type
                            break
        
        result_str = str(result.content) if result.is_success else str(result.error)
        
        is_actual_success = result.is_success
        if result.is_success:
            first_200_chars = result_str[:200] if result_str else ""
            critical_failure_patterns = ["Task failed", "CRITICAL ERROR:", "FATAL:"]
            has_critical_failure = any(pattern in first_200_chars for pattern in critical_failure_patterns)
            is_actual_success = not has_critical_failure
        
        # Extract intermediate_steps from metadata for embedding in traj.jsonl
        extra = {}
        if hasattr(result, 'metadata') and result.metadata:
            intermediate_steps = result.metadata.get("intermediate_steps")
            if intermediate_steps:
                extra["intermediate_steps"] = intermediate_steps
        
        step_info = await self._recorder.record_step(
            backend="gui",
            tool="gui_agent",
            command=command,
            result=self._result_with_evidence(
                {
                    "status": "success" if is_actual_success else "error",
                    "output": result_str,
                },
                tool_name=tool_name,
                tool_result=result,
            ),
            parameters=parameters,
            auto_screenshot=self.enable_screenshot,
            extra=extra if extra else None,
        )
        
        step_info["agent_name"] = self.agent_name
    
    async def _record_shell(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        task = (
            parameters.get("command")
            or parameters.get("script")
            or parameters.get("code")
            or parameters.get("task")
            or tool_name
        )
        exit_code = 0 if result.is_success else 1
        
        stdout = str(result.content) if result.is_success else ""
        stderr = str(result.error) if result.is_error else ""
        
        command = task  
        if hasattr(result, 'metadata') and result.metadata:
            code_history = result.metadata.get("code_history", [])
            if code_history:
                # Try to find the last successful execution
                found_success = False
                for code_info in reversed(code_history):
                    if code_info.get("status") == "success":
                        lang = code_info.get("lang", "bash")
                        code = code_info.get("code", "")
                        # String format code block: ```lang\ncode\n```
                        command = f"```{lang}\n{code}\n```"
                        found_success = True
                        break
                
                # If no successful execution found, use last code block
                if not found_success and code_history:
                    last_code = code_history[-1]
                    lang = last_code.get("lang", "bash")
                    code = last_code.get("code", "")
                    command = f"```{lang}\n{code}\n```"
        
        stdout_brief = stdout[:200] + "..." if len(stdout) > 200 else stdout
        stderr_brief = stderr[:200] + "..." if len(stderr) > 200 else stderr
        
        is_actual_success = result.is_success
        if result.is_success:
            first_500_chars = stdout[:500] if stdout else ""
            critical_failure_patterns = [
                "Task failed after",
                "[TASK_FAILED:",
                "EXECUTION ERROR",
                "timed out",
            ]
            has_critical_failure = any(pattern in first_500_chars for pattern in critical_failure_patterns)
            is_actual_success = not has_critical_failure
        
        step_info = await self._recorder.record_step(
            backend="shell",
            tool=tool_name,
            command=command,
            result=self._result_with_evidence(
                {
                    "status": "success" if is_actual_success else "error",
                    "exit_code": exit_code,
                    "stdout": stdout_brief,
                    "stderr": stderr_brief,
                },
                tool_name=tool_name,
                tool_result=result,
            ),
            auto_screenshot=self.enable_screenshot
        )
        
        step_info["agent_name"] = self.agent_name
    
    async def _record_meta(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        command = tool_name
        if parameters:
            key_params = []
            for key in ['path', 'file', 'directory', 'name', 'provider', 'backend']:
                if key in parameters and parameters[key]:
                    key_params.append(f"{parameters[key]}")
            if key_params:
                command = f"{tool_name}({', '.join(key_params[:2])})"
        
        result_str = str(result.content) if result.is_success else str(result.error)
        result_brief = result_str[:200] + "..." if len(result_str) > 200 else result_str
        
        is_actual_success = result.is_success
        if result.is_success and result_str:
            is_actual_success = not result_str.startswith("ERROR:")
        
        step_info = await self._recorder.record_step(
            backend="meta",
            tool=tool_name,
            command=command,
            result=self._result_with_evidence(
                {
                    "status": "success" if is_actual_success else "error",
                    "output": result_brief,
                },
                tool_name=tool_name,
                tool_result=result,
            ),
            auto_screenshot=self.enable_screenshot
        )
        
        step_info["agent_name"] = self.agent_name
    
    async def _record_web(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)

        if tool_name in ("web_fetch", "WebFetch"):
            url = parameters.get("url", "")
            prompt = parameters.get("prompt", "")
            command = f"{url}: {prompt}" if prompt else (url or "web_fetch")
        else:
            query = parameters.get("query", "")
            command = query if query else tool_name
        
        result_str = str(result.content) if result.is_success else str(result.error)
        
        is_actual_success = result.is_success
        if result.is_success and result_str:
            is_actual_success = not result_str.startswith("ERROR:")
        
        step_info = await self._recorder.record_step(
            backend="web",
            tool=tool_name,
            command=command,
            result=self._result_with_evidence(
                {
                    "status": "success" if is_actual_success else "error",
                    "output": result_str,  # Full output preserved for training/replay
                },
                tool_name=tool_name,
                tool_result=result,
            ),
            auto_screenshot=self.enable_screenshot
        )
        
        # Add agent_name to step_info
        step_info["agent_name"] = self.agent_name
    
    async def add_metadata(self, key: str, value: Any):
        if self._recorder:
            await self._recorder.add_metadata(key, value)
    
    async def save_plan(self, plan: Dict[str, Any], agent_name: str = "GroundingAgent"):
        """
        Save agent plan to recording directory.
        This integrates planning information with execution trajectory.
        
        Args:
            plan: The plan data (usually containing task_updates or plan steps)
            agent_name: Name of the agent creating the plan
        """
        if not self._recorder or not self._is_started:
            logger.warning("Cannot save plan: recording not started")
            return
        
        try:
            plan_dir = Path(self._recorder.get_trajectory_dir()) / "plans"
            plan_dir.mkdir(exist_ok=True)
            
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            plan_data = {
                "version": timestamp,
                "created_at": datetime.datetime.now().isoformat(),
                "created_by": agent_name,
                "plan": plan
            }
            
            # Save versioned plan
            plan_file = plan_dir / f"plan_{timestamp}.json"
            with open(plan_file, 'w', encoding='utf-8') as f:
                json.dump(plan_data, f, indent=2, ensure_ascii=False)
            
            # Save current plan (latest)
            current_plan_file = plan_dir / "current_plan.json"
            with open(current_plan_file, 'w', encoding='utf-8') as f:
                json.dump(plan_data, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"Saved plan to recording: {plan_file.name}")
        except Exception as e:
            logger.error(f"Failed to save plan: {e}")
    
    async def log_decision(
        self, 
        agent_name: str, 
        decision: str, 
        context: Optional[Dict[str, Any]] = None
    ):
        """
        Log agent decision with optional context.
        This provides insight into agent reasoning process.
        
        Args:
            agent_name: Name of the agent making the decision
            decision: Description of the decision
            context: Additional context information
        """
        if not self._recorder or not self._is_started:
            logger.warning("Cannot log decision: recording not started")
            return
        
        try:
            traj_dir = Path(self._recorder.get_trajectory_dir())
            log_file = traj_dir / "decisions.log"
            
            timestamp = datetime.datetime.now().isoformat()
            log_entry = f"[{timestamp}] {agent_name}: {decision}"
            if context:
                log_entry += f"\n  Context: {json.dumps(context, ensure_ascii=False)}"
            log_entry += "\n"
            
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)
            
            logger.debug(f"Logged decision from {agent_name}")
        except Exception as e:
            logger.error(f"Failed to log decision: {e}")
    
    async def record_agent_action(
        self,
        agent_name: str,
        action_type: str,
        input_data: Optional[Dict[str, Any]] = None,
        reasoning: Optional[Dict[str, Any]] = None,
        output_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        related_tool_steps: Optional[list] = None,
        correlation_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Record an agent's action and decision-making process.
        
        Args:
            agent_name: Name of the agent performing the action
            action_type: Type of action (plan | execute | evaluate | monitor)
            input_data: Input data the agent received (simplified)
            reasoning: Agent's reasoning process (structured)
            output_data: Agent's output/decision (structured)
            metadata: Additional metadata (LLM model, tokens, duration, etc.)
            related_tool_steps: List of tool execution step numbers related to this action
            correlation_id: Optional correlation ID to link related events
            
        Returns:
            The recorded action info, or None if recording not started
        """
        if not self._action_recorder or not self._is_started:
            logger.debug("Cannot record agent action: recording not started")
            return None
        
        try:
            action_info = await self._action_recorder.record_action(
                agent_name=agent_name,
                action_type=action_type,
                input_data=input_data,
                reasoning=reasoning,
                output_data=output_data,
                metadata=metadata,
                related_tool_steps=related_tool_steps,
                correlation_id=correlation_id,
            )
            
            logger.debug(f"Recorded agent action: {agent_name} - {action_type}")
            return action_info
            
        except Exception as e:
            logger.error(f"Failed to record agent action: {e}")
            return None
    
    async def generate_summary(self) -> Dict[str, Any]:
        """
        Generate a comprehensive summary of the recording session.
        """
        if not self._recorder or not self._is_started:
            logger.warning("Cannot generate summary: recording not started")
            return {}
        
        try:
            from .action_recorder import load_agent_actions, analyze_agent_actions
            from .utils import load_trajectory_from_jsonl, analyze_trajectory
            
            traj_dir = self._recorder.get_trajectory_dir()
            
            # Load all recorded data
            trajectory = load_trajectory_from_jsonl(f"{traj_dir}/traj.jsonl")
            agent_actions = load_agent_actions(traj_dir)
            
            # Analyze data
            traj_stats = analyze_trajectory(trajectory)
            action_stats = analyze_agent_actions(agent_actions)
            
            # Build summary
            summary = {
                "task_id": self.task_id,
                "start_time": self._recorder.metadata.get("start_time", ""),
                "end_time": self._recorder.metadata.get("end_time", ""),
                "trajectory": {
                    "total_steps": traj_stats.get("total_steps", 0),
                    "success_count": traj_stats.get("success_count", 0),
                    "success_rate": traj_stats.get("success_rate", 0),
                    "by_backend": traj_stats.get("backends", {}),
                    "by_tool": traj_stats.get("tools", {}),
                },
                "agent_actions": {
                    "total_actions": action_stats.get("total_actions", 0),
                    "by_agent": action_stats.get("by_agent", {}),
                    "by_type": action_stats.get("by_type", {}),
                }
            }
            
            # Save summary to file
            summary_file = Path(traj_dir) / "summary.json"
            with open(summary_file, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Generated summary: {summary_file}")
            return summary
            
        except Exception as e:
            logger.error(f"Failed to generate summary: {e}")
            return {}
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False
    
    @property
    def recording_status(self) -> bool:
        return self._is_started
    
    @property
    def trajectory_dir(self) -> Optional[str]:
        if self._recorder:
            return str(self._recorder.get_trajectory_dir())
        return None
    
    @property
    def recording_client(self):
        return self._recording_client
    
    @property
    def screenshot_client(self):
        return self._screenshot_client
    
    @property
    def step_count(self) -> int:
        """Get current step count"""
        return self._step_counter


__all__ = [
    'RecordingManager',
]
