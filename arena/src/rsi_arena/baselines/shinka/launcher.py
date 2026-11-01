"""Session-bound entrypoint for the vendored Shinka derivative."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from rsi_arena import (
    open_native_session,
    read_experiment,
    resume_native_session,
)
from rsi_arena.baselines._development_state import (
    BaselineDevelopmentState,
    initialize_development_state,
    load_development_state,
)
from rsi_arena.runner import ArenaSession

from .adapter import ArenaEvaluator
from .runtime.core import EvolutionConfig, ShinkaEvolveRunner
from .runtime.database import DatabaseConfig
from .runtime.launch import ArenaJobConfig
from .runtime.utils.languages import get_evolve_marker_examples


def _run_shinka(
    session: ArenaSession,
    config: Mapping[str, object],
    *,
    development_state: BaselineDevelopmentState,
) -> Path:
    """Run Shinka's native search loop against one existing Arena session.

    ``config`` is baseline-owned opaque experiment data.  The entrypoint does
    not inspect a source checkout, generate evaluator code or launch an
    external Shinka environment.
    """

    if not isinstance(config, Mapping):
        raise TypeError("Shinka config must be a mapping")
    candidate_path = config.get("candidate_path")
    if not isinstance(candidate_path, str) or not candidate_path:
        raise ValueError("Shinka config requires candidate_path")
    evaluator = ArenaEvaluator(
        session,
        candidate_path=candidate_path,
        development_state=development_state,
    )
    initial_path = session.candidate_workspace.joinpath(*evaluator.candidate_path.parts)
    initial_info = initial_path.lstat()
    if stat.S_ISLNK(initial_info.st_mode) or not stat.S_ISREG(initial_info.st_mode):
        raise ValueError("Shinka initial candidate is not a regular file")
    initial_program = initial_path.read_text(encoding="utf-8")

    state_root = session.method_state_directory / "shinka"
    results_dir = state_root / "results"
    results_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

    database_path = results_dir / "programs.sqlite"
    evaluator.reconcile_persisted(database_path)

    evolution = EvolutionConfig()
    _apply_dataclass_overrides(
        evolution,
        config,
        reserved={"init_program_path", "job_type", "results_dir", "task_sys_msg"},
    )
    if "task_system_message" in config:
        value = config["task_system_message"]
        evolution.task_sys_msg = str(value) if value is not None else None
    evolution.job_type = "arena"
    evolution.results_dir = str(results_dir)
    evolution.use_text_feedback = True
    initial_program = _with_evolve_block(initial_program, evolution.language)

    database = DatabaseConfig()
    _apply_dataclass_overrides(database, config, reserved={"db_path"})
    runner = ShinkaEvolveRunner(
        evo_config=evolution,
        job_config=ArenaJobConfig(evaluator=evaluator),
        db_config=database,
        verbose=bool(config.get("verbose", True)),
        max_evaluation_jobs=int(config.get("max_evaluation_jobs", 4)),
        max_proposal_jobs=int(config.get("max_proposal_jobs", 6)),
        max_db_workers=int(config.get("max_db_workers", 2)),
        init_program_str=initial_program,
    )
    runner.run()
    return results_dir


def _with_evolve_block(program: str, language: str) -> str:
    """Make an external Arena seed editable by Shinka's native patcher."""

    if "EVOLVE-BLOCK-START" in program or "EVOLVE-BLOCK-END" in program:
        return program
    start, end = get_evolve_marker_examples(language)
    separator = "" if program.endswith("\n") else "\n"
    return f"{start}\n{program}{separator}{end}\n"


def launch_shinka(
    experiment_mapping: Mapping[str, object],
    *,
    run_root: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
) -> Path:
    """Create an Arena run and execute Shinka from its frozen baseline config."""

    with open_native_session(
        experiment_mapping,
        run_root=run_root,
        workspace_root=workspace_root,
    ) as session:
        experiment = read_experiment(run_root)
        development_state = initialize_development_state(
            method_state_directory=session.method_state_directory,
            owner="shinka",
            experiment=experiment,
        )
        return _run_shinka(
            session,
            _stored_shinka_config(run_root),
            development_state=development_state,
        )


def resume_shinka(*, run_root: str | os.PathLike[str]) -> Path:
    """Resume Shinka using only the run's stored experiment authority."""

    with resume_native_session(run_root=run_root) as session:
        experiment = read_experiment(run_root)
        development_state = load_development_state(
            method_state_directory=session.method_state_directory,
            owner="shinka",
            experiment=experiment,
        )
        return _run_shinka(
            session,
            _stored_shinka_config(run_root),
            development_state=development_state,
        )


def _stored_shinka_config(
    run_root: str | os.PathLike[str],
) -> Mapping[str, object]:
    experiment = read_experiment(run_root)
    baseline = experiment.get("baseline")
    if not isinstance(baseline, Mapping):
        raise RuntimeError("stored Shinka baseline authority is unavailable")
    config = baseline.get("config")
    if not isinstance(config, Mapping):
        raise RuntimeError("stored Shinka config authority is unavailable")
    return config


def _apply_dataclass_overrides(
    target: object,
    config: Mapping[str, object],
    *,
    reserved: set[str],
) -> None:
    fields = getattr(target, "__dataclass_fields__")
    for name in fields:
        if name in config and name not in reserved:
            setattr(target, name, config[name])


__all__ = ["launch_shinka", "resume_shinka"]
