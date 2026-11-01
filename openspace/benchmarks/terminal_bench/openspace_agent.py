from __future__ import annotations

import os
import shlex
import shutil
import tempfile
import json
from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession


_TERMINAL_BENCH_PREAMBLE = """You are running inside a Terminal-Bench task container.
Use the available shell and file tools to inspect the working directory, make the required changes, and verify the result. Do not stop after describing what to do; only provide a final response after the task is actually complete.

Task:
"""


def _bool_env(value: bool | str | int) -> str:
    if isinstance(value, str):
        truthy = {"1", "true", "yes", "y", "on"}
        return "true" if value.strip().lower() in truthy else "false"
    return "true" if bool(value) else "false"


_PROVIDER_API_KEY_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY", "OR_API_KEY"),
}

_PROVIDER_DEFAULT_API_BASE = {
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
}


def _model_provider(model: str) -> str:
    if "/" not in str(model or ""):
        return ""
    provider = str(model).split("/", 1)[0].lower()
    if provider == "dpsk":
        return "deepseek"
    if provider == "or":
        return "openrouter"
    return provider


def _provider_key_env_names(provider: str) -> tuple[str, ...]:
    return _PROVIDER_API_KEY_ENV.get(provider, ())


def _host_config_api_key(model: str | None) -> str | None:
    for loader_path, function_name in (
        ("openspace.host_detection.nanobot", "try_read_nanobot_config"),
        ("openspace.host_detection.openclaw", "try_read_openclaw_config"),
    ):
        try:
            module_name = __import__(loader_path, fromlist=[function_name])
            loader = getattr(module_name, function_name)
            config = loader(model)
        except Exception:
            continue
        if not isinstance(config, dict):
            continue
        key = config.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def _normalize_model(model: object) -> str:
    text = str(model or "").strip()
    if text.lower().startswith("dpsk/"):
        return f"deepseek/{text.split('/', 1)[1]}"
    if text.lower().startswith("or/"):
        return f"openrouter/{text.split('/', 1)[1]}"
    if text.lower().startswith("deepseek-"):
        return f"deepseek/{text}"
    return text


