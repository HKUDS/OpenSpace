"""
System health checker that runs diagnostics on the OpenSpace environment.

Checks are grouped into Python-owned sections and streamed to the TUI as
sectioned results. The TUI remains a merge/render layer only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable, Optional

from openspace.protocol import CoreToTuiEvent
from openspace.communication.config import CommunicationConfig
from openspace.communication.config import load_communication_config
from openspace.config.grounding import GroundingConfig
from openspace.grounding.core.permissions import build_permission_rules_snapshot
from openspace.prompts import GroundingAgentPrompts
from openspace.skill_engine.skill_ranker import PREFILTER_THRESHOLD
from openspace.utils.logging import Logger
from pydantic import ValidationError

logger = Logger.get_logger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
_LOCK_FILE = Path.home() / ".openspace" / "lock"
_MCP_ALLOWED_TRANSPORTS = {"stdio", "sse", "streamable-http", "websocket"}
_RUNTIME_SERVICE_GETTERS = {
    "execution_analyzer": "get_execution_analyzer",
    "grounding_agent": "get_grounding_agent",
    "grounding_client": "get_grounding_client",
    "grounding_config": "get_grounding_config",
    "recording_manager": "get_recording_manager",
    "skill_registry": "get_skill_registry",
}


@dataclass(slots=True)
class CheckResult:
    status: str  # "pass", "warn", "fail"
    name: str
    description: str
    details: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "name": self.name,
            "message": self.description,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(slots=True)
class DoctorSection:
    key: str
    title: str
    checks: tuple[str, ...]


class Doctor:
    """Run a suite of environment health checks."""

    def __init__(
        self,
        tui_bridge: Any | None = None,
        openspace: Any | None = None,
    ) -> None:
        self._tui_bridge = tui_bridge
        self._openspace = openspace

    def _runtime_service(self, name: str) -> Any | None:
        if self._openspace is None:
            return None
        getter_name = _RUNTIME_SERVICE_GETTERS.get(name)
        if getter_name is None:
            return None
        getter = getattr(self._openspace, getter_name, None)
        if not callable(getter):
            return None
        return getter()

    async def run_all(self) -> list[CheckResult]:
        run_id = uuid.uuid4().hex
        sections = self._build_sections()
        all_results: list[CheckResult] = []

        for order, section in enumerate(sections):
            section_results: list[CheckResult] = []
            for check_name in section.checks:
                result = getattr(self, check_name)()
                section_results.append(result)
                all_results.append(result)
                await self._stream_section(
                    run_id=run_id,
                    section=section,
                    order=order,
                    checks=[result],
                    section_results=section_results,
                    section_done=False,
                    run_done=False,
                    summary=self._section_summary(section.title, section_results),
                )

            await self._stream_section(
                run_id=run_id,
                section=section,
                order=order,
                checks=[],
                section_results=section_results,
                section_done=True,
                run_done=order == len(sections) - 1,
                summary=self._section_summary(section.title, section_results),
            )

        return all_results

    def _build_sections(self) -> list[DoctorSection]:
        return [
            DoctorSection(
                key="diagnostics",
                title="Diagnostics",
                checks=(
                    "check_python_version",
                    "check_openspace_version",
                    "check_installation_path",
                    "check_invoked_binary",
                    "check_nodejs",
                    "check_ripgrep",
                ),
            ),
            DoctorSection(
                key="updates",
                title="Updates",
                checks=(
                    "check_update_channel",
                ),
            ),
            DoctorSection(
                key="configuration",
                title="Configuration",
                checks=(
                    "check_config",
                    "check_agent_config",
                    "check_communication_config",
                    "check_env_vars",
                ),
            ),
            DoctorSection(
                key="invalid_settings",
                title="Invalid Settings",
                checks=(
                    "check_grounding_settings_validation",
                    "check_agents_settings_validation",
                    "check_communication_settings_validation",
                    "check_env_override_validation",
                ),
            ),
            DoctorSection(
                key="permissions",
                title="Permissions",
                checks=(
                    "check_permission_mode",
                    "check_permission_rules",
                    "check_permission_rule_precedence",
                ),
            ),
            DoctorSection(
                key="sandbox",
                title="Sandbox",
                checks=(
                    "check_sandbox_settings",
                    "check_workspace_access",
                    "check_temp_dir",
                ),
            ),
            DoctorSection(
                key="mcp",
                title="MCP Diagnostics",
                checks=(
                    "check_mcp_servers",
                    "check_mcp_server_definitions",
                    "check_mcp_env_override",
                    "check_mcp_sessions",
                ),
            ),
            DoctorSection(
                key="plugins",
                title="Plugins & Skills",
                checks=(
                    "check_plugins",
                    "check_skill_directories",
                    "check_skill_registry",
                    "check_skill_parse_errors",
                    "check_recording",
                    "check_execution_analyzer",
                    "check_tool_quality",
                ),
            ),
            DoctorSection(
                key="sessions",
                title="Sessions",
                checks=(
                    "check_sessions_dir",
                    "check_lock_file",
                ),
            ),
            DoctorSection(
                key="context",
                title="Context",
                checks=(
                    "check_current_project",
                    "check_workspace_setting",
                    "check_backend_scope",
                ),
            ),
            DoctorSection(
                key="context_usage",
                title="Context Usage Warnings",
                checks=(
                    "check_system_prompt_context_pressure",
                    "check_skill_selection_context_pressure",
                    "check_skill_injection_context_pressure",
                    "check_channel_context_pressure",
                ),
            ),
        ]

    def check_python_version(self) -> CheckResult:
        vi = sys.version_info
        version_str = f"{vi.major}.{vi.minor}.{vi.micro}"
        if vi >= (3, 12):
            return CheckResult("pass", "Python", f"Python {version_str}")
        if vi >= (3, 10):
            return CheckResult(
                "warn",
                "Python",
                f"Python {version_str} (>= 3.12 recommended)",
            )
        return CheckResult(
            "fail",
            "Python",
            f"Python {version_str} is unsupported",
            details="Upgrade to Python 3.12 or later.",
        )

    def check_openspace_version(self) -> CheckResult:
        version = self._get_package_version()
        if version is None:
            return CheckResult(
                "fail",
                "OpenSpace",
                "openspace package not importable",
            )

        install_type = self._detect_install_type()
        return CheckResult(
            "pass",
            "OpenSpace",
            f"Version {version}",
            details=f"Install source: {install_type}",
        )

    def check_installation_path(self) -> CheckResult:
        package_path = self._get_package_path()
        if package_path is None:
            return CheckResult(
                "fail",
                "Installation Path",
                "Unable to resolve the openspace package path",
            )
        return CheckResult(
            "pass",
            "Installation Path",
            str(package_path),
        )

    def check_invoked_binary(self) -> CheckResult:
        python_executable = str(Path(sys.executable).resolve())
        argv0 = (sys.argv[0] or "").strip() if sys.argv else ""

        if not argv0:
            return CheckResult(
                "warn",
                "Invoked Binary",
                "Unable to determine argv[0]",
                details=f"Python executable: {python_executable}",
            )

        invoked_path = Path(argv0).expanduser()
        try:
            invoked_path = invoked_path.resolve()
        except OSError:
            pass

        return CheckResult(
            "pass",
            "Invoked Binary",
            str(invoked_path),
            details=f"Python executable: {python_executable}",
        )

    def check_nodejs(self) -> CheckResult:
        node = shutil.which("node")
        if not node:
            return CheckResult(
                "fail",
                "Node.js",
                "Node.js not found on PATH",
                details="Node.js is required for the TUI.",
            )
        try:
            out = subprocess.run(
                [node, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            version = out.stdout.strip() or out.stderr.strip() or "unknown"
        except (subprocess.TimeoutExpired, OSError):
            version = "unknown"
        return CheckResult("pass", "Node.js", f"{version} at {node}")

    def check_ripgrep(self) -> CheckResult:
        rg = shutil.which("rg")
        if rg:
            return CheckResult("pass", "ripgrep", f"Found at {rg}")
        return CheckResult(
            "warn",
            "ripgrep",
            "ripgrep (rg) not found on PATH",
            details="Install ripgrep for faster code search.",
        )

    def check_update_channel(self) -> CheckResult:
        version = self._get_package_version() or "unknown"
        install_type = self._detect_install_type()
        package_path = self._get_package_path()
        return CheckResult(
            "pass",
            "Installed Version",
            f"{version} ({install_type})",
            details=f"Package path: {package_path}" if package_path else None,
        )

    def check_config(self) -> CheckResult:
        required = [
            "config_grounding.json",
            "config_security.json",
            "config_agents.json",
        ]
        optional = [
            "config_mcp.json",
            "config_communication.json",
        ]
        missing: list[str] = []
        invalid: list[str] = []
        present_optional: list[str] = []

        for name in required + optional:
            path = _CONFIG_DIR / name
            if not path.exists():
                if name in required:
                    missing.append(name)
                continue

            if name in optional:
                present_optional.append(name)

            if self._load_json_path(path) is None:
                invalid.append(name)

        if missing or invalid:
            parts: list[str] = []
            if missing:
                parts.append(f"missing: {', '.join(missing)}")
            if invalid:
                parts.append(f"invalid JSON: {', '.join(invalid)}")
            return CheckResult(
                "fail" if missing else "warn",
                "Config Files",
                "; ".join(parts),
            )

        optional_details = (
            ", ".join(present_optional) if present_optional else "none"
        )
        return CheckResult(
            "pass",
            "Config Files",
            "Required config files parsed successfully",
            details=f"Optional configs present: {optional_details}",
        )

    def check_agent_config(self) -> CheckResult:
        path = _CONFIG_DIR / "config_agents.json"
        data = self._load_json_path(path)
        if data is None:
            return CheckResult(
                "warn",
                "Agent Config",
                "Unable to read config_agents.json",
                details=str(path),
            )

        agents = data.get("agents")
        if not isinstance(agents, list):
            return CheckResult(
                "warn",
                "Agent Config",
                "config_agents.json has no valid 'agents' array",
                details=str(path),
            )

        target = next(
            (
                agent
                for agent in agents
                if isinstance(agent, dict)
                and agent.get("name") == "GroundingAgent"
            ),
            None,
        )
        if target is None:
            return CheckResult(
                "warn",
                "Agent Config",
                "GroundingAgent entry not found in config_agents.json",
                details=str(path),
            )

        backend_scope = target.get("backend_scope") or []
        max_iterations = target.get("max_iterations", "unknown")
        return CheckResult(
            "pass",
            "Agent Config",
            "GroundingAgent config loaded",
            details=(
                f"backend_scope={backend_scope}; "
                f"max_iterations={max_iterations}"
            ),
        )

    def check_communication_config(self) -> CheckResult:
        active = _CONFIG_DIR / "config_communication.json"
        example = _CONFIG_DIR / "config_communication.json.example"

        if active.exists():
            data = self._load_json_path(active)
            if data is None:
                return CheckResult(
                    "warn",
                    "Communication Config",
                    "config_communication.json is invalid JSON",
                    details=str(active),
                )
            return CheckResult(
                "pass",
                "Communication Config",
                "Communication config is present",
                details=str(active),
            )

        if example.exists():
            return CheckResult(
                "pass",
                "Communication Config",
                "Optional communication config is not enabled",
                details=f"Example available at {example}",
            )

        return CheckResult(
            "pass",
            "Communication Config",
            "No communication config detected",
        )

    def check_grounding_settings_validation(self) -> CheckResult:
        raw_data, file_errors = self._build_grounding_validation_payload()
        if file_errors:
            return CheckResult(
                "fail",
                "Grounding Settings",
                "Unable to validate merged grounding config",
                details=self._summarize_items(file_errors, limit=5),
            )

        try:
            GroundingConfig.model_validate(raw_data)
        except ValidationError as exc:
            return CheckResult(
                "fail",
                "Grounding Settings",
                "Merged grounding config failed schema validation",
                details=self._format_validation_errors(exc),
            )

        return CheckResult(
            "pass",
            "Grounding Settings",
            "Merged grounding config passed schema validation",
        )

    def check_agents_settings_validation(self) -> CheckResult:
        path = _CONFIG_DIR / "config_agents.json"
        data, error = self._read_json_file(path)
        if data is None:
            return CheckResult(
                "fail",
                "Agent Settings",
                "Unable to load config_agents.json",
                details=error or str(path),
            )

        issues = self._validate_agents_payload(data)
        if issues:
            return CheckResult(
                "fail",
                "Agent Settings",
                f"config_agents.json has {len(issues)} validation issue(s)",
                details=self._summarize_items(issues, limit=5),
            )

        agent_names = [
            str(agent.get("name", "unknown"))
            for agent in data.get("agents", [])
            if isinstance(agent, dict)
        ]
        return CheckResult(
            "pass",
            "Agent Settings",
            f"Validated {len(agent_names)} agent definition(s)",
            details=self._summarize_items(agent_names),
        )

    def check_communication_settings_validation(self) -> CheckResult:
        path = _CONFIG_DIR / "config_communication.json"
        example = _CONFIG_DIR / "config_communication.json.example"
        if not path.exists():
            return CheckResult(
                "pass",
                "Communication Settings",
                "Communication config is not enabled",
                details=f"Example available at {example}" if example.exists() else None,
            )

        data, error = self._read_json_file(path)
        if data is None:
            return CheckResult(
                "fail",
                "Communication Settings",
                "Unable to parse config_communication.json",
                details=error or str(path),
            )

        try:
            CommunicationConfig.model_validate(data)
        except ValidationError as exc:
            return CheckResult(
                "fail",
                "Communication Settings",
                "config_communication.json failed schema validation",
                details=self._format_validation_errors(exc),
            )

        return CheckResult(
            "pass",
            "Communication Settings",
            "config_communication.json passed schema validation",
        )

    def check_env_override_validation(self) -> CheckResult:
        issues: list[str] = []
        notices: list[str] = []

        json_var_specs = [
            ("OPENSPACE_CONFIG_JSON", "dict"),
            ("OPENSPACE_MCP_SERVERS_JSON", "dict"),
            ("OPENSPACE_LLM_CONFIG", "dict"),
            ("OPENSPACE_LLM_EXTRA_HEADERS", "dict"),
        ]

        for name, expected_kind in json_var_specs:
            value = os.environ.get(name, "").strip()
            if not value:
                continue

            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                issues.append(
                    f"{name}: invalid JSON at line {exc.lineno} col {exc.colno}"
                )
                continue

            if expected_kind == "dict" and not isinstance(parsed, dict):
                issues.append(f"{name}: expected a JSON object")
                continue

            notices.append(f"{name}=JSON<{type(parsed).__name__}>")

        if issues:
            return CheckResult(
                "fail",
                "Environment Overrides",
                f"{len(issues)} invalid override(s) detected",
                details=self._summarize_items(issues, limit=5),
            )

        if notices:
            return CheckResult(
                "pass",
                "Environment Overrides",
                f"{len(notices)} JSON override(s) validated",
                details=self._summarize_items(notices, limit=5),
            )

        return CheckResult(
            "pass",
            "Environment Overrides",
            "No JSON env overrides set",
        )

    def check_env_vars(self) -> CheckResult:
        text_vars = [
            "OPENSPACE_MODEL",
            "OPENSPACE_WORKSPACE",
            "OPENSPACE_DEBUG",
            "OPENSPACE_LOG_LEVEL",
        ]
        json_vars = [
            "OPENSPACE_CONFIG_JSON",
            "OPENSPACE_MCP_SERVERS_JSON",
            "OPENSPACE_LLM_CONFIG",
            "OPENSPACE_LLM_EXTRA_HEADERS",
        ]

        found: list[str] = []

        for var in text_vars:
            value = os.environ.get(var)
            if value:
                found.append(f"{var}={value}")

        for var in json_vars:
            value = os.environ.get(var)
            if not value:
                continue
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue

            parsed_type = type(parsed).__name__
            found.append(f"{var}=JSON<{parsed_type}>")

        if not found:
            return CheckResult(
                "pass",
                "Environment Variables",
                "No OpenSpace env overrides set",
            )

        return CheckResult(
            "pass",
            "Environment Variables",
            f"{len(found)} env override(s) detected",
            details=self._summarize_items(found),
        )

    def check_permission_mode(self) -> CheckResult:
        try:
            rules = build_permission_rules_snapshot(self._resolve_workspace_dir())
            mode = rules.get("mode", {}).get("current")
        except Exception as exc:
            return CheckResult(
                "warn",
                "Permission Mode",
                "Permission mode unavailable",
                details=str(exc),
            )
        if isinstance(mode, str) and mode:
            return CheckResult("pass", "Permission Mode", f"Mode: {mode}")
        return CheckResult(
            "warn",
            "Permission Mode",
            "Permission mode unavailable",
        )

    def check_permission_rules(self) -> CheckResult:
        try:
            rules = build_permission_rules_snapshot(self._resolve_workspace_dir())
        except Exception as exc:
            return CheckResult(
                "warn",
                "Permission Rules",
                "Unable to inspect permission rules",
                details=str(exc),
            )

        session_rules = rules.get("session", {})
        persistent_rules = rules.get("persistent", {})
        if not session_rules and not persistent_rules:
            return CheckResult(
                "pass",
                "Permission Rules",
                "No custom permission rules configured",
            )

        return CheckResult(
            "pass",
            "Permission Rules",
            (
                f"{len(persistent_rules)} persistent and "
                f"{len(session_rules)} session rule(s)"
            ),
        )

    def check_permission_rule_precedence(self) -> CheckResult:
        try:
            rules = build_permission_rules_snapshot(self._resolve_workspace_dir())
        except Exception as exc:
            return CheckResult(
                "warn",
                "Permission Rule Precedence",
                "Unable to inspect permission rules",
                details=str(exc),
            )

        session_rules = rules.get("session", {})
        persistent_rules = rules.get("persistent", {})
        overshadowed: list[str] = []

        for session_pattern in session_rules:
            if self._is_glob_pattern(session_pattern):
                continue
            for persistent_pattern in persistent_rules:
                if fnmatch(session_pattern, persistent_pattern):
                    overshadowed.append(
                        f"{session_pattern} is shadowed by persistent rule {persistent_pattern}"
                    )
                    break

        if overshadowed:
            return CheckResult(
                "warn",
                "Permission Rule Precedence",
                f"{len(overshadowed)} session rule(s) are shadowed by persistent rules",
                details=self._summarize_items(overshadowed, limit=5),
            )

        return CheckResult(
            "pass",
            "Permission Rule Precedence",
            "No shadowed exact-match session rules detected",
            details="Wildcard-over-wildcard shadowing is not currently analyzed.",
        )

    def check_sandbox_settings(self) -> CheckResult:
        try:
            from openspace.services.sandbox import (
                build_sandbox_status,
                format_sandbox_doctor,
                get_process_sandbox_manager,
            )

            cwd = self._resolve_workspace_dir()
            manager = get_process_sandbox_manager(cwd=cwd)
            payload = build_sandbox_status(manager)
        except Exception as exc:
            return CheckResult(
                "warn",
                "Sandbox Settings",
                "Unable to inspect process sandbox runtime",
                details=str(exc),
            )

        mode = str(payload.get("mode") or "unknown")
        platform = str(payload.get("platform") or "unknown")
        active = self._bool_label(bool(payload.get("sandboxing_enabled")))
        enabled = self._bool_label(bool(payload.get("enabled_in_settings")))
        description = (
            f"mode={mode}, enabled={enabled}, active={active}, platform={platform}"
        )
        return CheckResult(
            str(payload.get("status") or "warn"),
            "Sandbox Settings",
            description,
            details=format_sandbox_doctor(payload),
        )

    def check_workspace_access(self) -> CheckResult:
        cwd = Path.cwd()
        if not cwd.exists():
            return CheckResult(
                "fail",
                "Workspace Access",
                f"Current directory missing: {cwd}",
            )
        if os.access(cwd, os.W_OK):
            return CheckResult(
                "pass",
                "Workspace Access",
                f"Workspace writable: {cwd}",
            )
        return CheckResult(
            "warn",
            "Workspace Access",
            f"Workspace is read-only: {cwd}",
        )

    def check_temp_dir(self) -> CheckResult:
        temp_dir = Path(os.getenv("TMPDIR") or "/tmp")
        if temp_dir.exists() and os.access(temp_dir, os.W_OK):
            return CheckResult(
                "pass",
                "Temp Directory",
                f"Writable: {temp_dir}",
            )
        return CheckResult(
            "warn",
            "Temp Directory",
            f"Temp dir not writable: {temp_dir}",
        )

    def check_mcp_servers(self) -> CheckResult:
        servers, source, error = self._get_effective_mcp_servers()
        if error:
            return CheckResult(
                "fail",
                "MCP Config",
                "Unable to resolve effective MCP server config",
                details=error,
            )

        if servers is not None:
            if not servers:
                return CheckResult(
                    "warn",
                    "MCP Config",
                    f"{source} is active but defines no servers",
                )
            return CheckResult(
                "pass",
                "MCP Config",
                f"{len(servers)} server(s) configured via {source}",
                details=self._summarize_items(list(servers.keys())),
            )

        mcp_example = _CONFIG_DIR / "config_mcp.json.example"
        mcp_active = _CONFIG_DIR / "config_mcp.json"
        if mcp_example.exists():
            return CheckResult(
                "warn",
                "MCP Config",
                "No active MCP config (example file exists)",
                details=f"Copy {mcp_example} to {mcp_active} and edit it.",
            )

        return CheckResult(
            "warn",
            "MCP Config",
            "No MCP config files found",
        )

    def check_mcp_server_definitions(self) -> CheckResult:
        servers, source, error = self._get_effective_mcp_servers()
        if error:
            return CheckResult(
                "fail",
                "MCP Server Definitions",
                "Unable to validate MCP server definitions",
                details=error,
            )

        if servers is None:
            return CheckResult(
                "warn",
                "MCP Server Definitions",
                "No active MCP server config to validate",
            )

        issues = self._validate_mcp_servers_payload(servers)
        if issues:
            return CheckResult(
                "fail",
                "MCP Server Definitions",
                f"{len(issues)} validation issue(s) found in {source}",
                details=self._summarize_items(issues, limit=5),
            )

        return CheckResult(
            "pass",
            "MCP Server Definitions",
            f"All MCP server definitions in {source} look valid",
        )

    def check_mcp_env_override(self) -> CheckResult:
        raw = os.environ.get("OPENSPACE_MCP_SERVERS_JSON", "").strip()
        if not raw:
            return CheckResult(
                "pass",
                "MCP Env Override",
                "No MCP env override active",
            )

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return CheckResult(
                "fail",
                "MCP Env Override",
                "OPENSPACE_MCP_SERVERS_JSON contains invalid JSON",
                details=f"line {exc.lineno}, col {exc.colno}",
            )

        if not isinstance(parsed, dict):
            return CheckResult(
                "fail",
                "MCP Env Override",
                "OPENSPACE_MCP_SERVERS_JSON must be a JSON object",
            )

        issues = self._validate_mcp_servers_payload(parsed)
        if issues:
            return CheckResult(
                "fail",
                "MCP Env Override",
                f"Env override has {len(issues)} invalid server definition(s)",
                details=self._summarize_items(issues, limit=5),
            )

        return CheckResult(
            "pass",
            "MCP Env Override",
            f"Using env override with {len(parsed)} server(s)",
            details=self._summarize_items(list(parsed.keys())),
        )

    def check_mcp_sessions(self) -> CheckResult:
        grounding_client = self._runtime_service("grounding_client")
        if grounding_client is None:
            return CheckResult(
                "warn",
                "MCP Sessions",
                "Grounding client is not initialized",
            )

        try:
            from openspace.grounding.core.types import BackendType

            provider = grounding_client.get_provider(BackendType.MCP)
        except Exception as exc:
            return CheckResult(
                "warn",
                "MCP Sessions",
                "MCP provider is not registered",
                details=str(exc),
            )

        configured = provider.list_servers() if hasattr(provider, "list_servers") else []
        if not configured:
            return CheckResult(
                "warn",
                "MCP Sessions",
                "No MCP servers configured",
            )

        active_sessions = [
            session_name
            for session_name in grounding_client.list_sessions()
            if session_name.startswith("mcp-")
        ]
        if not active_sessions:
            return CheckResult(
                "warn",
                "MCP Sessions",
                f"0 active session(s) for {len(configured)} configured server(s)",
                details="Sessions are created lazily after the first MCP use.",
            )

        status = "pass" if len(active_sessions) == len(configured) else "warn"
        return CheckResult(
            status,
            "MCP Sessions",
            (
                f"{len(active_sessions)} active session(s) for "
                f"{len(configured)} configured server(s)"
            ),
            details=self._summarize_items(active_sessions),
        )

    def check_plugins(self) -> CheckResult:
        plugin_marketplace = Path.cwd() / ".agents" / "plugins" / "marketplace.json"
        if plugin_marketplace.exists():
            if self._load_json_path(plugin_marketplace) is None:
                return CheckResult(
                    "warn",
                    "Plugin Marketplace",
                    "Plugin marketplace config is invalid JSON",
                    details=str(plugin_marketplace),
                )
            return CheckResult(
                "pass",
                "Plugin Marketplace",
                f"Config found at {plugin_marketplace}",
            )
        return CheckResult(
            "warn",
            "Plugin Marketplace",
            "No plugin marketplace config found",
            details="Plugin diagnostics are limited until plugin metadata exists.",
        )

    def check_skill_directories(self) -> CheckResult:
        skill_enabled = self._skills_enabled()
        resolved, missing = self._resolve_skill_directories()

        if not skill_enabled:
            return CheckResult(
                "warn",
                "Skill Directories",
                "Skill discovery is disabled in config_grounding.json",
            )

        if missing:
            return CheckResult(
                "warn",
                "Skill Directories",
                f"{len(missing)} configured skill dir(s) are missing",
                details=self._summarize_items(missing, limit=5),
            )

        if not resolved:
            return CheckResult(
                "warn",
                "Skill Directories",
                "No skill directories resolved",
            )

        return CheckResult(
            "pass",
            "Skill Directories",
            f"{len(resolved)} skill dir(s) resolved",
            details=self._summarize_items([str(path) for path in resolved], limit=4),
        )

    def check_skill_registry(self) -> CheckResult:
        grounding_config = self._runtime_service("grounding_config")
        skills_config = getattr(grounding_config, "skills", None)
        skills_enabled = bool(getattr(skills_config, "enabled", False))
        registry = self._runtime_service("skill_registry")

        if not skills_enabled:
            return CheckResult(
                "warn",
                "Skill Registry",
                "Skill discovery is disabled in config_grounding.json",
            )

        if registry is None:
            return CheckResult(
                "warn",
                "Skill Registry",
                "Skills are enabled but the registry is not initialized",
            )

        skills = registry.list_skills()
        skill_ids = [
            getattr(skill, "skill_id", None) or getattr(skill, "name", "unknown")
            for skill in skills
        ]
        return CheckResult(
            "pass" if skills else "warn",
            "Skill Registry",
            f"{len(skills)} skill(s) discovered",
            details=self._summarize_items(skill_ids) if skills else "No skills discovered",
        )

    def check_skill_parse_errors(self) -> CheckResult:
        registry = self._runtime_service("skill_registry")
        if registry is None:
            if self._skills_enabled():
                return CheckResult(
                    "warn",
                    "Skill Parse Errors",
                    "Skill registry is not initialized",
                )
            return CheckResult(
                "warn",
                "Skill Parse Errors",
                "Skill discovery is disabled",
            )

        diagnostics = registry.get_diagnostics() if hasattr(registry, "get_diagnostics") else []
        if not diagnostics:
            return CheckResult(
                "pass",
                "Skill Parse Errors",
                "No skill parse or safety issues detected",
            )

        fail_count = sum(1 for diagnostic in diagnostics if diagnostic.severity == "fail")
        warn_count = sum(1 for diagnostic in diagnostics if diagnostic.severity == "warn")
        status = "fail" if fail_count > 0 else "warn"
        summary_parts = []
        if fail_count > 0:
            summary_parts.append(f"{fail_count} parse failure(s)")
        if warn_count > 0:
            summary_parts.append(f"{warn_count} safety warning(s)")
        detail_items = []
        for diagnostic in diagnostics:
            item = f"{diagnostic.path}: {diagnostic.message}"
            if diagnostic.details:
                item += f" ({diagnostic.details})"
            detail_items.append(item)

        return CheckResult(
            status,
            "Skill Parse Errors",
            ", ".join(summary_parts),
            details=self._summarize_items(detail_items, limit=5),
        )

    def check_recording(self) -> CheckResult:
        config = getattr(self._openspace, "config", None)
        recording_enabled = bool(getattr(config, "enable_recording", False))
        manager = self._runtime_service("recording_manager")

        if not recording_enabled:
            return CheckResult(
                "warn",
                "Recording",
                "Recording is disabled in OpenSpace config",
            )

        if manager is None:
            return CheckResult(
                "warn",
                "Recording",
                "Recording is enabled but the manager is not initialized",
            )

        backends = sorted(getattr(manager, "backends", []))
        return CheckResult(
            "pass",
            "Recording",
            f"Enabled for {len(backends)} backend(s)",
            details=self._summarize_items(backends) if backends else None,
        )

    def check_execution_analyzer(self) -> CheckResult:
        analyzer = self._runtime_service("execution_analyzer")
        registry = self._runtime_service("skill_registry")
        config = getattr(self._openspace, "config", None)
        recording_enabled = bool(getattr(config, "enable_recording", False))

        if analyzer is not None:
            return CheckResult(
                "pass",
                "Execution Analyzer",
                "Execution analysis is initialized",
            )

        if registry is None:
            return CheckResult(
                "warn",
                "Execution Analyzer",
                "Execution analysis unavailable because the skill registry is not initialized",
            )

        if not recording_enabled:
            return CheckResult(
                "warn",
                "Execution Analyzer",
                "Execution analysis unavailable because recording is disabled",
            )

        return CheckResult(
            "warn",
            "Execution Analyzer",
            "Execution analysis is not initialized",
        )

    def check_tool_quality(self) -> CheckResult:
        grounding_config = self._runtime_service("grounding_config")
        quality_config = getattr(grounding_config, "tool_quality", None)
        quality_enabled = bool(getattr(quality_config, "enabled", False))
        grounding_client = self._runtime_service("grounding_client")

        if not quality_enabled:
            return CheckResult(
                "warn",
                "Tool Quality",
                "Tool quality tracking is disabled in config_grounding.json",
            )

        if grounding_client is None:
            return CheckResult(
                "warn",
                "Tool Quality",
                "Grounding client is not initialized",
            )

        manager = getattr(grounding_client, "quality_manager", None)
        if manager is None:
            return CheckResult(
                "warn",
                "Tool Quality",
                "Tool quality manager is not initialized",
            )

        report = grounding_client.get_quality_report()
        summary = report.get("summary", {})
        total_tools = summary.get("total_tools", 0)
        tested_tools = summary.get("tested_tools", 0)
        if total_tools == 0:
            return CheckResult(
                "pass",
                "Tool Quality",
                "Tool quality tracking is enabled (no data collected yet)",
            )

        return CheckResult(
            "pass",
            "Tool Quality",
            f"{tested_tools}/{total_tools} tool(s) have quality data",
            details=(
                "overall success rate: "
                f"{summary.get('overall_success_rate', 0):.1%}"
            ),
        )

    def check_sessions_dir(self) -> CheckResult:
        sessions_dir = Path.home() / ".openspace" / "sessions"
        try:
            sessions_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CheckResult(
                "warn",
                "Sessions Directory",
                f"Unable to prepare sessions dir: {sessions_dir}",
                details=str(exc),
            )
        if os.access(sessions_dir, os.W_OK):
            return CheckResult(
                "pass",
                "Sessions Directory",
                f"Writable: {sessions_dir}",
            )
        return CheckResult(
            "warn",
            "Sessions Directory",
            f"Sessions dir not writable: {sessions_dir}",
        )

    def check_lock_file(self) -> CheckResult:
        if not _LOCK_FILE.exists():
            return CheckResult("pass", "Lock File", "No lock file present")

        try:
            content = _LOCK_FILE.read_text(encoding="utf-8").strip()
            pid = int(content)
        except (ValueError, OSError):
            _LOCK_FILE.unlink(missing_ok=True)
            return CheckResult(
                "warn",
                "Lock File",
                "Removed unreadable lock file",
            )

        if self._pid_alive(pid):
            return CheckResult(
                "warn",
                "Lock File",
                f"Another instance may be running (PID {pid})",
            )

        _LOCK_FILE.unlink(missing_ok=True)
        return CheckResult(
            "pass",
            "Lock File",
            f"Cleaned stale lock (PID {pid} no longer running)",
        )

    def check_current_project(self) -> CheckResult:
        cwd = Path.cwd()
        git_dir = cwd / ".git"
        if git_dir.exists():
            return CheckResult(
                "pass",
                "Current Project",
                f"Git worktree detected: {cwd}",
            )
        return CheckResult(
            "warn",
            "Current Project",
            f"No .git directory in current workspace: {cwd}",
        )

    def check_workspace_setting(self) -> CheckResult:
        workspace_dir = getattr(getattr(self._openspace, "config", None), "workspace_dir", None)
        if workspace_dir:
            return CheckResult(
                "pass",
                "Workspace Setting",
                f"Configured workspace: {workspace_dir}",
            )
        return CheckResult(
            "warn",
            "Workspace Setting",
            "No explicit workspace_dir configured",
        )

    def check_backend_scope(self) -> CheckResult:
        grounding_agent = self._runtime_service("grounding_agent")
        if grounding_agent is None:
            return CheckResult(
                "warn",
                "Backend Scope",
                "GroundingAgent is not initialized",
            )

        backend_scope = getattr(grounding_agent, "backend_scope", None) or []
        if not backend_scope:
            return CheckResult(
                "warn",
                "Backend Scope",
                "No backend scope configured",
            )

        return CheckResult(
            "pass",
            "Backend Scope",
            f"{len(backend_scope)} backend(s) enabled",
            details=self._summarize_items(backend_scope),
        )

    def check_system_prompt_context_pressure(self) -> CheckResult:
        backend_scope = self._resolve_backend_scope()
        prompt = GroundingAgentPrompts.build_system_prompt(
            backend_scope if backend_scope else None
        )
        prompt_chars = len(prompt)
        prompt_tokens = self._estimate_tokens(prompt_chars)
        has_default_fallback = not backend_scope

        if has_default_fallback or len(backend_scope) >= 4 or prompt_tokens >= 500:
            reason = (
                "No explicit backend scope found, so the default all-backend prompt is used"
                if has_default_fallback
                else "Broad backend scope increases tool guidance and decision surface"
            )
            return CheckResult(
                "warn",
                "System Prompt Breadth",
                (
                    f"Backend scope ({len(backend_scope)} backends) produces "
                    f"a {prompt_chars:,}-char / ~{prompt_tokens:,} estimated-token system prompt"
                ),
                details=(
                    f"{reason}: "
                    f"{self._summarize_items(backend_scope, limit=5)}"
                ),
            )

        return CheckResult(
            "pass",
            "System Prompt Breadth",
            f"System prompt size is {prompt_chars:,} chars / ~{prompt_tokens:,} estimated tokens",
            details=(
                "Explicit backend scope keeps the prompt focused: "
                f"{self._summarize_items(backend_scope, limit=5)}"
            ),
        )

    def check_skill_selection_context_pressure(self) -> CheckResult:
        registry = self._runtime_service("skill_registry")
        if registry is None:
            return CheckResult(
                "pass",
                "Skill Selection Context",
                "No skill registry attached",
            )

        skills = registry.list_skills()
        skill_count = len(skills)
        if skill_count == 0:
            return CheckResult(
                "pass",
                "Skill Selection Context",
                "No discovered skills",
            )

        descriptions_chars = sum(len(skill.description or "") for skill in skills)
        descriptions_tokens = self._estimate_tokens(descriptions_chars)
        skill_cfg = getattr(self._runtime_service("grounding_config"), "skills", None)
        max_select = int(getattr(skill_cfg, "max_select", 2) or 2)
        prefilter_top_k = max(15, max_select * 5)

        if skill_count > PREFILTER_THRESHOLD:
            return CheckResult(
                "warn",
                "Skill Selection Context",
                (
                    f"{skill_count} discovered skills exceed the prefilter threshold "
                    f"({PREFILTER_THRESHOLD})"
                ),
                details=(
                    "Large skill catalogs expand the selection prompt before injection. "
                    f"Descriptions total {descriptions_chars:,} chars / ~{descriptions_tokens:,} estimated tokens; "
                    f"prefilter keeps at most {prefilter_top_k} candidates."
                ),
            )

        if skill_count >= max(8, PREFILTER_THRESHOLD - 2) and descriptions_tokens >= 250:
            return CheckResult(
                "warn",
                "Skill Selection Context",
                (
                    f"{skill_count} discovered skills stay below the prefilter threshold "
                    f"but still create a medium-size selection catalog"
                ),
                details=(
                    f"Descriptions total {descriptions_chars:,} chars / ~{descriptions_tokens:,} estimated tokens; "
                    f"selector may still inspect all {skill_count} skills."
                ),
            )

        return CheckResult(
            "pass",
            "Skill Selection Context",
            f"{skill_count} discovered skills stay within the prefilter threshold",
            details=(
                f"Descriptions total {descriptions_chars:,} chars / ~{descriptions_tokens:,} estimated tokens; "
                f"prefilter threshold is {PREFILTER_THRESHOLD}."
            ),
        )

    def check_skill_injection_context_pressure(self) -> CheckResult:
        registry = self._runtime_service("skill_registry")
        if registry is None:
            return CheckResult(
                "pass",
                "Skill Injection Context",
                "No skill registry attached",
            )

        skills = registry.list_skills()
        if not skills:
            return CheckResult(
                "pass",
                "Skill Injection Context",
                "No skill content available for injection",
            )

        skill_cfg = getattr(self._runtime_service("grounding_config"), "skills", None)
        max_select = int(getattr(skill_cfg, "max_select", 2) or 2)
        ranked = sorted(
            skills,
            key=lambda skill: len(registry.load_skill_content(skill.skill_id) or ""),
            reverse=True,
        )
        sample = ranked[:max_select]
        backend_scope = self._resolve_backend_scope()
        injection = registry.build_context_injection(sample, backends=backend_scope)
        injection_chars = len(injection)
        injection_tokens = self._estimate_tokens(injection_chars)
        selected_ids = [skill.skill_id for skill in sample]
        heaviest_skill_chars = max(
            (len(registry.load_skill_content(skill.skill_id) or "") for skill in sample),
            default=0,
        )

        if injection_tokens >= 1200 or injection_chars >= 5000:
            return CheckResult(
                "warn",
                "Skill Injection Context",
                (
                    f"Worst-case injection for {len(sample)} skill(s) is "
                    f"{injection_chars:,} chars / ~{injection_tokens:,} estimated tokens"
                ),
                details=(
                    f"max_select={max_select}; heaviest selected skill body is "
                    f"{heaviest_skill_chars:,} chars; skills: "
                    f"{self._summarize_items(selected_ids, limit=4)}"
                ),
            )

        return CheckResult(
            "pass",
            "Skill Injection Context",
            (
                f"Worst-case injection for {len(sample)} skill(s) is "
                f"{injection_chars:,} chars / ~{injection_tokens:,} estimated tokens"
            ),
            details=(
                f"max_select={max_select}; heaviest selected skill body is "
                f"{heaviest_skill_chars:,} chars; skills: "
                f"{self._summarize_items(selected_ids, limit=4)}"
            ),
        )

    def check_channel_context_pressure(self) -> CheckResult:
        config, error = self._load_active_communication_config()
        if error:
            return CheckResult(
                "warn",
                "Channel Context",
                "Unable to evaluate communication context settings",
                details=error,
            )

        if config is None or not config.enabled_platforms:
            return CheckResult(
                "pass",
                "Channel Context",
                "No communication channels enabled",
            )

        turns = config.sessions.history_max_turns
        platforms = config.enabled_platforms
        if turns >= 10:
            return CheckResult(
                "warn",
                "Channel Context",
                (
                    f"Communication mode can inject channel metadata and up to "
                    f"{turns} history turn(s)"
                ),
                details=(
                    f"Enabled platforms: {self._summarize_items(platforms)}; "
                    "larger history windows expand every turn's working context."
                ),
            )

        return CheckResult(
            "pass",
            "Channel Context",
            (
                f"Communication mode can inject channel metadata and up to "
                f"{turns} history turn(s)"
            ),
            details=(
                f"Enabled platforms: {self._summarize_items(platforms)}; "
                "history window is moderate."
            ),
        )

    def _build_grounding_validation_payload(self) -> tuple[dict[str, Any], list[str]]:
        file_errors: list[str] = []
        raw_data: dict[str, Any] = {}

        for filename in (
            "config_grounding.json",
            "config_security.json",
        ):
            path = _CONFIG_DIR / filename
            data, error = self._read_json_file(path)
            if data is None:
                file_errors.append(f"{filename}: {error or 'missing or unreadable'}")
                continue
            raw_data = self._deep_merge(raw_data, data)

        dev_path = _CONFIG_DIR / "config_dev.json"
        if dev_path.exists():
            data, error = self._read_json_file(dev_path)
            if data is None:
                file_errors.append(f"{dev_path.name}: {error or 'unreadable'}")
            else:
                raw_data = self._deep_merge(raw_data, data)

        mcp_path = _CONFIG_DIR / "config_mcp.json"
        if mcp_path.exists():
            mcp_data, error = self._read_json_file(mcp_path)
            if mcp_data is None:
                file_errors.append(f"{mcp_path.name}: {error or 'unreadable'}")
            elif "mcpServers" in mcp_data:
                merged_mcp = dict(raw_data.get("mcp", {}))
                merged_mcp["servers"] = mcp_data["mcpServers"]
                raw_data["mcp"] = merged_mcp

        return raw_data, file_errors

    def _get_effective_mcp_servers(
        self,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        raw = os.environ.get("OPENSPACE_MCP_SERVERS_JSON", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                return None, "OPENSPACE_MCP_SERVERS_JSON", (
                    f"OPENSPACE_MCP_SERVERS_JSON invalid JSON at "
                    f"line {exc.lineno}, col {exc.colno}"
                )
            if not isinstance(parsed, dict):
                return None, "OPENSPACE_MCP_SERVERS_JSON", (
                    "OPENSPACE_MCP_SERVERS_JSON must be a JSON object"
                )
            return parsed, "OPENSPACE_MCP_SERVERS_JSON", None

        mcp_path = _CONFIG_DIR / "config_mcp.json"
        if not mcp_path.exists():
            return None, "config_mcp.json", None

        data, error = self._read_json_file(mcp_path)
        if data is None:
            return None, "config_mcp.json", error

        servers = data.get("mcpServers")
        if servers is None:
            return None, "config_mcp.json", "Missing top-level 'mcpServers' key"
        if not isinstance(servers, dict):
            return None, "config_mcp.json", "'mcpServers' must be a JSON object"
        return servers, "config_mcp.json", None

    def _validate_agents_payload(self, data: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        agents = data.get("agents")
        if not isinstance(agents, list):
            return ["Top-level 'agents' field must be a list"]

        for index, agent in enumerate(agents):
            prefix = f"agents[{index}]"
            if not isinstance(agent, dict):
                issues.append(f"{prefix}: entry must be an object")
                continue

            name = agent.get("name")
            if not isinstance(name, str) or not name.strip():
                issues.append(f"{prefix}.name: must be a non-empty string")

            class_name = agent.get("class_name")
            if not isinstance(class_name, str) or not class_name.strip():
                issues.append(f"{prefix}.class_name: must be a non-empty string")

            backend_scope = agent.get("backend_scope")
            if backend_scope is not None:
                if not isinstance(backend_scope, list) or not all(
                    isinstance(item, str) and item.strip() for item in backend_scope
                ):
                    issues.append(f"{prefix}.backend_scope: must be a list of strings")

            max_iterations = agent.get("max_iterations")
            if max_iterations is not None:
                if not isinstance(max_iterations, int) or max_iterations <= 0:
                    issues.append(f"{prefix}.max_iterations: must be a positive integer")

        return issues

    def _validate_mcp_servers_payload(
        self,
        servers: dict[str, Any],
    ) -> list[str]:
        issues: list[str] = []
        for server_name, config in servers.items():
            prefix = f"{server_name}"
            if not isinstance(config, dict):
                issues.append(f"{prefix}: server config must be an object")
                continue

            has_command = isinstance(config.get("command"), str) and bool(
                config.get("command", "").strip()
            )
            has_url = isinstance(config.get("url"), str) and bool(
                config.get("url", "").strip()
            )
            if not has_command and not has_url:
                issues.append(f"{prefix}: expected either 'command' or 'url'")

            if "args" in config:
                args = config.get("args")
                if not isinstance(args, list) or not all(
                    isinstance(arg, str) for arg in args
                ):
                    issues.append(f"{prefix}.args: must be a list of strings")

            if "env" in config:
                env = config.get("env")
                if not isinstance(env, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in env.items()
                ):
                    issues.append(f"{prefix}.env: must be an object of string pairs")

            if "transport" in config:
                transport = config.get("transport")
                if not isinstance(transport, str) or transport not in _MCP_ALLOWED_TRANSPORTS:
                    issues.append(
                        f"{prefix}.transport: must be one of {sorted(_MCP_ALLOWED_TRANSPORTS)}"
                    )

        return issues

    def _resolve_skill_directories(self) -> tuple[list[Path], list[str]]:
        resolved: list[Path] = []
        missing: list[str] = []

        host_dirs_raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", "")
        if host_dirs_raw:
            for raw in host_dirs_raw.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                path = Path(raw)
                if path.exists():
                    resolved.append(path)
                else:
                    missing.append(raw)

        grounding_config = self._runtime_service("grounding_config")
        skill_cfg = getattr(grounding_config, "skills", None)
        skill_dirs = getattr(skill_cfg, "skill_dirs", []) if skill_cfg else []
        for raw in skill_dirs:
            path = Path(raw)
            if path in resolved:
                continue
            if path.exists():
                resolved.append(path)
            else:
                missing.append(raw)

        if _BUILTIN_SKILLS_DIR.exists():
            resolved.append(_BUILTIN_SKILLS_DIR)
        else:
            missing.append(str(_BUILTIN_SKILLS_DIR))

        return resolved, missing

    def _skills_enabled(self) -> bool:
        grounding_config = self._runtime_service("grounding_config")
        skills_config = getattr(grounding_config, "skills", None)
        return bool(getattr(skills_config, "enabled", False))

    def _resolve_backend_scope(self) -> list[str]:
        grounding_agent = self._runtime_service("grounding_agent")
        if grounding_agent is not None:
            backend_scope = getattr(grounding_agent, "backend_scope", None) or []
            if backend_scope:
                return list(backend_scope)

        agent_config_path = _CONFIG_DIR / "config_agents.json"
        data = self._load_json_path(agent_config_path)
        if isinstance(data, dict):
            for agent in data.get("agents", []):
                if (
                    isinstance(agent, dict)
                    and agent.get("name") == "GroundingAgent"
                    and isinstance(agent.get("backend_scope"), list)
                ):
                    return [str(item) for item in agent.get("backend_scope", []) if item]

        return []

    def _load_active_communication_config(
        self,
    ) -> tuple[CommunicationConfig | None, str | None]:
        try:
            config = load_communication_config()
        except FileNotFoundError:
            return None, None
        except Exception as exc:
            return None, str(exc)
        return config, None

    async def _stream_section(
        self,
        *,
        run_id: str,
        section: DoctorSection,
        order: int,
        checks: Iterable[CheckResult],
        section_results: list[CheckResult],
        section_done: bool,
        run_done: bool,
        summary: str,
    ) -> None:
        if self._tui_bridge is None:
            return

        try:
            await self._tui_bridge.send(
                CoreToTuiEvent.DOCTOR_RESULT.value,
                {
                    "run_id": run_id,
                    "section": section.key,
                    "section_title": section.title,
                    "section_order": order,
                    "section_status": self._section_status(section_results),
                    "checks": [check.to_dict() for check in checks],
                    "summary": summary,
                    "section_done": section_done,
                    "run_done": run_done,
                    "done": section_done,
                },
            )
        except Exception:
            logger.debug("Failed to stream doctor result to TUI", exc_info=True)

    @staticmethod
    def _section_status(results: Iterable[CheckResult]) -> str:
        statuses = {result.status for result in results}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        if "pass" in statuses:
            return "pass"
        return "info"

    @staticmethod
    def _section_summary(title: str, results: list[CheckResult]) -> str:
        if not results:
            return f"{title}: no checks"
        pass_count = sum(1 for result in results if result.status == "pass")
        warn_count = sum(1 for result in results if result.status == "warn")
        fail_count = sum(1 for result in results if result.status == "fail")
        return (
            f"{title}: {pass_count} pass, {warn_count} warn, {fail_count} fail"
        )

    @staticmethod
    def _detect_install_type() -> str:
        try:
            import openspace as _os_pkg

            pkg_file = getattr(_os_pkg, "__file__", "") or ""
        except ImportError:
            return "unknown"

        if "site-packages" in pkg_file:
            if "pipx" in pkg_file:
                return "pipx"
            return "pip"
        return "source"

    @staticmethod
    def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in update.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = Doctor._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _format_validation_errors(error: ValidationError, limit: int = 5) -> str:
        issues = []
        for entry in error.errors():
            location = ".".join(str(part) for part in entry.get("loc", ()))
            message = entry.get("msg", "validation error")
            issues.append(f"{location}: {message}" if location else message)
        return Doctor._summarize_items(issues, limit=limit)

    @staticmethod
    def _read_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON at line {exc.lineno}, col {exc.colno}"
        except OSError as exc:
            return None, str(exc)

        if not isinstance(data, dict):
            return None, "top-level JSON value must be an object"

        return data, None

    @staticmethod
    def _load_json_path(path: Path) -> dict[str, Any] | None:
        data, _ = Doctor._read_json_file(path)
        return data

    def _resolve_workspace_dir(self) -> str:
        metadata = getattr(self._openspace, "current_session_metadata", None)
        if isinstance(metadata, dict):
            for key in ("workspace_dir", "project_path", "worktree_path"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            worktree = metadata.get("worktree")
            if isinstance(worktree, dict):
                value = worktree.get("workspace_dir")
                if isinstance(value, str) and value.strip():
                    return value
        config = getattr(self._openspace, "config", None)
        workspace_dir = getattr(config, "workspace_dir", None)
        if isinstance(workspace_dir, str) and workspace_dir.strip():
            return workspace_dir
        return os.getcwd()

    @staticmethod
    def _bool_label(value: bool) -> str:
        return "on" if value else "off"

    @staticmethod
    def _summarize_items(items: list[str], limit: int = 4) -> str:
        compact = [item for item in items if item]
        if not compact:
            return "none"

        shown = compact[:limit]
        remaining = len(compact) - len(shown)
        text = ", ".join(shown)
        if remaining > 0:
            text += f" (+{remaining} more)"
        return text

    @staticmethod
    def _get_package_path() -> Path | None:
        try:
            import openspace as _os_pkg

            pkg_file = getattr(_os_pkg, "__file__", "") or ""
            if not pkg_file:
                return None
            return Path(pkg_file).resolve()
        except ImportError:
            return None

    @staticmethod
    def _get_package_version() -> str | None:
        try:
            import openspace as _os_pkg

            return getattr(_os_pkg, "__version__", "unknown")
        except ImportError:
            return None

    @staticmethod
    def _is_glob_pattern(pattern: str) -> bool:
        return any(char in pattern for char in "*?[]")

    @staticmethod
    def _estimate_tokens(char_count: int) -> int:
        return max(1, char_count // 4) if char_count > 0 else 0

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
