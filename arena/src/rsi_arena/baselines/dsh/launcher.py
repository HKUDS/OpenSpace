"""Explicit DSH SDK runner construction and run-scoped Arena entrypoints."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from rsi_arena.baselines._development_state import (
    initialize_development_state,
    load_development_state,
)
from rsi_arena.baselines.agentic.runner import (
    AgenticBaselineRunner as ArenaAgenticRunner,
)
from rsi_arena.baselines.agentic.runtime import (
    AgenticRunConfig,
    NativeSessionRunner,
    RunResult,
)
from rsi_arena.baselines.dsh.native import (
    DSH_API_BASE_URL_ENV,
    DSH_CORDIS_PATH,
    DSH_CREDENTIAL_ENV,
    DSH_OFFICIAL_PROVIDER,
    DshSdkSession,
    DshSdkSessionConfig,
    project_dsh_sdk_environment,
    resolve_dsh_sdk_session_authority,
)
from rsi_arena.common import UsageLedger
from rsi_arena.experiment import read_experiment
from rsi_arena.launcher import ARENA_EXPERIMENT_PATH_ENV
from rsi_arena.native import open_native_session, resume_native_session
from rsi_arena.runner import ArenaSession


def create_session(
    config: DshSdkSessionConfig,
    *,
    ledger: UsageLedger,
) -> DshSdkSession:
    return DshSdkSession(config, ledger=ledger)


def create_runner(
    run_config: AgenticRunConfig,
    session_config: DshSdkSessionConfig,
    *,
    ledger: UsageLedger,
    arena_runner: object,
) -> NativeSessionRunner:
    expected = (
        (session_config.worktree, run_config.worktree, "workspace"),
        (session_config.run_dir, run_config.run_dir, "runtime"),
        (
            session_config.trusted_dir,
            run_config.trusted_dir or run_config.run_dir,
            "trusted",
        ),
    )
    for supplied, configured, label in expected:
        if supplied.resolve() != configured.resolve():
            raise ValueError(f"DSH {label} path disagrees with runner config")
    return NativeSessionRunner(
        run_config,
        ledger=ledger,
        arena_runner=arena_runner,
        method_session=create_session(session_config, ledger=ledger),
    )


def _string(
    config: Mapping[str, object],
    name: str,
    *,
    default: str | None = None,
) -> str:
    value = config.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"DSH baseline config requires non-empty {name}")
    return value


def _optional_number(
    config: Mapping[str, object],
    name: str,
    *,
    default: float | None,
) -> float | None:
    value = config.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"DSH baseline config {name} must be numeric or null")
    return float(value)


def _optional_positive_integer(
    config: Mapping[str, object],
    name: str,
) -> int | None:
    value = config.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"DSH baseline config {name} must be an integer or null")
    if value <= 0:
        raise ValueError(f"DSH baseline config {name} must be positive")
    return value


def _boolean(
    config: Mapping[str, object],
    name: str,
    *,
    default: bool,
) -> bool:
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"DSH baseline config {name} must be boolean")
    return value


def _stored_experiment(session: ArenaSession) -> dict[str, object]:
    experiment_path = session.authority_environment.get(ARENA_EXPERIMENT_PATH_ENV)
    if not isinstance(experiment_path, str) or not experiment_path:
        raise RuntimeError("Arena session lacks its stored experiment authority")
    return read_experiment(Path(experiment_path).parent)


def _stored_wall_seconds(session: ArenaSession) -> float:
    document = _stored_experiment(session)
    arena = document.get("arena")
    if not isinstance(arena, Mapping):
        raise TypeError("stored experiment arena config must be a mapping")
    wall_seconds = arena.get("wall_seconds")
    if isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float)):
        raise TypeError("stored experiment arena.wall_seconds must be numeric")
    return float(wall_seconds)


def _stored_environment_references(session: ArenaSession) -> Mapping[str, str]:
    document = _stored_experiment(session)
    baseline = document.get("baseline")
    if not isinstance(baseline, Mapping):
        raise TypeError("stored experiment baseline must be a mapping")
    environment = baseline.get("env")
    if not isinstance(environment, Mapping):
        raise TypeError("stored experiment baseline.env must be a mapping")
    if not all(
        isinstance(target, str) and isinstance(source, str)
        for target, source in environment.items()
    ):
        raise TypeError("stored experiment baseline.env must map names to names")
    return {str(target): str(source) for target, source in environment.items()}


def _stored_baseline_config(run_root: str | os.PathLike[str]) -> dict[str, object]:
    document = read_experiment(run_root)
    baseline = document.get("baseline")
    if not isinstance(baseline, Mapping):
        raise TypeError("stored experiment baseline must be a mapping")
    config = baseline.get("config")
    if not isinstance(config, Mapping):
        raise TypeError("stored experiment baseline.config must be a mapping")
    return dict(config)


def _runner_config(
    session: ArenaSession,
    baseline_config: Mapping[str, object],
) -> AgenticRunConfig:
    context = session.client.context()
    runtime_dir = session.method_state_directory / "dsh"
    trusted_dir = session.service.layout.root.resolve(strict=True)
    budget_usd = _optional_number(
        baseline_config,
        "budget_usd",
        default=None,
    )
    reasoning_effort = baseline_config.get("reasoning_effort")
    if reasoning_effort is not None and not isinstance(reasoning_effort, str):
        raise TypeError("DSH baseline config reasoning_effort must be a string or null")
    return AgenticRunConfig(
        cli_path=None,
        worktree=session.candidate_workspace,
        run_dir=runtime_dir,
        trusted_dir=trusted_dir,
        prompt=_string(baseline_config, "prompt"),
        model=_string(baseline_config, "model"),
        reasoning_effort=reasoning_effort,
        budget_usd=budget_usd,
        wall_budget_s=_stored_wall_seconds(session),
        max_new_terminal_a_per_b_cycle=_optional_positive_integer(
            baseline_config,
            "max_new_terminal_a_per_b_cycle",
        ),
        sandbox_backend=_string(
            baseline_config,
            "sandbox_backend",
            default="bwrap",
        ),
        bwrap_path=_string(baseline_config, "bwrap_path", default="bwrap"),
        sandbox_network=_string(
            baseline_config,
            "sandbox_network",
            default="open",
        ),
        allow_unsafe_host=_boolean(
            baseline_config,
            "allow_unsafe_host",
            default=False,
        ),
        run_id=context.run_id,
    )


def _dsh_session_config(
    session: ArenaSession,
    baseline_config: Mapping[str, object],
    runner_config: AgenticRunConfig,
) -> DshSdkSessionConfig:
    environment_references = _stored_environment_references(session)
    api_base_url_env = environment_references.get(
        DSH_API_BASE_URL_ENV,
        DSH_API_BASE_URL_ENV,
    )
    credential_env = environment_references.get(
        DSH_CREDENTIAL_ENV,
        DSH_CREDENTIAL_ENV,
    )
    base_url = os.environ.get(api_base_url_env, "").strip().rstrip("/")
    authority = resolve_dsh_sdk_session_authority(
        provider=_string(
            baseline_config,
            "provider",
            default=DSH_OFFICIAL_PROVIDER,
        ),
        model=_string(baseline_config, "model"),
        api_base_url_env=api_base_url_env,
        credential_env=credential_env,
        base_url=base_url,
        max_tokens=baseline_config.get("max_tokens"),
        wall_seconds=runner_config.wall_budget_s,
        reasoning_effort=baseline_config.get("reasoning_effort"),
    )
    projected_environment = project_dsh_sdk_environment(authority)
    # The official SDK reads these canonical names from the current process.
    # Their values are launch-time authority and never enter experiment.json.
    os.environ.update(projected_environment)
    trusted_dir = session.service.layout.root.resolve(strict=True)
    return DshSdkSessionConfig(
        worktree=session.candidate_workspace,
        run_dir=Path(runner_config.run_dir),
        trusted_dir=trusted_dir,
        cordis_path=DSH_CORDIS_PATH,
        provider=DSH_OFFICIAL_PROVIDER,
        model=authority.model,
        max_tokens=authority.max_tokens,
        reasoning_effort=authority.reasoning_effort,
        bash_command_timeout_ms=authority.bash_command_timeout_ms,
        credential_env=(DSH_CREDENTIAL_ENV,),
        environment=((DSH_API_BASE_URL_ENV, authority.base_url),),
        sandbox_backend=runner_config.sandbox_backend,
        bwrap_path=runner_config.bwrap_path,
        sandbox_network=runner_config.sandbox_network,
        allow_unsafe_host=runner_config.allow_unsafe_host,
    )


def run_dsh(
    session: ArenaSession,
    baseline_config: Mapping[str, object],
) -> RunResult:
    """Run the official DSH SDK loop against one existing Arena session."""

    if not isinstance(session, ArenaSession):
        raise TypeError("DSH requires an ArenaSession")
    if not isinstance(baseline_config, Mapping):
        raise TypeError("DSH baseline config must be an in-memory mapping")
    run_config = _runner_config(session, baseline_config)
    session_config = _dsh_session_config(session, baseline_config, run_config)
    development_state = load_development_state(
        method_state_directory=session.method_state_directory,
        owner="dsh",
        experiment=_stored_experiment(session),
    )
    ledger = session.usage_sink
    if not isinstance(ledger, UsageLedger):
        raise TypeError("Arena session lacks the shared usage ledger")
    runner = create_runner(
        run_config,
        session_config,
        ledger=ledger,
        arena_runner=ArenaAgenticRunner(
            session.service,
            fresh_attempt_slot_provider=development_state.slots_for_attempt,
            fixed_a_task_ids_by_benchmark=(
                development_state.fixed_a_task_ids_by_benchmark()
            ),
            max_new_terminal_a_per_b_cycle=(run_config.max_new_terminal_a_per_b_cycle),
        ),
    )
    return runner.run()


def run_new_dsh(
    experiment_mapping: Mapping[str, object],
    *,
    run_root: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
) -> RunResult:
    """Create a new Arena run, then launch DSH from its stored config."""

    with open_native_session(
        experiment_mapping,
        run_root=run_root,
        workspace_root=workspace_root,
    ) as session:
        initialize_development_state(
            method_state_directory=session.method_state_directory,
            owner="dsh",
            experiment=_stored_experiment(session),
        )
        return run_dsh(session, _stored_baseline_config(run_root))


def resume_dsh(*, run_root: str | os.PathLike[str]) -> RunResult:
    """Resume DSH exclusively from the run's stored experiment authority."""

    baseline_config = _stored_baseline_config(run_root)
    with resume_native_session(run_root=run_root) as session:
        return run_dsh(session, baseline_config)


__all__ = [
    "create_runner",
    "create_session",
    "resume_dsh",
    "run_dsh",
    "run_new_dsh",
]