class OpenSpaceTerminalBenchAgent(BaseAgent):
    """Run the local OpenSpace source tree inside a Terminal-Bench task container."""

    INSTALL_FAILED_MARKER = "OPENSPACE_TB_INSTALL_FAILED"
    RUN_FAILED_MARKER = "OPENSPACE_TB_RUN_FAILED"

    @staticmethod
    def name() -> str:
        return "openspace"

    def __init__(
        self,
        model_name: str | None = None,
        repo_path: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_iterations: int = 30,
        backend_scope: str = "shell,meta",
        workspace_dir: str = "/app",
        permission_mode: str = "bypassPermissions",
        llm_max_retries: int = 0,
        llm_max_tokens: int = 4096,
        evolution_enabled: bool | str = True,
        evolution_mode: str = "autonomous",
        evolution_allow_single_observation_capture: bool | str = True,
        skill_trust_promotion_min_independent_successes: int = 2,
        evolution_routing_eval_enabled: bool | str = False,
        evolution_behavior_eval_require_replay_runner: bool | str = False,
        quality_signal_enabled: bool | str = True,
        evidence_db_path: str = "/installed-agent/openspace-evidence.db",
        recording_enabled: bool | str = True,
        recording_log_dir: str = "/installed-agent/openspace-recordings",
        enable_screenshot: bool | str = False,
        enable_video: bool | str = False,
        enable_conversation_log: bool | str = True,
        debug_tool_calls: bool | str = False,
        bench_checker_failure_guard: bool | str = True,
        log_level: str = "INFO",
        install_timeout_sec: float = 900.0,
        run_timeout_sec: float = float("inf"),
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._model_name = _normalize_model(
            model_name
            or os.environ.get("OPENSPACE_MODEL")
            or "openrouter/qwen/qwen3.7-max"
        )
        self._repo_path = Path(repo_path).resolve() if repo_path else self._default_repo_path()
        self._api_key = api_key
        self._base_url = base_url
        self._max_iterations = int(max_iterations)
        self._backend_scope = backend_scope
        self._workspace_dir = workspace_dir
        self._permission_mode = permission_mode
        self._llm_max_retries = int(llm_max_retries)
        self._llm_max_tokens = int(llm_max_tokens)
        self._evolution_enabled = evolution_enabled
        self._evolution_mode = evolution_mode
        self._evolution_allow_single_observation_capture = (
            evolution_allow_single_observation_capture
        )
        self._skill_trust_promotion_min_independent_successes = max(
            1,
            int(skill_trust_promotion_min_independent_successes),
        )
        self._evolution_routing_eval_enabled = evolution_routing_eval_enabled
        self._evolution_behavior_eval_require_replay_runner = (
            evolution_behavior_eval_require_replay_runner
        )
        self._quality_signal_enabled = quality_signal_enabled
        self._evidence_db_path = evidence_db_path
        self._recording_enabled = recording_enabled
        self._recording_log_dir = recording_log_dir
        self._enable_screenshot = enable_screenshot
        self._enable_video = enable_video
        self._enable_conversation_log = enable_conversation_log
        self._debug_tool_calls = debug_tool_calls
        self._bench_checker_failure_guard = bench_checker_failure_guard
        self._log_level = log_level
        self._install_timeout_sec = float(install_timeout_sec)
        self._run_timeout_sec = float(run_timeout_sec)

    @staticmethod
    def _default_repo_path() -> Path:
        return Path(__file__).resolve().parents[2]

    def _env(self) -> dict[str, str]:
        provider = _model_provider(self._model_name)
        api_key = self._api_key
        key_env_name = "OPENSPACE_LLM_API_KEY" if api_key else None
        if not api_key:
            for env_name in _provider_key_env_names(provider):
                api_key = os.environ.get(env_name)
                if api_key:
                    key_env_name = env_name
                    break
        if not api_key and provider == "openrouter":
            api_key = _host_config_api_key(self._model_name)
            if api_key:
                provider_env_names = _provider_key_env_names(provider)
                key_env_name = (
                    provider_env_names[0]
                    if provider_env_names
                    else "OPENSPACE_LLM_API_KEY"
                )
        if not api_key:
            api_key = os.environ.get("OPENSPACE_LLM_API_KEY")
            key_env_name = "OPENSPACE_LLM_API_KEY" if api_key else None
        if not api_key:
            expected = ", ".join(
                ("OPENSPACE_LLM_API_KEY", *_provider_key_env_names(provider))
            )
            raise ValueError(
                "LLM API key is not set for model "
                f"{self._model_name!r}. Set one of: {expected}; "
                "or pass --agent-kwarg api_key=..."
            )

        env = {
            "OPENSPACE_MODEL": self._model_name,
            "OPENSPACE_MAX_ITERATIONS": str(self._max_iterations),
            "OPENSPACE_BACKEND_SCOPE": self._backend_scope,
            "OPENSPACE_WORKSPACE": self._workspace_dir,
            "OPENSPACE_SHELL_WORKING_DIR": self._workspace_dir,
            "OPENSPACE_PERMISSION_MODE": self._permission_mode,
            "OPENSPACE_MAX_RETRIES": str(self._llm_max_retries),
            "OPENSPACE_REQUIRE_TOOL_USE": "true",
            "OPENSPACE_REQUIRE_TOOL_USE_MAX_NUDGES": "3",
            "OPENSPACE_FORCE_TOOL_ON_MAX_OUTPUT_RECOVERY": "true",
            "OPENSPACE_BENCH_STRICT_NO_TOOL_FINAL": "true",
            "OPENSPACE_BENCH_NO_TOOL_FINAL_MAX_NUDGES": "2",
            "OPENSPACE_BENCH_CHECKER_FAILURE_GUARD": _bool_env(
                self._bench_checker_failure_guard
            ),
            "OPENSPACE_BENCH_CHECKER_FAILURE_MAX_NUDGES": "2",
            "OPENSPACE_DEBUG_TOOL_CALLS": _bool_env(self._debug_tool_calls),
            "OPENSPACE_PARSE_TEXT_TOOL_CALLS": "true",
            "OPENSPACE_LLM_CONFIG": json.dumps({"max_tokens": self._llm_max_tokens}),
            "OPENSPACE_ENABLE_RECORDING": _bool_env(self._recording_enabled),
            "OPENSPACE_SKIP_DOTENV": "1",
            "OPENSPACE_LOG_LEVEL": self._log_level,
            "OPENSPACE_CAPTURE_SKILL_DIR": "/installed-agent/evolved-skills",
            "OPENSPACE_EVOLUTION_EVIDENCE_DB_PATH": self._evidence_db_path,
            "OPENSPACE_EVOLUTION_EVIDENCE_ENABLED": _bool_env(self._evolution_enabled),
            "OPENSPACE_EVOLUTION_TRIGGERS_ENABLED": _bool_env(self._evolution_enabled),
            "OPENSPACE_EVOLUTION_ENGINE_ENABLED": _bool_env(self._evolution_enabled),
            "OPENSPACE_EVOLUTION_MODE": self._evolution_mode,
            "OPENSPACE_EVOLUTION_ALLOW_SINGLE_OBSERVATION_CAPTURE": _bool_env(
                self._evolution_allow_single_observation_capture
            ),
            "OPENSPACE_SKILL_TRUST_PROMOTION_MIN_INDEPENDENT_SUCCESSES": str(
                self._skill_trust_promotion_min_independent_successes
            ),
            "OPENSPACE_EVOLUTION_ROUTING_EVAL_ENABLED": _bool_env(
                self._evolution_routing_eval_enabled
            ),
            "OPENSPACE_EVOLUTION_BEHAVIOR_EVAL_REQUIRE_REPLAY_RUNNER": _bool_env(
                self._evolution_behavior_eval_require_replay_runner
            ),
            "OPENSPACE_QUALITY_SIGNAL_DETECTOR_ENABLED": _bool_env(
                self._quality_signal_enabled
            ),
            "OPENSPACE_QUALITY_SIGNAL_TRIGGER_ENABLED": _bool_env(
                self._quality_signal_enabled
            ),
            "OPENSPACE_QUALITY_SIGNAL_RECONCILIATION_ENABLED": _bool_env(
                self._quality_signal_enabled
            ),
        }
        if key_env_name:
            env[key_env_name] = api_key
        for native_env_name in _provider_key_env_names(provider):
            env.setdefault(native_env_name, api_key)

        base_url = (
            self._base_url
            or _PROVIDER_DEFAULT_API_BASE.get(provider)
            or os.environ.get("OPENSPACE_LLM_API_BASE")
        )
        if base_url:
            env["OPENSPACE_LLM_API_BASE"] = base_url

        llm_config = os.environ.get("OPENSPACE_LLM_CONFIG")
        if llm_config:
            env["OPENSPACE_LLM_CONFIG"] = llm_config

        extra_headers = os.environ.get("OPENSPACE_LLM_EXTRA_HEADERS")
        if extra_headers:
            env["OPENSPACE_LLM_EXTRA_HEADERS"] = extra_headers

        return env

    def _write_env_file(self, session: TmuxSession) -> None:
        env_content = "\n".join(
            f"export {key}={shlex.quote(value)}" for key, value in self._env().items()
        )
        session.container.exec_run(["mkdir", "-p", "/installed-agent"])
        session.container.exec_run(
            [
                "sh",
                "-c",
                (
                    "printf %s "
                    f"{shlex.quote(env_content)} > /installed-agent/openspace-env.sh"
                ),
            ]
        )

    def _copy_minimal_source(self, session: TmuxSession) -> None:
        if not self._repo_path.exists():
            raise FileNotFoundError(f"OpenSpace repo path does not exist: {self._repo_path}")

        with tempfile.TemporaryDirectory(prefix="openspace-tbench-src-") as tmp:
            tmp_path = Path(tmp)
            for name in ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE"):
                source = self._repo_path / name
                if source.exists():
                    shutil.copy2(source, tmp_path / name)

            shutil.copytree(
                self._repo_path / "openspace",
                tmp_path / "openspace",
                ignore=shutil.ignore_patterns(
                    ".env",
                    ".env.*",
                    "__pycache__",
                    "*.pyc",
                    ".pytest_cache",
                    "logs",
                    "recordings",
                ),
            )

            session.copy_to_container(
                tmp_path,
                container_dir="/installed-agent/openspace-src",
            )

    @staticmethod
    def _run_bash(session: TmuxSession, script: str, timeout_sec: float) -> None:
        session.send_keys(
            [f"bash -lc {shlex.quote(script)}", "Enter"],
            block=True,
            max_timeout_sec=timeout_sec,
        )

    def _install_openspace(self, session: TmuxSession) -> bool:
        install_script = f"""
set -e
source /installed-agent/openspace-env.sh
cd /installed-agent/openspace-src
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install --break-system-packages -e . || python3 -m pip install -e .
""".strip()
        self._run_bash(
            session,
            f"{install_script} || echo {self.INSTALL_FAILED_MARKER}",
            self._install_timeout_sec,
        )
        return self.INSTALL_FAILED_MARKER not in session.capture_pane(capture_entire=True)

    def _run_openspace(self, instruction: str, session: TmuxSession) -> bool:
        task_only_instruction = instruction
        instruction = _TERMINAL_BENCH_PREAMBLE + task_only_instruction
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as task_file:
            task_file.write(instruction)
            task_file_path = Path(task_file.name)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as config_file:
            json.dump(
                {
                    "workspace_dir": self._workspace_dir,
                    "capture_skill_dir": "/installed-agent/evolved-skills",
                    "llm_max_retries": self._llm_max_retries,
                    "evolution_allow_single_observation_capture": (
                        _bool_env(self._evolution_allow_single_observation_capture)
                        == "true"
                    ),
                    "skill_trust_promotion_min_independent_successes": (
                        self._skill_trust_promotion_min_independent_successes
                    ),
                    "tool_retrieval_query": task_only_instruction,
                    "recording_log_dir": self._recording_log_dir,
                    "enable_screenshot": _bool_env(self._enable_screenshot) == "true",
                    "enable_video": _bool_env(self._enable_video) == "true",
                    "enable_conversation_log": (
                        _bool_env(self._enable_conversation_log) == "true"
                    ),
                },
                config_file,
            )
            config_file_path = Path(config_file.name)

        try:
            session.copy_to_container(
                task_file_path,
                container_dir="/installed-agent",
                container_filename="task.txt",
            )
            session.copy_to_container(
                config_file_path,
                container_dir="/installed-agent",
                container_filename="openspace-run-config.json",
            )
        finally:
            task_file_path.unlink(missing_ok=True)
            config_file_path.unlink(missing_ok=True)

        run_script = f"""
set -e
source /installed-agent/openspace-env.sh
cd "$OPENSPACE_WORKSPACE"
python3 -c 'import os; from openspace.grounding.core.permissions import set_session_permission_mode; set_session_permission_mode(os.environ.get("OPENSPACE_PERMISSION_MODE", "bypassPermissions"), os.environ.get("OPENSPACE_WORKSPACE", "/app"))'
python3 -m openspace.entrypoints.cli.main \\
  --config /installed-agent/openspace-run-config.json \\
  --no-ui \\
  --no-tui \\
  --model "$OPENSPACE_MODEL" \\
  --max-iterations "$OPENSPACE_MAX_ITERATIONS" \\
  --query "$(cat /installed-agent/task.txt)"
""".strip()
        self._run_bash(
            session,
            f"{run_script} || echo {self.RUN_FAILED_MARKER}",
            self._run_timeout_sec,
        )
        return self.RUN_FAILED_MARKER not in session.capture_pane(capture_entire=True)

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        rendered_instruction = self._render_instruction(instruction)
        if logging_dir is not None:
            logging_dir.mkdir(parents=True, exist_ok=True)
            (logging_dir / "instruction.txt").write_text(
                rendered_instruction,
                encoding="utf-8",
            )

        self._copy_minimal_source(session)
        self._write_env_file(session)

        if not self._install_openspace(session):
            return AgentResult(failure_mode=FailureMode.AGENT_INSTALLATION_FAILED)

        if not self._run_openspace(rendered_instruction, session):
            return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)

        return AgentResult(failure_mode=FailureMode.NONE)
