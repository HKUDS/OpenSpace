#!/usr/bin/env python3
"""Run OpenSpace on Terminal-Bench 2.1 through Harbor.

Examples:
    python -m benchmarks.terminal_bench.run_benchmark --sample smoke
    python -m benchmarks.terminal_bench.run_benchmark --sample sample20
    python -m benchmarks.terminal_bench.run_benchmark --sample full --workers 4
    python -m benchmarks.terminal_bench.run_benchmark --task fix-git --task raman-fitting
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import shlex
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .samples import SAMPLE_20_TASKS, distribution, normalize_task_name, sample_for_name


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_RUNS_DIR = Path("benchmarks/terminal_bench/runs")
_DEFAULT_DATASET = "terminal-bench/terminal-bench-2-1"
_DEFAULT_LOCAL_DATASET = Path("benchmarks/terminal_bench/datasets/terminal-bench-2-1")
_AGENT_IMPORT_PATH = "benchmarks.terminal_bench.openspace_harbor_agent:OpenSpaceHarborAgent"
_HARBOR_BASE_AGENT_SETUP_TIMEOUT_SEC = 120
_DEFAULT_ALLOWED_AGENT_HOSTS = (
    "pypi.org",
    "files.pythonhosted.org",
    "openrouter.ai",
    "api.deepseek.com",
    "raw.githubusercontent.com",
)

_PROVIDER_ALIASES = {
    "or": "openrouter",
    "openrouter": "openrouter",
    "dpsk": "deepseek",
    "deepseek": "deepseek",
}

_DIRECT_PROVIDER_PREFIXES = {
    "anthropic",
    "azure",
    "bedrock",
    "dashscope",
    "deepseek",
    "gemini",
    "google",
    "groq",
    "minimax",
    "moonshot",
    "ollama",
    "openai",
    "openrouter",
    "vertex_ai",
    "xai",
    "zhipu",
}

_PROVIDER_API_KEY_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY", "OR_API_KEY"),
}


def _load_dotenv() -> None:
    if os.environ.get("OPENSPACE_SKIP_DOTENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(_REPO_ROOT / "openspace" / ".env")
    load_dotenv(_REPO_ROOT / ".env")


def _short_model_name(model: str) -> str:
    return (
        model.rsplit("/", 1)[-1]
        .replace(":", "-")
        .replace(".", "")
        .replace("_", "-")
    )


def _normalize_model(model: str) -> str:
    text = str(model or "").strip()
    if not text:
        return text

    if "/" in text:
        provider, rest = text.split("/", 1)
        normalized_provider = _PROVIDER_ALIASES.get(provider.lower())
        if normalized_provider:
            return f"{normalized_provider}/{rest}"
        if provider.lower() in _DIRECT_PROVIDER_PREFIXES:
            return text
        return f"openrouter/{text}"

    if text.lower().startswith("deepseek-"):
        return f"deepseek/{text}"
    return text


def _model_provider(model: str) -> str:
    text = _normalize_model(model)
    if "/" not in text:
        return ""
    provider = text.split("/", 1)[0].lower()
    return _PROVIDER_ALIASES.get(provider, provider)


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


def _missing_credentials_message(model: str) -> str | None:
    provider = _model_provider(model)
    env_names = _PROVIDER_API_KEY_ENV.get(provider, ())
    if any(os.environ.get(name) for name in env_names):
        return None
    if provider == "openrouter" and _host_config_api_key(model):
        return None
    if os.environ.get("OPENSPACE_LLM_API_KEY"):
        return None
    if env_names:
        return (
            "Missing LLM credentials for model "
            f"{model!r}. Set OPENSPACE_LLM_API_KEY or one of: "
            + ", ".join(env_names)
        )
    return (
        "Missing LLM credentials. Set OPENSPACE_LLM_API_KEY or the "
        "provider-native API key for the selected model."
    )


def _default_job_name(sample: str, model: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"terminal_bench_{sample}_{_short_model_name(model)}_{stamp}"


def _run_label(args: argparse.Namespace) -> str:
    if args.task:
        return "custom"
    return args.sample


def _task_names(args: argparse.Namespace) -> list[str]:
    if args.task:
        task_names = [normalize_task_name(task) for task in args.task]
    else:
        sample = sample_for_name(args.sample)
        task_names = [task.harbor_name for task in sample]

    if args.local_dataset:
        return [name.split("/", 1)[1] if name.startswith("terminal-bench/") else name for name in task_names]
    return task_names


def _agent_setup_timeout_multiplier(args: argparse.Namespace) -> float:
    if args.agent_setup_timeout_multiplier is not None:
        return args.agent_setup_timeout_multiplier

    install_timeout = max(float(args.install_timeout_sec), 0.0)
    install_multiplier = math.ceil(
        (install_timeout + 60.0) / _HARBOR_BASE_AGENT_SETUP_TIMEOUT_SEC
    )
    return max(float(install_multiplier), float(args.agent_timeout_multiplier or 1.0))


def _print_sample_summary(tasks: Iterable[str]) -> None:
    selected = {normalize_task_name(task).split("/", 1)[1] for task in tasks}
    sample_tasks = tuple(task for task in SAMPLE_20_TASKS if task.slug in selected)
    if not sample_tasks:
        print("No static sample metadata available for this task set.")
        return

    dist = distribution(sample_tasks)
    print("Selected task distribution:")
    for label in ("category", "difficulty"):
        print(f"  {label}:")
        for key, count in sorted(dist[label].items()):
            print(f"    {key}: {count}")

    print("\nSelected tasks:")
    for task in sample_tasks:
        print(f"  {task.slug} | {task.category} | {task.difficulty} | {task.reason}")


def build_harbor_command(args: argparse.Namespace) -> list[str]:
    command = [
        "harbor",
        "run",
    ]

    if args.local_dataset:
        command.extend(["--path", str(args.local_dataset)])
    else:
        command.extend(["--dataset", args.dataset])

    for task_name in _task_names(args):
        command.extend(["--include-task-name", task_name])

    command.extend(
        [
            "--agent-import-path",
            _AGENT_IMPORT_PATH,
            "--model",
            args.model,
            "--agent-kwarg",
            f"max_iterations={args.max_iterations}",
            "--agent-kwarg",
            f"install_timeout_sec={args.install_timeout_sec}",
            "--agent-kwarg",
            f"llm_timeout_sec={args.llm_timeout_sec}",
            "--n-concurrent",
            str(args.workers),
            "--n-attempts",
            str(args.attempts),
            "--jobs-dir",
            str(args.runs_dir),
            "--job-name",
            args.job_name or _default_job_name(_run_label(args), args.model),
        ]
    )

    if args.backend_scope:
        command.extend(["--agent-kwarg", f"backend_scope={args.backend_scope}"])
    if args.run_timeout_sec is not None:
        command.extend(["--agent-kwarg", f"run_timeout_sec={args.run_timeout_sec}"])
    if args.replay_from_run:
        command.extend(
            ["--agent-kwarg", f"replay_seed_run_dir={args.replay_from_run}"]
        )
        explicit_kwarg_names = {
            item.split("=", 1)[0].strip()
            for item in args.agent_kwarg or []
            if item.split("=", 1)[0].strip()
        }
        if "skills_disabled" not in explicit_kwarg_names:
            command.extend(["--agent-kwarg", "skills_disabled=false"])
        replay_defaults = {
            "evolution_recovery_stale_job_timeout_s": "30",
            "evolution_startup_retryable_drain_limit": "4",
            "evolution_startup_retryable_drain_timeout_s": "180",
            "evolution_startup_retryable_drain_statuses": "pending,failed_retryable",
        }
        for key, value in replay_defaults.items():
            if key not in explicit_kwarg_names:
                command.extend(["--agent-kwarg", f"{key}={value}"])
    if args.timeout_multiplier is not None:
        command.extend(["--timeout-multiplier", str(args.timeout_multiplier)])
    if args.agent_timeout_multiplier is not None:
        command.extend(
            ["--agent-timeout-multiplier", str(args.agent_timeout_multiplier)]
        )
    if args.verifier_timeout_multiplier is not None:
        command.extend(
            ["--verifier-timeout-multiplier", str(args.verifier_timeout_multiplier)]
        )
    command.extend(
        [
            "--agent-setup-timeout-multiplier",
            str(_agent_setup_timeout_multiplier(args)),
        ]
    )
    if args.environment_build_timeout_multiplier is not None:
        command.extend(
            [
                "--environment-build-timeout-multiplier",
                str(args.environment_build_timeout_multiplier),
            ]
        )
    if args.harbor_max_retries is not None:
        command.extend(["--max-retries", str(args.harbor_max_retries)])
    for item in args.retry_include or []:
        command.extend(["--retry-include", item])
    for item in args.retry_exclude or []:
        command.extend(["--retry-exclude", item])
    if args.force_build:
        command.append("--force-build")
    if not args.delete:
        command.append("--no-delete")
    if args.debug:
        command.append("--debug")
    if args.yes:
        command.append("--yes")

    allowed_hosts = list(_DEFAULT_ALLOWED_AGENT_HOSTS)
    allowed_hosts.extend(args.allow_agent_host or [])
    for host in dict.fromkeys(allowed_hosts):
        command.extend(["--allow-agent-host", host])

    for item in args.agent_kwarg or []:
        command.extend(["--agent-kwarg", item])
    for item in args.agent_env or []:
        command.extend(["--agent-env", item])
    for artifact in args.artifact or []:
        command.extend(["--artifact", artifact])

    return command


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run OpenSpace on Terminal-Bench 2.1 through Harbor.",
    )
    parser.add_argument(
        "--sample",
        choices=("smoke", "sample20", "full"),
        default="sample20",
        help="Task set to run. Ignored when --task is provided.",
    )
    parser.add_argument(
        "--task",
        action="append",
        help="Specific task slug or terminal-bench/<slug>. Repeatable.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENSPACE_MODEL", "openrouter/qwen/qwen3.5-plus-02-15"),
        help="OpenSpace model name passed to Harbor.",
    )
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--workers", "-n", type=int, default=1)
    parser.add_argument("--attempts", "-k", type=int, default=1)
    parser.add_argument("--runs-dir", type=Path, default=_DEFAULT_RUNS_DIR)
    parser.add_argument("--job-name")
    parser.add_argument(
        "--replay-from-run",
        type=Path,
        help=(
            "Seed each task from the matching trial in a previous run directory. "
            "Copies that task's evidence DB and evolved skills into the new trial."
        ),
    )
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument(
        "--local-dataset",
        nargs="?",
        const=_DEFAULT_LOCAL_DATASET,
        type=Path,
        help="Use a local dataset path instead of Harbor package lookup.",
    )
    parser.add_argument("--backend-scope", default="shell,meta")
    parser.add_argument("--install-timeout-sec", type=int, default=1200)
    parser.add_argument("--run-timeout-sec", type=int)
    parser.add_argument("--llm-timeout-sec", type=int, default=60)
    parser.add_argument("--timeout-multiplier", type=float)
    parser.add_argument("--agent-timeout-multiplier", type=float)
    parser.add_argument("--verifier-timeout-multiplier", type=float)
    parser.add_argument("--agent-setup-timeout-multiplier", type=float)
    parser.add_argument("--environment-build-timeout-multiplier", type=float)
    parser.add_argument(
        "--harbor-max-retries",
        type=int,
        help="Retry failed Harbor trials. Distinct from --attempts/pass@k.",
    )
    parser.add_argument("--retry-include", action="append")
    parser.add_argument("--retry-exclude", action="append")
    parser.add_argument("--allow-agent-host", action="append")
    parser.add_argument("--agent-kwarg", action="append")
    parser.add_argument("--agent-env", action="append")
    parser.add_argument("--artifact", action="append")
    parser.add_argument("--force-build", action="store_true")
    parser.add_argument("--delete", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--yes", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Harbor command without running it.",
    )
    parser.add_argument(
        "--list-sample",
        action="store_true",
        help="Print selected sample distribution and exit.",
    )
    return parser.parse_args(argv)


def cli(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = parse_args(argv)

    if args.sample == "smoke" and not args.task and args.max_iterations == 30:
        args.max_iterations = 1
    if args.local_dataset:
        args.local_dataset = args.local_dataset.expanduser()
    if args.replay_from_run:
        args.replay_from_run = args.replay_from_run.expanduser().resolve()
        if args.attempts != 1:
            print(
                "--replay-from-run currently requires --attempts 1 so each warm "
                "trial has exactly one matching cold seed.",
                file=sys.stderr,
            )
            return 2
    args.model = _normalize_model(args.model)

    tasks = _task_names(args)
    if args.list_sample:
        _print_sample_summary(tasks)
        return 0

    credential_error = _missing_credentials_message(args.model)
    if credential_error:
        print(credential_error, file=sys.stderr)
        return 2

    command = build_harbor_command(args)
    print("Running:")
    print(shlex.join(command))
    print()

    if args.dry_run:
        return 0

    completed = subprocess.run(command, cwd=_REPO_ROOT, env=os.environ.copy(), check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(cli())
