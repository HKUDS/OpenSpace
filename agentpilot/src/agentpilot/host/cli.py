"""Skill installation and read-only project inspection helpers."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

from .inspection import inspect_project
from .setup import COMPONENTS, SUPPORTED_HOSTS, export_skills, install_skills


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agentpilot", description="Use AgentPilot with your current coding agent.")
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="install host skills without changing host models or credentials")
    setup.add_argument("--host", choices=SUPPORTED_HOSTS, required=True)
    setup.add_argument("--components", nargs="+", choices=COMPONENTS, default=["agentpilot"])
    setup.add_argument("--skill-dir", type=Path, help="explicit host skills directory")
    setup.add_argument("--export-dir", type=Path, help="export WorkBuddy skill ZIPs for import on this machine")
    commands.add_parser("doctor", help="check the local installation without model or authentication calls")
    project = commands.add_parser("project", help="inspect existing project test commands without running them")
    actions = project.add_subparsers(dest="action", required=True)
    inspect = actions.add_parser("inspect", help="suggest existing test commands from project files")
    inspect.add_argument("--cwd", type=Path, default=Path.cwd())
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "setup":
            if arguments.export_dir and arguments.skill_dir:
                raise ValueError("choose either --skill-dir or --export-dir")
            if arguments.export_dir:
                if arguments.host != "workbuddy":
                    raise ValueError("--export-dir is for WorkBuddy skill ZIPs; use --skill-dir for other hosts")
                payload = export_skills(arguments.export_dir, components=tuple(arguments.components))
            else:
                payload = install_skills(host=arguments.host, components=tuple(arguments.components), skill_dir=arguments.skill_dir)
        elif arguments.command == "doctor":
            from agentpilot.app.runtime_config import load_app_config  # noqa: F401 -- local import check only

            payload = {
                "status": "installation_ok", "python": sys.executable,
                "python_version": platform.python_version(), "mode": "agent-guided",
                "configured_runner_available": True, "configuration_entry_imported": True,
                "model_probe_performed": False, "worker_probe_performed": False,
                "note": "The skill guides the current agent. Formal runs require their own valid configuration, environment adapter, worker and model providers; these have not been checked.",
            }
        else:
            payload = inspect_project(arguments.cwd)
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
