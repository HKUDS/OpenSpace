import asyncio
import json
import os as _os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Tuple

from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.grounding.core.session import BaseSession
from openspace.grounding.backends.shell.transport.local_connector import LocalShellConnector
from openspace.grounding.core.tool import BaseTool
from openspace.grounding.core.security.policies import SecurityPolicyManager
from openspace.grounding.core.security.shell_parser import extract_output_redirections
from openspace.grounding.backends.shell.bash_security import (
    detect_blocked_sleep_pattern,
    get_destructive_command_warning,
    interpret_command_result,
    is_autobackgrounding_allowed,
)
from openspace.prompts.tools.bash_prompt import get_simple_prompt
from openspace.persistence.file_history import record_snapshot
from openspace.services.tooling.context import ReadFileEntry
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

# OpenSpace BashTool defaults.  OpenSpace reads these from
# ``utils/timeouts.ts`` which is environment-configurable; for OS we
# hard-code the OpenSpace defaults of 2 minutes default / 10 minutes cap.
# These values also match OpenSpace ``prompt.ts`` L335 which interpolates them
# into the tool instructions.
_DEFAULT_BASH_TIMEOUT_MS: int = 2 * 60 * 1000
_MAX_BASH_TIMEOUT_MS: int = 10 * 60 * 1000
_PROGRESS_THRESHOLD_MS: int = 2_000
_ASSISTANT_BLOCKING_BUDGET_MS: int = 15_000
_MAX_PERSISTED_SIZE: int = 64 * 1024 * 1024

# Filesystem layout for run_in_background outputs — mirrors OpenSpace
# ``getTaskOutputPath`` (``utils/tasks.ts``): one file per task ID under
# a stable tool-results subdirectory so subsequent Read calls can stream
# the output.
_BG_TASK_DIR_NAME = "openspace-bash-tasks"
_HOOK_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


@dataclass(slots=True)
class _SandboxExecutionPlan:
    manager: Any
    decision: Any
    wrapped: Any | None = None


def _get_bg_task_dir() -> str:
    path = _os.path.join(tempfile.gettempdir(), _BG_TASK_DIR_NAME)
    _os.makedirs(path, exist_ok=True)
    return path


def _get_bg_task_output_path(task_id: str) -> str:
    """OpenSpace ``getTaskOutputPath(taskId)`` — stable path for the merged fd stream."""
    return _os.path.join(_get_bg_task_dir(), f"{task_id}.out")


def _quick_sandbox_enabled_candidate(cwd: str | None) -> bool:
    env_value = _os.environ.get("OPENSPACE_SANDBOX_ENABLED")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}

    candidates: list[str] = []
    config_home = _os.environ.get("OPENSPACE_CONFIG_HOME")
    if config_home:
        candidates.append(_os.path.join(config_home, "settings.json"))
    else:
        candidates.append(_os.path.expanduser("~/.openspace/settings.json"))

    try:
        current = _os.path.abspath(_os.path.expanduser(cwd or _os.getcwd()))
        while True:
            candidates.append(_os.path.join(current, ".openspace", "settings.json"))
            candidates.append(
                _os.path.join(current, ".openspace", "settings.local.json")
            )
            parent = _os.path.dirname(current)
            if parent == current:
                break
            current = parent
    except OSError:
        return True

    for candidate in candidates:
        if not _os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception:
            return True
        sandbox = raw.get("sandbox") if isinstance(raw, dict) else None
        if isinstance(sandbox, dict) and sandbox.get("enabled") is True:
            return True
    return False


def _parse_shell_result(result: Any) -> Tuple[str, str, int]:
    """Parse a connector result dict into ``(stdout, stderr, returncode)``."""
    if isinstance(result, dict):
        stdout = (
            result.get("content")
            or result.get("output")
            or result.get("stdout")
            or ""
        )
        stderr = result.get("error") or result.get("stderr") or ""
        rc = result.get("returncode", 0)
        return stdout, stderr, rc
    return str(result), "", 0


