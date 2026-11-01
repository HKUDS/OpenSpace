"""Harbor physical task-cell backend for a frozen SWE inner-agent candidate.

Harbor owns Docker and the fresh task package.  The candidate's admitted
source tree feeds either the unchanged native mini-swe-agent wrapper or the
shared isolated Harbor bridge.  The selected agent modifies only the package-
owned task environment, then trusted code stops it and publishes a signed tree
snapshot.  Harbor's
``environment_mode = separate`` verifier grades that snapshot in a different
container.  The outer runner validates Harbor provenance and derives the
task-specific binary patch; it has no RunRecorder or baseline feedback access.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import signal
import subprocess
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from rsi_arena.benchmarks.harbor_runtime import frozen_mini_agent
from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    AGENT_IMPORT_PATH as SHARED_HARBOR_AGENT_IMPORT_PATH,
)
from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    CANDIDATE_RUNTIME_FAILURE_KEY,
    candidate_runtime_failure_from_context,
    harbor_agent_bridge_identity,
    harbor_cli_agent_args,
    load_hmac_key_file,
)
from rsi_arena.benchmarks.harbor_runtime.controller import (
    APPROVED_HARBOR_CONTROLLER_COMMIT,
    APPROVED_HARBOR_CONTROLLER_TREE,
    HarborControllerSpec,
    HarborRuntimeError,
    verify_approved_controller,
)
from rsi_arena.benchmarks.harbor_runtime.execution import (
    HarborExecutionError,
    cleanup_scoped_compose_cell,
    docker_container_inventory,
    scoped_compose_container_ids,
    signal_process_group,
    stage_credential_handle,
    terminate_process_group_and_cleanup,
)
from rsi_arena.benchmarks.harbor_runtime.materials import snapshot_task_materials
from rsi_arena.benchmarks.harbor_runtime.provenance import (
    ArtifactEnvelopeExpectation,
    load_validated_trial_result,
    provider_calls_from_trial,
)
from rsi_arena.benchmarks.swe.campaign import (
    SweTaskCellInfraError,
    SweTaskMeasurement,
    TrustedSweTaskSpec,
)
from rsi_arena.benchmarks.swe.docker_runtime import (
    OfficialSweDockerError,
    OfficialSweTaskSource,
    materialize_content_pinned_task_package,
)
from rsi_arena.benchmarks.swe.runtime import (
    HarborModelDelivery,
    freeze_exported_task_snapshot,
)
from rsi_arena.contracts import canonical_digest
from rsi_arena.evaluation_gateway import GatewayCandidate
from rsi_arena.inner_agent_runtime import parse_python_inner_agent_ref
from rsi_arena.run_layout import RunLayout

TREE_PAYLOAD_NAME = "rsi-arena-tree.tar"
SWE_PROVIDER_BASE_URL_ENV = "RSI_ARENA_SWE_PROVIDER_BASE_URL"
_SWE_TASK_ARTIFACT_SOURCE = "/testbed"
_BASE_ENV_NAMES = ("PATH", "LANG", "DOCKER_HOST", "DOCKER_CONFIG")
_HARBOR_AGENT_SETUP_TIMEOUT_SECONDS = 360.0
_HARBOR_ORCHESTRATION_SLACK_SECONDS = 300.0
_WORKER_STATUS_MAX_BYTES = 64 * 1024
_LOG = logging.getLogger(__name__)


class SweHarborCellError(RuntimeError):
    """A physical Harbor task cell failed its trusted boundary."""


def _candidate_runtime_failure_for_public_feedback(
    trial: Mapping[str, object],
    *,
    reward: float,
    credential_literal: str | None,
    task_id: str,
) -> dict[str, str] | None:
    """Return the shared validated zero-score marker with literals scrubbed."""

    if reward != 0.0:
        return None
    forbidden_literals = tuple(
        literal
        for literal in (credential_literal, task_id)
        if isinstance(literal, str) and literal
    )
    return candidate_runtime_failure_from_context(
        trial.get("agent_result"),
        forbidden_literals=forbidden_literals,
    )


def _agent_runtime_failure_recorded(cell_root: Path) -> bool:
    """Detect a failed isolated worker without publishing its diagnostics."""

    job_root = Path(cell_root) / "harbor" / "cell"
    try:
        trials = [
            child
            for child in job_root.iterdir()
            if child.is_dir() and not child.is_symlink()
        ]
    except OSError:
        return False
    if len(trials) != 1:
        return False
    status = trials[0] / "agent" / "worker-status.json"
    try:
        info = status.lstat()
        if (
            not status.is_file()
            or status.is_symlink()
            or info.st_nlink != 1
            or info.st_size > _WORKER_STATUS_MAX_BYTES
        ):
            return False
        descriptor = os.open(status, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(descriptor, _WORKER_STATUS_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) != info.st_size or len(raw) > _WORKER_STATUS_MAX_BYTES:
            return False
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping) or payload.get("ok") is not False:
        return False
    error_type = payload.get("error_type")
    return (
        isinstance(error_type, str)
        and 0 < len(error_type) <= 128
        or CANDIDATE_RUNTIME_FAILURE_KEY in payload
    )


def _trusted_git_env() -> dict[str, str]:
    """Return a deterministic Git environment with no provider credentials."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/dev/null",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def _trusted_docker_env() -> dict[str, str]:
    """Return only the host settings needed to reach the Docker daemon."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **(
            {"DOCKER_HOST": os.environ["DOCKER_HOST"]}
            if "DOCKER_HOST" in os.environ
            else {}
        ),
        **(
            {"DOCKER_CONFIG": os.environ["DOCKER_CONFIG"]}
            if "DOCKER_CONFIG" in os.environ
            else {}
        ),
    }


@dataclass(frozen=True)
class HarborTimeoutBudget:
    """Explicit inner phase bounds and a strictly larger outer wall."""

    environment_build_seconds: float
    agent_setup_seconds: float
    worker_seconds: float
    candidate_harvest_grace_seconds: float
    verifier_seconds: float
    cleanup_grace_seconds: float
    orchestration_slack_seconds: float
    harbor_wall_seconds: float
    agent_timeout_multiplier: float
    verifier_timeout_multiplier: float

    @property
    def agent_phase_seconds(self) -> float:
        return self.worker_seconds + self.candidate_harvest_grace_seconds

    @property
    def required_harbor_wall_seconds(self) -> float:
        return (
            2 * self.environment_build_seconds
            + self.agent_setup_seconds
            + self.agent_phase_seconds
            + self.verifier_seconds
            + 2 * self.cleanup_grace_seconds
            + self.orchestration_slack_seconds
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "environment_build_seconds": self.environment_build_seconds,
            "environment_build_phase_count": 2.0,
            "agent_setup_seconds": self.agent_setup_seconds,
            "worker_seconds": self.worker_seconds,
            "candidate_harvest_grace_seconds": (self.candidate_harvest_grace_seconds),
            "agent_phase_seconds": self.agent_phase_seconds,
            "verifier_seconds": self.verifier_seconds,
            "cleanup_grace_seconds": self.cleanup_grace_seconds,
            "cleanup_phase_count": 2.0,
            "orchestration_slack_seconds": self.orchestration_slack_seconds,
            "required_harbor_wall_seconds": self.required_harbor_wall_seconds,
            "harbor_wall_seconds": self.harbor_wall_seconds,
            "agent_timeout_multiplier": self.agent_timeout_multiplier,
            "verifier_timeout_multiplier": self.verifier_timeout_multiplier,
        }


def _positive_seconds(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise SweHarborCellError(f"{label} must be a positive finite timeout")
    return float(value)


def _require_supported_swe_inner_agent_config(
    agent_ref: str, agent_config: Mapping[str, object]
) -> None:
    """Reject config that the current native mini runner cannot execute."""

    if agent_ref == "builtin:mini-swe-agent" and agent_config:
        raise ValueError(
            "benchmark_runtime.inner_agent.config must be empty: current native "
            "mini runner does not support inner-agent config"
        )


def harbor_timeout_budget(
    task_dir: Path,
    *,
    worker_timeout_seconds: float,
    candidate_harvest_grace_seconds: float,
    verifier_timeout_seconds: float,
    harbor_wall_timeout_seconds: float,
    cleanup_grace_seconds: float,
) -> HarborTimeoutBudget:
    """Bind the outer wall above every Harbor phase and cleanup allowance."""

    try:
        config = tomllib.loads((Path(task_dir) / "task.toml").read_text())
        agent_base = _positive_seconds(
            config["agent"]["timeout_sec"], "task agent timeout"
        )
        verifier_base = _positive_seconds(
            config["verifier"]["timeout_sec"], "task verifier timeout"
        )
        environment_build = _positive_seconds(
            config["environment"]["build_timeout_sec"],
            "task environment build timeout",
        )
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise SweHarborCellError("task timeout policy is missing or invalid") from exc
    worker = _positive_seconds(worker_timeout_seconds, "candidate worker timeout")
    harvest = _positive_seconds(
        candidate_harvest_grace_seconds, "candidate harvest grace timeout"
    )
    verifier = _positive_seconds(verifier_timeout_seconds, "verifier timeout")
    outer = _positive_seconds(harbor_wall_timeout_seconds, "Harbor wall timeout")
    cleanup = _positive_seconds(cleanup_grace_seconds, "cleanup grace timeout")
    budget = HarborTimeoutBudget(
        environment_build_seconds=environment_build,
        agent_setup_seconds=_HARBOR_AGENT_SETUP_TIMEOUT_SECONDS,
        worker_seconds=worker,
        candidate_harvest_grace_seconds=harvest,
        verifier_seconds=verifier,
        cleanup_grace_seconds=cleanup,
        orchestration_slack_seconds=_HARBOR_ORCHESTRATION_SLACK_SECONDS,
        harbor_wall_seconds=outer,
        agent_timeout_multiplier=(worker + harvest) / agent_base,
        verifier_timeout_multiplier=verifier / verifier_base,
    )
    if outer <= budget.required_harbor_wall_seconds:
        raise SweHarborCellError(
            f"Harbor wall timeout ({outer:g}s) must exceed task-required "
            "build+setup+agent+verifier+cleanup bounds "
            f"({budget.required_harbor_wall_seconds:g}s)"
        )
    return budget


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def harbor_swe_backend_digest(
    trusted_harbor_root: Path,
    model: HarborModelDelivery,
    *,
    agent_ref: str,
    agent_config: Mapping[str, object],
    bwrap_path: str | Path,
    bridge_identity_digest: str | None,
    worker_timeout_seconds: float,
    candidate_harvest_grace_seconds: float,
    verifier_timeout_seconds: float,
    harbor_wall_timeout_seconds: float,
    cleanup_grace_seconds: float,
) -> str:
    """Return the layout-bindable physical backend identity before a run exists."""

    _require_supported_swe_inner_agent_config(agent_ref, agent_config)
    return canonical_digest(
        {
            "schema": "swe-harbor-backend",
            "harbor_locator": str(Path(trusted_harbor_root).expanduser()),
            "approved_harbor_controller": {
                "commit": APPROVED_HARBOR_CONTROLLER_COMMIT,
                "tree": APPROVED_HARBOR_CONTROLLER_TREE,
            },
            "implementation_digest": "sha256:"
            + canonical_digest(
                {
                    "swe_harbor_backend": _sha256_file(Path(__file__)),
                    "shared_frozen_mini_agent": _sha256_file(
                        Path(str(frozen_mini_agent.__file__))
                    ),
                }
            ),
            "inner_agent_ref": agent_ref,
            "inner_agent_config": dict(agent_config),
            "native_entrypoint": (
                "python -m minisweagent.run.mini"
                if agent_ref == "builtin:mini-swe-agent"
                else None
            ),
            "shared_bridge_identity_digest": bridge_identity_digest,
            "bwrap_locator": str(bwrap_path),
            "candidate_mount": "read_only",
            "task_container": "fresh_per_call",
            "verifier_environment_mode": "separate",
            "model_delivery": model.delivery_identity(),
            "worker_timeout_seconds": _positive_seconds(
                worker_timeout_seconds, "candidate worker timeout"
            ),
            "candidate_harvest_grace_seconds": _positive_seconds(
                candidate_harvest_grace_seconds,
                "candidate harvest grace timeout",
            ),
            "verifier_timeout_seconds": _positive_seconds(
                verifier_timeout_seconds, "verifier timeout"
            ),
            "harbor_wall_timeout_seconds": _positive_seconds(
                harbor_wall_timeout_seconds, "Harbor wall timeout"
            ),
            "cleanup_grace_seconds": _positive_seconds(
                cleanup_grace_seconds, "cleanup grace timeout"
            ),
        }
    )


def harbor_controller_runtime_identity(
    trusted_harbor_root: Path,
    docker_path: str | Path = "/usr/bin/docker",
) -> dict[str, str]:
    """Verify and bind the sole approved clean Harbor controller checkout."""

    try:
        docker = (
            Path(docker_path).expanduser().resolve(strict=True)
            if Path(docker_path).is_absolute()
            else Path(shutil.which(str(docker_path)) or "").resolve(strict=True)
        )
        identity = verify_approved_controller(
            HarborControllerSpec(
                root=Path(trusted_harbor_root).expanduser().absolute(),
                docker_executable=docker,
            )
        )
    except (HarborRuntimeError, OSError, ValueError) as exc:
        raise SweHarborCellError(str(exc)) from exc
    return {
        "schema": "rsi-arena-approved-harbor-controller",
        "root": str(identity.root),
        "commit": identity.commit,
        "tree": identity.tree,
        "executable_sha256": _sha256_file(identity.executable),
        "docker_executable_sha256": _sha256_file(identity.docker_executable),
    }


def _signal_process_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
    signal_process_group(process, int(signal.Signals(signal_number)))


def _docker_container_inventory(docker: Path) -> list[dict[str, object]]:
    try:
        return docker_container_inventory(docker)
    except HarborExecutionError as exc:
        raise SweHarborCellError(str(exc)) from exc


def _scoped_compose_container_ids(
    inventory: list[dict[str, object]], cell_root: Path, *, running_only: bool
) -> tuple[str, ...]:
    """Select only Compose containers anchored by this task cell's bind mounts."""

    return scoped_compose_container_ids(inventory, cell_root, running_only=running_only)


