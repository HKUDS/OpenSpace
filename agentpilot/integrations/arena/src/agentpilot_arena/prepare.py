"""Create a preparation prompt, run one optional worker, or check its delivery.

Preparation supplies missing program/evaluation inputs. It does not launch an
evaluation, install discovered code in the controller, or start policy learning.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--goal", help="Describe the program and evaluation you need")
    command.add_argument("--output", type=Path, help="Fresh preparation directory")
    command.add_argument("--program", type=Path, help="Existing program checkout to retain")
    command.add_argument("--eval", dest="evaluation", type=Path, help="Existing evaluation file or directory to retain")
    command.add_argument("--worker-config", type=Path, help="Standalone [worker] TOML; required with --execute")
    command.add_argument("--env-file", type=Path, help="Execution only: load credentials without overriding host variables")
    command.add_argument("--execute", action="store_true", help="Run one configured worker; may incur external charges")
    command.add_argument("--check", type=Path, metavar="MANIFEST", help="Statically validate an existing prepared.json")
    return command


def _write_json(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, default=str)
        stream.write("\n")


def _checked(path: Path) -> dict:
    from .preparation_bundle import load_prepared_bundle

    bundle = load_prepared_bundle(path)
    result = {
        "status": "validated",
        "manifest": str(bundle.manifest),
        "program": str(bundle.program),
        "kind": bundle.kind,
        "validation": {"scope": "static", "runtime_tested": False},
    }
    if bundle.kind == "arena_setup":
        required_options = ["--output", "--worker-config", "--model", "--cell-max-cost-usd"]
        if bundle.setup["benchmark"] == "tb21":
            required_options.append("--docker-inner-address-pool")
        result.update(
            setup=bundle.setup,
            next_commands=[shlex.join(["agentpilot-arena-setup", "--prepared", str(bundle.manifest)])],
            required_options=required_options,
        )
    else:
        result.update(
            config=str(bundle.config),
            next_commands=[
                shlex.join(["agentpilot", "validate-config", "--config", str(bundle.config)]),
                shlex.join(["agentpilot", "run", "--config", str(bundle.config)]),
            ],
            required_run_options=["--run-id", "--batch-id", "--rounds"],
        )
    return result


def _prepare(args: argparse.Namespace) -> dict:
    from .preparation_prompt import build_preparation_prompt

    goal = args.goal.strip()
    if not goal or "\x00" in goal:
        raise ValueError("--goal must be nonempty text without NUL characters")
    output_input = args.output.expanduser()
    if output_input.exists() or output_input.is_symlink():
        raise ValueError("--output must be a new directory; existing preparation artifacts are never overwritten")
    output = output_input.resolve()
    program = args.program.expanduser().resolve(strict=True) if args.program else None
    evaluation = args.evaluation.expanduser().resolve(strict=True) if args.evaluation else None
    if program is not None and not program.is_dir():
        raise ValueError("--program must be an existing directory")
    if evaluation is not None and not (evaluation.is_file() or evaluation.is_dir()):
        raise ValueError("--eval must be an existing regular file or directory")
    if any(source is not None and source.is_dir() and output.is_relative_to(source)
           for source in (program, evaluation)):
        raise ValueError("--output must be outside the supplied program and evaluation directories")
    prompt = build_preparation_prompt(goal=goal, program=program, evaluation=evaluation).encode("utf-8")
    request = {"schema": "agentpilot-preparation-request/v1", "goal": goal,
               "program": program, "evaluation": evaluation}
    harness = settings = git = None
    if args.execute:
        from agentpilot.app.cli import _load_env_file
        from .runtime.worker_cli import ExternalCliHarness
        from .runtime.worker_settings import load_worker_config

        settings = load_worker_config(args.worker_config.expanduser().resolve(strict=True))
        private = settings.runtime_root.resolve()
        if private == output or private in output.parents or output in private.parents:
            raise ValueError("worker runtime_root must be disjoint from --output")
        for source in (program, evaluation):
            if source is not None and source.is_dir() and (
                private == source or private.is_relative_to(source) or source.is_relative_to(private)
            ):
                raise ValueError("worker runtime_root must be disjoint from supplied program and evaluation directories")
        git = shutil.which("git")
        if git is None:
            raise ValueError("Git is required to initialize the preparation workspace")
        if args.env_file is not None:
            _load_env_file(args.env_file)
        if len(prompt) > settings.max_prompt_bytes:
            raise ValueError("preparation prompt exceeds the worker's configured allowance")
        if any(os.environ.get(name) and os.environ[name].encode() in prompt
               for name in settings.secret_env_names):
            raise ValueError("preparation prompt contains a configured credential; remove it from the goal or input paths")
        harness = ExternalCliHarness(settings, environment=os.environ, prompt_builder=lambda *unused: prompt)
    elif args.worker_config is not None:
        args.worker_config.expanduser().resolve(strict=True)
    output.mkdir(mode=0o700, parents=True)
    with (output / "prompt.md").open("xb") as stream:
        stream.write(prompt)
    _write_json(output / "request.json", request)
    manifest = output / "prepared.json"
    if not args.execute:
        return {
            "status": "prompt_ready", "output": str(output),
            "prompt": str(output / "prompt.md"), "expected_manifest": str(manifest),
            "next_commands": [shlex.join(["agentpilot-arena-prepare", "--check", str(manifest)])],
        }
    subprocess.run([git, "init", "--quiet", str(output)], check=True,
                   capture_output=True, text=True, timeout=30)
    result = harness.run(None, SimpleNamespace(path=output))
    _write_json(output / "worker-result.json", {
        "diagnostics": dict(result.diagnostics), "cost_usd": result.cost_usd,
        "reservation_usd": settings.reservation_usd,
    })
    if result.diagnostics.get("exit_code") != 0:
        raise RuntimeError("preparation worker did not complete; inspect worker-result.json")
    report = _checked(manifest)
    report.update(output=str(output), worker_result=str(output / "worker-result.json"))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    if args.check is not None:
        if any(value is not None for value in (args.goal, args.output, args.program, args.evaluation,
                                               args.worker_config, args.env_file)) or args.execute:
            command.error("--check cannot be combined with preparation or execution options")
    else:
        if args.goal is None or args.output is None:
            command.error("--goal and --output are required unless using --check")
        if args.execute and args.worker_config is None:
            command.error("--execute requires --worker-config")
        if args.env_file is not None and not args.execute:
            command.error("--env-file is only used with --execute")
    try:
        report = _checked(args.check) if args.check is not None else _prepare(args)
    except KeyboardInterrupt:
        report = {"status": "interrupted", "recovery": "Check the existing delivery with --check, or use a new --output; execution is never retried automatically."}
        if args.output is not None:
            report["output"] = str(args.output.expanduser().resolve())
        print(json.dumps(report), file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report = {"status": "error", "error_type": type(error).__name__, "message": str(error)}
        if args.output is not None and args.output.expanduser().exists():
            report.update(output=str(args.output.expanduser().resolve()),
                          recovery="Inspect the preserved artifacts and use --check for a completed delivery, or choose a new --output.")
        print(json.dumps(report), file=sys.stderr)
        return 1
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