def _normalize_bash_timeout_seconds(timeout: int | float | None) -> int:
    timeout_ms = int(timeout) if timeout else _DEFAULT_BASH_TIMEOUT_MS
    timeout_ms = max(1000, min(timeout_ms, _MAX_BASH_TIMEOUT_MS))
    return max(1, timeout_ms // 1000)


def _env_truthy(name: str) -> bool:
    value = _os.environ.get(name)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _background_tasks_disabled() -> bool:
    return _env_truthy("OPENSPACE_DISABLE_BACKGROUND_TASKS")


def _assistant_blocking_budget_ms() -> int:
    raw = _os.environ.get("OPENSPACE_ASSISTANT_BLOCKING_BUDGET_MS")
    if raw is None:
        return _ASSISTANT_BLOCKING_BUDGET_MS
    try:
        return max(1, int(raw))
    except ValueError:
        return _ASSISTANT_BLOCKING_BUDGET_MS


class ShellSession(BaseSession):
    backend_type = BackendType.SHELL

    def __init__(
        self,
        connector: LocalShellConnector,
        *,
        session_id: str,
        security_manager: SecurityPolicyManager = None,
        default_working_dir: str = None,
        default_env: dict = None,
        default_conda_env: str = None,
        model: str = None,
        use_clawwork_productivity: bool = False,
        productivity_date: str = "default",
    ):
        super().__init__(connector=connector, session_id=session_id,
                         backend_type=BackendType.SHELL)
        self.security_manager = security_manager
        self.default_working_dir = default_working_dir
        self.default_env = default_env or {}
        self.default_conda_env = default_conda_env
        self.model = model
        self.use_clawwork_productivity = use_clawwork_productivity
        self.productivity_date = productivity_date or "default"

    def configure_workspace(self, workspace_dir: str) -> None:
        """Update the default workspace used by shell-backed tools."""
        self.default_working_dir = workspace_dir
        for tool in getattr(self, "tools", []):
            if hasattr(tool, "_default_working_dir"):
                setattr(tool, "_default_working_dir", workspace_dir)

    async def initialize(self):
        from openspace.grounding.backends.shell.file_tools import (
            FileEditTool,
            ReadFileTool,
            WriteFileTool,
        )
        from openspace.tools.notebook_edit_tool import NotebookEditTool
        from openspace.grounding.backends.shell.search_tools import (
            GlobTool,
            GrepTool,
            ListDirTool,
        )
        from openspace.tools.memory_tools import (
            MemoryReadTool,
            MemoryWriteTool,
        )

        self.tools = [
            WriteFileTool(self),
            FileEditTool(self),
            NotebookEditTool(self),
            GrepTool(self),
            GlobTool(self),
            ListDirTool(self),
            MemoryReadTool(self),
            MemoryWriteTool(self),
            BashTool(self),
        ]
        if not self.use_clawwork_productivity:
            self.tools.insert(1, ReadFileTool(self))
        if self.use_clawwork_productivity:
            from openspace.grounding.backends.shell.productivity_tools import get_productivity_tools
            extra = get_productivity_tools(
                self,
                data_path=self.default_working_dir,
                current_date=self.productivity_date,
            )
            if extra:
                self.tools.extend(extra)
                logger.info("ClawWork productivity tools enabled: %s", [t.name for t in extra])
            else:
                logger.warning("use_clawwork_productivity is True but livebench not available; productivity tools not added.")
        return {"tools": [t.name for t in self.tools]}


class BashTool(BaseTool):
    """Run a shell command and return stdout/stderr.

    This class owns the bash tool schema, ``validate_input``, exit-code
    interpretation, optional background execution, classifier/permission checks,
    and process sandbox wrapping.

    Parameters:

    * ``command``                       → executed via the shell connector
    * ``timeout``                       → milliseconds, capped at
      ``_MAX_BASH_TIMEOUT_MS`` (10 min), default
      ``_DEFAULT_BASH_TIMEOUT_MS`` (2 min)
    * ``description``                   → human summary for the permission
      dialog / transcript
    * ``run_in_background``             → detach and stream output to a
      per-task file (see :func:`_get_bg_task_output_path`)
    * ``dangerously_disable_sandbox``   → sandbox bypass request; honored only
      when sandbox policy allows unsandboxed commands

    Internal-only parameters:

    * ``_simulatedSedEdit``             → preview channel used by sed edit
      permission flows. It is omitted from the model-facing schema but accepted
      by ``_arun``.
    """

    _name = "bash"
    _description = get_simple_prompt()
    backend_type = BackendType.SHELL
    search_hint = "execute shell commands"
    parameter_descriptions = {
        "command": "The command to execute",
        "timeout": f"Optional timeout in milliseconds (max {_MAX_BASH_TIMEOUT_MS}).",
        "description": '''Clear, concise description of what this command does in active voice. Never use words like "complex" or "risk" in the description - just describe what it does.

For simple commands (git, npm, standard CLI tools), keep it brief (5-10 words):
- ls -> "List files in current directory"
- git status -> "Show working tree status"
- npm install -> "Install package dependencies"

For commands that are harder to parse at a glance (piped commands, obscure flags, etc.), add enough context to clarify what it does:
- find . -name "*.tmp" -exec rm {} \\; -> "Find and delete all .tmp files recursively"
- git reset --hard origin/main -> "Discard all local changes and match remote main"
- curl -s url | jq '.data[]' -> "Fetch JSON from URL and extract data array elements"''',
        "run_in_background": (
            "Set to true to run this command in the background. The result "
            "will include an output file path; inspect it later with a short "
            "bash command such as `cat <path>` or `tail <path>`, unless a "
            "TaskGet tool is actually available in this turn."
        ),
        "backgroundedByUser": (
            "Internal field: true if the user manually backgrounded the "
            "command with Ctrl+B."
        ),
        "assistantAutoBackgrounded": (
            "Internal field: true if assistant-mode auto-backgrounded a "
            "long-running blocking command."
        ),
        "dangerously_disable_sandbox": (
            "Set this to true to dangerously override sandbox mode and run "
            "commands without sandboxing."
        ),
    }

    # Default max result size for shell command output.
    max_result_size_chars = 30_000

    # --- dynamic tool properties ----------------------------------------------

    def is_read_only(self, input: dict | None = None) -> bool:
        """Return True iff the command is classified as read-only.

        Delegates to :func:`check_read_only_constraints` from
        :mod:`openspace.grounding.core.security.bash_classifier`.

        The classifier internally computes ``compound_command_has_cd``
        when the caller does not provide it.

        Any non-string ``command`` or missing input returns ``False`` so the
        partitioner defaults to the serial path.
        """
        if not isinstance(input, dict):
            return False
        command = input.get("command")
        if not isinstance(command, str) or not command.strip():
            return False
        try:
            from openspace.grounding.core.security.bash_classifier import (
                check_read_only_constraints,
            )
        except Exception:
            return False
        try:
            context = getattr(self, "_current_context", None)
            cwd = (
                input.get("cwd")
                if isinstance(input.get("cwd"), str)
                else getattr(context, "cwd", None)
                or getattr(self._session, "default_working_dir", None)
            )
            original_cwd = (
                input.get("original_cwd")
                if isinstance(input.get("original_cwd"), str)
                else getattr(context, "original_cwd", None)
            )
            result = check_read_only_constraints(
                command,
                cwd=cwd,
                original_cwd=original_cwd,
            )
        except Exception:
            return False
        return result.get("behavior") == "allow"

    def is_concurrency_safe(self, input: dict | None = None) -> bool:
        """Bash commands are concurrency-safe when they are read-only."""
        return self.is_read_only(input)

    def is_destructive(self, input: dict | None = None) -> bool:
        """Best-effort destructive signal for the permission dialog.

        Destructive-command detection is surfaced through
        ``getDestructiveCommandWarning`` (see :mod:`bash_security`) and exposed
        here so ``partition_tool_calls`` and permission prompts can use the
        unified ``BaseTool.is_destructive`` abstraction.
        """
        if not input:
            return False
        cmd = input.get("command") if isinstance(input, dict) else None
        if not isinstance(cmd, str):
            return False
        return get_destructive_command_warning(cmd) is not None

    # --- lifecycle ------------------------------------------------------

    def __init__(self, session: "ShellSession"):
        self._session = session
        self._current_context: Any | None = None
        super().__init__()
        self.schema.description = get_simple_prompt()

    def get_prompt(self, context: Any | None = None) -> str:
        return get_simple_prompt()

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def _execute_raw(self, **kwargs) -> ToolResult:
        kwargs.pop("_skill_hook_command", None)
        simulated_edit = kwargs.pop("_simulatedSedEdit", None)
        if simulated_edit is None:
            return await super()._execute_raw(**kwargs)

        start = time.time()
        try:
            self.validate_parameters(kwargs)
            command = kwargs.get("command", "") or ""
            description = kwargs.get("description")
            dangerously_disable_sandbox = bool(kwargs.get("dangerously_disable_sandbox", False))
            metadata: dict[str, Any] = {"tool": self._name}
            if description:
                metadata["description"] = description
            destructive_warning = (
                get_destructive_command_warning(command)
                if isinstance(command, str)
                else None
            )
            if destructive_warning:
                metadata["destructive_warning"] = destructive_warning
            if dangerously_disable_sandbox:
                metadata["dangerously_disable_sandbox"] = True
            raw = await self._apply_simulated_sed_edit(
                simulated_edit,
                metadata=metadata,
            )
            result = self._wrap_result(raw, time.time() - start)
        except Exception as exc:
            if self.handle_errors:
                result = ToolResult(
                    status=ToolStatus.ERROR,
                    error=str(exc),
                    metadata={"tool": self.schema.name},
                )
            else:
                raise
        await self._auto_record_execution(
            {**kwargs, "_simulatedSedEdit": simulated_edit},
            result,
            time.time() - start,
        )
        return result

    # --- OpenSpace checkPermissions -------------------------------------------

    async def check_permissions(self, input: dict[str, Any], context: Any):
        """Delegate to bash_permissions.bash_tool_has_permission.

        OpenSpace behavior: ``BashTool.checkPermissions`` (BashTool.tsx L600+) calls
        ``bashToolHasPermission`` which performs the full subcommand
        cascade. Missing permission context is a runtime wiring error and
        fails closed.
        """
        from openspace.grounding.core.permissions import (
            PermissionAllow,
            bash_tool_has_permission,
            deny_missing_permission_context,
        )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        command = input.get("command", "") or ""
        if not isinstance(command, str):
            return PermissionAllow(updated_input=None)

        result = await bash_tool_has_permission(
            command=command,
            cwd=getattr(context, "cwd", _os.getcwd()),
            original_cwd=getattr(context, "original_cwd", None),
            description=input.get("description"),
            context=perm_ctx,
            tool_name=self._name,
            connector_kind="local",
            dangerously_disable_sandbox=bool(
                input.get("dangerously_disable_sandbox", False)
            ),
        )
        if isinstance(result, PermissionAllow) and isinstance(result.updated_input, dict):
            return replace(result, updated_input={**input, **result.updated_input})
        return result

    # --- OpenSpace validateInput ----------------------------------------------

    async def validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """OpenSpace ``BashTool.validateInput(input)`` (BashTool.tsx L524-538).

        Flags leading ``sleep N`` (``N >= 2``) patterns used as the first
        subcommand, steering the model toward ``run_in_background: true``
        or the Monitor tool.  OpenSpace gates this on the ``MONITOR_TOOL`` feature
        flag and the ``OPENSPACE_DISABLE_BACKGROUND_TASKS`` env var; OpenSpace
        applies the check unconditionally because the guidance is still useful.

        Returns ``None`` on success (OpenSpace ``{ result: true }``) or an error
        string (OpenSpace ``{ result: false, message, errorCode: 10 }``).
        """
        command = input.get("command", "")
        if not isinstance(command, str):
            return None

        # Skip when the model has already opted into background — that
        # is exactly the escape hatch the error message is trying to
        # recommend.
        if input.get("run_in_background"):
            return None

        sleep_pattern = detect_blocked_sleep_pattern(command)
        if sleep_pattern is None:
            return None

        # Error message is a near-verbatim copy of OpenSpace's (L530), trimmed
        # of the Monitor-tool reference because that tool is out of scope.
        return (
            f"Blocked: {sleep_pattern}. Run blocking commands in the "
            "background with run_in_background: true — you'll get the "
            "output file path immediately and can read it when the task "
            "finishes.  If you genuinely need a delay (rate limiting, "
            "deliberate pacing), keep it under 2 seconds."
        )

    # --- execution ------------------------------------------------------

    async def _arun(
        self,
        command: str,
        timeout: int = _DEFAULT_BASH_TIMEOUT_MS,
        description: str | None = None,
        run_in_background: bool = False,
        backgroundedByUser: bool | None = None,
        assistantAutoBackgrounded: bool | None = None,
        dangerously_disable_sandbox: bool = False,
    ) -> ToolResult:
        """Execute the command.

        Implementation notes:

        * ``_simulatedSedEdit``: supported as an internal-only input, matching
          OpenSpace's permission preview apply path; it is not model-visible.
        * ``persistedOutputPath`` for large foreground outputs stores the raw
          merged fd stream on disk and emits a ``<persisted-output>`` wrapper.
        * Sandbox annotation (``SandboxManager.annotateStderrWithSandboxFailures``):
          stage 25.2 annotates sandboxed command output and records sandbox
          state in metadata.
        """
        # Normalize milliseconds to seconds before delegating to the connector.
        timeout_sec = _normalize_bash_timeout_seconds(timeout)

        # Destructive-command warning is informational and can be surfaced by
        # permission prompts.
        destructive_warning = get_destructive_command_warning(command)
        if destructive_warning:
            logger.info("[bash] destructive command: %s", destructive_warning)

        metadata: dict[str, Any] = {"tool": self._name}
        if description:
            metadata["description"] = description
        if dangerously_disable_sandbox:
            metadata["dangerously_disable_sandbox"] = True
        if destructive_warning:
            metadata["destructive_warning"] = destructive_warning

        context = self._current_context
        effective_cwd = (
            getattr(context, "cwd", None)
            or getattr(self._session, "default_working_dir", None)
            or None
        )
        effective_env = dict(getattr(self._session, "default_env", None) or {})
        conda_env = getattr(self._session, "default_conda_env", None)
        sandbox_plan, sandbox_error = await self._prepare_sandbox_execution(
            command,
            dangerously_disable_sandbox=dangerously_disable_sandbox,
            metadata=metadata,
            cwd=effective_cwd,
            env=effective_env,
            conda_env=conda_env,
            connector_kind="local",
        )
        if sandbox_error is not None:
            return sandbox_error

        await self._record_output_redirection_snapshots(
            command,
            cwd=effective_cwd,
            metadata=metadata,
        )

        if run_in_background and not _background_tasks_disabled():
            return await self._run_in_background(
                command,
                description=description,
                metadata=metadata,
                sandbox_plan=sandbox_plan,
                cwd=effective_cwd,
                env=effective_env,
                conda_env=conda_env,
            )

        if (
            not _background_tasks_disabled()
            and getattr(self._current_context, "task_manager", None) is not None
        ):
            return await self._run_shell_command_with_progress(
                command,
                timeout_sec=timeout_sec,
                description=description,
                metadata=metadata,
                initial_backgrounded_by_user=bool(backgroundedByUser),
                initial_assistant_auto_backgrounded=bool(assistantAutoBackgrounded),
                sandbox_plan=sandbox_plan,
                cwd=effective_cwd,
                env=effective_env,
                conda_env=conda_env,
            )

        return await self._run_foreground_direct(
            command,
            timeout_sec=timeout_sec,
            metadata=metadata,
            sandbox_plan=sandbox_plan,
            cwd=effective_cwd,
            env=effective_env,
            conda_env=conda_env,
        )

    async def _prepare_sandbox_execution(
        self,
        command: str,
        *,
        dangerously_disable_sandbox: bool,
        metadata: dict[str, Any],
        cwd: str | None,
        env: dict[str, str],
        conda_env: str | None,
        connector_kind: str,
    ) -> tuple[_SandboxExecutionPlan | None, ToolResult | None]:
        from openspace.grounding.backends.shell.transport.local_connector import (
            _wrap_script_with_conda,
        )

        if (
            not dangerously_disable_sandbox
            and not _quick_sandbox_enabled_candidate(cwd)
        ):
            return None, None

        if not dangerously_disable_sandbox:
            from openspace.services.runtime_support.settings import get_effective_settings

            sandbox_settings = get_effective_settings(cwd).get("sandbox")
            if not (
                isinstance(sandbox_settings, dict)
                and bool(sandbox_settings.get("enabled", False))
            ):
                return None, None

        from openspace.services.sandbox import get_process_sandbox_manager

        manager = get_process_sandbox_manager(cwd=cwd)
        if not manager.is_enabled_in_settings() and not dangerously_disable_sandbox:
            return None, None

        from openspace.services.sandbox.should_use_sandbox import (
            ShouldUseSandboxInput,
            should_use_sandbox,
        )

        decision = should_use_sandbox(
            ShouldUseSandboxInput(
                command=command,
                dangerously_disable_sandbox=dangerously_disable_sandbox,
                cwd=cwd,
                connector_kind=connector_kind,
            ),
            sandbox_manager=manager,
        )
        plan = _SandboxExecutionPlan(manager=manager, decision=decision)
        metadata["sandbox"] = self._sandbox_metadata(manager, decision)

        if decision.bypassed:
            metadata["sandbox_bypassed"] = True
            metadata["sandbox"]["bypassed"] = True
            metadata["sandbox"]["bypass_reason"] = decision.reason

        if not decision.should_sandbox:
            if manager.is_sandbox_required() and decision.reason not in {
                "settings_disabled",
                "empty_command",
            }:
                return plan, ToolResult(
                    status=ToolStatus.ERROR,
                    content=(
                        "Sandbox is required but unavailable for this command: "
                        f"{decision.unavailable_reason or decision.reason}"
                    ),
                    metadata=metadata,
                )
            return plan, None

        command_for_sandbox = _wrap_script_with_conda(command, conda_env)
        try:
            wrapped = await manager.wrap_command(
                command_for_sandbox,
                cwd=cwd,
                env=env or None,
                shell="/bin/bash",
            )
        except Exception as exc:
            metadata["sandbox"]["applied"] = False
            metadata["sandbox"]["unavailable_reason"] = str(exc)
            return plan, ToolResult(
                status=ToolStatus.ERROR,
                content=f"bash failed to start sandbox: {exc}",
                metadata=metadata,
            )

        plan.wrapped = wrapped
        metadata["sandbox"].update(wrapped.to_metadata())
        metadata["sandbox"]["enabled"] = True
        metadata["sandbox"]["applied"] = True
        return plan, None

    def _sandbox_metadata(self, manager: Any, decision: Any) -> dict[str, Any]:
        warnings = [
            issue.message
            for issue in manager.check_dependencies()
            if getattr(issue, "severity", None) == "warning"
        ]
        return {
            "requested": manager.is_enabled_in_settings(),
            "enabled": manager.is_sandboxing_enabled(),
            "applied": False,
            "platform": manager.platform,
            "policy_name": manager.runtime_config().policy.name,
            "bypassed": bool(decision.bypassed),
            "bypass_reason": decision.reason if decision.bypassed else None,
            "reason": decision.reason,
            "unavailable_reason": decision.unavailable_reason,
            "dependency_warnings": warnings,
            "violation_count": 0,
        }

    async def _run_foreground_direct(
        self,
        command: str,
        *,
        timeout_sec: int,
        metadata: dict[str, Any],
        sandbox_plan: _SandboxExecutionPlan | None,
        cwd: str | None,
        env: dict[str, str],
        conda_env: str | None,
    ) -> ToolResult:
        wrapped = sandbox_plan.wrapped if sandbox_plan is not None else None
        try:
            if wrapped is not None:
                result = await self._session.connector.run_command_argv(
                    wrapped.argv,
                    timeout=timeout_sec,
                    working_dir=wrapped.cwd,
                    env=wrapped.env,
                    security_command=command,
                )
            else:
                result = await self._session.connector.run_bash_command(
                    command,
                    timeout=timeout_sec,
                    working_dir=cwd,
                    env=env or None,
                    conda_env=conda_env,
                )
            stdout, stderr, rc = _parse_shell_result(result)
            stdout, stderr = self._apply_sandbox_annotation(
                command,
                stdout or "",
                stderr or "",
                sandbox_plan,
                metadata,
            )
            tool_result = self._build_completed_result(
                command,
                stdout=stdout or "",
                stderr=stderr or "",
                rc=rc,
                metadata=metadata,
            )
            await self._emit_bash_command_executed(
                command,
                stdout=stdout or "",
                stderr=stderr or "",
                rc=rc,
                interrupted=False,
            )
            return tool_result
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"bash failed: {e}",
                metadata=metadata,
            )
        finally:
            if wrapped is not None:
                try:
                    await sandbox_plan.manager.cleanup_after_command(wrapped)
                except Exception:
                    logger.debug("sandbox cleanup failed", exc_info=True)

    async def _run_shell_command_with_progress(
        self,
        command: str,
        *,
        timeout_sec: int,
        description: str | None,
        metadata: dict[str, Any],
        initial_backgrounded_by_user: bool = False,
        initial_assistant_auto_backgrounded: bool = False,
        sandbox_plan: _SandboxExecutionPlan | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        conda_env: str | None = None,
    ) -> ToolResult:
        from openspace.agents.task_manager import (
            TaskType,
            generate_task_id,
            get_task_output_path,
        )
        from openspace.grounding.backends.shell.shell_command_handle import (
            BackgroundShellHandle,
            BackgroundShellStatus,
        )

        context = self._current_context
        manager = getattr(context, "task_manager", None)
        task_id = generate_task_id(TaskType.LOCAL_BASH)
        output_path = get_task_output_path(task_id, manager.output_dir)
        effective_cwd = cwd or getattr(context, "cwd", None) or getattr(self._session, "default_working_dir", None) or None
        effective_env = dict(env or getattr(self._session, "default_env", None) or {})
        wrapped = sandbox_plan.wrapped if sandbox_plan is not None else None

        try:
            shell_command = await BackgroundShellHandle.spawn(
                command,
                task_id=task_id,
                output_path=output_path,
                cwd=wrapped.cwd if wrapped is not None else effective_cwd,
                env=wrapped.env if wrapped is not None else (effective_env or None),
                conda_env=None if wrapped is not None else conda_env,
                argv=wrapped.argv if wrapped is not None else None,
                cleanup_callbacks=self._sandbox_cleanup_callbacks(sandbox_plan),
                output_transform=self._sandbox_output_transform(
                    command,
                    sandbox_plan,
                    metadata,
                ),
            )
        except Exception as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"bash failed to start: {exc}",
                metadata=metadata,
            )

        start = time.monotonic()
        timeout_deadline = start + timeout_sec
        budget_ms = _assistant_blocking_budget_ms()
        should_auto_background = is_autobackgrounding_allowed(command)
        progress_deadline = (
            start + (_PROGRESS_THRESHOLD_MS / 1000)
            if should_auto_background
            else float("inf")
        )
        assistant_budget_enabled = (
            should_auto_background
            and not getattr(context, "is_async_agent", False)
            and budget_ms > 0
        )
        assistant_budget_deadline = start + (budget_ms / 1000)
        foreground_task = None
        background_task_id: str | None = None
        assistant_auto_backgrounded = initial_assistant_auto_backgrounded
        backgrounded_by_user = initial_backgrounded_by_user

        async def register_foreground_if_needed() -> Any:
            nonlocal foreground_task
            if foreground_task is not None:
                return foreground_task
            foreground_task = await manager.register_local_shell_task(
                command=command,
                description=description or command,
                shell_command=shell_command,
                task_id=task_id,
                tool_use_id=metadata.get("tool_use_id"),
                agent_id=getattr(context, "agent_id", None),
                is_backgrounded=False,
                notification_queue=getattr(context, "async_rewake_queue", None),
                kind="bash",
                finalize_on_completion=False,
            )
            return foreground_task

        async def start_backgrounding(
            *,
            by_user: bool = False,
            by_assistant_budget: bool = False,
        ) -> str | None:
            nonlocal foreground_task, background_task_id
            if shell_command.result.done():
                return None
            if foreground_task is None:
                foreground_task = await manager.register_local_shell_task(
                    command=command,
                    description=description or command,
                    shell_command=shell_command,
                    task_id=task_id,
                    tool_use_id=metadata.get("tool_use_id"),
                    agent_id=getattr(context, "agent_id", None),
                    is_backgrounded=True,
                    backgrounded_by_user=by_user,
                    assistant_auto_backgrounded=by_assistant_budget,
                    notification_queue=getattr(
                        context, "async_rewake_queue", None
                    ),
                    kind="bash",
                    finalize_on_completion=True,
                )
            else:
                await manager.background_existing_foreground_shell_task(
                    foreground_task.id,
                    backgrounded_by_user=by_user,
                    assistant_auto_backgrounded=by_assistant_budget,
                )
            background_task_id = foreground_task.id
            return background_task_id

        try:
            while True:
                if shell_command.result.done():
                    result = shell_command.result.result()
                    if foreground_task is not None:
                        if getattr(foreground_task, "is_backgrounded", False):
                            manager.mark_task_notified(foreground_task.id)
                        else:
                            await manager.unregister_foreground_shell_task(
                                foreground_task.id,
                                result=result,
                            )
                    await shell_command.cleanup()
                    output = self._read_shell_output(str(output_path))
                    tool_result = self._build_completed_result(
                        command,
                        stdout=output,
                        stderr="",
                        rc=result.code,
                        metadata=metadata,
                        output_file_path=str(output_path),
                        output_task_id=task_id,
                    )
                    await self._emit_bash_command_executed(
                        command,
                        stdout=output,
                        stderr="",
                        rc=result.code,
                        interrupted=result.interrupted,
                    )
                    return tool_result

                if foreground_task is not None and (
                    getattr(foreground_task, "is_backgrounded", False)
                    or shell_command.status == BackgroundShellStatus.BACKGROUNDED
                ):
                    background_task_id = foreground_task.id
                    backgrounded_by_user = bool(
                        backgrounded_by_user
                        or getattr(foreground_task, "backgrounded_by_user", False)
                    )
                    assistant_auto_backgrounded = bool(
                        assistant_auto_backgrounded
                        or getattr(
                            foreground_task,
                            "assistant_auto_backgrounded",
                            False,
                        )
                    )
                    return await self._build_background_result(
                        command,
                        metadata=metadata,
                        task_id=background_task_id,
                        output_path=str(output_path),
                        pid=shell_command.pid,
                        backgrounded_by_user=backgrounded_by_user,
                        assistant_auto_backgrounded=assistant_auto_backgrounded,
                    )

                now = time.monotonic()
                if now >= timeout_deadline:
                    if should_auto_background:
                        maybe_task_id = await start_backgrounding()
                        if maybe_task_id is not None:
                            return await self._build_background_result(
                                command,
                                metadata=metadata,
                                task_id=maybe_task_id,
                                output_path=str(output_path),
                                pid=shell_command.pid,
                            )
                    await shell_command.kill("TERM")
                    result = await shell_command.result
                    output = self._read_shell_output(str(output_path))
                    tool_result = self._build_completed_result(
                        command,
                        stdout=output or f"Command timed out after {timeout_sec} seconds",
                        stderr="",
                        rc=result.code,
                        metadata=metadata,
                    )
                    await self._emit_bash_command_executed(
                        command,
                        stdout=output,
                        stderr="",
                        rc=result.code,
                        interrupted=True,
                    )
                    return tool_result

                if assistant_budget_enabled and now >= assistant_budget_deadline:
                    assistant_auto_backgrounded = True
                    maybe_task_id = await start_backgrounding(
                        by_assistant_budget=True
                    )
                    if maybe_task_id is not None:
                        return await self._build_background_result(
                            command,
                            metadata=metadata,
                            task_id=maybe_task_id,
                            output_path=str(output_path),
                            pid=shell_command.pid,
                            assistant_auto_backgrounded=True,
                        )

                if should_auto_background and now >= progress_deadline:
                    await register_foreground_if_needed()
                    progress_deadline = float("inf")

                next_deadline = min(timeout_deadline, progress_deadline)
                if assistant_budget_enabled:
                    next_deadline = min(next_deadline, assistant_budget_deadline)
                sleep_for = min(0.1, max(0.01, next_deadline - now))
                await asyncio.sleep(sleep_for)
        except Exception:
            if not shell_command.result.done():
                await shell_command.kill("TERM")
            await shell_command.cleanup()
            raise

    def _apply_sandbox_annotation(
        self,
        command: str,
        stdout: str,
        stderr: str,
        sandbox_plan: _SandboxExecutionPlan | None,
        metadata: dict[str, Any],
    ) -> tuple[str, str]:
        if sandbox_plan is None or sandbox_plan.wrapped is None:
            return stdout, stderr
        combined = stdout
        if stderr:
            combined = f"{combined}\n{stderr}" if combined else stderr
        annotated = sandbox_plan.manager.annotate_stderr_with_sandbox_failures(
            command,
            combined,
            command_tag=sandbox_plan.wrapped.command_tag,
        )
        self._record_sandbox_notes(
            metadata,
            combined,
            annotated,
            manager=sandbox_plan.manager,
            command_tag=sandbox_plan.wrapped.command_tag,
        )
        return annotated, ""

    def _sandbox_output_transform(
        self,
        command: str,
        sandbox_plan: _SandboxExecutionPlan | None,
        metadata: dict[str, Any] | None = None,
    ):
        if sandbox_plan is None or sandbox_plan.wrapped is None:
            return None

        def _transform(output: str) -> str:
            annotated = sandbox_plan.manager.annotate_stderr_with_sandbox_failures(
                command,
                output,
                command_tag=sandbox_plan.wrapped.command_tag,
            )
            if metadata is not None:
                self._record_sandbox_notes(
                    metadata,
                    output,
                    annotated,
                    manager=sandbox_plan.manager,
                    command_tag=sandbox_plan.wrapped.command_tag,
                )
            return annotated

        return _transform

    def _sandbox_cleanup_callbacks(
        self,
        sandbox_plan: _SandboxExecutionPlan | None,
    ) -> list[Any] | None:
        if sandbox_plan is None or sandbox_plan.wrapped is None:
            return None
        return [lambda: sandbox_plan.manager.cleanup_after_command(sandbox_plan.wrapped)]

    @staticmethod
    def _record_sandbox_notes(
        metadata: dict[str, Any],
        original: str,
        annotated: str,
        *,
        manager: Any | None = None,
        command_tag: str | None = None,
    ) -> None:
        if annotated == original:
            return
        note = annotated[len(original) :].strip() if annotated.startswith(original) else annotated
        if note:
            metadata["sandbox_notes"] = note
        sandbox_meta = metadata.get("sandbox")
        if isinstance(sandbox_meta, dict):
            sandbox_meta["violation_count"] = max(
                int(sandbox_meta.get("violation_count") or 0),
                1 if "<sandbox_violations>" in annotated else 0,
            )
            violations: list[dict[str, Any]] = []
            if manager is not None and command_tag:
                try:
                    violations = [
                        item.to_json()
                        for item in manager.get_violation_store().for_command_tag(command_tag)[-10:]
                    ]
                except Exception:
                    violations = []
            if not violations and "<sandbox_violations>" in annotated:
                violations = [{"raw_message": note}]
            if violations:
                sandbox_meta["violations"] = violations

    def _read_shell_output(self, output_path: str) -> str:
        try:
            with open(output_path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""

    def _build_completed_result(
        self,
        command: str,
        *,
        stdout: str,
        stderr: str,
        rc: int,
        metadata: dict[str, Any],
        output_file_path: str | None = None,
        output_task_id: str | None = None,
    ) -> ToolResult:
        interp = interpret_command_result(command, rc, stdout or "", stderr or "")
        output = stdout or ""
        if stderr:
            output = f"{output}\n[STDERR]\n{stderr}" if output else stderr
        if output_file_path and output_task_id:
            output = self._persist_large_shell_output(
                output,
                output_file_path=output_file_path,
                output_task_id=output_task_id,
                metadata=metadata,
            )
        if interp.is_error and rc != 0 and not output:
            output = f"Command exited with code {rc}"
        if interp.message:
            output = f"{output}\n({interp.message})" if output else f"({interp.message})"
            metadata["return_code_interpretation"] = interp.message
        metadata["exit_code"] = rc
        if output_file_path:
            metadata["output_file_path"] = output_file_path
        status = ToolStatus.ERROR if interp.is_error else ToolStatus.SUCCESS
        return ToolResult(
            status=status,
            content=output or "(no output)",
            metadata=metadata,
        )

    def _persist_large_shell_output(
        self,
        output: str,
        *,
        output_file_path: str,
        output_task_id: str,
        metadata: dict[str, Any],
    ) -> str:
        try:
            original_size = _os.path.getsize(output_file_path)
        except OSError:
            return output
        if original_size <= self.max_result_size_chars:
            return output

        try:
            if original_size > _MAX_PERSISTED_SIZE:
                _os.truncate(output_file_path, _MAX_PERSISTED_SIZE)
                metadata["output_truncated_to_bytes"] = _MAX_PERSISTED_SIZE

            from openspace.services.tooling.results import (
                PersistedToolResult,
                build_persisted_output_message,
                generate_preview,
                get_results_dir,
            )

            context = self._current_context
            results_dir = get_results_dir(getattr(context, "tool_results_dir", None))
            dest = _os.path.join(results_dir, f"{output_task_id}.txt")
            if not _os.path.exists(dest):
                try:
                    _os.link(output_file_path, dest)
                except OSError:
                    shutil.copyfile(output_file_path, dest)
            preview, has_more = generate_preview(output)
            metadata.update(
                {
                    "persisted_output_path": dest,
                    "persisted_output_size": original_size,
                    "persisted_path": dest,
                    "original_length": original_size,
                }
            )
            message = build_persisted_output_message(
                PersistedToolResult(
                    filepath=dest,
                    original_size=original_size,
                    preview=preview,
                    has_more=has_more or original_size > len(output),
                )
            )
            if original_size > _MAX_PERSISTED_SIZE:
                message += (
                    f"\nOutput truncated to {_MAX_PERSISTED_SIZE} bytes "
                    f"(original size: {original_size} bytes)."
                )
            return message
        except Exception:
            return output

    async def _build_background_result(
        self,
        command: str,
        *,
        metadata: dict[str, Any],
        task_id: str,
        output_path: str,
        pid: int | None,
        backgrounded_by_user: bool = False,
        assistant_auto_backgrounded: bool = False,
    ) -> ToolResult:
        metadata.update(
            {
                "background_task_id": task_id,
                "background_output_path": output_path,
                "background_pid": pid,
                "background_task_type": "local_bash",
                "background_semantics": "task_manager",
            }
        )
        if backgrounded_by_user:
            metadata["backgrounded_by_user"] = True
            metadata["backgroundedByUser"] = True
        if assistant_auto_backgrounded:
            metadata["assistant_auto_backgrounded"] = True
            metadata["assistantAutoBackgrounded"] = True

        if assistant_auto_backgrounded:
            content = (
                "Command exceeded the assistant-mode blocking budget "
                f"({_ASSISTANT_BLOCKING_BUDGET_MS // 1000}s) and was moved "
                f"to the background with ID: {task_id}. It is still running - "
                "you will be notified when it completes. Output is being "
                f"written to: {output_path}. In assistant mode, delegate "
                "long-running work to a subagent or use run_in_background "
                "to keep this conversation responsive. If you need the output "
                "before continuing and TaskGet is available in this turn, use "
                f"`TaskGet(task_id=\"{task_id}\", block=true, timeout=600000)`. "
                "Otherwise inspect it with a short bash command such as "
                f"`cat {output_path}` or `tail {output_path}`."
            )
        elif backgrounded_by_user:
            content = (
                f"Command was manually backgrounded by user with ID: {task_id}. "
                f"Output is being written to: {output_path}. Inspect it with "
                f"`cat {output_path}` or `tail {output_path}`."
            )
        else:
            content = (
                f"Command running in background with ID: {task_id}. "
                f"Output is being written to: {output_path} (pid {pid}).\n"
                "If TaskGet is available in this turn, inspect output with "
                f"`TaskGet(task_id=\"{task_id}\", block=true, timeout=600000)`. "
                "Otherwise use a short bash command such as "
                f"`cat {output_path}` or `tail {output_path}`."
            )
        await self._emit_bash_command_executed(
            command,
            stdout="",
            stderr="",
            rc=0,
            interrupted=False,
        )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata=metadata,
        )

    async def _record_output_redirection_snapshots(
        self,
        command: str,
        *,
        cwd: str | None,
        metadata: dict[str, Any],
    ) -> None:
        context = self._current_context
        if context is None:
            return
        try:
            parsed = extract_output_redirections(command)
        except Exception:
            return
        if parsed.has_dangerous_redirection or not parsed.redirections:
            return
        targets: list[str] = []
        for redirection in parsed.redirections:
            path = self._resolve_redirection_target(redirection.target, cwd=cwd)
            if path is None:
                continue
            await record_snapshot(path, context=context)
            targets.append(str(path))
        if targets:
            metadata["file_history_redirection_paths"] = targets

    def _resolve_redirection_target(
        self,
        target: str,
        *,
        cwd: str | None,
    ) -> str | None:
        raw = str(target or "").strip()
        if not raw:
            return None
        if raw in {"/dev/null", "/dev/stdout", "/dev/stderr"}:
            return None
        raw = raw.strip("'\"")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            base = Path(cwd or getattr(self._current_context, "cwd", None) or _os.getcwd())
            path = base / path
        try:
            return str(path.resolve(strict=False))
        except OSError:
            return str(path)

    async def _emit_bash_command_executed(
        self,
        command: str,
        *,
        stdout: str,
        stderr: str,
        rc: int,
        interrupted: bool,
    ) -> None:
        context = self._current_context
        if context is None or not hasattr(context, "emit_event"):
            return
        await context.emit_event(
            "bash_tool_command_executed",
            {
                "command_type": (command.split(" ")[0] if command else "other"),
                "stdout_length": len(stdout or ""),
                "stderr_length": len(stderr or ""),
                "exit_code": rc,
                "interrupted": interrupted,
            },
        )

    async def _apply_simulated_sed_edit(
        self,
        simulated_edit: dict[str, Any],
        *,
        metadata: dict[str, Any],
    ) -> ToolResult:
        file_path = simulated_edit.get("filePath") or simulated_edit.get("file_path")
        new_content = simulated_edit.get("newContent", simulated_edit.get("new_content"))
        if not isinstance(file_path, str) or not isinstance(new_content, str):
            return ToolResult(
                status=ToolStatus.ERROR,
                content="Invalid simulated sed edit payload.",
                metadata=metadata,
            )

        base_dir = (
            getattr(self._current_context, "cwd", None)
            or getattr(self._session, "default_working_dir", None)
            or _os.getcwd()
        )
        absolute_path = file_path if _os.path.isabs(file_path) else _os.path.join(base_dir, file_path)
        try:
            raw_original = b""
            try:
                with open(absolute_path, "rb") as handle:
                    raw_original = handle.read()
            except FileNotFoundError:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content=f"sed: {file_path}: No such file or directory\nExit code 1",
                    metadata={**metadata, "exit_code": 1},
                )

            await record_snapshot(absolute_path, context=self._current_context)

            is_utf16le = (
                len(raw_original) >= 2
                and raw_original[0] == 0xFF
                and raw_original[1] == 0xFE
            )
            has_crlf = b"\r\n" in raw_original
            write_content = new_content.replace("\n", "\r\n") if has_crlf else new_content
            if is_utf16le:
                with open(absolute_path, "wb") as handle:
                    handle.write(write_content.encode("utf-16-le"))
            else:
                with open(absolute_path, "w", encoding="utf-8") as handle:
                    handle.write(write_content)

            ctx = self._current_context
            if ctx is not None and hasattr(ctx, "read_file_state"):
                ctx.read_file_state[absolute_path] = ReadFileEntry(
                    content=new_content,
                    timestamp=_os.stat(absolute_path).st_mtime_ns,
                    offset=None,
                    limit=None,
                    is_partial_view=False,
                )

            return ToolResult(
                status=ToolStatus.SUCCESS,
                content="(no output)",
                metadata={**metadata, "simulated_sed_edit": True, "file_path": absolute_path},
            )
        except Exception as exc:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"sed edit failed: {exc}",
                metadata=metadata,
            )

    async def _run_in_background(
        self,
        command: str,
        *,
        description: str | None,
        metadata: dict[str, Any],
        sandbox_plan: _SandboxExecutionPlan | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        conda_env: str | None = None,
    ) -> ToolResult:
        """Launch *command* detached, stream output to a task file.

        Implementation: the ``backgroundTaskId`` branch of ``runShellCommand``
        (BashTool.tsx L854-...).  OpenSpace owns a full ``TaskManager`` (app-state
        map of running tasks, Ctrl+B interrupt, completion notifications,
        auto-backgrounding budget).  OS has none of that infrastructure
        yet, so this implementation delivers the *contract* only:

        * spawn the command detached via ``asyncio.create_subprocess_shell``;
        * merge stdout+stderr into ``_get_bg_task_output_path(task_id)``;
        * return immediately with the task id + output path so the model
          can ``read(output_path)`` when it chooses.

        Deferred to a later phase (documented so reviewers know):

        * assistant-mode auto-backgrounding when foreground exceeds a
          budget (OpenSpace ``assistantAutoBackgrounded``);
        * user-initiated backgrounding via Ctrl+B
          (OpenSpace ``backgroundedByUser``);
        * completion notifications and task-state querying
          (OpenSpace ``tengu_bash_tool_command_executed`` + TaskManager hooks);
        * 64 MB truncation of persisted output
          (OpenSpace ``MAX_PERSISTED_SIZE``).
        """
        manager = getattr(self._current_context, "task_manager", None)
        wrapped = sandbox_plan.wrapped if sandbox_plan is not None else None
        if manager is not None:
            try:
                effective_cwd = (
                    cwd
                    or getattr(self._current_context, "cwd", None)
                    or getattr(self._session, "default_working_dir", None)
                    or None
                )
                effective_env = dict(env or getattr(self._session, "default_env", None) or {})
                task = await manager.spawn_local_shell_task(
                    command=command,
                    description=description or command,
                    cwd=wrapped.cwd if wrapped is not None else effective_cwd,
                    env=wrapped.env if wrapped is not None else (effective_env or None),
                    conda_env=None if wrapped is not None else conda_env,
                    argv=wrapped.argv if wrapped is not None else None,
                    cleanup_callbacks=self._sandbox_cleanup_callbacks(sandbox_plan),
                    output_transform=self._sandbox_output_transform(
                        command,
                        sandbox_plan,
                        metadata,
                    ),
                    agent_id=getattr(self._current_context, "agent_id", None),
                    notification_queue=getattr(
                        self._current_context, "async_rewake_queue", None
                    ),
                    is_backgrounded=True,
                    kind="bash",
                )
            except Exception as exc:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    content=f"bash failed to start background task: {exc}",
                    metadata=metadata,
                )

            metadata.update(
                {
                    "background_task_id": task.id,
                    "background_output_path": task.output_file,
                    "background_pid": task.pid,
                    "background_task_type": task.type.value,
                    "background_semantics": "task_manager",
                }
            )
            content = (
                f"Command running in background with ID: {task.id}. "
                f"Output is being written to: {task.output_file} (pid {task.pid}).\n"
                "If TaskGet is available in this turn, inspect output with "
                f"`TaskGet(task_id=\"{task.id}\", block=true, timeout=600000)`. "
                "Otherwise use a short bash command such as "
                f"`cat {task.output_file}` or `tail {task.output_file}`."
            )
            await self._emit_bash_command_executed(
                command,
                stdout="",
                stderr="",
                rc=0,
                interrupted=False,
            )
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=content,
                metadata=metadata,
            )

        task_id = f"bash-{uuid.uuid4().hex[:12]}"
        output_path = _get_bg_task_output_path(task_id)

        # Use /dev/null for stdin and redirect stderr → stdout so the
        # merged stream matches OpenSpace's ``merged fd`` convention.
        output_file = open(output_path, "wb")
        try:
            if wrapped is not None:
                process = await asyncio.create_subprocess_exec(
                    *wrapped.argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=output_file,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=wrapped.cwd,
                    env={**_os.environ, **wrapped.env},
                    start_new_session=True,
                )
            else:
                from openspace.grounding.backends.shell.transport.local_connector import (
                    _wrap_script_with_conda,
                )

                final_command = _wrap_script_with_conda(command, conda_env)
                effective_env = {**_os.environ, **(env or {})}
                bash_path = shutil.which("bash", path=effective_env.get("PATH"))
                if bash_path:
                    process = await asyncio.create_subprocess_exec(
                        bash_path,
                        "-lc",
                        final_command,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=output_file,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd or None,
                        env=effective_env,
                        start_new_session=True,  # detach from parent process group
                    )
                else:
                    process = await asyncio.create_subprocess_shell(
                        final_command,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=output_file,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd or None,
                        env=effective_env,
                        start_new_session=True,  # detach from parent process group
                    )
        except Exception as e:
            output_file.close()
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"bash failed to start background task: {e}",
                metadata=metadata,
            )

        # Intentionally do NOT await process.wait() — we release control
        # to the caller immediately.  We close our copy of the fd because
        # the child already inherited it; the child keeps writing.
        output_file.close()
        if wrapped is not None:
            async def _cleanup_detached() -> None:
                await process.wait()
                try:
                    current = ""
                    try:
                        with open(output_path, "r", encoding="utf-8", errors="replace") as handle:
                            current = handle.read()
                    except OSError:
                        current = ""
                    transform = self._sandbox_output_transform(command, sandbox_plan, metadata)
                    if transform is not None:
                        transformed = transform(current)
                        if transformed != current:
                            with open(output_path, "w", encoding="utf-8", errors="replace") as handle:
                                handle.write(transformed)
                    await sandbox_plan.manager.cleanup_after_command(wrapped)
                except Exception:
                    logger.debug("detached sandbox cleanup failed", exc_info=True)

            task = asyncio.create_task(_cleanup_detached())
            _HOOK_BACKGROUND_TASKS.add(task)
            task.add_done_callback(_HOOK_BACKGROUND_TASKS.discard)

        metadata.update({
            "background_task_id": task_id,
            "background_output_path": output_path,
            "background_pid": process.pid,
        })

        content = (
            f"Command running in background with ID: {task_id}. "
            f"Output is being written to: {output_path} (pid {process.pid}).\n"
            "If TaskGet is available in this turn, inspect output with "
            f"`TaskGet(task_id=\"{task_id}\", block=true, timeout=600000)`. "
            "Otherwise use a short bash command such as "
            f"`cat {output_path}` or `tail {output_path}`."
        )
        await self._emit_bash_command_executed(
            command,
            stdout="",
            stderr="",
            rc=0,
            interrupted=False,
        )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata=metadata,
        )