def _stop_scoped_compose_containers(
    docker: Path, cell_root: Path, *, cleanup_grace_seconds: float
) -> None:
    try:
        cleanup_scoped_compose_cell(
            docker,
            cell_root,
            cleanup_grace_seconds=cleanup_grace_seconds,
            inventory=_docker_container_inventory,
            run=subprocess.run,
        )
    except HarborExecutionError as exc:
        raise SweHarborCellError(str(exc)) from exc


def _terminate_harbor_and_stop_cell(
    process: subprocess.Popen[bytes],
    *,
    docker: Path,
    cell_root: Path,
    cleanup_grace_seconds: float,
) -> None:
    """Gracefully terminate Harbor, then stop only this cell's Compose project."""

    terminate_process_group_and_cleanup(
        process,
        docker=docker,
        cell_root=cell_root,
        cleanup_grace_seconds=cleanup_grace_seconds,
        signal_group=_signal_process_group,
        cleanup=_stop_scoped_compose_containers,
    )


def _trial_directory(job_dir: Path) -> Path:
    trials = [
        child
        for child in Path(job_dir).iterdir()
        if child.is_dir()
        and not child.is_symlink()
        and (child / "result.json").is_file()
    ]
    if len(trials) != 1:
        raise SweHarborCellError("Harbor job does not contain exactly one trial")
    return trials[0]


