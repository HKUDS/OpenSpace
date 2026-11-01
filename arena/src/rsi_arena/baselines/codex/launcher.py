"""Run the retained Codex CLI adapter inside one native Arena session."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from rsi_arena.baselines._development_state import (
    initialize_development_state,
    load_development_state,
)
from rsi_arena.baselines.agentic.runner import (
    AgenticBaselineRunner as ArenaAgenticRunner,
)
from rsi_arena.baselines.agentic.runtime import AgenticRunConfig, RunResult
from rsi_arena.experiment import read_experiment
from rsi_arena.launcher import ARENA_EXPERIMENT_PATH_ENV
from rsi_arena.native import open_native_session, resume_native_session
from rsi_arena.runner import ArenaSession

_DEFAULT_PROMPT = (
    "Use the installed rsi-arena client and the public A materials to "
    "improve the candidate, then submit candidates to B."
)
_SEGMENT_NAME = re.compile(r"segment-(\d+)\.jsonl\Z")


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"baseline.config.{field} must be a string or null")
    return value


def _string(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"baseline.config.{field} must be a string")
    return value


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"baseline.config.{field} must be a number")
    return float(value)


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"baseline.config.{field} must be an integer")
    return value


def _optional_positive_integer(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    parsed = _integer(value, field=field)
    if parsed <= 0:
        raise ValueError(f"baseline.config.{field} must be positive")
    return parsed


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"baseline.config.{field} must be a boolean")
    return value


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"baseline.config.{field} must be an array of strings")
    result = tuple(value)
    if not all(isinstance(item, str) for item in result):
        raise TypeError(f"baseline.config.{field} must be an array of strings")
    return result


def _stored_experiment(session: ArenaSession) -> Mapping[str, object]:
    experiment_path = Path(session.authority_environment[ARENA_EXPERIMENT_PATH_ENV])
    return read_experiment(experiment_path.parent)


def _stored_environment_overrides(
    experiment: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    baseline = experiment.get("baseline")
    if not isinstance(baseline, Mapping):
        return ()
    references = baseline.get("env")
    if not isinstance(references, Mapping):
        return ()
    overrides: list[tuple[str, str]] = []
    for target_name, source_name in references.items():
        if not isinstance(target_name, str) or not isinstance(source_name, str):
            continue
        value = os.environ.get(source_name)
        if value is not None:
            overrides.append((target_name, value))
    return tuple(overrides)


def _agentic_config(
    session: ArenaSession,
) -> AgenticRunConfig:
    """Project only Codex choices; Arena remains the path/run authority."""

    experiment = _stored_experiment(session)
    baseline = experiment.get("baseline")
    if not isinstance(baseline, Mapping):
        raise TypeError("stored experiment baseline must be a mapping")
    baseline_config = baseline.get("config")
    if not isinstance(baseline_config, Mapping):
        raise TypeError("stored experiment baseline.config must be a mapping")
    arena = experiment.get("arena")
    arena_mapping = arena if isinstance(arena, Mapping) else {}

    configured_wall = arena_mapping.get("wall_seconds", 12 * 3600.0)
    configured_budget = baseline_config.get("budget_usd")
    budget_usd = (
        None
        if configured_budget is None
        else _number(configured_budget, field="budget_usd")
    )
    auth_file = _optional_string(baseline_config.get("auth_file"), field="auth_file")
    session_id = _optional_string(baseline_config.get("session_id"), field="session_id")
    run_dir = session.method_state_directory / "codex"
    resume_segment_ordinal = _resume_segment_ordinal(
        run_dir,
        session.service.layout.root / "trajectory" / "raw",
    )
    return AgenticRunConfig(
        cli_path=_string(baseline_config.get("cli_path", "codex"), field="cli_path"),
        worktree=session.candidate_workspace,
        run_dir=run_dir,
        trusted_dir=session.service.layout.root,
        prompt=_string(baseline_config.get("prompt", _DEFAULT_PROMPT), field="prompt"),
        model=_optional_string(baseline_config.get("model"), field="model"),
        reasoning_effort=_optional_string(
            baseline_config.get("reasoning_effort"), field="reasoning_effort"
        ),
        provider=_string(baseline_config.get("provider", "native"), field="provider"),
        provider_base_url=_optional_string(
            baseline_config.get("provider_base_url"), field="provider_base_url"
        ),
        provider_env_key=_optional_string(
            baseline_config.get("provider_env_key"), field="provider_env_key"
        ),
        provider_name=_optional_string(
            baseline_config.get("provider_name"), field="provider_name"
        ),
        input_cost_per_million=_number(
            baseline_config.get("input_cost_per_million", 0.0),
            field="input_cost_per_million",
        ),
        output_cost_per_million=_number(
            baseline_config.get("output_cost_per_million", 0.0),
            field="output_cost_per_million",
        ),
        budget_usd=budget_usd,
        wall_budget_s=_number(configured_wall, field="wall_budget_s"),
        max_resumes=_integer(
            baseline_config.get("max_resumes", 100), field="max_resumes"
        ),
        min_segment_s=_number(
            baseline_config.get("min_segment_s", 1.0), field="min_segment_s"
        ),
        max_new_terminal_a_per_b_cycle=_optional_positive_integer(
            baseline_config.get("max_new_terminal_a_per_b_cycle"),
            field="max_new_terminal_a_per_b_cycle",
        ),
        env_overrides=_stored_environment_overrides(experiment),
        extra_cli_args=_string_tuple(
            baseline_config.get("extra_cli_args", ()), field="extra_cli_args"
        ),
        auth_file=Path(auth_file).expanduser() if auth_file is not None else None,
        sandbox_backend=_string(
            baseline_config.get("sandbox_backend", "bwrap"),
            field="sandbox_backend",
        ),
        bwrap_path=_string(
            baseline_config.get("bwrap_path", "bwrap"), field="bwrap_path"
        ),
        sandbox_network=_string(
            baseline_config.get("sandbox_network", "open"),
            field="sandbox_network",
        ),
        allow_unsafe_host=_boolean(
            baseline_config.get("allow_unsafe_host", False),
            field="allow_unsafe_host",
        ),
        run_kind=_string(baseline_config.get("run_kind", "smoke"), field="run_kind"),
        session_id=session_id,
        run_id=session.service.layout.run_id,
        resume_segment_ordinal=resume_segment_ordinal,
    )


def _resume_segment_ordinal(run_dir: Path, trajectory_root: Path) -> int | None:
    """Return the next raw segment only when Codex has durable native state."""

    if not (run_dir / "cli-home").is_dir():
        return None
    ordinals = []
    for path in trajectory_root.glob("segment-*.jsonl"):
        match = _SEGMENT_NAME.fullmatch(path.name)
        if match is not None:
            ordinals.append(int(match.group(1)))
    return max(ordinals, default=-1) + 1


def run_codex(
    session: ArenaSession,
) -> RunResult:
    """Run Codex from the session's stored experiment authority."""

    from rsi_arena.baselines.codex import create_runner

    config = _agentic_config(session)
    development_state = load_development_state(
        method_state_directory=session.method_state_directory,
        owner="codex",
        experiment=_stored_experiment(session),
    )
    runner = create_runner(
        config,
        ledger=session.usage_sink,
        arena_runner=ArenaAgenticRunner(
            session.service,
            fresh_attempt_slot_provider=development_state.slots_for_attempt,
            fixed_a_task_ids_by_benchmark=(
                development_state.fixed_a_task_ids_by_benchmark()
            ),
            max_new_terminal_a_per_b_cycle=(config.max_new_terminal_a_per_b_cycle),
        ),
    )
    return runner.run()


def run_new_codex(
    experiment_mapping: Mapping[str, object],
    *,
    run_root: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
) -> RunResult:
    """Create a run from a mapping, then enter the retained Codex loop."""

    with open_native_session(
        experiment_mapping,
        run_root=run_root,
        workspace_root=workspace_root,
    ) as session:
        initialize_development_state(
            method_state_directory=session.method_state_directory,
            owner="codex",
            experiment=_stored_experiment(session),
        )
        return run_codex(session)


def resume_codex(*, run_root: str | os.PathLike[str]) -> RunResult:
    """Resume Codex using only the run's stored experiment authority."""

    with resume_native_session(run_root=run_root) as session:
        return run_codex(session)


__all__ = ["resume_codex", "run_codex", "run_new_codex"]
