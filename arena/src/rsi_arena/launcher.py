"""Generic command launcher over one composed run-scoped Arena service."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .experiment import (
    EXPERIMENT_FILENAME,
    ExperimentAlreadyExistsError,
    JSONObject,
    experiment_digest,
    read_experiment,
    resolve_experiment,
    write_experiment,
)

ARENA_RUN_ROOT_ENV = "RSI_ARENA_RUN_ROOT"
ARENA_EXPERIMENT_PATH_ENV = "RSI_ARENA_EXPERIMENT_PATH"
ARENA_WORKSPACE_ENV = "RSI_ARENA_WORKSPACE"
ARENA_CANDIDATE_ROOT_ENV = "RSI_ARENA_CANDIDATE_ROOT"


class CommandLaunchError(ValueError):
    """A resolved experiment cannot construct its requested command exec."""


def _argv(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CommandLaunchError("baseline.command must be a non-empty argv array")
    argv: list[str] = []
    for index, argument in enumerate(value):
        if not isinstance(argument, str):
            raise CommandLaunchError(
                f"baseline.command[{index}] must be a string argv element"
            )
        argv.append(argument)
    if not argv[0]:
        raise CommandLaunchError("baseline.command[0] must name a command")
    return tuple(argv)


def _command_environment(
    references: Mapping[str, object],
    *,
    environ: Mapping[str, str],
    composition: Any,
    client_environment: Mapping[str, str],
) -> dict[str, str]:
    child_environment: dict[str, str] = {}
    for target_name, source_name in references.items():
        assert isinstance(source_name, str)  # resolved experiment shape
        try:
            value = environ[source_name]
        except KeyError as exc:
            raise CommandLaunchError(
                f"declared environment variable is unavailable: {source_name}"
            ) from exc
        if not isinstance(value, str):
            raise CommandLaunchError(
                f"environment variable {source_name} does not contain text"
            )
        child_environment[target_name] = value

    client_bin = composition.client_bin_dir
    client_library = client_bin / ".rsi-arena-lib"
    child_environment["PATH"] = os.pathsep.join(
        (str(client_bin), child_environment.get("PATH", os.defpath))
    )
    child_environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (str(client_library), child_environment.get("PYTHONPATH", "")),
        )
    )
    child_environment.update(client_environment)
    child_environment.update(
        {
            ARENA_RUN_ROOT_ENV: str(composition.layout.root),
            ARENA_EXPERIMENT_PATH_ENV: str(
                composition.control_root / EXPERIMENT_FILENAME
            ),
            ARENA_WORKSPACE_ENV: str(composition.workspace),
            ARENA_CANDIDATE_ROOT_ENV: str(composition.candidate_root),
        }
    )
    return child_environment


def _execute_resolved_command(
    experiment: Mapping[str, object],
    *,
    composition: Any,
    environ: Mapping[str, str] | None = None,
    capture_output: bool = False,
    text: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Start the run-scoped service and perform the first real command exec."""

    resolved = resolve_experiment(experiment)
    baseline = resolved["baseline"]
    assert isinstance(baseline, dict)
    argv = _argv(baseline.get("command"))
    references = baseline["env"]
    assert isinstance(references, dict)

    client_environment = composition.start_client()
    try:
        child_environment = _command_environment(
            references,
            environ=os.environ if environ is None else environ,
            composition=composition,
            client_environment=client_environment,
        )
        return subprocess.run(
            argv,
            cwd=composition.cwd,
            env=child_environment,
            check=False,
            capture_output=capture_output,
            text=text,
        )
    finally:
        composition.stop_client()


def launch_command(
    source: Mapping[str, object],
    *,
    run_root: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
    capture_output: bool = False,
    text: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Compose one new Arena run, write its authority once, and exec a command."""

    from .product_composition import compose_new_generic_run

    resolved: JSONObject = resolve_experiment(source)
    root = Path(run_root).expanduser().absolute()
    if (root / EXPERIMENT_FILENAME).exists():
        raise ExperimentAlreadyExistsError(
            f"experiment record already exists under {root}"
        )
    composition = compose_new_generic_run(
        resolved,
        run_root=root,
        workspace_source=workspace_root,
    )
    try:
        write_experiment(root, resolved)
        composition.bind_experiment(experiment_digest(resolved))
        completed = _execute_resolved_command(
            resolved,
            composition=composition,
            environ=environ,
            capture_output=capture_output,
            text=text,
        )
        return completed
    finally:
        composition.close()


def resume_command(
    *,
    run_root: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
    capture_output: bool = False,
    text: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Resume the same service/workspace exclusively from the stored authority."""

    from .held_out_evaluation import assert_evaluation_resume_allowed
    from .product_composition import compose_resumed_generic_run

    assert_evaluation_resume_allowed(run_root)
    resolved = read_experiment(run_root)
    composition = compose_resumed_generic_run(resolved, run_root=run_root)
    try:
        composition.bind_experiment(experiment_digest(resolved))
        completed = _execute_resolved_command(
            resolved,
            composition=composition,
            environ=environ,
            capture_output=capture_output,
            text=text,
        )
        return completed
    finally:
        composition.close()


__all__ = [
    "ARENA_CANDIDATE_ROOT_ENV",
    "ARENA_EXPERIMENT_PATH_ENV",
    "ARENA_RUN_ROOT_ENV",
    "ARENA_WORKSPACE_ENV",
    "CommandLaunchError",
    "launch_command",
    "resume_command",
]