def _validated_usage(
    trial: Mapping[str, object],
) -> tuple[dict[str, object], float | None, str]:
    context = trial.get("agent_result")
    if not isinstance(context, Mapping):
        return (
            {
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_tokens": None,
                "token_source": "unknown",
                "token_status": "missing",
                "input_token_status": "missing",
                "output_token_status": "missing",
                "cache_token_status": "missing",
                "usage_source": "harbor-agent-result",
                "status": "missing",
            },
            None,
            "missing",
        )
    raw_input = context.get("n_input_tokens")
    raw_output = context.get("n_output_tokens")
    if all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (raw_input, raw_output)
    ):
        input_tokens: int | None = int(raw_input)
        output_tokens: int | None = int(raw_output)
        token_status = "reported"
    else:
        input_tokens = output_tokens = None
        token_status = "missing"
    raw_cache = context.get("n_cache_tokens")
    cache_tokens = (
        int(raw_cache)
        if isinstance(raw_cache, int)
        and not isinstance(raw_cache, bool)
        and raw_cache >= 0
        else None
    )
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_tokens,
        "token_source": "provider" if token_status == "reported" else "unknown",
        "token_status": token_status,
        "input_token_status": token_status,
        "output_token_status": token_status,
        "cache_token_status": "reported" if cache_tokens is not None else "missing",
        "usage_source": "harbor-agent-result",
        "status": token_status,
        "calls": [
            {
                "role": "solver-agent",
                **call,
            }
            for call in provider_calls_from_trial(trial)
        ],
    }
    raw_cost = context.get("cost_usd")
    if (
        isinstance(raw_cost, (int, float))
        and not isinstance(raw_cost, bool)
        and math.isfinite(float(raw_cost))
        and float(raw_cost) >= 0
    ):
        return usage, float(raw_cost), "reported"
    return usage, None, "missing"


