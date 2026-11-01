"""Harbor-owned harvest seam for method-visible development trajectories."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rsi_arena.development import DevelopmentArtifact
from rsi_arena.run_layout import atomic_write_bytes

_TRAJECTORY_SOURCE = Path("agent/trajectory.json")
_TERMINUS_PANE_SOURCE = Path("agent/terminus_2.pane")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarvestedDevelopmentAgentTrajectory:
    """One run-private trajectory bundle ready for best-effort development publication."""

    task_id: str
    artifact_digest: str | None
    artifacts: tuple[DevelopmentArtifact, ...]
    provider_diagnostics_digest: str | None = None

    def public_event(
        self, status: Literal["completed", "timeout"]
    ) -> Mapping[str, object] | None:
        if self.artifact_digest is None and self.provider_diagnostics_digest is None:
            return None
        event = {
            "event": "development_task_agent_trajectory",
            "task_id": self.task_id,
            "status": status,
            "artifact_path": f"artifacts/{self.task_id}/agent-trajectory.json"
            if self.artifact_digest
            else None,
            "artifact_digest": self.artifact_digest,
            "partial": status == "timeout",
        }
        if self.provider_diagnostics_digest is not None:
            event["provider_diagnostics_path"] = (
                f"artifacts/{self.task_id}/provider-diagnostics.json"
            )
            event["provider_diagnostics_digest"] = self.provider_diagnostics_digest
        return event


def harvest_development_agent_trajectory(
    *,
    task_id: str,
    trial_artifact_root: Path,
    durable_artifact_root: Path,
) -> HarvestedDevelopmentAgentTrajectory | None:
    """Independently preserve available Harbor trial trajectory files.

    Harbor trial records are trusted local inputs.  A source read or run-private
    landing failure drops only that file and never changes evaluation authority.
    """

    trial_root = Path(trial_artifact_root)
    try:
        trajectory = (trial_root / _TRAJECTORY_SOURCE).read_bytes()
    except FileNotFoundError:
        trajectory = None
    except OSError:
        trajectory = None
        _LOG.warning(
            "development trajectory artifact dropped: task=%s file=%s stage=%s",
            task_id,
            "agent-trajectory.json",
            "source-read",
        )
    try:
        terminus_pane = (trial_root / _TERMINUS_PANE_SOURCE).read_bytes()
    except FileNotFoundError:
        terminus_pane = None
    except OSError:
        terminus_pane = None
        _LOG.warning(
            "development trajectory artifact dropped: task=%s file=%s stage=%s",
            task_id,
            "terminus-pane.txt",
            "source-read",
        )

    durable_root = Path(durable_artifact_root)
    destination = durable_root / task_id
    artifacts: list[DevelopmentArtifact] = []
    artifact_digest: str | None = None
    provider_diagnostics_digest: str | None = None
    # This sidecar is written in the trusted trial parent, outside logs/agent.
    # Validate its bounded schema before publishing it as development evidence.
    from .trial_facts import ResultProvenanceError, _load_provider_diagnostics
    from .trial_files import (
        TrialFileBoundaryError,
        TrialFileMissing,
        read_bounded_trial_file,
    )

    try:
        provider_facts = _load_provider_diagnostics(trial_root, not_before_ns=0)
        if provider_facts is not None:
            provider_bytes = json.dumps(
                provider_facts[0],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            atomic_write_bytes(
                destination / "provider-diagnostics.json", provider_bytes
            )
            artifacts.append(
                DevelopmentArtifact(
                    f"{task_id}/provider-diagnostics.json", provider_bytes
                )
            )
            provider_diagnostics_digest = (
                "sha256:" + hashlib.sha256(provider_bytes).hexdigest()
            )
            for call in provider_facts[4]:
                if call.response_artifact is None:
                    continue
                try:
                    response_bytes = read_bounded_trial_file(
                        trial_root,
                        call.response_artifact,
                        maximum=4 * 1024 * 1024,
                        not_before_ns=0,
                    )
                    if (
                        hashlib.sha256(response_bytes).hexdigest()
                        != call.response_sha256
                    ):
                        raise ValueError("response evidence digest mismatch")
                    atomic_write_bytes(
                        destination / call.response_artifact, response_bytes
                    )
                    artifacts.append(
                        DevelopmentArtifact(
                            f"{task_id}/{call.response_artifact}", response_bytes
                        )
                    )
                except (TrialFileBoundaryError, TrialFileMissing, OSError, ValueError):
                    _LOG.warning(
                        "development provider response dropped: task=%s call=%s",
                        task_id,
                        call.ordinal,
                    )
    except (ResultProvenanceError, OSError):
        _LOG.warning("development provider diagnostics dropped: task=%s", task_id)
    if trajectory is not None:
        try:
            atomic_write_bytes(destination / "agent-trajectory.json", trajectory)
        except OSError:
            _discard_failed_atomic_write(destination / "agent-trajectory.json")
            _LOG.warning(
                "development trajectory artifact dropped: task=%s file=%s stage=%s",
                task_id,
                "agent-trajectory.json",
                "durable-write",
            )
        else:
            artifacts.append(
                DevelopmentArtifact(f"{task_id}/agent-trajectory.json", trajectory)
            )
            artifact_digest = "sha256:" + hashlib.sha256(trajectory).hexdigest()
    if terminus_pane is not None:
        try:
            atomic_write_bytes(destination / "terminus-pane.txt", terminus_pane)
        except OSError:
            _discard_failed_atomic_write(destination / "terminus-pane.txt")
            _LOG.warning(
                "development trajectory artifact dropped: task=%s file=%s stage=%s",
                task_id,
                "terminus-pane.txt",
                "durable-write",
            )
        else:
            artifacts.append(
                DevelopmentArtifact(f"{task_id}/terminus-pane.txt", terminus_pane)
            )
    if not artifacts:
        return None
    return HarvestedDevelopmentAgentTrajectory(
        task_id=task_id,
        artifact_digest=artifact_digest,
        artifacts=tuple(artifacts),
        provider_diagnostics_digest=provider_diagnostics_digest,
    )


def _discard_failed_atomic_write(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    try:
        temporary_paths = tuple(path.parent.glob(f".{path.name}.*.tmp"))
    except OSError:
        return
    for temporary in temporary_paths:
        try:
            temporary.unlink()
        except OSError:
            pass


__all__ = [
    "HarvestedDevelopmentAgentTrajectory",
    "harvest_development_agent_trajectory",
]
