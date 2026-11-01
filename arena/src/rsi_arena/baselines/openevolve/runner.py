"""Programmatic entrypoint for the OpenEvolve vendored derivative."""

from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path, PurePosixPath
from typing import Mapping

from rsi_arena.baselines._development_state import (
    BaselineDevelopmentState,
    initialize_development_state,
    load_development_state,
)
from rsi_arena.baselines.openevolve.runtime.config import Config
from rsi_arena.baselines.openevolve.runtime.controller import OpenEvolve
from rsi_arena.baselines.openevolve.runtime.database import ProgramDatabase
from rsi_arena.baselines.openevolve.runtime.process_parallel import (
    recover_evaluator_pending,
)
from rsi_arena.experiment import read_experiment
from rsi_arena.native import open_native_session, resume_native_session
from rsi_arena.run_layout import AttemptLimit
from rsi_arena.runner import ArenaSession

from .evaluator import OpenEvolveArenaEvaluator

logger = logging.getLogger(__name__)


class _ArenaOpenEvolve(OpenEvolve):
    """Treat a durably exhausted Arena attempt cap as native completion."""

    async def _run_evolution_with_checkpoints(
        self,
        start_iteration: int,
        max_iterations: int,
        target_score: float | None,
    ) -> None:
        if self.evaluator.session.service.attempt_cap_exhausted:
            return
        try:
            await super()._run_evolution_with_checkpoints(
                start_iteration, max_iterations, target_score
            )
        except AttemptLimit:
            if not self.evaluator.session.service.attempt_cap_exhausted:
                raise
            logger.info("OpenEvolve stopped after exhausting the Arena attempt cap")


async def _run_openevolve_async(
    session: ArenaSession,
    *,
    initial_program: str | Path,
    candidate_path: str,
    config: Mapping[str, object],
    output_dir: str | Path,
    development_state: BaselineDevelopmentState,
):
    """Run the native search loop against an already-open Arena session."""

    if not isinstance(config, Mapping):
        raise TypeError("OpenEvolve config must be a mapping")
    output = Path(output_dir).expanduser().absolute()
    output.mkdir(parents=True, exist_ok=True)
    native_config = Config.from_dict(copy.deepcopy(dict(config)))
    native_config.database.db_path = str(output / "native-database")
    native_config.database.artifacts_base_path = str(output / "native-artifacts")
    native_config.log_dir = str(output / "logs")
    if native_config.evolution_trace.enabled:
        native_config.evolution_trace.output_path = str(
            output / "native-trajectory.jsonl"
        )
    evaluator = OpenEvolveArenaEvaluator(
        session,
        candidate_path=candidate_path,
        development_state=development_state,
    )
    if session.service.attempt_cap_exhausted:
        recovery_database = ProgramDatabase(native_config.database)
        await recover_evaluator_pending(recovery_database, evaluator)
        if not recovery_database.programs:
            logger.info(
                "OpenEvolve attempt cap was exhausted with no admitted native program"
            )
            return None
    controller = _ArenaOpenEvolve(
        initial_program_path=str(
            Path(initial_program).expanduser().resolve(strict=True)
        ),
        evaluator=evaluator,
        config=native_config,
        output_dir=str(output),
    )
    return await controller.run()


def _run_openevolve(
    session: ArenaSession,
    *,
    initial_program: str | Path,
    candidate_path: str,
    config: Mapping[str, object],
    output_dir: str | Path,
    development_state: BaselineDevelopmentState,
):
    """Internal synchronous bridge into the vendored async loop."""

    return asyncio.run(
        _run_openevolve_async(
            session,
            initial_program=initial_program,
            candidate_path=candidate_path,
            config=config,
            output_dir=output_dir,
            development_state=development_state,
        )
    )


def launch_openevolve(
    experiment_mapping: Mapping[str, object],
    *,
    run_root: str | Path,
    workspace_root: str | Path,
):
    """Create an Arena run and execute the vendored OpenEvolve loop.

    ``baseline.config.candidate_path`` names the initial program under the
    Arena-owned candidate workspace. ``baseline.config.native`` is passed to
    OpenEvolve's existing ``Config`` shape. All native state and output paths
    are derived from the opened ``ArenaSession``.
    """

    with open_native_session(
        experiment_mapping,
        run_root=run_root,
        workspace_root=workspace_root,
    ) as session:
        initialize_development_state(
            method_state_directory=session.method_state_directory,
            owner="openevolve",
            experiment=read_experiment(run_root),
        )
        return _run_frozen_openevolve(session, run_root=run_root)


def resume_openevolve(*, run_root: str | Path):
    """Resume OpenEvolve using only the run's frozen experiment authority."""

    with resume_native_session(run_root=run_root) as session:
        return _run_frozen_openevolve(session, run_root=run_root)


def _run_frozen_openevolve(session: ArenaSession, *, run_root: str | Path):
    resolved = read_experiment(run_root)
    development_state = load_development_state(
        method_state_directory=session.method_state_directory,
        owner="openevolve",
        experiment=resolved,
    )
    baseline = resolved.get("baseline")
    if not isinstance(baseline, Mapping):
        raise ValueError("frozen experiment lacks baseline config")
    baseline_config = baseline.get("config")
    if not isinstance(baseline_config, Mapping):
        raise ValueError("frozen OpenEvolve baseline config must be a mapping")

    candidate_path = baseline_config.get("candidate_path")
    relative = (
        PurePosixPath(candidate_path) if isinstance(candidate_path, str) else None
    )
    if (
        relative is None
        or not candidate_path
        or relative.is_absolute()
        or "." in relative.parts
        or ".." in relative.parts
    ):
        raise ValueError(
            "frozen OpenEvolve baseline config requires a contained candidate_path"
        )
    native_config = baseline_config.get("native", {})
    if not isinstance(native_config, Mapping):
        raise TypeError("frozen OpenEvolve native config must be a mapping")

    workspace = session.candidate_workspace.resolve(strict=True)
    initial_program = workspace.joinpath(*relative.parts).resolve(strict=True)
    try:
        initial_program.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            "OpenEvolve initial program escaped the Arena workspace"
        ) from exc
    if not initial_program.is_file():
        raise ValueError("OpenEvolve initial program must be a regular file")

    output_dir = session.method_state_directory / "openevolve"
    return _run_openevolve(
        session,
        initial_program=initial_program,
        candidate_path=relative.as_posix(),
        config=native_config,
        output_dir=output_dir,
        development_state=development_state,
    )


__all__ = [
    "launch_openevolve",
    "resume_openevolve",
]