def _reward(trial: Mapping[str, object]) -> float:
    verifier = trial.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, Mapping) else None
    if not isinstance(rewards, Mapping) or not rewards:
        raise SweHarborCellError("separate official grader omitted its reward")
    raw = rewards.get("reward", next(iter(rewards.values())))
    if raw not in {0, 1}:
        raise SweHarborCellError("official SWE reward is not binary")
    return float(raw)


def harbor_swe_cell_command(
    *,
    harbor_bin: Path,
    task_dir: Path,
    jobs_dir: Path,
    candidate_checkout: Path,
    trusted_harbor_root: Path | None = None,
    candidate_tree_digest: str,
    task_public_digest: str,
    backend_digest: str,
    nonce: str,
    envelope_key_file: Path,
    model: HarborModelDelivery,
    agent_ref: str = "builtin:mini-swe-agent",
    agent_config: Mapping[str, object] | None = None,
    bridge_identity_digest: str | None = None,
    bwrap_path: str | Path = "bwrap",
    credential_handle: Path | None = None,
    worker_timeout_seconds: int,
    candidate_harvest_grace_seconds: int,
    agent_timeout_multiplier: float,
    verifier_timeout_multiplier: float,
) -> tuple[str, ...]:
    """Build the auditable Harbor argv without accepting a credential value."""

    if not model.credential_delivery_env:
        raise ValueError("Harbor command requires provider model delivery")
    _require_supported_swe_inner_agent_config(agent_ref, agent_config or {})
    credential_env = str(model.credential_delivery_env)
    command = [
        str(harbor_bin),
        "run",
        "-p",
        str(task_dir),
        "-m",
        model.model_name,
        "--jobs-dir",
        str(jobs_dir),
        "--job-name",
        "cell",
        "-n",
        "1",
        "-k",
        "1",
        "--max-retries",
        "0",
        "--delete",
        "--agent-timeout-multiplier",
        str(float(agent_timeout_multiplier)),
        "--verifier-timeout-multiplier",
        str(float(verifier_timeout_multiplier)),
        "--agent-setup-timeout-multiplier",
        "1.0",
        "--environment-build-timeout-multiplier",
        "1.0",
        "-q",
        "-y",
    ]
    if agent_ref == "builtin:mini-swe-agent":
        mounts = json.dumps(
            [
                {
                    "type": "bind",
                    "source": str(Path(candidate_checkout).resolve(strict=True)),
                    "target": str(frozen_mini_agent.GUEST_CANDIDATE_ROOT),
                    "read_only": True,
                }
            ],
            separators=(",", ":"),
        )
        command += [
            "--agent",
            frozen_mini_agent.AGENT_IMPORT_PATH,
            "--agent-kwarg",
            f"candidate_tree_digest={candidate_tree_digest}",
            "--agent-kwarg",
            f"task_public_digest={task_public_digest}",
            "--agent-kwarg",
            f"backend_digest={backend_digest}",
            "--agent-kwarg",
            f"nonce={nonce}",
            "--agent-kwarg",
            f"envelope_key_file={envelope_key_file}",
            "--agent-kwarg",
            f"api_base_url={model.api_base_url}",
            "--agent-kwarg",
            f"credential_env_name={credential_env}",
            "--agent-kwarg",
            f"model_revision={model.model_revision}",
            "--agent-kwarg",
            f"artifact_source={_SWE_TASK_ARTIFACT_SOURCE}",
            "--agent-kwarg",
            f"artifact_payload_name={TREE_PAYLOAD_NAME}",
            "--agent-kwarg",
            f"worker_timeout_seconds={int(worker_timeout_seconds)}",
            "--agent-kwarg",
            (f"candidate_harvest_grace_seconds={int(candidate_harvest_grace_seconds)}"),
            "--agent-env",
            (
                f"{frozen_mini_agent.CREDENTIAL_HANDLE_ENV}="
                f"${{{frozen_mini_agent.CREDENTIAL_HANDLE_ENV}}}"
            ),
            "--mounts",
            mounts,
        ]
    else:
        if (
            agent_ref != "builtin:terminus-2"
            and parse_python_inner_agent_ref(agent_ref) is None
        ):
            raise ValueError("SWE inner-agent ref is unsupported")
        if trusted_harbor_root is None:
            raise ValueError("shared SWE bridge requires trusted_harbor_root")
        if bridge_identity_digest is None:
            raise ValueError("shared SWE bridge requires its frozen identity")
        if credential_handle is None:
            raise ValueError("shared SWE bridge requires a credential handle")
        command += harbor_cli_agent_args(
            agent_ref=agent_ref,
            agent_config=agent_config,
            benchmark_id="swe",
            candidate_checkout=candidate_checkout,
            trusted_harbor_root=trusted_harbor_root,
            codec="tree_snapshot",
            source="/testbed",
            payload_name=TREE_PAYLOAD_NAME,
            nonce=nonce,
            candidate_tree_digest=candidate_tree_digest,
            task_public_digest=task_public_digest,
            bridge_identity_digest=bridge_identity_digest,
            envelope_key_file=envelope_key_file,
            worker_timeout_seconds=worker_timeout_seconds,
            cleanup_grace_seconds=candidate_harvest_grace_seconds,
            bwrap_path=bwrap_path,
            credential_handle=credential_handle,
            credential_env_name=credential_env,
            provider_name=model.provider_name,
            provider_base_url=model.api_base_url,
            provider_base_url_env_name=SWE_PROVIDER_BASE_URL_ENV,
        )
    provider_host = urlparse(str(model.api_base_url)).hostname
    if provider_host:
        command += ["--allow-agent-host", provider_host]
    return tuple(command)


