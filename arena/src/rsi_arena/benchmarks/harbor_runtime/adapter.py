"""Shared Harbor/Terminus-2 benchmark adapter.

Runs inside the evaluator domain.  Per cell:

1. ensure the candidate checkout has a synced venv (``uv sync --frozen``,
   once per checkout, lock-serialized across cell threads) — the console
   script ``harbor`` then runs the *evolved* code (uv installs the project
   editable from the checkout);
2. run one task: ``<checkout>/.venv/bin/harbor run -p <task_dir>
   --agent terminus-2 -m <model>`` in local Docker;
3. parse the trial ``result.json``: ``verifier_result.rewards`` is the
   container-verified outcome (no LLM judge), ``agent_result`` carries the
   real token/cost totals (LiteLLM accounting — attributable agent spend,
   unlike OM2W where the agent runs inside the evolved checkout's own
   config).

Metrics: ``reward`` (primary), ``harness_boot`` (constraint: the evolved
agent must still run at all), ``agent_cost_usd`` / ``duration_s`` /
``n_input_tokens`` / ``n_cache_tokens`` / ``n_output_tokens`` /
``agent_timeout_observed`` (auxiliary).  Harbor runs
the verifier after an agent deadline; a fully validated verifier score is
kept while the timeout remains explicit in that last metric.

Task materials live outside the target repo; solver-side code never
imports this package (contract 03 §5).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

import tomllib

from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    AGENT_IMPORT_PATH as ISOLATED_AGENT_IMPORT_PATH,
)
from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    HarborBridgeUnavailable,
    _provider_credential_delivery_env,
    harbor_agent_bridge_identity,
    harbor_cli_agent_args,
    load_hmac_key_file,
)
from rsi_arena.benchmarks.harbor_runtime.execution import (
    HarborExecutionError,
    ImmutableDockerImageIdentity,
    cleanup_scoped_compose_cell,
    remove_credential_handle,
    resolve_docker_executable,
    resolve_immutable_docker_image,
    revalidate_immutable_docker_image,
    scrub_credential_literal_files,
    stage_credential_handle,
    terminate_process_group_and_cleanup,
)
from rsi_arena.benchmarks.harbor_runtime.materials import (
    MaterialBoundaryError,
    assert_candidate_path_boundary,
    resolve_task_dir,
    snapshot_task_materials,
)
from rsi_arena.benchmarks.harbor_runtime.provenance import (
    ArtifactEnvelopeExpectation,
    CostAccountingProvenanceError,
    ResultProvenanceError,
    load_validated_trial_result,
    provider_calls_from_trial,
)
from rsi_arena.benchmarks.harbor_runtime.verifier_environment import (
    harbor_verifier_env_args,
    verifier_dependency_cache_env,
)
from rsi_arena.benchmarks.harbor_runtime.verifier_settings import VerifierSettings
from rsi_arena.benchmarks.task_records import (
    task_list_digest,
    to_task_refs,
)
from rsi_arena.contracts import (
    BenchIdentity,
    CellBudget,
    EnvProfile,
    MetricDirection,
    MetricRole,
    MetricSchema,
    MetricSpec,
    SampleOutcomeKind,
    TaskRef,
    canonical_digest,
)
from rsi_arena.evaluation import (
    BenchIsolationCapabilities,
    CellObservation,
    CellResult,
    TaskMaterialManifest,
    UnmeasuredCell,
)
from rsi_arena.inner_agent_runtime import parse_python_inner_agent_ref

log = logging.getLogger(__name__)


# Env vars the harbor subprocess actually needs; everything else from the
# evaluator's environment is withheld (plus explicit passthrough for API keys).
_BASE_ENV_VARS = ("PATH", "LANG", "DOCKER_HOST", "DOCKER_CONFIG")


class TaskTimeoutAuthority(Protocol):
    agent_deadline_seconds: float
    bridge_rpc_deadline_seconds: float
    verifier_deadline_seconds: float
    outer_deadline_seconds: float


@dataclass(frozen=True, slots=True)
class _StandardVerifierTimeoutAuthority:
    agent_deadline_seconds: float
    bridge_rpc_deadline_seconds: float
    verifier_deadline_seconds: float
    outer_deadline_seconds: float


def _standard_verifier_timeout_authority(
    task: Mapping[str, object],
    *,
    worker_timeout_seconds: float,
    outer_timeout_seconds: float,
    verifier_timeout_multiplier: float,
) -> _StandardVerifierTimeoutAuthority:
    agent = float(task["agent_timeout_sec"])
    verifier = float(task["verifier_timeout_sec"]) * float(verifier_timeout_multiplier)
    worker = float(worker_timeout_seconds)
    outer = float(outer_timeout_seconds)
    if any(
        not math.isfinite(value) or value <= 0
        for value in (agent, verifier, worker, outer)
    ):
        raise ValueError("standard verifier timeouts must be positive and finite")
    if worker >= agent:
        raise ValueError("bridge worker timeout must be below the agent deadline")
    if verifier >= outer:
        raise ValueError("verifier timeout must be below the outer deadline")
    return _StandardVerifierTimeoutAuthority(
        agent_deadline_seconds=agent,
        bridge_rpc_deadline_seconds=worker,
        verifier_deadline_seconds=verifier,
        outer_deadline_seconds=outer,
    )


@dataclass(frozen=True, slots=True)
class PhysicalVerifierRuntime:
    """Benchmark-owned ports required by the isolated Harbor path."""

    import_path: str
    source_commit: str
    tasks_tree: str
    task_ids: frozenset[str]
    resolve_timeout: Callable[..., TaskTimeoutAuthority]
    launch_kwargs: Callable[..., Mapping[str, object]]
    launch_binding: Callable[[list[str]], tuple[str, Mapping[str, object]]]
    admit: Callable[..., object]

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.import_path, self.source_commit, self.tasks_tree)
        ):
            raise ValueError("physical verifier identity must be non-empty")
        if not self.task_ids or any(not task_id for task_id in self.task_ids):
            raise ValueError("physical verifier task authority must be non-empty")
        for name in ("resolve_timeout", "launch_kwargs", "launch_binding", "admit"):
            if not callable(getattr(self, name)):
                raise TypeError(f"physical verifier {name} must be callable")


def _configured_docker_image(task_manifest: bytes) -> str:
    try:
        config = tomllib.loads(task_manifest.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise MaterialBoundaryError("task.toml is invalid") from exc
    environment = config.get("environment")
    image = (
        environment.get("docker_image") if isinstance(environment, Mapping) else None
    )
    if not isinstance(image, str) or not image:
        raise MaterialBoundaryError("task has no environment.docker_image")
    return image


def _rewrite_docker_image(
    task_manifest: bytes, identity: ImmutableDockerImageIdentity
) -> bytes:
    """Rewrite only the copied A-cell image reference to its frozen identity."""

    try:
        text = task_manifest.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaterialBoundaryError("task.toml is invalid") from exc
    rendered: list[str] = []
    in_environment = False
    replacements = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_environment = stripped == "[environment]"
        if in_environment and stripped.startswith("docker_image"):
            key, separator, _value = stripped.partition("=")
            if key.strip() == "docker_image" and separator:
                newline = "\n" if line.endswith("\n") else ""
                rendered.append(
                    f"docker_image = {json.dumps(identity.runtime_ref)}{newline}"
                )
                replacements += 1
                continue
        rendered.append(line)
    rewritten = "".join(rendered).encode("utf-8")
    if replacements != 1 or _configured_docker_image(rewritten) != identity.runtime_ref:
        raise MaterialBoundaryError("task docker image rewrite is ambiguous")
    return rewritten


_RSI_ARENA_SOURCE_ROOT = Path(__file__).resolve().parents[3]
_PHYSICAL_AGENT_RESERVED_KWARGS = frozenset(
    {
        "agent_ref",
        "agent_config_json",
        "benchmark_id",
        "candidate_checkout",
        "trusted_harbor_root",
        "codec",
        "source",
        "nonce",
        "candidate_tree_digest",
        "task_public_digest",
        "bridge_identity_digest",
        "envelope_key_file",
        "bwrap_path",
        "payload_name",
        "worker_timeout_seconds",
        "settled_cost_stop_usd",
        "credential_handle",
        "credential_env_name",
        "provider_name",
        "provider_base_url",
        "provider_base_url_env_name",
    }
)


def _git_output(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise MaterialBoundaryError(
            f"cannot establish Git identity for candidate/trusted repo: {repo}"
        ) from exc
    return result.stdout.strip()


def _ensure_private_envelope_key(artifacts_root: Path) -> tuple[Path, bytes]:
    trusted_dir = artifacts_root / ".trusted"
    try:
        trusted_dir.mkdir(mode=0o700, exist_ok=True)
        info = trusted_dir.lstat()
    except OSError as exc:
        raise OSError("cannot create trusted artifact-key directory") from exc
    if not info or not trusted_dir.is_dir() or trusted_dir.is_symlink():
        raise OSError("artifact-key directory is not a real directory")
    if info.st_mode & 0o077:
        raise OSError("artifact-key directory permissions must be 0700")

    key_path = trusted_dir / "artifact-envelope.key"
    if not key_path.exists() and not key_path.is_symlink():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(key_path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                raw = (secrets.token_hex(32) + "\n").encode("ascii")
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
    return key_path, load_hmac_key_file(key_path)


class HarborBench:
    def __init__(
        self,
        *,
        tasks: list[dict],
        tasks_root: Path,
        artifacts_root: Path,
        model: str,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
        temperature: float | None = None,
        agent_ref: str,
        agent_config: Mapping[str, object],
        agent_kwargs: dict | None = None,
        env_passthrough: tuple[str, ...] = (),
        docker_host: str = "",
        docker_path: str | Path = "docker",
        harbor_max_retries: int = 0,
        worker_timeout_seconds: float = 14_400,
        cleanup_grace_seconds: float = 30.0,
        uv_path: str = "uv",
        pass_reward_threshold: float = 1.0,
        physical_isolation: bool = False,
        trusted_harbor_root: Path | None = None,
        bridge_bwrap_path: str = "bwrap",
        artifact_codec: str | None = None,
        artifact_source: str | None = None,
        artifact_payload_name: str | None = None,
        benchmark_id: str,
        provider_name: str | None = None,
        provider_base_url: str | None = None,
        provider_base_url_env_name: str | None = None,
        credential_value: str | None = None,
        credential_env_name: str | None = None,
        verifier_env: Mapping[str, str] | None = None,
        verifier_settings: VerifierSettings,
        approved_image_identities: (
            Mapping[str, ImmutableDockerImageIdentity] | None
        ) = None,
        task_source_identity: Mapping[str, str] | None = None,
        physical_verifier: PhysicalVerifierRuntime | None = None,
    ):
        self._task_records = {t["task_id"]: t for t in tasks}
        self._task_refs = to_task_refs(tasks, benchmark_id=benchmark_id)
        # Both roots must be absolute: they are handed to a `harbor run`
        # subprocess whose cwd is the candidate checkout, so a relative
        # jobs-dir would silently land *inside the evolution target repo*
        # while run_cell reads the orchestrator-relative path and sees
        # nothing (missing_trial_result on every cell).
        self._tasks_root = Path(tasks_root).expanduser().resolve()
        self._owned_task_material_root: Path | None = None
        self._artifacts_root = Path(artifacts_root).expanduser().resolve()
        self._artifacts_root.mkdir(parents=True, exist_ok=True)
        self._model = model
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be a non-empty string")
        self._reasoning_effort = reasoning_effort
        if reasoning_enabled is not None and not isinstance(reasoning_enabled, bool):
            raise ValueError("reasoning_enabled must be boolean")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
        ):
            raise ValueError("temperature must be finite")
        self._reasoning_enabled = reasoning_enabled
        self._temperature = float(temperature) if temperature is not None else None
        if (
            agent_ref != "builtin:terminus-2"
            and parse_python_inner_agent_ref(agent_ref) is None
        ):
            raise ValueError("Harbor inner-agent ref is unsupported")
        try:
            copied_agent_config = json.loads(
                json.dumps(dict(agent_config), allow_nan=False)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("inner_agent.config must be finite JSON") from exc
        if not isinstance(copied_agent_config, dict):
            raise TypeError("inner_agent.config must be an object")
        self._agent_ref = agent_ref
        self._agent_config = MappingProxyType(copied_agent_config)
        self._agent_kwargs = dict(agent_kwargs or {})
        overlap = sorted(set(self._agent_config) & set(self._agent_kwargs))
        if self._agent_ref == "builtin:terminus-2" and overlap:
            raise ValueError(
                "inner_agent.config collides with package-owned agent_kwargs: "
                + ", ".join(overlap)
            )
        if self._agent_ref != "builtin:terminus-2" and not physical_isolation:
            raise ValueError("custom inner agents require physical isolation")
        self._verifier_env = (
            verifier_dependency_cache_env(verifier_env)
            if verifier_env is not None
            else MappingProxyType({})
        )
        if not isinstance(verifier_settings, VerifierSettings):
            raise TypeError("verifier_settings must be VerifierSettings")
        self._verifier_settings = verifier_settings
        if task_source_identity is not None and (
            not isinstance(task_source_identity, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in task_source_identity.items()
            )
        ):
            raise ValueError("task_source_identity must map strings to strings")
        self._task_source_identity = (
            MappingProxyType(dict(task_source_identity))
            if task_source_identity is not None
            else None
        )
        if physical_verifier is not None and not isinstance(
            physical_verifier, PhysicalVerifierRuntime
        ):
            raise TypeError("physical_verifier must be a PhysicalVerifierRuntime")
        self._physical_verifier = physical_verifier
        self._env_passthrough = tuple(env_passthrough)
        self._docker_host = str(docker_host).strip()
        self._docker_path = docker_path
        if isinstance(harbor_max_retries, bool) or harbor_max_retries != 0:
            raise ValueError("Harbor infrastructure failures must not auto-retry")
        self._harbor_max_retries = harbor_max_retries
        if (
            isinstance(worker_timeout_seconds, bool)
            or not isinstance(worker_timeout_seconds, (int, float))
            or not math.isfinite(float(worker_timeout_seconds))
            or float(worker_timeout_seconds) <= 0.0
        ):
            raise ValueError("worker_timeout_seconds must be positive and finite")
        self._worker_timeout_seconds = float(worker_timeout_seconds)
        if (
            isinstance(cleanup_grace_seconds, bool)
            or not isinstance(cleanup_grace_seconds, (int, float))
            or not math.isfinite(float(cleanup_grace_seconds))
            or float(cleanup_grace_seconds) <= 0.0
        ):
            raise ValueError("cleanup_grace_seconds must be positive and finite")
        self._cleanup_grace_seconds = float(cleanup_grace_seconds)
        self._benchmark_id = benchmark_id
        self._approved_docker: Path | None = None
        self._uv_path = uv_path
        self._pass_reward_threshold = float(pass_reward_threshold)
        self._physical_isolation = bool(physical_isolation)
        self._trusted_harbor_root: Path | None = None
        self._trusted_harbor_locator = trusted_harbor_root
        self._bridge_bwrap_path = bridge_bwrap_path
        self._bridge_identity_digest: str | None = None
        self._artifact_codec = artifact_codec
        self._artifact_source = artifact_source
        self._artifact_payload_name = artifact_payload_name
        self._artifact_replay = bool(
            self._physical_isolation and self._artifact_codec != "native_state"
        )
        route_values = (
            provider_name,
            provider_base_url,
            provider_base_url_env_name,
            credential_env_name,
        )
        self._provider_name: str | None = None
        self._provider_base_url: str | None = None
        self._provider_base_url_env_name: str | None = None
        self._credential_value: str | None = None
        self._credential_source_env_name: str | None = None
        self._credential_env_name: str | None = None
        if any(value is not None for value in route_values):
            if not self._physical_isolation or not all(
                isinstance(value, str) and value for value in route_values
            ):
                raise ValueError(
                    "physical provider route and credential handle must be complete"
                )
            self._provider_name = str(provider_name)
            self._provider_base_url = str(provider_base_url)
            self._provider_base_url_env_name = str(provider_base_url_env_name)
            source_env_name = str(credential_env_name)
            delivery_env_name = _provider_credential_delivery_env(
                self._provider_name, source_env_name
            )
            if (
                not source_env_name.isidentifier()
                or source_env_name.upper() != source_env_name
                or self._provider_base_url_env_name
                in {source_env_name, delivery_env_name}
                or (credential_value is not None and "\x00" in str(credential_value))
            ):
                raise ValueError(
                    "physical provider credential configuration is invalid"
                )
            self._credential_value = (
                str(credential_value) if credential_value is not None else None
            )
            self._credential_source_env_name = source_env_name
            self._credential_env_name = delivery_env_name
        self._envelope_key_path: Path | None = None
        self._envelope_hmac_key: bytes | None = None
        self._envelope_key_digest: str | None = None
        self._tasks_digest = task_list_digest(tasks)
        self._material_manifests: dict[str, TaskMaterialManifest] = {}
        for task_id in sorted(self._task_records):
            task_dir = resolve_task_dir(self._tasks_root, task_id)
            self._material_manifests[task_id] = snapshot_task_materials(task_dir)
        self._materials_digest = canonical_digest(
            [
                (task_id, manifest.evaluator_digest)
                for task_id, manifest in sorted(self._material_manifests.items())
            ]
        )
        self._tasks_pin = "sha256:" + self._materials_digest
        self._venv_locks: dict[str, threading.Lock] = {}
        self._venv_locks_guard = threading.Lock()
        if (
            self._credential_source_env_name in self._env_passthrough
            or self._credential_env_name in self._env_passthrough
            or self._provider_base_url_env_name in self._env_passthrough
        ):
            raise ValueError(
                "physical provider route must use the handle/config bridge, not env_passthrough"
            )
        if self._physical_isolation:
            overlap = sorted(_PHYSICAL_AGENT_RESERVED_KWARGS & set(self._agent_kwargs))
            if overlap:
                raise ValueError(
                    "agent_kwargs collide with trusted bridge fields: "
                    + ", ".join(overlap)
                )
        approved_images: dict[str, ImmutableDockerImageIdentity] | None = None
        if approved_image_identities is not None:
            approved_images = dict(approved_image_identities)
            if (
                not self._physical_isolation
                or not approved_images
                or any(
                    not isinstance(key, str)
                    or not key
                    or not isinstance(value, ImmutableDockerImageIdentity)
                    or value.configured_ref != key
                    for key, value in approved_images.items()
                )
            ):
                raise ValueError("approved_image_identities is invalid")
        self._approved_image_identities = (
            MappingProxyType(approved_images) if approved_images is not None else None
        )
        if self._physical_isolation:
            if (
                self._physical_verifier is None
                and self._artifact_codec != "integer_answer"
            ):
                raise ValueError(
                    "physical isolation requires a verifier runtime unless the "
                    "signed artifact is an integer answer"
                )
            if self._physical_verifier is not None and not set(
                self._task_records
            ).issubset(self._physical_verifier.task_ids):
                raise ValueError("offline verifier authority omits an A task")
            self._configure_physical_isolation(trusted_harbor_root)

    def _configure_physical_isolation(self, trusted_harbor_root: Path | None) -> None:
        if trusted_harbor_root is None:
            raise ValueError(
                "physical_isolation requires an explicit trusted_harbor_root"
            )
        if not isinstance(self._artifact_codec, str) or not self._artifact_codec:
            raise ValueError("physical_isolation requires artifact_codec")
        if self._artifact_codec not in {
            "native_state",
            "integer_answer",
            "text_answer",
            "tree_snapshot",
        }:
            raise ValueError(
                f"unsupported physical artifact codec: {self._artifact_codec!r}"
            )
        if self._artifact_codec == "native_state":
            # The bridge signature stays uniform, but the native-state codec
            # neither reads this path nor publishes a payload.
            self._artifact_source = self._artifact_source or "/"
            self._artifact_payload_name = self._artifact_payload_name or "native-state"
        elif not all(
            isinstance(value, str) and value
            for value in (self._artifact_source, self._artifact_payload_name)
        ):
            raise ValueError(
                "artifact-replay isolation requires artifact_source and "
                "artifact_payload_name"
            )

        for task_id in sorted(self._task_records):
            task_dir = resolve_task_dir(self._tasks_root, task_id)
            try:
                task_config = tomllib.loads((task_dir / "task.toml").read_text())
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ValueError(f"invalid task.toml for {task_id}") from exc
            if (
                self._artifact_replay
                and task_config.get("verifier", {}).get("environment_mode")
                != "separate"
            ):
                raise ValueError(
                    "physical bridge refuses a task without "
                    f"verifier.environment_mode='separate': {task_id}"
                )
            if self._artifact_replay and task_config.get("artifacts") != [
                "/logs/artifacts"
            ]:
                raise ValueError(
                    "physical bridge requires the single typed convention "
                    f"artifact contract for task {task_id}"
                )

        self._bridge_identity_digest = harbor_agent_bridge_identity()
        self._envelope_key_path, self._envelope_hmac_key = _ensure_private_envelope_key(
            self._artifacts_root
        )
        # This key authenticates every historical candidate artifact.  Its
        # loss or rotation must move the frozen evaluator identity; otherwise
        # a resume could silently forget an earlier B trajectory.
        self._envelope_key_digest = (
            "sha256:" + hashlib.sha256(self._envelope_hmac_key).hexdigest()
        )

    def _resolve_execution_runtime(self) -> None:
        if self._credential_source_env_name and self._credential_value is None:
            credential = os.environ.get(self._credential_source_env_name)
            if credential is None:
                raise OSError(
                    "provider credential environment is unavailable: "
                    + self._credential_source_env_name
                )
            self._credential_value = credential
        if self._physical_isolation and self._trusted_harbor_root is None:
            assert self._trusted_harbor_locator is not None
            self._trusted_harbor_root = (
                Path(self._trusted_harbor_locator).expanduser().resolve(strict=True)
            )
        if self._physical_isolation and self._approved_docker is None:
            try:
                self._approved_docker = resolve_docker_executable(self._docker_path)
            except HarborExecutionError as exc:
                raise OSError("approved Docker executable is invalid") from exc

    def _execution_identity_payload(self) -> dict:
        return {
            "inner_agent": {
                "ref": self._agent_ref,
                "config": dict(self._agent_config),
            },
            "model": self._model,
            "reasoning_effort": self._reasoning_effort,
            "reasoning_enabled": self._reasoning_enabled,
            "temperature": self._temperature,
            "docker_host": self._docker_host,
            "harbor_max_retries": self._harbor_max_retries,
            "worker_timeout_seconds": self._worker_timeout_seconds,
            "agent_kwargs": self._agent_kwargs,
            "physical_isolation": self._physical_isolation,
            "bridge_identity_digest": self._bridge_identity_digest,
            "artifact_codec": self._artifact_codec,
            "provider_name": self._provider_name,
            "provider_base_url": self._provider_base_url,
            "provider_base_url_env_name": self._provider_base_url_env_name,
            "credential_source_env_name": self._credential_source_env_name,
            "credential_delivery_env_name": self._credential_env_name,
            "verifier_env": dict(self._verifier_env),
            "verifier_settings": self._verifier_settings.as_dict(),
            "offline_verifier": (
                {
                    "source_commit": self._physical_verifier.source_commit,
                    "tasks_tree": self._physical_verifier.tasks_tree,
                    "custom_verifier": self._physical_verifier.import_path,
                }
                if self._physical_verifier is not None
                else None
            ),
            **(
                {"task_source_identity": dict(self._task_source_identity)}
                if self._task_source_identity is not None
                else {}
            ),
            "approved_image_identities": (
                {
                    key: value.as_dict()
                    for key, value in sorted(self._approved_image_identities.items())
                }
                if self._approved_image_identities is not None
                else None
            ),
            "artifact_replay": self._artifact_replay,
            "artifact_source": self._artifact_source,
            "artifact_payload_name": self._artifact_payload_name,
            # Fingerprint only: the secret itself never enters manifests or
            # prompts.  None in guarded mode keeps the boundary explicit.
            "artifact_envelope_key_digest": self._envelope_key_digest,
        }

    # -- identity / schema ---------------------------------------------------

    def identity(self) -> BenchIdentity:
        raise NotImplementedError(
            "a concrete Harbor benchmark must define its evaluator identity"
        )

    def metric_schema(self) -> MetricSchema:
        return MetricSchema(
            metrics=(
                MetricSpec(
                    name="reward",
                    direction=MetricDirection.MAXIMIZE.value,
                    role=MetricRole.PRIMARY.value,
                    unit="ratio",
                ),
                MetricSpec(
                    name="harness_boot",
                    direction=MetricDirection.MAXIMIZE.value,
                    role=MetricRole.CONSTRAINT.value,
                    unit="ratio",
                ),
                MetricSpec(
                    name="agent_cost_usd",
                    direction=MetricDirection.MINIMIZE.value,
                    role=MetricRole.AUXILIARY.value,
                    unit="usd",
                ),
                MetricSpec(
                    name="duration_s",
                    direction=MetricDirection.MINIMIZE.value,
                    role=MetricRole.AUXILIARY.value,
                    unit="seconds",
                ),
                MetricSpec(
                    name="n_input_tokens",
                    direction=MetricDirection.MINIMIZE.value,
                    role=MetricRole.AUXILIARY.value,
                    unit="count",
                ),
                MetricSpec(
                    name="n_cache_tokens",
                    direction=MetricDirection.MINIMIZE.value,
                    role=MetricRole.AUXILIARY.value,
                    unit="count",
                ),
                MetricSpec(
                    name="n_output_tokens",
                    direction=MetricDirection.MINIMIZE.value,
                    role=MetricRole.AUXILIARY.value,
                    unit="count",
                ),
                MetricSpec(
                    name="agent_timeout_observed",
                    direction=MetricDirection.MINIMIZE.value,
                    role=MetricRole.AUXILIARY.value,
                    unit="ratio",
                ),
            )
        )

    def list_tasks(self) -> list[TaskRef]:
        return list(self._task_refs)

    def environment_profile(self) -> str:
        # containers are stable across runs; the agent LLM is stochastic
        return EnvProfile.OFFLINE_STOCHASTIC.value

    def isolation_capabilities(self) -> BenchIsolationCapabilities:
        # Guarded mode retains the historical host-import path and therefore
        # makes no strong claim.  Both physical modes use the probed bwrap
        # worker and a fixed trusted Harbor controller.  Only artifact replay
        # additionally requires a fresh separate verifier; native-state
        # intentionally keeps the task's official in-place verifier.
        return BenchIsolationCapabilities(
            task_material_manifest=True,
            result_freshness_validation=True,
            result_consistency_validation=True,
            manifest_redaction_canary=True,
            development_material_hook=True,
            run_private_home=True,
            fresh_candidate_worker=self._physical_isolation,
            candidate_secret_isolation=False,
            candidate_host_egress_isolation=False,
            trusted_verifier_process=self._artifact_replay,
            hermetic_candidate_fs=self._physical_isolation,
            backend_probe_digest=self._bridge_identity_digest,
        )

    def evaluator_material_manifest(self, task: TaskRef) -> TaskMaterialManifest:
        """Full manifest; evaluator-side consumers only."""
        try:
            return self._material_manifests[task.task_id]
        except KeyError as exc:
            raise KeyError(f"unknown task {task.task_id!r}") from exc

    def candidate_material_manifest(self, task: TaskRef) -> dict:
        """Redacted A-dev identity safe to serialize to a coding harness."""
        return self.evaluator_material_manifest(task).candidate_view()

    def _nonphysical_agent_cli_args(self) -> tuple[str, ...]:
        args = ["--agent", "terminus-2"]
        for key, value in self._agent_config.items():
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            args += ["--agent-kwarg", f"{key}={encoded}"]
        for key, value in self._agent_kwargs.items():
            args += ["--agent-kwarg", f"{key}={value}"]
        return tuple(args)

    @staticmethod
    def _candidate_git_identity(checkout: Path, agent_ref: str) -> tuple[str, str]:
        checkout = Path(checkout).resolve()
        top_level = Path(
            _git_output(checkout, "rev-parse", "--show-toplevel")
        ).resolve()
        if top_level != checkout:
            raise MaterialBoundaryError(
                "physical evaluation requires the candidate path itself to be "
                "a standalone Git worktree root"
            )
        status = _git_output(checkout, "status", "--porcelain", "--untracked-files=all")
        if status:
            raise MaterialBoundaryError(
                "physical evaluation requires a clean candidate commit checkout"
            )
        parsed = parse_python_inner_agent_ref(agent_ref)
        agent_paths = (
            (Path("src/harbor/agents/terminus_2"),)
            if agent_ref == "builtin:terminus-2"
            else (
                Path("src") / parsed.top_level,
                Path("src") / f"{parsed.top_level}.py",
                Path(parsed.top_level),
                Path(f"{parsed.top_level}.py"),
            )
            if parsed is not None
            else ()
        )
        if not agent_paths:
            raise MaterialBoundaryError("candidate inner-agent ref is invalid")
        agent_path_args = tuple(str(path) for path in agent_paths)
        ignored = _git_output(
            checkout,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--",
            *agent_path_args,
        )
        if ignored:
            raise MaterialBoundaryError(
                "physical evaluation refuses ignored files in the candidate agent"
            )
        tracked = _git_output(checkout, "ls-files", "-v", "-z", "--", *agent_path_args)
        rows = [row for row in tracked.split("\0") if row]
        if (agent_ref == "builtin:terminus-2" and not rows) or any(
            not row.startswith("H ") for row in rows
        ):
            raise MaterialBoundaryError(
                "physical evaluation refuses assume-unchanged/skip-worktree "
                "candidate files"
            )
        commit_sha = _git_output(checkout, "rev-parse", "HEAD")
        tree_sha = _git_output(checkout, "rev-parse", "HEAD^{tree}")
        if len(commit_sha) != 40 or len(tree_sha) != 40:
            raise MaterialBoundaryError("candidate Git identity is malformed")
        return commit_sha, f"git-tree:{tree_sha}"

    @staticmethod
    def _write_cell_metadata(path: Path, payload: dict) -> None:
        data = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- execution -----------------------------------------------------------

    def run_cell(
        self,
        checkout_path: Path,
        task: TaskRef,
        seed: int,
        budget: CellBudget,
        *,
        timeout_authority: TaskTimeoutAuthority | None = None,
        pre_cleanup_trial_hook: Callable[[Path], None] | None = None,
    ) -> CellObservation:
        self._resolve_execution_runtime()
        out_dir = (
            self._artifacts_root
            / task.task_id
            / f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        approved_image: ImmutableDockerImageIdentity | None = None
        approved_task_root: Path | None = None
        execution_task_dir: Path | None = None
        if self._physical_isolation:
            try:
                task_dir = resolve_task_dir(self._tasks_root, task.task_id)
                configured_ref = _configured_docker_image(
                    (task_dir / "task.toml").read_bytes()
                )
                if self._approved_image_identities is None:
                    approved_image = resolve_immutable_docker_image(
                        self._approved_docker, configured_ref
                    )
                else:
                    approved_image = self._approved_image_identities.get(configured_ref)
                    if approved_image is None:
                        raise HarborExecutionError(
                            "task Docker image lacks a frozen runtime identity"
                        )
                revalidate_immutable_docker_image(self._approved_docker, approved_image)
                approved_task_root = Path(
                    tempfile.mkdtemp(prefix="rsi-arena-harbor-task-")
                )
                execution_task_dir = approved_task_root / "task"
                shutil.copytree(task_dir, execution_task_dir, symlinks=True)
                runtime_manifest = execution_task_dir / "task.toml"
                runtime_manifest.write_bytes(
                    _rewrite_docker_image(runtime_manifest.read_bytes(), approved_image)
                )
                if (execution_task_dir / "instruction.md").read_bytes() != (
                    task_dir / "instruction.md"
                ).read_bytes() or _configured_docker_image(
                    runtime_manifest.read_bytes()
                ) != approved_image.runtime_ref:
                    raise MaterialBoundaryError("frozen A task materialization drifted")
            except (HarborExecutionError, MaterialBoundaryError, OSError) as exc:
                if approved_task_root is not None:
                    shutil.rmtree(approved_task_root, ignore_errors=True)
                return self._unmeasured(
                    "approved_image_identity_failed",
                    start,
                    diagnostics={"exception_type": type(exc).__name__},
                )

        result: CellObservation | None = None
        primary: BaseException | None = None
        try:
            result = self._run_cell(
                checkout_path,
                task,
                seed,
                budget,
                timeout_authority=timeout_authority,
                pre_cleanup_trial_hook=pre_cleanup_trial_hook,
                out_dir=out_dir,
                start=start,
                execution_task_dir=execution_task_dir,
                approved_image=approved_image,
            )
        except BaseException as exc:  # noqa: BLE001 - preserve primary for cleanup
            primary = exc

        boundary_errors: list[Exception] = []
        runtime_root = out_dir / ".runtime"
        if approved_image is not None:
            try:
                revalidate_immutable_docker_image(self._approved_docker, approved_image)
            except (HarborExecutionError, OSError) as exc:
                boundary_errors.append(exc)
        if runtime_root.exists():
            try:
                for handle in runtime_root.glob("provider-credential-*"):
                    remove_credential_handle(handle)
                if self._credential_value is not None:
                    scrub_credential_literal_files(
                        runtime_root,
                        tuple(runtime_root.rglob("*")),
                        self._credential_value,
                    )
            except (HarborExecutionError, OSError) as exc:
                boundary_errors.append(exc)
            try:
                shutil.rmtree(runtime_root)
            except OSError as exc:
                boundary_errors.append(exc)
        if approved_task_root is not None:
            try:
                shutil.rmtree(approved_task_root)
            except OSError as exc:
                boundary_errors.append(exc)

        if primary is not None:
            if not isinstance(primary, Exception):
                raise primary
            if boundary_errors:
                return self._unmeasured(
                    "credential_or_image_cleanup_failed",
                    start,
                    diagnostics={
                        "exception_type": type(boundary_errors[0]).__name__,
                        "primary_exception_type": type(primary).__name__,
                    },
                )
            raise primary
        if boundary_errors:
            return self._unmeasured(
                "credential_or_image_cleanup_failed",
                start,
                diagnostics={"exception_type": type(boundary_errors[0]).__name__},
            )
        if result is None:
            return self._unmeasured("missing_cell_result", start)
        return result

    def _run_cell(
        self,
        checkout_path: Path,
        task: TaskRef,
        seed: int,
        budget: CellBudget,
        *,
        timeout_authority: TaskTimeoutAuthority | None,
        pre_cleanup_trial_hook: Callable[[Path], None] | None,
        out_dir: Path,
        start: float,
        execution_task_dir: Path | None,
        approved_image: ImmutableDockerImageIdentity | None,
    ) -> CellObservation:
        del seed  # harbor/terminus has no seed control; replicates measure noise
        checkout = Path(checkout_path).expanduser().resolve()
        if self._physical_isolation and timeout_authority is None:
            try:
                timeout_authority = (
                    _standard_verifier_timeout_authority(
                        self._task_records[task.task_id],
                        worker_timeout_seconds=self._worker_timeout_seconds,
                        outer_timeout_seconds=budget.wall_seconds,
                        verifier_timeout_multiplier=(
                            self._verifier_settings.timeout_multiplier
                        ),
                    )
                    if self._physical_verifier is None
                    else self._physical_verifier.resolve_timeout(
                        self._task_records[task.task_id],
                        worker_timeout_seconds=self._worker_timeout_seconds,
                        outer_timeout_seconds=budget.wall_seconds,
                        cleanup_grace_seconds=self._cleanup_grace_seconds,
                        verifier_timeout_multiplier=(
                            self._verifier_settings.timeout_multiplier
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                return self._unmeasured(
                    "harbor_timeout_budget_invalid",
                    start,
                    diagnostics={"exception_type": type(exc).__name__},
                )
        worker_deadline_seconds = (
            timeout_authority.bridge_rpc_deadline_seconds
            if timeout_authority is not None
            else self._worker_timeout_seconds
        )
        outer_deadline_seconds = (
            timeout_authority.outer_deadline_seconds
            if timeout_authority is not None
            else budget.wall_seconds
        )

        try:
            task_dir = resolve_task_dir(self._tasks_root, task.task_id)
            assert_candidate_path_boundary(
                checkout,
                tasks_root=self._tasks_root,
                artifacts_root=self._artifacts_root,
            )
            expected_materials = self.evaluator_material_manifest(task)
            if snapshot_task_materials(task_dir) != expected_materials:
                raise MaterialBoundaryError("task package moved after adapter creation")
        except (MaterialBoundaryError, OSError) as exc:
            return self._unmeasured(
                "material_boundary_violation",
                start,
                diagnostics={
                    "exception_type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )

        candidate_commit_sha: str | None = None
        candidate_tree_digest: str | None = None
        nonce: str | None = None
        artifact_expectation: ArtifactEnvelopeExpectation | None = None
        try:
            if self._physical_isolation:
                candidate_commit_sha, candidate_tree_digest = (
                    self._candidate_git_identity(checkout, self._agent_ref)
                )
            else:
                self._ensure_venv(checkout)
        except Exception as exc:
            log.exception("candidate environment setup failed for %s", checkout)
            return self._unmeasured(
                "environment_setup_failed",
                start,
                diagnostics={
                    "exception_type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )

        runtime_root = out_dir / ".runtime"
        (runtime_root / "home").mkdir(parents=True, mode=0o700)
        (runtime_root / "tmp").mkdir(parents=True, mode=0o700)
        if execution_task_dir is None:
            execution_task_dir = task_dir
        credential_handle: Path | None = None
        if self._credential_value is not None:
            try:
                credential_handle = stage_credential_handle(
                    runtime_root, self._credential_value
                )
            except (HarborExecutionError, OSError) as exc:
                return self._unmeasured(
                    "credential_handle_setup_failed",
                    start,
                    diagnostics={"exception_type": type(exc).__name__},
                )

        cell_id = "harbor-cell-" + uuid.uuid4().hex
        expected_offline_import: str | None = None
        expected_offline_kwargs: Mapping[str, object] | None = None
        harbor_bin = (
            self._trusted_harbor_root / ".venv" / "bin" / "harbor"
            if self._physical_isolation and self._trusted_harbor_root is not None
            else checkout / ".venv" / "bin" / "harbor"
        )
        cmd = [
            str(harbor_bin),
            "run",
            "-p",
            str(execution_task_dir),
            "-m",
            self._model,
            "--jobs-dir",
            str(out_dir),
            "--job-name",
            "cell",
            "-n",
            "1",
            "--max-retries",
            str(self._harbor_max_retries),
        ]
        cmd += [
            "--verifier-timeout-multiplier",
            str(self._verifier_settings.timeout_multiplier),
        ]
        cmd += ["-q", "-y"]
        if self._physical_isolation:
            assert candidate_commit_sha is not None
            assert candidate_tree_digest is not None
            assert self._trusted_harbor_root is not None
            assert self._bridge_identity_digest is not None
            assert self._envelope_key_path is not None
            assert self._envelope_hmac_key is not None
            assert self._artifact_codec is not None
            assert self._artifact_source is not None
            assert self._artifact_payload_name is not None
            assert self._approved_docker is not None
            assert approved_image is not None
            assert timeout_authority is not None
            nonce = f"cell-{uuid.uuid4().hex}"
            cmd += [
                "--agent-timeout-multiplier",
                (
                    "1.0"
                    if timeout_authority is not None
                    else str(
                        self._worker_timeout_seconds
                        / float(task.meta["agent_timeout_sec"])
                    )
                ),
            ]
            try:
                bridge_args = harbor_cli_agent_args(
                    agent_ref=self._agent_ref,
                    agent_config=self._agent_config,
                    benchmark_id=self._benchmark_id,
                    candidate_checkout=checkout,
                    trusted_harbor_root=self._trusted_harbor_root,
                    codec=self._artifact_codec,
                    source=self._artifact_source,
                    nonce=nonce,
                    candidate_tree_digest=candidate_tree_digest,
                    task_public_digest=expected_materials.public_digest,
                    bridge_identity_digest=self._bridge_identity_digest,
                    envelope_key_file=self._envelope_key_path,
                    worker_timeout_seconds=worker_deadline_seconds,
                    cleanup_grace_seconds=self._cleanup_grace_seconds,
                    bwrap_path=self._bridge_bwrap_path,
                    payload_name=self._artifact_payload_name,
                    credential_handle=credential_handle,
                    credential_env_name=self._credential_env_name,
                    provider_name=self._provider_name,
                    provider_base_url=self._provider_base_url,
                    provider_base_url_env_name=self._provider_base_url_env_name,
                    reasoning_effort=self._reasoning_effort,
                    reasoning_enabled=self._reasoning_enabled,
                    temperature=self._temperature,
                    # CellBudget.max_cost_usd is a post-hoc measurement
                    # acceptance threshold. The broker receives the same value
                    # only as an early stop after already-settled provider cost;
                    # it is not a hard spend cap for the in-flight request.
                    settled_cost_stop_usd=budget.max_cost_usd,
                )
            except (HarborBridgeUnavailable, OSError, ValueError) as exc:
                remove_credential_handle(credential_handle)
                return self._unmeasured(
                    "physical_bridge_setup_failed",
                    start,
                    diagnostics={"exception_type": type(exc).__name__},
                )
            cmd += list(bridge_args)
            if self._physical_verifier is not None:
                try:
                    verifier_kwargs = self._physical_verifier.launch_kwargs(
                        task_id=task.task_id,
                        run_id=cell_id,
                        expected_agent_image_id=approved_image.image_id,
                        tests_path=task_dir / "tests",
                        docker=self._approved_docker,
                        docker_host=(
                            self._docker_host or os.environ.get("DOCKER_HOST", "")
                        ),
                        verifier_timeout_seconds=(
                            timeout_authority.verifier_deadline_seconds
                        ),
                    )
                    cmd += ["--verifier", self._physical_verifier.import_path]
                    for name, value in sorted(verifier_kwargs.items()):
                        cmd += ["--verifier-kwarg", f"{name}={value}"]
                    (
                        expected_offline_import,
                        expected_offline_kwargs,
                    ) = self._physical_verifier.launch_binding(cmd)
                except (OSError, RuntimeError, ValueError) as exc:
                    remove_credential_handle(credential_handle)
                    return self._unmeasured(
                        "offline_verifier_binding_failed",
                        start,
                        diagnostics={"exception_type": type(exc).__name__},
                    )
            for name in self._env_passthrough:
                cmd += ["--agent-env", f"{name}=${{{name}}}"]
            if self._artifact_replay:
                artifact_expectation = ArtifactEnvelopeExpectation(
                    nonce=nonce,
                    codec=self._artifact_codec,
                    payload_name=self._artifact_payload_name,
                    candidate_tree_digest=candidate_tree_digest,
                    task_public_digest=expected_materials.public_digest,
                    bridge_identity_digest=self._bridge_identity_digest,
                    hmac_key=self._envelope_hmac_key,
                )
            try:
                self._write_cell_metadata(
                    out_dir / ".rsi-arena-cell.json",
                    {
                        "schema": "rsi-arena-physical-cell",
                        "candidate_commit_sha": candidate_commit_sha,
                        "candidate_tree_digest": candidate_tree_digest,
                        "nonce": nonce,
                        "codec": self._artifact_codec,
                        "payload_name": self._artifact_payload_name,
                        "task_public_digest": expected_materials.public_digest,
                        "bridge_identity_digest": self._bridge_identity_digest,
                        "offline_verifier": (
                            {
                                "source_commit": self._physical_verifier.source_commit,
                                "tasks_tree": self._physical_verifier.tasks_tree,
                                "custom_verifier": self._physical_verifier.import_path,
                                "run_id": cell_id,
                            }
                            if self._physical_verifier is not None
                            else None
                        ),
                        "resolved_timeouts": (
                            {
                                "agent_deadline_seconds": (
                                    timeout_authority.agent_deadline_seconds
                                ),
                                "verifier_deadline_seconds": (
                                    timeout_authority.verifier_deadline_seconds
                                ),
                                "outer_deadline_seconds": (
                                    timeout_authority.outer_deadline_seconds
                                ),
                            }
                            if timeout_authority is not None
                            else None
                        ),
                        "resolved_verifier_settings": (
                            self._verifier_settings.resolved_task_settings(task.task_id)
                        ),
                    },
                )
            except OSError as exc:
                remove_credential_handle(credential_handle)
                return self._unmeasured(
                    "physical_metadata_setup_failed",
                    start,
                    diagnostics={"exception_type": type(exc).__name__},
                )
        else:
            cmd += list(self._nonphysical_agent_cli_args())
        if self._physical_isolation:
            for key, value in self._agent_kwargs.items():
                cmd += ["--agent-kwarg", f"{key}={value}"]
        resolved_settings = self._verifier_settings.resolved_task_settings(task.task_id)
        task_adaptation = resolved_settings["task_adaptation"]
        task_env = (
            task_adaptation["verifier_env"]
            if isinstance(task_adaptation, Mapping)
            else None
        )
        if self._verifier_env or task_env:
            cmd += harbor_verifier_env_args(self._verifier_env, task_env=task_env)

        # Compare artifact mtimes to a marker written by the same filesystem.
        # On virtualized hosts CLOCK_REALTIME can lead the filesystem clock by
        # a few milliseconds, making a time.time_ns() threshold reject output
        # that was genuinely written afterwards.
        start_marker = out_dir / ".provenance-start"
        try:
            start_marker.write_text(uuid.uuid4().hex)
            not_before_ns = start_marker.stat().st_mtime_ns
        except OSError as exc:
            return self._unmeasured(
                "physical_marker_setup_failed",
                start,
                diagnostics={"exception_type": type(exc).__name__},
            )
        try:
            with (out_dir / "harbor.log").open("wb") as harbor_log:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(runtime_root if self._physical_isolation else checkout),
                    env=self._subprocess_env(
                        runtime_root=runtime_root,
                        trusted_import=self._physical_isolation,
                    ),
                    stdout=harbor_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # own process group: timeout kills the tree
                )
        except (OSError, ValueError) as exc:
            remove_credential_handle(credential_handle)
            return self._unmeasured(
                "harbor_launch_failed",
                start,
                diagnostics={"exception_type": type(exc).__name__},
            )
        timed_out = False
        interrupted: BaseException | None = None
        cleanup_error: Exception | None = None
        try:
            try:
                proc.wait(timeout=outer_deadline_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
            except BaseException as exc:  # noqa: BLE001 - cleanup must still run
                interrupted = exc
            if not timed_out and proc.returncode is not None:
                _invoke_pre_cleanup_trial_hook(out_dir / "cell", pre_cleanup_trial_hook)
            try:
                docker = self._approved_docker or resolve_docker_executable(
                    self._docker_path
                )
                if proc.returncode is None:
                    terminate_process_group_and_cleanup(
                        proc,
                        docker=docker,
                        cell_root=out_dir / "cell",
                        cleanup_grace_seconds=self._cleanup_grace_seconds,
                        docker_host=self._docker_host,
                    )
                else:
                    cleanup_scoped_compose_cell(
                        docker,
                        out_dir / "cell",
                        cleanup_grace_seconds=self._cleanup_grace_seconds,
                        docker_host=self._docker_host,
                    )
            except Exception as exc:  # noqa: BLE001 - cleanup fault is secondary
                cleanup_error = exc
                if proc.returncode is None:
                    _kill_process_group(proc)
        finally:
            remove_credential_handle(credential_handle)
        if timed_out and proc.returncode is not None:
            _invoke_pre_cleanup_trial_hook(
                out_dir / "cell",
                pre_cleanup_trial_hook,
                allow_partial=True,
            )
        scrub_error: Exception | None = None
        if self._credential_value is not None:
            try:
                scrub_credential_literal_files(
                    out_dir,
                    tuple(out_dir.rglob("*")),
                    self._credential_value,
                )
            except (HarborExecutionError, OSError) as exc:
                scrub_error = exc
        if interrupted is not None:
            raise interrupted
        if scrub_error is not None:
            return self._unmeasured(
                "credential_output_boundary_failed",
                start,
                diagnostics={"exception_type": type(scrub_error).__name__},
            )
        if cleanup_error is not None:
            return self._unmeasured(
                "harbor_cleanup_failed",
                start,
                diagnostics={"exception_type": type(cleanup_error).__name__},
            )
        if timed_out:
            # Harbor wraps candidate execution, providers, Docker and the
            # verifier in one opaque process.  Its outer timeout is therefore
            # not sufficient evidence of a candidate-controlled failure.
            return self._unmeasured(
                "opaque_bench_timeout",
                start,
                diagnostics={"harbor_exit": proc.returncode},
            )

        try:
            if snapshot_task_materials(task_dir) != expected_materials:
                raise MaterialBoundaryError("task package changed during candidate run")
        except (MaterialBoundaryError, OSError) as exc:
            return self._unmeasured(
                "task_material_drift",
                start,
                diagnostics={
                    "exception_type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )

        native_state_authority = self._artifact_codec == "native_state"
        if proc.returncode < 0 or (proc.returncode != 0 and not native_state_authority):
            return self._unmeasured(
                "harbor_nonzero_exit",
                start,
                diagnostics={"harbor_exit": proc.returncode},
            )

        try:
            task_config = tomllib.loads((task_dir / "task.toml").read_text())
            expected_task_name = task_config.get("task", {}).get("name", task.task_id)
            trial = load_validated_trial_result(
                out_dir / "cell",
                expected_task_path=execution_task_dir,
                expected_task_name=expected_task_name,
                expected_agent_name=(
                    ISOLATED_AGENT_IMPORT_PATH
                    if self._physical_isolation
                    else "terminus-2"
                ),
                expected_model=self._model,
                not_before_ns=not_before_ns,
                require_separate_verifier=self._artifact_replay,
                artifact_expectation=artifact_expectation,
                native_state_verifier_only_authority=native_state_authority,
            )
        except (OSError, tomllib.TOMLDecodeError, ResultProvenanceError) as exc:
            log.error(
                "untrusted trial result for task %s (harbor exit %s): %s; see %s",
                task.task_id,
                proc.returncode,
                exc,
                out_dir / "harbor.log",
            )
            diagnostics = {
                "harbor_exit": proc.returncode,
                "exception_type": type(exc).__name__,
                "message": str(exc)[:500],
            }
            cost_status = "missing"
            if isinstance(exc, CostAccountingProvenanceError):
                diagnostics["n_output_tokens"] = float(exc.tokens_out)
                cost_status = "unverified"
            return self._unmeasured(
                (
                    "harbor_nonzero_exit"
                    if proc.returncode != 0
                    else "untrusted_trial_result"
                ),
                start,
                diagnostics=diagnostics,
                cost_status=cost_status,
            )

        provider_calls = provider_calls_from_trial(trial)
        tokens_in, cache_tokens, tokens_out, cost_usd, cost_status = _extract_usage(
            trial,
            allow_step_results=not native_state_authority,
        )

        if self._physical_verifier is not None:
            if expected_offline_import is None or expected_offline_kwargs is None:
                return self._unmeasured(
                    "offline_verifier_binding_missing",
                    start,
                    diagnostics={"harbor_exit": proc.returncode},
                )
            try:
                self._physical_verifier.admit(
                    jobs_dir=out_dir,
                    trial=trial,
                    expected_import_path=expected_offline_import,
                    expected_kwargs=expected_offline_kwargs,
                    not_before_ns=not_before_ns,
                )
            except ResultProvenanceError as exc:
                return self._unmeasured(
                    "offline_verifier_result_untrusted",
                    start,
                    diagnostics={
                        "harbor_exit": proc.returncode,
                        "exception_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                    cost_usd=cost_usd,
                    cost_status=cost_status,
                    provider_calls=provider_calls,
                )

        reward = _extract_reward(trial)
        cost_budget_failure = _cell_cost_budget_failure(
            budget, cost_usd=cost_usd, cost_status=cost_status
        )
        if cost_budget_failure == "cost_budget_unverified":
            return self._unmeasured(
                cost_budget_failure,
                start,
                diagnostics={"harbor_exit": proc.returncode},
                cost_usd=cost_usd,
                cost_status=cost_status,
                provider_calls=provider_calls,
            )
        if cost_budget_failure == "cost_budget_exceeded":
            assert budget.max_cost_usd is not None
            return self._unmeasured(
                cost_budget_failure,
                start,
                diagnostics={
                    "harbor_exit": proc.returncode,
                    "max_cost_usd": float(budget.max_cost_usd),
                },
                cost_usd=cost_usd,
                cost_status=cost_status,
                provider_calls=provider_calls,
            )
        agent_timed_out = _agent_timed_out(trial)
        agent_ran = trial.get("agent_result") is not None or bool(
            trial.get("step_results")
        )
        if reward is None:
            # agent may have run, but the verifier produced no reward:
            # insufficient evidence, not a measured zero (M2 lesson (a))
            usage_diagnostics = {"agent_ran": bool(agent_ran)}
            if tokens_in is not None:
                usage_diagnostics["n_input_tokens"] = float(tokens_in)
            if cache_tokens is not None:
                usage_diagnostics["n_cache_tokens"] = float(cache_tokens)
            if tokens_out is not None:
                usage_diagnostics["n_output_tokens"] = float(tokens_out)
            return self._unmeasured(
                "missing_verifier_reward",
                start,
                diagnostics=usage_diagnostics,
                cost_usd=cost_usd,
                cost_status=cost_status,
                provider_calls=provider_calls,
            )
        if artifact_expectation is not None and not self._artifact_reward_matches(
            out_dir=out_dir,
            task=task,
            payload_name=artifact_expectation.payload_name,
            reward=reward,
        ):
            return self._unmeasured(
                "artifact_reward_mismatch",
                start,
                diagnostics={"artifact_state": "reward_mismatch"},
                cost_usd=cost_usd,
                cost_status=cost_status,
                provider_calls=provider_calls,
            )
        outcome = (
            SampleOutcomeKind.PASS.value
            if reward >= self._pass_reward_threshold
            else SampleOutcomeKind.FAIL.value
        )
        artifact_state = "not_required"
        if artifact_expectation is not None:
            envelopes = tuple(
                (out_dir / "cell").glob(
                    "*/artifacts/logs/artifacts/rsi-arena-envelope.json"
                )
            )
            artifact_state = (
                "present"
                if len(envelopes) == 1 and envelopes[0].is_file()
                else "missing"
            )
        return self._cell(
            outcome,
            reward,
            1.0,
            start,
            cost_usd=cost_usd,
            cost_status=cost_status,
            tokens_in=tokens_in,
            cache_tokens=cache_tokens,
            tokens_out=tokens_out,
            agent_timed_out=agent_timed_out,
            provider_calls=provider_calls,
            diagnostics={
                "agent_state": "timed_out" if agent_timed_out else "completed",
                "artifact_state": artifact_state,
            },
        )

    # -- helpers -------------------------------------------------------------

    def _artifact_reward_matches(
        self,
        *,
        out_dir: Path,
        task: TaskRef,
        payload_name: str,
        reward: float,
    ) -> bool:
        """Benchmark hook for binding a signed payload to verifier reward."""

        del out_dir, task, payload_name, reward
        return True

    def _subprocess_env(
        self,
        *,
        runtime_root: Path | None = None,
        trusted_import: bool = False,
    ) -> dict:
        env = {k: os.environ[k] for k in _BASE_ENV_VARS if k in os.environ}
        if self._docker_host:
            env["DOCKER_HOST"] = self._docker_host
        if runtime_root is None:
            # Only the trusted uv setup path receives the operator cache home.
            for name in ("HOME", "TMPDIR"):
                if name in os.environ:
                    env[name] = os.environ[name]
        else:
            env["HOME"] = str(runtime_root / "home")
            env["TMPDIR"] = str(runtime_root / "tmp")
        for name in self._env_passthrough:
            env[name] = os.environ[name]
        if trusted_import:
            # The fixed Harbor interpreter imports only the bridge from the
            # trusted RSI Arena control checkout.  The candidate checkout is
            # never placed on this host process's Python path.
            env["PYTHONPATH"] = str(_RSI_ARENA_SOURCE_ROOT)
            env["PYTHONNOUSERSITE"] = "1"
        return env

    def _ensure_venv(self, checkout: Path) -> None:
        """``uv sync --frozen`` once per checkout; cells share checkouts, so
        serialize per path.  uv hardlinks from its cache — cheap when warm."""
        with self._venv_locks_guard:
            lock = self._venv_locks.setdefault(str(checkout), threading.Lock())
        with lock:
            harbor_bin = checkout / ".venv" / "bin" / "harbor"
            if harbor_bin.exists():
                return
            subprocess.run(
                [self._uv_path, "sync", "--frozen", "--no-dev"],
                cwd=str(checkout),
                env=self._subprocess_env(),
                capture_output=True,
                check=True,
                timeout=600,
            )
            if not harbor_bin.exists():
                raise RuntimeError(
                    f"uv sync produced no harbor entrypoint in {checkout}"
                )

    def _cell(
        self,
        outcome: str,
        reward: float,
        harness_boot: float,
        start: float,
        cost_usd: float | None = None,
        cost_status: str = "missing",
        tokens_in: float | None = None,
        cache_tokens: float | None = None,
        tokens_out: float | None = None,
        agent_timed_out: bool = False,
        provider_calls: tuple[Mapping[str, object], ...] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> CellResult:
        duration = time.monotonic() - start
        metrics = {
            "reward": float(reward),
            "harness_boot": float(harness_boot),
            "duration_s": duration,
            # A trusted verifier can score the state left at the agent
            # deadline.  Preserve that score while making the timeout
            # visible instead of silently turning it into a clean pass.
            "agent_timeout_observed": float(bool(agent_timed_out)),
        }
        if tokens_in is not None:
            metrics["n_input_tokens"] = float(tokens_in)
        if cache_tokens is not None:
            metrics["n_cache_tokens"] = float(cache_tokens)
        if tokens_out is not None:
            metrics["n_output_tokens"] = float(tokens_out)
        if cost_usd is not None:
            metrics["agent_cost_usd"] = float(cost_usd)
        return CellResult(
            outcome=outcome,
            raw_metrics=metrics,
            duration_s=duration,
            cost_usd=cost_usd,
            cost_status=cost_status,
            provider_calls=provider_calls,
            diagnostics=MappingProxyType(dict(diagnostics or {})),
        )

    def _unmeasured(
        self,
        reason: str,
        start: float,
        *,
        diagnostics: dict | None = None,
        cost_usd: float | None = None,
        cost_status: str = "missing",
        provider_calls: tuple[Mapping[str, object], ...] = (),
    ) -> UnmeasuredCell:
        evidence = dict(diagnostics or {})
        return UnmeasuredCell(
            reason=reason,
            diagnostics=evidence,
            duration_s=time.monotonic() - start,
            cost_usd=cost_usd,
            cost_status=cost_status,
            provider_calls=provider_calls,
        )


def _kill_process_group(proc: subprocess.Popen) -> None:
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(timeout=30)


def _cell_cost_budget_failure(
    budget: CellBudget,
    *,
    cost_usd: float | None,
    cost_status: str,
) -> str | None:
    """Return the fail-closed terminal reason for one configured cell cap."""

    maximum = budget.max_cost_usd
    if maximum is None:
        return None
    if cost_status != "reported" or cost_usd is None:
        return "cost_budget_unverified"
    if cost_usd > maximum:
        return "cost_budget_exceeded"
    return None


def _invoke_pre_cleanup_trial_hook(
    job_dir: Path,
    hook: Callable[[Path], None] | None,
    *,
    allow_partial: bool = False,
) -> None:
    """Call an optional observer for one unambiguous completed or partial trial."""

    if hook is None:
        return
    try:
        directories = tuple(
            sorted(child for child in Path(job_dir).iterdir() if child.is_dir())
        )
    except OSError:
        if allow_partial:
            log.warning("pre-cleanup partial trajectory unavailable")
        return
    completed = tuple(
        child for child in directories if (child / "result.json").is_file()
    )
    trial: Path | None = completed[0] if len(completed) == 1 else None
    if trial is None and not completed and allow_partial:
        partial = tuple(
            child
            for child in directories
            if (child / "agent/trajectory.json").is_file()
        )
        trial = partial[0] if len(partial) == 1 else None
        if trial is None:
            log.warning("pre-cleanup partial trajectory unavailable")
    if trial is None:
        return
    try:
        hook(trial)
    except Exception:  # noqa: BLE001 - optional evidence must not change outcome
        log.warning("pre-cleanup trajectory harvest failed")


def _extract_reward(trial: dict) -> float | None:
    verifier = trial.get("verifier_result") or {}
    rewards = verifier.get("rewards")
    if not rewards:
        return None
    if "reward" in rewards:
        return float(rewards["reward"])
    return float(next(iter(rewards.values())))


def _agent_timed_out(trial: dict) -> bool:
    exception_info = trial.get("exception_info")
    return bool(
        isinstance(exception_info, dict)
        and exception_info.get("exception_type") == "AgentTimeoutError"
    )


def _extract_usage(
    trial: dict,
    *,
    allow_step_results: bool = True,
) -> tuple[
    int | float | None, int | float | None, int | float | None, float | None, str
]:
    """Return tokens plus a non-lossy provider-cost observation.

    Harbor may omit ``cost_usd`` while still reporting tokens.  Missing is
    therefore not a numeric zero; an explicit zero remains a reported value.
    Typed paths may aggregate step results; native-state authority uses
    only the top-level accounting already cross-checked against job stats.
    """
    contexts: list[Mapping[str, object]] = []
    agent_result = trial.get("agent_result")
    if isinstance(agent_result, Mapping) and agent_result:
        contexts = [agent_result]
    elif allow_step_results and isinstance(trial.get("step_results"), list):
        contexts = [
            step_agent
            for step in trial["step_results"]
            if isinstance(step, Mapping)
            and isinstance((step_agent := step.get("agent_result")), Mapping)
            and step_agent
        ]

    def token_total(name: str) -> int | float | None:
        total = 0
        for ctx in contexts:
            if name not in ctx or ctx.get(name) is None:
                return None
            value = ctx[name]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0.0
                or not float(value).is_integer()
            ):
                # Preserve an invalid numeric marker so the trusted aggregate
                # evaluator records this dimension as unverified.
                return float("nan")
            total += int(value)
        return total if contexts else None

    tokens_in = token_total("n_input_tokens")
    cache_tokens = token_total("n_cache_tokens")
    tokens_out = token_total("n_output_tokens")
    cost = 0.0
    cost_status = "reported"
    for ctx in contexts:
        if ctx.get("cost_usd") is None:
            if cost_status == "reported":
                cost_status = "missing"
            continue
        value = ctx.get("cost_usd")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0.0
        ):
            cost_status = "unverified"
            continue
        cost += float(value)
    if not contexts:
        cost_status = "missing"
    return (
        tokens_in,
        cache_tokens,
        tokens_out,
        cost if cost_status == "reported" else None,
        cost_status,
    )


__all__ = ["HarborBench", "PhysicalVerifierRuntime", "TaskTimeoutAuthority"]
