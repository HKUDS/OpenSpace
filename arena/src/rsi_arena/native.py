"""Programmatic lifecycle for one benchmark-neutral native Arena session."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .client import ArenaClient
from .experiment import (
    EXPERIMENT_FILENAME,
    ExperimentAlreadyExistsError,
    JSONObject,
    experiment_digest,
    read_experiment,
    resolve_experiment,
    write_experiment,
)
from .launcher import (
    ARENA_CANDIDATE_ROOT_ENV,
    ARENA_EXPERIMENT_PATH_ENV,
    ARENA_RUN_ROOT_ENV,
    ARENA_WORKSPACE_ENV,
)
from .run_layout import RunValidationError
from .runner import ArenaSession

if TYPE_CHECKING:
    from .composition import GenericArenaComposition


def _authority_environment(
    composition: GenericArenaComposition,
) -> Mapping[str, str]:
    return {
        ARENA_RUN_ROOT_ENV: str(composition.layout.root),
        ARENA_EXPERIMENT_PATH_ENV: str(composition.control_root / EXPERIMENT_FILENAME),
        ARENA_WORKSPACE_ENV: str(composition.workspace),
        ARENA_CANDIDATE_ROOT_ENV: str(composition.candidate_root),
    }


def _open_composed_session(
    composition: GenericArenaComposition,
) -> ArenaSession:
    try:
        run_root = composition.control_root.resolve(strict=True)
        method_root = composition.layout.method_root.resolve(strict=True)
        state_directory = (method_root / "state").resolve(strict=True)
        workspace = composition.workspace.resolve(strict=True)
        context_parent = (workspace / ".rsi-arena").resolve(strict=True)
        method_root.relative_to(run_root)
        state_directory.relative_to(method_root)
        workspace.relative_to(method_root)
        context_parent.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise RunValidationError(
            "native session paths escaped the composed run"
        ) from exc

    client = ArenaClient.from_service(
        composition.service,
        client_state_dir=state_directory / "native-client",
    )
    context_path = context_parent / "context.json"
    context_path.write_text(
        json.dumps(
            client.context().as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return ArenaSession(
        service=composition.service,
        spine=composition.spine,
        client=client,
        developments=composition.spine.developments,
        candidate_workspace=composition.candidate_root,
        prompt_context_path=context_path,
        method_state_directory=state_directory,
        authority_environment=_authority_environment(composition),
        usage_sink=composition.usage_ledger,
        trajectory_sink=composition.spine.recorder,
        event_sink=composition.spine.recorder,
    )


@contextmanager
def open_native_session(
    experiment_mapping: Mapping[str, object],
    *,
    run_root: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
    trusted_candidate_root: str | os.PathLike[str] | None = None,
) -> Iterator[ArenaSession]:
    """Create one new native run and close all run owners on context exit."""

    from .product_composition import compose_new_generic_run

    resolved: JSONObject = resolve_experiment(experiment_mapping)
    root = Path(run_root).expanduser().absolute()
    if (root / EXPERIMENT_FILENAME).exists():
        raise ExperimentAlreadyExistsError(
            f"experiment record already exists under {root}"
        )
    composition = compose_new_generic_run(
        resolved,
        run_root=root,
        workspace_source=workspace_root,
        trusted_candidate_source=trusted_candidate_root,
    )
    try:
        write_experiment(root, resolved)
        composition.bind_experiment(experiment_digest(resolved))
        yield _open_composed_session(composition)
    finally:
        composition.close()


@contextmanager
def resume_native_session(
    *,
    run_root: str | os.PathLike[str],
) -> Iterator[ArenaSession]:
    """Resume exclusively from one run's stored experiment authority."""

    from .held_out_evaluation import assert_evaluation_resume_allowed
    from .product_composition import compose_resumed_generic_run

    assert_evaluation_resume_allowed(run_root)
    resolved = read_experiment(run_root)
    composition = compose_resumed_generic_run(resolved, run_root=run_root)
    try:
        composition.bind_experiment(experiment_digest(resolved))
        yield _open_composed_session(composition)
    finally:
        composition.close()


__all__ = ["open_native_session", "resume_native_session"]