class HarborSweTaskCellRunner:
    """One-call physical backend; deliberately owns no aggregate state."""

    def __init__(
        self,
        *,
        layout: RunLayout,
        trusted_harbor_root: Path,
        docker_path: str | Path,
        bwrap_path: str | Path = "bwrap",
        agent_ref: str = "builtin:mini-swe-agent",
        agent_config: Mapping[str, object] | None = None,
        model: HarborModelDelivery,
        worker_timeout_seconds: float,
        candidate_harvest_grace_seconds: float,
        verifier_timeout_seconds: float,
        harbor_wall_timeout_seconds: float,
        cleanup_grace_seconds: float,
    ) -> None:
        if not model.credential_delivery_env:
            raise ValueError("physical Harbor runner requires a provider model")
        self.layout = layout
        self._trusted_harbor_locator = Path(trusted_harbor_root).expanduser()
        self._docker_locator = docker_path
        self.bwrap_path = str(bwrap_path)
        self.trusted_harbor_root: Path | None = None
        self.harbor_bin: Path | None = None
        self.harbor_controller: Mapping[str, str] | None = None
        self.docker: Path | None = None
        self.model = model
        if (
            agent_ref not in {"builtin:mini-swe-agent", "builtin:terminus-2"}
            and parse_python_inner_agent_ref(agent_ref) is None
        ):
            raise ValueError("SWE inner-agent ref is unsupported")
        try:
            copied_agent_config = json.loads(
                json.dumps(dict(agent_config or {}), allow_nan=False)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("SWE inner-agent config must be finite JSON") from exc
        assert isinstance(copied_agent_config, dict)
        _require_supported_swe_inner_agent_config(agent_ref, copied_agent_config)
        self.agent_ref = agent_ref
        self.agent_config = MappingProxyType(copied_agent_config)
        self.bridge_identity_digest = (
            None
            if agent_ref == "builtin:mini-swe-agent"
            else harbor_agent_bridge_identity()
        )
        self.credential_value: str | None = None
        self.worker_timeout_seconds = _positive_seconds(
            worker_timeout_seconds, "candidate worker timeout"
        )
        self.candidate_harvest_grace_seconds = _positive_seconds(
            candidate_harvest_grace_seconds, "candidate harvest grace timeout"
        )
        self.verifier_timeout_seconds = _positive_seconds(
            verifier_timeout_seconds, "verifier timeout"
        )
        self.harbor_wall_timeout_seconds = _positive_seconds(
            harbor_wall_timeout_seconds, "Harbor wall timeout"
        )
        self.cleanup_grace_seconds = _positive_seconds(
            cleanup_grace_seconds, "cleanup grace timeout"
        )
        key_path = layout.trusted_runtime_root / "harbor-swe-envelope.key"
        if not key_path.exists():
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(descriptor, secrets.token_hex(32).encode("ascii") + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.key_path = key_path
        self.hmac_key = load_hmac_key_file(key_path)
        self._backend_digest = harbor_swe_backend_digest(
            self._trusted_harbor_locator,
            model,
            agent_ref=self.agent_ref,
            agent_config=self.agent_config,
            bwrap_path=self.bwrap_path,
            bridge_identity_digest=self.bridge_identity_digest,
            worker_timeout_seconds=self.worker_timeout_seconds,
            candidate_harvest_grace_seconds=(self.candidate_harvest_grace_seconds),
            verifier_timeout_seconds=self.verifier_timeout_seconds,
            harbor_wall_timeout_seconds=self.harbor_wall_timeout_seconds,
            cleanup_grace_seconds=self.cleanup_grace_seconds,
        )

    @property
    def backend_digest(self) -> str:
        return self._backend_digest

    @property
    def model_name(self) -> str:
        return self.model.model_name

    def resolve_task_timeout_budget(
        self, trusted_task_spec: TrustedSweTaskSpec
    ) -> HarborTimeoutBudget:
        """Validate and resolve one package-owned task timeout policy."""

        return harbor_timeout_budget(
            trusted_task_spec.task_dir,
            worker_timeout_seconds=self.worker_timeout_seconds,
            candidate_harvest_grace_seconds=(self.candidate_harvest_grace_seconds),
            verifier_timeout_seconds=self.verifier_timeout_seconds,
            harbor_wall_timeout_seconds=self.harbor_wall_timeout_seconds,
            cleanup_grace_seconds=self.cleanup_grace_seconds,
        )

    def _stage_credential_handle(self, cell_root: Path) -> Path:
        try:
            return stage_credential_handle(cell_root, self.credential_value)
        except HarborExecutionError as exc:
            raise SweHarborCellError(str(exc)) from exc

    def _resolve_runtime(self) -> None:
        if self.harbor_bin is not None:
            return
        credential = os.environ.get(self.model.credential_source_env)
        if credential is None:
            raise SweHarborCellError("provider credential is missing")
        docker = (
            Path(self._docker_locator).resolve(strict=True)
            if Path(self._docker_locator).is_absolute()
            else Path(shutil.which(str(self._docker_locator)) or "")
        )
        if not docker.is_file():
            raise SweHarborCellError("Docker executable is unavailable")
        root = self._trusted_harbor_locator.resolve(strict=True)
        controller = harbor_controller_runtime_identity(root, docker)
        harbor_bin = root / ".venv/bin/harbor"
        self.trusted_harbor_root = root
        self.harbor_bin = harbor_bin
        self.harbor_controller = MappingProxyType(controller)
        self.docker = docker
        self.credential_value = credential

    def _subprocess_env(
        self, cell_root: Path, credential_handle: Path
    ) -> dict[str, str]:
        env = {name: os.environ[name] for name in _BASE_ENV_NAMES if name in os.environ}
        env.update(
            {
                "HOME": str(cell_root / "home"),
                "TMPDIR": str(cell_root / "tmp"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                "PYTHONNOUSERSITE": "1",
                frozen_mini_agent.CREDENTIAL_HANDLE_ENV: str(credential_handle),
            }
        )
        return env

    def _start_harbor_process(
        self,
        command: tuple[str, ...],
        *,
        cell_root: Path,
        credential_handle: Path,
        output: Any,
    ) -> subprocess.Popen[bytes]:
        """Launch Harbor with the exact outer environment audited by tests."""

        return subprocess.Popen(
            list(command),
            cwd=str(cell_root),
            env=self._subprocess_env(cell_root, credential_handle),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def run_frozen_candidate_on_task(
        self,
        frozen_candidate: GatewayCandidate,
        trusted_task_spec: TrustedSweTaskSpec,
        *,
        pre_cleanup_trial_hook: Callable[[Path], None] | None = None,
    ) -> SweTaskMeasurement:
        self._resolve_runtime()
        started = time.monotonic()
        cell_id = "harbor-cell-" + uuid.uuid4().hex
        runtime_use: dict[str, object] = {}
        official_root = (
            self.layout.root / "task-cells" / trusted_task_spec.task_id / cell_id
        )
        try:
            return self._run_physical_cell(
                frozen_candidate,
                trusted_task_spec,
                runtime_use=runtime_use,
                started=started,
                cell_id=cell_id,
                pre_cleanup_trial_hook=pre_cleanup_trial_hook,
            )
        except SweTaskCellInfraError:
            raise
        except Exception as exc:
            artifact_refs = {
                "cell": official_root.relative_to(self.layout.root).as_posix()
            }
            harbor_log = official_root / "harbor.stdout.log"
            if harbor_log.is_file():
                artifact_refs["harbor_log"] = harbor_log.relative_to(
                    self.layout.root
                ).as_posix()
            harbor_job = official_root / "harbor" / "cell"
            if harbor_job.is_dir():
                artifact_refs["harbor_job"] = harbor_job.relative_to(
                    self.layout.root
                ).as_posix()
            agent_runtime_failure = _agent_runtime_failure_recorded(official_root)
            raise SweTaskCellInfraError(
                cell_id=cell_id,
                error_type=(
                    "swe_runtime_resolution_failed"
                    if isinstance(exc, OfficialSweDockerError) and not runtime_use
                    else (
                        "agent_runtime_failure"
                        if agent_runtime_failure
                        else "harbor_task_cell_failure"
                    )
                ),
                artifact_refs=artifact_refs,
                provenance={
                    "schema": "swe-harbor-task-inconclusive",
                    "backend_digest": self.backend_digest,
                    "harbor_controller": dict(self.harbor_controller or {}),
                    "physical_cell_allocated": official_root.is_dir(),
                    "runtime_identity_recorded": bool(runtime_use),
                    "agent_runtime_failure_recorded": agent_runtime_failure,
                    **runtime_use,
                    "exception_detail_recorded": False,
                },
            ) from exc

    def _run_physical_cell(
        self,
        frozen_candidate: GatewayCandidate,
        trusted_task_spec: TrustedSweTaskSpec,
        *,
        runtime_use: dict[str, object],
        started: float,
        cell_id: str,
        pre_cleanup_trial_hook: Callable[[Path], None] | None = None,
    ) -> SweTaskMeasurement:
        candidate = Path(frozen_candidate.checkout_path).resolve(strict=True)
        task_dir = trusted_task_spec.task_dir.resolve(strict=True)
        timeout_budget = self.resolve_task_timeout_budget(trusted_task_spec)
        before = snapshot_task_materials(task_dir)
        if (
            before.public_digest != trusted_task_spec.public_material_digest
            or before.evaluator_digest != trusted_task_spec.evaluator_material_digest
        ):
            raise SweHarborCellError("trusted task material moved before execution")
        cell_root = self.layout.trusted_runtime_root / "task-cells" / cell_id
        for child in (cell_root, cell_root / "home", cell_root / "tmp"):
            child.mkdir(mode=0o700, parents=True)
        official_root = (
            self.layout.root / "task-cells" / trusted_task_spec.task_id / cell_id
        )
        official_root.mkdir(mode=0o700, parents=True)

        source_repo = cell_root / "official-source"
        source = OfficialSweTaskSource(
            task_dir,
            docker_path=self.docker,
            expected_task_id=trusted_task_spec.task_id,
            expected_repo=trusted_task_spec.repo,
        )
        source_export = source.export_fresh_repo(source_repo)
        runtime_use.update(
            {
                "runtime_identity_recorded": True,
                "runtime_identity": source_export.runtime_identity().as_dict(),
                "source_export": source_export.binding(),
                "source_export_digest": source_export.digest,
            }
        )
        materialized_task_dir = cell_root / "content-pinned-task"
        task_materialization = materialize_content_pinned_task_package(
            task_dir,
            materialized_task_dir,
            runtime_image_ref=source_export.runtime_image_ref,
        )
        materialized_before = snapshot_task_materials(materialized_task_dir)
        base_bare = cell_root / "task-base.git"
        cloned = subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--no-hardlinks",
                "--",
                str(source_repo),
                str(base_bare),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=_trusted_git_env(),
        )
        if cloned.returncode != 0:
            raise SweHarborCellError("could not freeze official task base")

        nonce = "swe-cell-" + uuid.uuid4().hex
        expectation = ArtifactEnvelopeExpectation(
            nonce=nonce,
            codec="tree_snapshot",
            payload_name=TREE_PAYLOAD_NAME,
            candidate_tree_digest=frozen_candidate.tree_sha,
            task_public_digest=trusted_task_spec.public_material_digest,
            bridge_identity_digest=(
                self.backend_digest
                if self.bridge_identity_digest is None
                else self.bridge_identity_digest
            ),
            hmac_key=self.hmac_key,
        )
        jobs_dir = official_root / "harbor"
        credential_handle: Path | None = None
        try:
            if self.agent_ref != "builtin:mini-swe-agent":
                credential_handle = self._stage_credential_handle(cell_root)
            command = harbor_swe_cell_command(
                harbor_bin=self.harbor_bin,
                task_dir=materialized_task_dir,
                jobs_dir=jobs_dir,
                candidate_checkout=candidate,
                trusted_harbor_root=self.trusted_harbor_root,
                candidate_tree_digest=frozen_candidate.tree_sha,
                task_public_digest=trusted_task_spec.public_material_digest,
                backend_digest=self.backend_digest,
                nonce=nonce,
                envelope_key_file=self.key_path,
                model=self.model,
                agent_ref=self.agent_ref,
                agent_config=self.agent_config,
                bridge_identity_digest=self.bridge_identity_digest,
                bwrap_path=self.bwrap_path,
                credential_handle=credential_handle,
                worker_timeout_seconds=int(self.worker_timeout_seconds),
                candidate_harvest_grace_seconds=int(
                    self.candidate_harvest_grace_seconds
                ),
                agent_timeout_multiplier=timeout_budget.agent_timeout_multiplier,
                verifier_timeout_multiplier=(
                    timeout_budget.verifier_timeout_multiplier
                ),
            )
            marker = official_root / ".provenance-start"
            marker.write_text(uuid.uuid4().hex, encoding="ascii")
            not_before_ns = marker.stat().st_mtime_ns
            harbor_log = official_root / "harbor.stdout.log"
            source.revalidate_runtime_identity(source_export, exported_repo=source_repo)
            if credential_handle is None:
                credential_handle = self._stage_credential_handle(cell_root)
            with harbor_log.open("wb") as output:
                process = self._start_harbor_process(
                    command,
                    cell_root=cell_root,
                    credential_handle=credential_handle,
                    output=output,
                )
                try:
                    process.wait(timeout=self.harbor_wall_timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    _terminate_harbor_and_stop_cell(
                        process,
                        docker=self.docker,
                        cell_root=official_root,
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                    )
                    raise SweHarborCellError(
                        "Harbor task cell exceeded its wall bound"
                    ) from exc
                except BaseException:
                    _terminate_harbor_and_stop_cell(
                        process,
                        docker=self.docker,
                        cell_root=official_root,
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                    )
                    raise
        finally:
            if credential_handle is not None:
                credential_handle.unlink(missing_ok=True)
        _stop_scoped_compose_containers(
            self.docker,
            official_root,
            cleanup_grace_seconds=self.cleanup_grace_seconds,
        )
        source.revalidate_runtime_identity(source_export, exported_repo=source_repo)
        if process.returncode != 0:
            raise SweHarborCellError(
                f"Harbor task cell exited nonzero ({process.returncode})"
            )
        if snapshot_task_materials(task_dir) != before:
            raise SweHarborCellError("trusted task material moved during execution")
        if snapshot_task_materials(materialized_task_dir) != materialized_before:
            raise SweHarborCellError(
                "content-pinned task package moved during execution"
            )

        task_config = tomllib.loads((materialized_task_dir / "task.toml").read_text())
        expected_task_name = task_config.get("task", {}).get(
            "name", trusted_task_spec.task_id
        )
        trial = load_validated_trial_result(
            jobs_dir / "cell",
            expected_task_path=materialized_task_dir,
            expected_task_name=str(expected_task_name),
            expected_agent_name=(
                frozen_mini_agent.AGENT_IMPORT_PATH
                if self.agent_ref == "builtin:mini-swe-agent"
                else SHARED_HARBOR_AGENT_IMPORT_PATH
            ),
            expected_model=self.model.model_name,
            not_before_ns=not_before_ns,
            require_separate_verifier=True,
            artifact_expectation=expectation,
        )
        trial_dir = _trial_directory(jobs_dir / "cell")
        if pre_cleanup_trial_hook is not None:
            try:
                pre_cleanup_trial_hook(trial_dir)
            except Exception:
                _LOG.warning(
                    "SWE development trajectory harvest failed: task=%s",
                    trusted_task_spec.task_id,
                )
        tree_export = trial_dir / "artifacts" / "logs" / "artifacts" / TREE_PAYLOAD_NAME
        snapshot = freeze_exported_task_snapshot(
            task_id=trusted_task_spec.task_id,
            exported_tree_path=tree_export,
            base_bare=base_bare,
            base_commit=source_export.repo_head,
            destination=official_root / "measurement",
        )
        score = _reward(trial)
        candidate_runtime_failure = _candidate_runtime_failure_for_public_feedback(
            trial,
            reward=score,
            credential_literal=self.credential_value,
            task_id=trusted_task_spec.task_id,
        )
        usage, cost, cost_status = _validated_usage(trial)
        usage.update(
            {
                "provider": self.model.provider_name,
                "model": self.model.model_name,
                "model_revision": self.model.model_revision,
                "provider_reported_cost": (cost if cost_status == "reported" else None),
                "cost_source": ("provider" if cost_status == "reported" else "unknown"),
                "cost_status": cost_status,
            }
        )
        usage["calls"] = [
            {
                "provider": self.model.provider_name,
                "model": self.model.model_name,
                **dict(call),
            }
            for call in usage["calls"]
        ]
        artifact_refs = {
            "tree": snapshot.tree_path.relative_to(self.layout.root).as_posix(),
            "patch": snapshot.patch_path.relative_to(self.layout.root).as_posix(),
            "harbor_job": (jobs_dir / "cell").relative_to(self.layout.root).as_posix(),
        }
        trajectory_path = (
            trial_dir
            / "agent"
            / (
                "mini-swe-agent.trajectory.json"
                if self.agent_ref == "builtin:mini-swe-agent"
                else "trajectory.json"
            )
        )
        if trajectory_path.is_file():
            artifact_refs["trajectory"] = trajectory_path.relative_to(
                self.layout.root
            ).as_posix()
        agent_context_path = trial_dir / "agent" / "agent-context.json"
        if agent_context_path.is_file():
            artifact_refs["agent_context"] = agent_context_path.relative_to(
                self.layout.root
            ).as_posix()
        outcome_provenance: dict[str, object] = {
            "schema": "swe-harbor-task-outcome",
            "backend_digest": self.backend_digest,
            "harbor_controller": dict(self.harbor_controller or {}),
            "candidate_tree_sha": frozen_candidate.tree_sha,
            "task_static_evaluator_digest": (trusted_task_spec.static_evaluator_digest),
            **runtime_use,
            "task_materialization": task_materialization,
            "tree_digest": snapshot.tree_digest,
            "patch_digest": snapshot.patch_digest,
            "verifier_environment_mode": "separate",
            "inner_agent_ref": self.agent_ref,
            "agent_import_path": (
                frozen_mini_agent.AGENT_IMPORT_PATH
                if self.agent_ref == "builtin:mini-swe-agent"
                else SHARED_HARBOR_AGENT_IMPORT_PATH
            ),
            "native_entrypoint": (
                "python -m minisweagent.run.mini"
                if self.agent_ref == "builtin:mini-swe-agent"
                else None
            ),
            "timeout_budget": timeout_budget.as_dict(),
            "usage": usage,
        }
        if candidate_runtime_failure is not None:
            outcome_provenance[CANDIDATE_RUNTIME_FAILURE_KEY] = (
                candidate_runtime_failure
            )
        if pre_cleanup_trial_hook is not None:
            exception_info = trial.get("exception_info")
            outcome_provenance["development_agent_status"] = (
                "timeout"
                if isinstance(exception_info, Mapping)
                and exception_info.get("exception_type") == "AgentTimeoutError"
                else "completed"
            )
        return SweTaskMeasurement(
            task_id=trusted_task_spec.task_id,
            score=score,
            wall_seconds=time.monotonic() - started,
            cell_id=cell_id,
            artifact_refs=artifact_refs,
            provenance=outcome_provenance,
            input_tokens=int(usage["input_tokens"] or 0),
            output_tokens=int(usage["output_tokens"] or 0),
            token_status=str(usage["token_status"]),
            cost_usd=cost,
            cost_status=cost_status,
        )


__all__ = [
    "SWE_PROVIDER_BASE_URL_ENV",
    "HarborSweTaskCellRunner",
    "HarborTimeoutBudget",
    "SweHarborCellError",
    "harbor_controller_runtime_identity",
    "harbor_swe_backend_digest",
    "harbor_swe_cell_command",
    "harbor_timeout_budget",
]
