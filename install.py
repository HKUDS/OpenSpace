#!/usr/bin/env python3
"""Install the local AgentPilot runtime and host skills in one command.

No models, benchmarks, or credentials are configured. --plugin explicitly adds
the repository marketplace and AgentPilot plugin to Codex's plugin settings.
Run from a checkout: python install.py --host codex
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import venv


_SOURCE = Path(__file__).resolve().parent / "agentpilot"
# Bootstrap with Python 3.9 without importing the Python 3.11+ runtime package.
_HOSTS = runpy.run_path(str(_SOURCE / "src/agentpilot/host/hosts.py"))["HOSTS"]

# Check candidate skill files before replacing a runtime used by an older skill.
# The target Python exists after venv creation, even on a Python 3.9 bootstrap.
# Import the selected source only for this read-only subprocess, without pyc files.
_PREFLIGHT = """import json, sys
sys.path.insert(0, sys.argv[1])
from agentpilot.host.setup import export_skills, install_skills
options = json.loads(sys.argv[2])
try:
    if options['export_dir']:
        export_skills(options['export_dir'], components=options['components'], check_only=True)
    else:
        install_skills(host=options['host'], components=options['components'], skill_dir=options['skill_dir'], check_only=True)
except (OSError, ValueError) as error:
    print('AgentPilot skill preflight failed: ' + str(error), file=sys.stderr)
    raise SystemExit(2)
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=tuple(_HOSTS), required=True)
    parser.add_argument("--components", nargs="+", choices=("agentpilot", "openspace", "rsi-arena"), default=["agentpilot"])
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local" / "share" / "agentpilot" / "host-runtime")
    parser.add_argument("--skill-dir", type=Path)
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--plugin", action="store_true", help="also register and install the local Codex plugin for @ selection")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.skill_dir and args.export_dir:
            raise ValueError("choose --skill-dir or --export-dir")
        if args.export_dir and args.host != "workbuddy":
            raise ValueError("--export-dir creates WorkBuddy skill ZIPs; use --host workbuddy")
        if args.host != "workbuddy" and _HOSTS[args.host]["default_dir"] is None and not args.skill_dir:
            raise ValueError("this host requires --skill-dir with its documented skills directory")
        codex = None
        if args.plugin:
            if args.host != "codex" or args.export_dir:
                raise ValueError("--plugin is for a local Codex installation")
            codex = shutil.which("codex")
            if not codex:
                raise ValueError("--plugin requires a Codex CLI with plugin support; otherwise install the skill and use the desktop Plugins UI")
        prefix = args.prefix.expanduser().absolute()
        source = _SOURCE
        marker = prefix / ".agentpilot-runtime.json"
        if prefix.is_symlink():
            raise ValueError("runtime prefix must not be a symlink")
        if prefix.exists():
            ownership = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
            if not isinstance(ownership, dict) or ownership.get("owner") != "agentpilot-host-installer":
                raise ValueError("runtime prefix already exists and is not managed by AgentPilot; choose another --prefix")
        interpreter = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        uv = shutil.which("uv")
        commands = []
        if not interpreter.exists():
            if uv:
                commands.append([uv, "venv", "--allow-existing", "--python", "3.12", str(prefix)])
            elif sys.version_info < (3, 11):
                raise ValueError("install uv or run this installer with Python 3.11+; the default Python is too old")
        if not uv:
            # A previous uv installation may have created a venv without pip.
            # ensurepip is local and makes switching to this fallback work.
            commands.append([str(interpreter), "-m", "ensurepip", "--upgrade"])
        install = ([uv, "pip", "install", "--python", str(interpreter), str(source)] if uv else
                   [str(interpreter), "-m", "pip", "install", str(source)])
        setup = [str(interpreter), "-m", "agentpilot.host", "setup", "--host", args.host,
                 "--components", *args.components]
        export_dir = args.export_dir
        if args.host == "workbuddy" and not args.skill_dir and not export_dir:
            export_dir = prefix / "exports"
        if args.skill_dir:
            setup += ["--skill-dir", str(args.skill_dir.expanduser().absolute())]
        if export_dir:
            setup += ["--export-dir", str(export_dir.expanduser().absolute())]
        preflight = [str(interpreter), "-B", "-c", _PREFLIGHT, str(source / "src"), json.dumps({
            "host": args.host, "components": args.components,
            "skill_dir": str(args.skill_dir.expanduser().absolute()) if args.skill_dir else None,
            "export_dir": str(export_dir.expanduser().absolute()) if export_dir else None,
        })]
        commands += [preflight, install, [str(interpreter), "-m", "agentpilot.host", "doctor"], setup]
        if codex:
            commands += [[codex, "plugin", "marketplace", "add", str(source.parent)],
                         [codex, "plugin", "add", "agentpilot@agentpilot-local"]]
        if args.dry_run:
            print(json.dumps({"prefix": str(prefix), "venv_fallback": not uv and not interpreter.exists(),
                              "commands": commands}, indent=2))
            return 0
        prefix.mkdir(parents=True, exist_ok=True)
        ownership_text = json.dumps({"owner": "agentpilot-host-installer", "source": str(source)})
        if not marker.exists():
            marker.write_text(ownership_text, encoding="utf-8")
        if not uv and not interpreter.exists():
            # Match `python -m venv`: copying a relocated interpreter (for
            # example a uv-managed Python) can lose its standard-library path.
            venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(prefix)
        for command in commands:
            if command is preflight:
                if subprocess.run(command).returncode:
                    print("AgentPilot installation stopped before replacing the runtime. Resolve the skill conflict or choose another --skill-dir/--export-dir.", file=sys.stderr)
                    return 1
                continue
            subprocess.run(command, check=True)
        marker.write_text(ownership_text, encoding="utf-8")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"AgentPilot installation did not complete: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
