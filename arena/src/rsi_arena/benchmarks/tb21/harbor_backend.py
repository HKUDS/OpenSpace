"""TB2.1 physical Harbor backend for the method-neutral campaign seam.

The backend owns exactly one fresh task cell per ``execute`` call.  It does
not sample tasks, allocate candidate-evaluation slots, create a recorder, or
grade anything itself: Harbor runs the pinned official task verifier and this
module only accepts a provenance-valid reward.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any

import tomllib

from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    AGENT_IMPORT_PATH,
    CANDIDATE_RUNTIME_FAILURE_KEY,
    candidate_runtime_failure_from_context,
    harbor_agent_bridge_identity,
    harbor_cli_agent_args,
)
from rsi_arena.benchmarks.harbor_runtime.execution import (
    HarborExecutionError,
    ImmutableDockerImageIdentity,
    cleanup_scoped_compose_cell,
    harbor_controller_identity,
    inspect_docker_image,
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
    snapshot_task_materials,
)
from rsi_arena.benchmarks.harbor_runtime.provenance import (
    ResultAuthorityError,
    ResultProvenanceError,
    RewardCtrfConflictError,
    load_validated_trial_result,
    provider_calls_from_trial,
)
from rsi_arena.benchmarks.harbor_runtime.verifier_environment import (
    harbor_verifier_env_args,
    verifier_dependency_cache_env,
)
from rsi_arena.benchmarks.harbor_runtime.verifier_settings import VerifierSettings
from rsi_arena.contracts import canonical_digest
from rsi_arena.evaluation_gateway import GatewayCandidate
from rsi_arena.inner_agent_runtime import parse_python_inner_agent_ref
from rsi_arena.run_layout import RunLayout, atomic_write_json, file_digest

from .campaign import TB2CampaignTask, TB2TaskOutcome
from .offline_verifier import (
    OFFLINE_VERIFIER_IMPORT_PATH,
    SNAPSHOT_SCHEMA,
    OfflineClosureAuthority,
    TB21OfflineVerifierError,
    load_offline_ctrf_counts,
    offline_verifier_launch_kwargs,
)
from .tasks import (
    TB2TaskTimeoutAuthority,
    TB21SnapshotError,
    TB21TaskSnapshot,
    materialize_tb21_task,
    resolve_tb2_task_timeout_authority,
    validate_tb21_task_snapshot,
)

_BASE_ENV_NAMES = ("PATH", "LANG", "DOCKER_HOST", "DOCKER_CONFIG")
_CELL_ID_RE = re.compile(r"tb21-harbor-cell-[0-9a-f]{32}")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_OFFLINE_VERIFIER_KWARGS = frozenset(
    {
        "task_id",
        "run_id",
        "snapshot_digest",
        "source_commit",
        "source_tree",
        "tasks_tree",
        "authority_digest",
        "authority_content_digest",
        "lock_digest",
        "closure_digest",
        "build_input_digest",
        "task_material_digest",
        "closure_image_id",
        "closure_base_image_id",
        "expected_agent_image_id",
        "dynamic_candidate_uv",
        "shared_run_root",
        "tests_path",
        "tests_digest",
        "docker",
        "docker_host",
        "verifier_timeout_seconds",
    }
)
_OFFLINE_PROVENANCE_MAX_BYTES = 16 * 1024 * 1024
_LOG = logging.getLogger(__name__)


def _candidate_runtime_failure_for_public_feedback(
    trial: Mapping[str, object],
    *,
    reward: float,
    credential_literal: str | None,
    task_id: str,
) -> dict[str, str] | None:
    """Return the validated zero-score marker with private literals scrubbed."""

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


class TB21HarborBackendError(RuntimeError):
    """One typed, non-candidate TB2 backend failure."""

    def __init__(self, error_type: str, message: str = "") -> None:
        self.error_type = str(error_type)
        super().__init__(message or self.error_type)


class TB21HarborInfraError(TB21HarborBackendError):
    pass


class TB21HarborInvariantError(TB21HarborBackendError):
    pass


class TB21HarborAuthorityError(TB21HarborInvariantError):
    """One evaluator-wide authority mismatch that must block aggregation."""


class TB21HarborEvaluatorError(TB21HarborBackendError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _positive_seconds(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"{label} must be positive and finite")
    return float(value)


def _exclusive_private_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short private-file write")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _task_config(task_manifest: bytes) -> Mapping[str, Any]:
    try:
        value = tomllib.loads(task_manifest.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TB21HarborInfraError(
            "tb21_task_material_invalid", "official task.toml is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise TB21HarborInfraError("tb21_task_material_invalid")
    return value


def _configured_image(task_manifest: bytes) -> str:
    environment = _task_config(task_manifest).get("environment")
    image = (
        environment.get("docker_image") if isinstance(environment, Mapping) else None
    )
    if not isinstance(image, str) or not image:
        raise TB21HarborInfraError(
            "tb21_task_image_missing", "official task has no environment.docker_image"
        )
    return image


def _rewrite_task_image(
    task_manifest: bytes, identity: ImmutableDockerImageIdentity
) -> bytes:
    """Replace only environment.docker_image in one fresh task.toml copy."""

    try:
        text = task_manifest.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TB21HarborInfraError("tb21_task_material_invalid") from exc
    lines = text.splitlines(keepends=True)
    in_environment = False
    replacements = 0
    rendered: list[str] = []
    for line in lines:
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
    if replacements != 1:
        raise TB21HarborInfraError("tb21_task_image_ambiguous")
    rewritten = "".join(rendered).encode("utf-8")
    if _configured_image(rewritten) != identity.runtime_ref:
        raise TB21HarborInfraError("tb21_task_image_rewrite_failed")
    return rewritten


def _validated_regular_file(path: Path, root: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise TB21HarborInvariantError("tb21_evidence_path_invalid") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
    ):
        raise TB21HarborInvariantError("tb21_evidence_file_invalid")
    return resolved


def _trial_directory(job_dir: Path) -> Path:
    trials = [
        child
        for child in job_dir.iterdir()
        if child.is_dir()
        and not child.is_symlink()
        and (child / "result.json").is_file()
    ]
    if len(trials) != 1:
        raise TB21HarborInvariantError("tb21_trial_artifact_ambiguous")
    return trials[0]


def _integer_usage(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value >= 0.0
        and value.is_integer()
    ):
        return int(value)
    return None


def _fresh_json_object(
    path: Path,
    *,
    not_before_ns: int,
    max_bytes: int = _OFFLINE_PROVENANCE_MAX_BYTES,
) -> dict[str, object]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ResultProvenanceError(
            f"missing offline verifier record: {path.name}"
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_mtime_ns < not_before_ns
        or info.st_size > max_bytes
    ):
        raise ResultProvenanceError(
            f"offline verifier record is not a fresh bounded file: {path.name}"
        )
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultProvenanceError(
            f"offline verifier record is unreadable: {path.name}"
        ) from exc
    if not isinstance(payload, dict):
        raise ResultProvenanceError(
            f"offline verifier record is not an object: {path.name}"
        )
    return payload


def offline_verifier_launch_binding(
    command: Sequence[str],
) -> tuple[str, Mapping[str, object]]:
    verifier_positions = [
        index for index, value in enumerate(command) if value == "--verifier"
    ]
    if len(verifier_positions) != 1:
        raise TB21HarborInvariantError("tb21_untrusted_grader_result")
    verifier_position = verifier_positions[0]
    if verifier_position + 1 >= len(command):
        raise TB21HarborInvariantError("tb21_untrusted_grader_result")
    import_path = command[verifier_position + 1]
    raw_kwargs: dict[str, str] = {}
    for index, value in enumerate(command):
        if value != "--verifier-kwarg":
            continue
        if index + 1 >= len(command):
            raise TB21HarborInvariantError("tb21_untrusted_grader_result")
        name, separator, raw = command[index + 1].partition("=")
        if not separator or name in raw_kwargs:
            raise TB21HarborInvariantError("tb21_untrusted_grader_result")
        raw_kwargs[name] = raw
    if import_path != OFFLINE_VERIFIER_IMPORT_PATH or set(raw_kwargs) != set(
        _OFFLINE_VERIFIER_KWARGS
    ):
        raise TB21HarborInvariantError("tb21_untrusted_grader_result")
    try:
        normalized: dict[str, object] = dict(raw_kwargs)
        dynamic = raw_kwargs["dynamic_candidate_uv"]
        if dynamic not in {"true", "false"}:
            raise ValueError("invalid dynamic flag")
        normalized["dynamic_candidate_uv"] = dynamic == "true"
        normalized["verifier_timeout_seconds"] = float(
            raw_kwargs["verifier_timeout_seconds"]
        )
    except (KeyError, ValueError) as exc:
        raise TB21HarborInvariantError("tb21_untrusted_grader_result") from exc
    return import_path, MappingProxyType(normalized)


def _configured_offline_verifier(
    record: object,
    *,
    expected_import_path: str,
    expected_kwargs: Mapping[str, object],
) -> None:
    if not isinstance(record, Mapping):
        raise ResultProvenanceError("Harbor record omits the offline verifier")
    if (
        record.get("import_path") != expected_import_path
        or record.get("kwargs") != dict(expected_kwargs)
        or ("disable" in record and record.get("disable") is not False)
    ):
        raise ResultProvenanceError(
            "Harbor record changed the offline verifier binding"
        )


def _trial_reward(trial: Mapping[str, object]) -> float:
    verifier = trial.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, Mapping) else None
    raw = (
        rewards.get("reward", next(iter(rewards.values())))
        if isinstance(rewards, Mapping) and rewards
        else None
    )
    if (
        not isinstance(raw, (int, float))
        or isinstance(raw, bool)
        or not math.isfinite(float(raw))
        or not 0.0 <= float(raw) <= 1.0
    ):
        raise ResultProvenanceError("offline verifier reward is invalid")
    return float(raw)


def _valid_snapshot_identity(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"schema", "sha256", "entries"}
        and value.get("schema") == SNAPSHOT_SCHEMA
        and isinstance(value.get("sha256"), str)
        and _SHA256_RE.fullmatch(value["sha256"]) is not None
        and isinstance(value.get("entries"), int)
        and not isinstance(value.get("entries"), bool)
        and value["entries"] >= 0
    )


def admit_offline_verifier(
    *,
    jobs_dir: Path,
    trial: Mapping[str, object],
    expected_import_path: str,
    expected_kwargs: Mapping[str, object],
    not_before_ns: int,
) -> Mapping[str, object]:
    """Require the launched post-agent verifier's complete terminal record."""

    job_dir = jobs_dir / "cell"
    trial_dir = _trial_directory(job_dir)
    job_lock = _fresh_json_object(job_dir / "lock.json", not_before_ns=not_before_ns)
    trial_config = _fresh_json_object(
        trial_dir / "config.json", not_before_ns=not_before_ns
    )
    trial_lock = _fresh_json_object(
        trial_dir / "lock.json", not_before_ns=not_before_ns
    )
    locked_trials = job_lock.get("trials")
    if (
        not isinstance(locked_trials, list)
        or len(locked_trials) != 1
        or not isinstance(locked_trials[0], Mapping)
    ):
        raise ResultProvenanceError("Harbor lock does not bind one offline verifier")
    embedded = trial.get("config")
    for record in (
        locked_trials[0].get("verifier"),
        trial_config.get("verifier"),
        trial_lock.get("verifier"),
        embedded.get("verifier") if isinstance(embedded, Mapping) else None,
    ):
        _configured_offline_verifier(
            record,
            expected_import_path=expected_import_path,
            expected_kwargs=expected_kwargs,
        )

    provenance = _fresh_json_object(
        trial_dir / "verifier" / "offline-verifier-provenance.json",
        not_before_ns=not_before_ns,
    )
    ctrf_path = trial_dir / "verifier" / "ctrf.json"
    try:
        ctrf_info = ctrf_path.lstat()
        if (
            stat.S_ISLNK(ctrf_info.st_mode)
            or not stat.S_ISREG(ctrf_info.st_mode)
            or ctrf_info.st_mtime_ns < not_before_ns
        ):
            raise TB21OfflineVerifierError("offline verifier CTRF is stale")
        ctrf = load_offline_ctrf_counts(ctrf_path)
    except (OSError, TB21OfflineVerifierError) as exc:
        raise ResultProvenanceError(
            "offline verifier CTRF did not pass strict admission"
        ) from exc
    reward = _trial_reward(trial)
    before = provenance.get("candidate_snapshot")
    after = provenance.get("candidate_snapshot_after_verifier")
    expected_values = {
        "task_id": expected_kwargs["task_id"],
        "run_id": expected_kwargs["run_id"],
        "source_commit": expected_kwargs["source_commit"],
        "source_tree": expected_kwargs["source_tree"],
        "tasks_tree": expected_kwargs["tasks_tree"],
        "expected_agent_image_id": expected_kwargs["expected_agent_image_id"],
        "closure_base_image_id": expected_kwargs["closure_base_image_id"],
        "dynamic_candidate_uv": expected_kwargs["dynamic_candidate_uv"],
        "network_mode": "none",
        "agent_environment_dependency_preparation": False,
        "original_test_script": str(
            Path(str(expected_kwargs["tests_path"])) / "test.sh"
        ),
        "candidate_snapshot_unchanged": True,
    }
    if (
        any(provenance.get(name) != value for name, value in expected_values.items())
        or not _valid_snapshot_identity(before)
        or before != after
        or provenance.get("ctrf") != dict(ctrf)
        or provenance.get("reward") != reward
    ):
        raise ResultProvenanceError(
            "offline verifier provenance does not match the launched cell"
        )
    dynamic = expected_kwargs["dynamic_candidate_uv"] is True
    closure_image_id = provenance.get("closure_image_id")
    if (
        not isinstance(closure_image_id, str)
        or _SHA256_RE.fullmatch(closure_image_id) is None
        or (not dynamic and closure_image_id != expected_kwargs["closure_image_id"])
    ):
        raise ResultProvenanceError(
            "offline verifier provenance changed closure image authority"
        )
    if dynamic:
        dependency = provenance.get("dependency_preparation")
        if not isinstance(dependency, Mapping):
            raise ResultProvenanceError(
                "dynamic reshard provenance omits dependency preparation"
            )
    return MappingProxyType(dict(provenance))


def admit_offline_verifier_job(
    *,
    job_dir: Path,
    expected_import_path: str,
    expected_kwargs: Mapping[str, object],
    not_before_ns: int,
) -> Mapping[str, object]:
    """Admit one completed Harbor job when its caller does not retain trial JSON."""

    root = Path(job_dir)
    if root.name != "cell":
        raise ResultProvenanceError("offline verifier job name is not cell")
    trial_dir = _trial_directory(root)
    trial = _fresh_json_object(trial_dir / "result.json", not_before_ns=not_before_ns)
    return admit_offline_verifier(
        jobs_dir=root.parent,
        trial=trial,
        expected_import_path=expected_import_path,
        expected_kwargs=expected_kwargs,
        not_before_ns=not_before_ns,
    )


def _require_tb2_layout(layout: RunLayout) -> None:
    if layout.benchmark == "tb2":
        return
    try:
        run_manifest = json.loads(
            (layout.root / "run-manifest.json").read_text(encoding="utf-8")
        )
        freeze = run_manifest["campaign_run_freeze"]
    except (KeyError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("TB2 Harbor backend requires a TB2 RunLayout") from exc
    if (
        not isinstance(freeze, Mapping)
        or freeze.get("benchmark_ids") != ["tb2"]
        or freeze.get("campaign_cell_id") != layout.benchmark
    ):
        raise ValueError("TB2 Harbor backend requires a TB2 RunLayout")


class TB21HarborExecutionBackend:
    """Stateless-per-call implementation of ``TB2CampaignExecutionBackend``."""

    def __init__(
        self,
        *,
        layout: RunLayout,
        snapshot: TB21TaskSnapshot,
        trusted_harbor_root: Path,
        docker_path: str | Path,
        docker_host: str,
        bwrap_path: str | Path,
        agent_ref: str,
        agent_config: Mapping[str, object],
        model_name: str,
        provider_name: str,
        provider_base_url: str,
        provider_base_url_env_name: str,
        credential_env_name: str,
        reasoning_effort: str | None = None,
        worker_timeout_seconds: float,
        settled_cost_stop_usd: float | None = None,
        harbor_wall_timeout_seconds: float,
        cleanup_grace_seconds: float,
        verifier_settings: VerifierSettings,
        verifier_env: Mapping[str, str] | None = None,
        approved_image_identities: (
            Mapping[str, ImmutableDockerImageIdentity] | None
        ) = None,
        require_approved_image_identities: bool = False,
        pass_reward_threshold: float = 1.0,
        offline_closure_authority: OfflineClosureAuthority | None = None,
        offline_verifier_shared_root: Path | None = None,
    ) -> None:
        _require_tb2_layout(layout)
        if not isinstance(model_name, str) or not model_name:
            raise ValueError("model_name is required")
        if (
            agent_ref != "builtin:terminus-2"
            and parse_python_inner_agent_ref(agent_ref) is None
        ):
            raise ValueError("TB2 inner-agent ref is unsupported")
        try:
            copied_agent_config = json.loads(
                json.dumps(dict(agent_config), allow_nan=False)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("TB2 inner-agent config must be finite JSON") from exc
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be a non-empty string")
        if (
            not isinstance(credential_env_name, str)
            or not credential_env_name.isidentifier()
            or credential_env_name.upper() != credential_env_name
        ):
            raise ValueError("credential_env_name is invalid")
        if not isinstance(require_approved_image_identities, bool):
            raise ValueError("require_approved_image_identities must be boolean")
        if (
            not isinstance(pass_reward_threshold, (int, float))
            or isinstance(pass_reward_threshold, bool)
            or not math.isfinite(float(pass_reward_threshold))
            or not 0.0 <= float(pass_reward_threshold) <= 1.0
        ):
            raise ValueError("pass_reward_threshold must be in [0, 1]")

        try:
            validate_tb21_task_snapshot(snapshot)
        except TB21SnapshotError as exc:
            raise ValueError("supplied TB2 snapshot is invalid") from exc
        self.layout = layout
        self.snapshot = snapshot
        self._trusted_harbor_locator = Path(trusted_harbor_root).expanduser()
        self._docker_locator = docker_path
        if not isinstance(docker_host, str) or not docker_host.strip():
            raise ValueError("docker_host must be explicit non-empty text")
        self.docker_host = docker_host.strip()
        self._image_inspector = partial(
            inspect_docker_image, docker_host=self.docker_host
        )
        self.trusted_harbor_root: Path | None = None
        self.controller = None
        self.docker: Path | None = None
        self.bwrap_path = str(bwrap_path)
        self.bridge_identity_digest = harbor_agent_bridge_identity()
        self.agent_ref = agent_ref
        self.agent_config = MappingProxyType(copied_agent_config)
        self.model_name = model_name
        self.reasoning_effort = reasoning_effort
        self.provider_name = str(provider_name)
        self.provider_base_url = str(provider_base_url)
        self.provider_base_url_env_name = str(provider_base_url_env_name)
        if self.provider_base_url_env_name == credential_env_name:
            raise ValueError(
                "provider base URL and credential environment names must differ"
            )
        self.credential_value: str | None = None
        self.credential_env_name = credential_env_name
        self.worker_timeout_seconds = _positive_seconds(
            worker_timeout_seconds, "worker timeout"
        )
        if settled_cost_stop_usd is not None and (
            isinstance(settled_cost_stop_usd, bool)
            or not isinstance(settled_cost_stop_usd, (int, float))
            or not math.isfinite(float(settled_cost_stop_usd))
            or float(settled_cost_stop_usd) < 0.0
        ):
            raise ValueError("settled_cost_stop_usd must be finite and non-negative")
        self.settled_cost_stop_usd = (
            None if settled_cost_stop_usd is None else float(settled_cost_stop_usd)
        )
        self.harbor_wall_timeout_seconds = _positive_seconds(
            harbor_wall_timeout_seconds, "Harbor wall timeout"
        )
        self.cleanup_grace_seconds = _positive_seconds(
            cleanup_grace_seconds, "cleanup grace"
        )
        if not isinstance(verifier_settings, VerifierSettings):
            raise ValueError("verifier_settings must be VerifierSettings")
        self.verifier_settings = verifier_settings
        self.verifier_env = (
            verifier_dependency_cache_env(verifier_env)
            if verifier_env is not None
            else MappingProxyType({})
        )
        approved_images: dict[str, ImmutableDockerImageIdentity] | None = None
        if approved_image_identities is not None:
            approved_images = dict(approved_image_identities)
            if not approved_images or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, ImmutableDockerImageIdentity)
                or value.configured_ref != key
                for key, value in approved_images.items()
            ):
                raise ValueError("approved_image_identities is invalid")
        self.approved_image_identities = (
            MappingProxyType(approved_images) if approved_images is not None else None
        )
        self.require_approved_image_identities = require_approved_image_identities
        self._approved_image_lock = threading.Lock()
        self.pass_reward_threshold = float(pass_reward_threshold)
        self.offline_closure_authority = (
            offline_closure_authority if offline_closure_authority is not None else None
        )
        if self.offline_closure_authority is None:
            raise ValueError("offline verifier authority must be explicitly supplied")
        if (
            self.offline_closure_authority.snapshot_digest
            != self.snapshot.snapshot_digest
            or self.offline_closure_authority.source_commit != self.snapshot.commit
            or set(self.offline_closure_authority.tasks)
            != {str(item["task_id"]) for item in self.snapshot.tasks}
        ):
            raise ValueError("offline verifier authority disagrees with TB2 snapshot")
        if offline_verifier_shared_root is None:
            raise ValueError("offline verifier shared root must be explicitly supplied")
        self.offline_verifier_shared_root = Path(offline_verifier_shared_root)
        if (
            not self.offline_verifier_shared_root.is_absolute()
            or self.offline_verifier_shared_root == Path("/")
        ):
            raise ValueError("offline verifier shared root must be narrow and absolute")
        self._implementation_files = (
            Path(__file__).resolve(),
            Path(__file__).with_name("offline_verifier.py").resolve(),
            Path(__file__).parents[1] / "harbor_runtime/provenance.py",
            Path(__file__).parents[1] / "harbor_runtime/execution.py",
            Path(__file__).parents[1] / "harbor_runtime/agent_bridge.py",
            Path(__file__).parents[1] / "harbor_runtime/agent_worker.py",
        )
        self._implementation_digests = {
            path.name: file_digest(path) for path in self._implementation_files
        }
        self._backend_digest = "sha256:" + canonical_digest(
            {
                "schema": "tb21-harbor-execution-backend",
                "snapshot": self.snapshot.identity,
                "controller_locator": str(self._trusted_harbor_locator),
                "docker_locator": str(self._docker_locator),
                "docker_host": self.docker_host,
                "bridge_identity_digest": self.bridge_identity_digest,
                "inner_agent": {
                    "ref": self.agent_ref,
                    "config": dict(self.agent_config),
                },
                "implementation_digests": self._implementation_digests,
                "model_name": self.model_name,
                "reasoning_effort": self.reasoning_effort,
                "provider_name": self.provider_name,
                "provider_base_url": self.provider_base_url,
                "provider_base_url_env_name": self.provider_base_url_env_name,
                "credential_env_name": self.credential_env_name,
                "worker_timeout_seconds": self.worker_timeout_seconds,
                "settled_cost_stop_usd": self.settled_cost_stop_usd,
                "harbor_wall_timeout_seconds": self.harbor_wall_timeout_seconds,
                "cleanup_grace_seconds": self.cleanup_grace_seconds,
                "verifier_settings": self.verifier_settings.as_dict(),
                "verifier_env": dict(self.verifier_env),
                "pass_reward_threshold": self.pass_reward_threshold,
                "offline_closure_authority": {
                    "source_commit": self.offline_closure_authority.source_commit,
                    "tasks_tree": self.offline_closure_authority.tasks_tree,
                    "task_count": len(self.offline_closure_authority.tasks),
                    "custom_verifier": OFFLINE_VERIFIER_IMPORT_PATH,
                },
                "approved_image_identities": (
                    {
                        key: value.as_dict()
                        for key, value in sorted(self.approved_image_identities.items())
                    }
                    if self.approved_image_identities is not None
                    else None
                ),
                "require_approved_image_identities": (
                    self.require_approved_image_identities
                ),
                "artifact_codec": "native_state",
            }
        )
        key_path = self.layout.trusted_runtime_root / (
            "tb21-harbor-envelope-" + uuid.uuid4().hex + ".key"
        )
        _exclusive_private_bytes(
            key_path, secrets.token_hex(32).encode("ascii") + b"\n"
        )
        self.envelope_key_file = key_path

    @property
    def backend_digest(self) -> str:
        return self._backend_digest

    def bind_approved_image_identities(
        self,
        identities: Mapping[str, ImmutableDockerImageIdentity],
    ) -> None:
        """Monotonically bind deferred, pre-execution image identities.

        Candidate-scoped launchers use this after the shared recorder has
        durably realized a draw and before any task worker is started.  The
        ordinary constructor path remains unchanged for SWE/TB2 callers that
        provide a static approval set up front.
        """

        supplied = dict(identities)
        if not self.require_approved_image_identities:
            raise ValueError("backend is not configured for deferred image approval")
        if not supplied or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, ImmutableDockerImageIdentity)
            or value.configured_ref != key
            for key, value in supplied.items()
        ):
            raise ValueError("approved_image_identities is invalid")
        normalized = MappingProxyType(dict(sorted(supplied.items())))
        with self._approved_image_lock:
            if self.approved_image_identities is None:
                self.approved_image_identities = normalized
                return
            existing = dict(self.approved_image_identities)
            if any(
                key in existing and existing[key] != identity
                for key, identity in normalized.items()
            ):
                raise ValueError("deferred image approval changed after binding")
            existing.update(normalized)
            self.approved_image_identities = MappingProxyType(
                dict(sorted(existing.items()))
            )

    def _assert_backend_identity(self) -> None:
        observed = {path.name: file_digest(path) for path in self._implementation_files}
        if observed != self._implementation_digests:
            raise TB21HarborAuthorityError("tb21_backend_implementation_drift")

    def _resolve_runtime(self) -> None:
        if self.controller is not None:
            return
        credential = os.environ.get(self.credential_env_name)
        if credential is None:
            raise TB21HarborInfraError("tb21_credential_unavailable")
        try:
            root = self._trusted_harbor_locator.resolve(strict=True)
            controller = harbor_controller_identity(root)
            docker = resolve_docker_executable(self._docker_locator)
        except (OSError, HarborExecutionError) as exc:
            raise TB21HarborInfraError("tb21_runtime_unavailable") from exc
        self.trusted_harbor_root = root
        self.controller = controller
        self.docker = docker
        self.credential_value = credential

    def _assert_controller_identity(self) -> None:
        if self.trusted_harbor_root is None or self.controller is None:
            raise TB21HarborAuthorityError("tb21_harbor_controller_unavailable")
        try:
            observed = harbor_controller_identity(self.trusted_harbor_root)
        except (OSError, HarborExecutionError) as exc:
            raise TB21HarborAuthorityError("tb21_harbor_controller_drift") from exc
        if observed != self.controller:
            raise TB21HarborAuthorityError("tb21_harbor_controller_drift")

    def _assert_snapshot_identity(self) -> None:
        try:
            validate_tb21_task_snapshot(self.snapshot)
        except Exception as exc:
            raise TB21HarborAuthorityError("tb21_task_snapshot_drift") from exc

    def _timeout_authority(self, task_id: str) -> TB2TaskTimeoutAuthority:
        matches = [
            record for record in self.snapshot.tasks if record.get("task_id") == task_id
        ]
        if len(matches) != 1:
            raise TB21HarborAuthorityError("tb21_task_timeout_settings_invalid")
        try:
            return resolve_tb2_task_timeout_authority(
                matches[0],
                worker_timeout_seconds=self.worker_timeout_seconds,
                outer_timeout_seconds=self.harbor_wall_timeout_seconds,
                cleanup_grace_seconds=self.cleanup_grace_seconds,
                verifier_timeout_multiplier=(self.verifier_settings.timeout_multiplier),
            )
        except (KeyError, TypeError) as exc:
            raise TB21HarborAuthorityError(
                "tb21_task_timeout_settings_invalid"
            ) from exc
        except ValueError as exc:
            raise TB21HarborAuthorityError(
                "tb21_harbor_timeout_budget_invalid"
            ) from exc

    def _materialize_task(
        self,
        task: TB2CampaignTask,
        destination: Path,
        image: ImmutableDockerImageIdentity,
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        pinned = self.snapshot.pinned_task_material(task.sample_slot.task_id)
        if (
            task.source_commit != self.snapshot.commit
            or task.repository_tree != self.snapshot.repository_tree
            or task.tasks_tree != self.snapshot.tasks_tree
            or task.snapshot_digest != self.snapshot.snapshot_digest
            or task.instruction != pinned.instruction
            or task.task_manifest != pinned.task_manifest
            or _configured_image(task.task_manifest) != image.configured_ref
        ):
            raise TB21HarborAuthorityError("tb21_task_request_drift")
        try:
            materialize_tb21_task(
                self.snapshot,
                task.sample_slot.task_id,
                destination,
            )
        except TB21SnapshotError as exc:
            raise TB21HarborInfraError("tb21_task_materialization_failed") from exc
        instruction_path = destination / "instruction.md"
        manifest_path = destination / "task.toml"
        try:
            if (
                instruction_path.read_bytes() != task.instruction
                or manifest_path.read_bytes() != task.task_manifest
            ):
                raise TB21HarborAuthorityError("tb21_task_archive_blob_mismatch")
            original = snapshot_task_materials(destination)
            rewritten = _rewrite_task_image(task.task_manifest, image)
            manifest_path.write_bytes(rewritten)
            runtime = snapshot_task_materials(destination)
        except (OSError, MaterialBoundaryError) as exc:
            raise TB21HarborInfraError("tb21_task_materialization_failed") from exc
        return (
            {
                "public_digest": original.public_digest,
                "evaluator_digest": original.evaluator_digest,
            },
            {
                "public_digest": runtime.public_digest,
                "evaluator_digest": runtime.evaluator_digest,
            },
        )

    def _command(
        self,
        *,
        candidate: GatewayCandidate,
        task: TB2CampaignTask,
        task_dir: Path,
        jobs_dir: Path,
        credential_handle: Path,
        nonce: str,
        task_public_digest: str,
        timeout_authority: TB2TaskTimeoutAuthority,
        image: ImmutableDockerImageIdentity,
        cell_id: str,
    ) -> tuple[str, ...]:
        command = [
            str(self.trusted_harbor_root / ".venv/bin/harbor"),
            "run",
            "-p",
            str(task_dir),
            "-m",
            self.model_name,
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
            "1.0",
            "--verifier-timeout-multiplier",
            str(self.verifier_settings.timeout_multiplier),
            "--agent-setup-timeout-multiplier",
            "1.0",
            "--environment-build-timeout-multiplier",
            "1.0",
            "-q",
            "-y",
        ]
        command += harbor_cli_agent_args(
            agent_ref=self.agent_ref,
            agent_config=self.agent_config,
            benchmark_id="tb2",
            candidate_checkout=candidate.checkout_path,
            trusted_harbor_root=self.trusted_harbor_root,
            codec="native_state",
            source="/workspace",
            nonce=nonce,
            candidate_tree_digest=candidate.tree_sha,
            task_public_digest=task_public_digest,
            bridge_identity_digest=self.bridge_identity_digest,
            envelope_key_file=self.envelope_key_file,
            worker_timeout_seconds=timeout_authority.bridge_rpc_deadline_seconds,
            cleanup_grace_seconds=self.cleanup_grace_seconds,
            bwrap_path=self.bwrap_path,
            credential_handle=credential_handle,
            credential_env_name=self.credential_env_name,
            provider_name=self.provider_name,
            provider_base_url=self.provider_base_url,
            provider_base_url_env_name=self.provider_base_url_env_name,
            reasoning_effort=self.reasoning_effort,
            settled_cost_stop_usd=self.settled_cost_stop_usd,
        )
        try:
            verifier_kwargs = offline_verifier_launch_kwargs(
                authority=self.offline_closure_authority,
                source_commit=self.snapshot.commit,
                task_id=task.sample_slot.task_id,
                run_id=cell_id,
                expected_agent_image_id=image.image_id,
                shared_run_root=self.offline_verifier_shared_root,
                tests_path=(
                    self.snapshot.tasks_root / task.sample_slot.task_id / "tests"
                ),
                docker=self.docker,
                docker_host=self.docker_host,
                verifier_timeout_seconds=(timeout_authority.verifier_deadline_seconds),
            )
        except (OSError, TB21OfflineVerifierError) as exc:
            raise TB21HarborAuthorityError(
                "tb21_offline_verifier_binding_invalid"
            ) from exc
        command.extend(("--verifier", OFFLINE_VERIFIER_IMPORT_PATH))
        for name, value in sorted(verifier_kwargs.items()):
            command.extend(("--verifier-kwarg", f"{name}={value}"))
        resolved_settings = self.verifier_settings.resolved_task_settings(
            task.sample_slot.task_id
        )
        task_adaptation = resolved_settings["task_adaptation"]
        task_env = (
            task_adaptation["verifier_env"]
            if isinstance(task_adaptation, Mapping)
            else None
        )
        command += harbor_verifier_env_args(self.verifier_env, task_env=task_env)
        return tuple(command)

    def _subprocess_env(self, cell_root: Path) -> dict[str, str]:
        env = {name: os.environ[name] for name in _BASE_ENV_NAMES if name in os.environ}
        env.update(
            {
                "HOME": str(cell_root / "home"),
                "TMPDIR": str(cell_root / "tmp"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                "PYTHONNOUSERSITE": "1",
                "DOCKER_HOST": self.docker_host,
            }
        )
        return env

    def _start_harbor_process(
        self,
        command: tuple[str, ...],
        *,
        cell_root: Path,
        output: Any,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            list(command),
            cwd=str(cell_root),
            env=self._subprocess_env(cell_root),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def _run_cell(
        self,
        candidate: GatewayCandidate,
        task: TB2CampaignTask,
        *,
        cell_root: Path,
        official_root: Path,
        state: dict[str, object],
    ) -> dict:
        image: ImmutableDockerImageIdentity | None = None
        try:
            self._assert_backend_identity()
            self._assert_controller_identity()
            self._assert_snapshot_identity()
            configured_ref = _configured_image(task.task_manifest)
            if self.approved_image_identities is None:
                if self.require_approved_image_identities:
                    raise TB21HarborAuthorityError("tb21_image_identity_unavailable")
                try:
                    image = resolve_immutable_docker_image(
                        self.docker,
                        configured_ref,
                        inspect=self._image_inspector,
                    )
                except HarborExecutionError as exc:
                    raise TB21HarborInfraError("tb21_image_resolution_failed") from exc
            else:
                image = self.approved_image_identities.get(configured_ref)
                if image is None:
                    raise TB21HarborAuthorityError("tb21_image_identity_unavailable")
                try:
                    revalidate_immutable_docker_image(
                        self.docker, image, inspect=self._image_inspector
                    )
                except HarborExecutionError as exc:
                    raise TB21HarborAuthorityError("tb21_image_identity_drift") from exc
            state["image_identity"] = image.as_dict()
            offline_binding = self.offline_closure_authority.binding(
                task.sample_slot.task_id
            )
            state["offline_verifier_binding"] = {
                "source_commit": self.offline_closure_authority.source_commit,
                "tasks_tree": self.offline_closure_authority.tasks_tree,
                "custom_verifier": OFFLINE_VERIFIER_IMPORT_PATH,
                "task_id": offline_binding.task_id,
                "family": offline_binding.family,
                "agent_image_id": image.image_id,
                "closure_base_image_id": offline_binding.closure_base_image_id,
                "closure_image_id": offline_binding.closure_image_id,
                "dynamic_candidate_uv": offline_binding.dynamic_candidate_uv,
            }

            task_dir = cell_root / "task"
            original_material, runtime_material = self._materialize_task(
                task, task_dir, image
            )
            state["original_task_material"] = dict(original_material)
            state["runtime_task_material"] = dict(runtime_material)
            timeout_authority = self._timeout_authority(task.sample_slot.task_id)
            state["resolved_timeouts"] = {
                "agent_deadline_seconds": (timeout_authority.agent_deadline_seconds),
                "verifier_deadline_seconds": (
                    timeout_authority.verifier_deadline_seconds
                ),
                "outer_deadline_seconds": timeout_authority.outer_deadline_seconds,
            }
            state["resolved_verifier_settings"] = (
                self.verifier_settings.resolved_task_settings(task.sample_slot.task_id)
            )
        except BaseException as primary:
            cleanup_failure = self._ensure_cell_cleanup(cell_root, state)
            post_errors: list[Exception] = []
            checks = [
                self._assert_controller_identity,
                self._assert_snapshot_identity,
                self._assert_backend_identity,
            ]
            if image is not None:
                checks.insert(
                    0,
                    lambda: revalidate_immutable_docker_image(
                        self.docker, image, inspect=self._image_inspector
                    ),
                )
            for check in checks:
                try:
                    check()
                except Exception as exc:
                    post_errors.append(exc)
            invariant = next(
                (
                    item
                    for item in (primary, *post_errors)
                    if isinstance(item, TB21HarborInvariantError)
                ),
                None,
            )
            if invariant is not None:
                raise invariant
            if post_errors:
                raise TB21HarborAuthorityError(
                    "tb21_post_use_revalidation_failed"
                ) from post_errors[0]
            if cleanup_failure is not None:
                raise cleanup_failure
            if not isinstance(primary, Exception):
                raise primary
            if isinstance(primary, TB21HarborBackendError):
                raise primary
            if isinstance(primary, OSError):
                raise TB21HarborInfraError("tb21_task_cell_setup_failed") from primary
            raise TB21HarborInvariantError("tb21_backend_opaque_exception") from primary

        if image is None:  # Defensive only; runtime resolution sets it.
            raise TB21HarborInvariantError("tb21_image_identity_missing")

        credential_handle: Path | None = None
        process: subprocess.Popen[bytes] | None = None
        primary: BaseException | None = None
        cleanup_error: Exception | None = None
        post_errors: list[Exception] = []
        jobs_dir = official_root / "harbor"
        harbor_log = official_root / "harbor.stdout.log"
        nonce = "tb21-cell-" + uuid.uuid4().hex
        marker = official_root / ".provenance-start"
        marker.write_text(uuid.uuid4().hex, encoding="ascii")
        not_before_ns = marker.stat().st_mtime_ns
        expected_offline_import: str | None = None
        expected_offline_kwargs: Mapping[str, object] | None = None
        try:
            credential_handle = stage_credential_handle(
                cell_root, self.credential_value
            )
            revalidate_immutable_docker_image(
                self.docker, image, inspect=self._image_inspector
            )
            self._assert_controller_identity()
            command = self._command(
                candidate=candidate,
                task=task,
                task_dir=task_dir,
                jobs_dir=jobs_dir,
                credential_handle=credential_handle,
                nonce=nonce,
                task_public_digest=str(original_material["public_digest"]),
                timeout_authority=timeout_authority,
                image=image,
                cell_id=str(state["cell_id"]),
            )
            (
                expected_offline_import,
                expected_offline_kwargs,
            ) = offline_verifier_launch_binding(command)
            with harbor_log.open("wb") as output:
                process = self._start_harbor_process(
                    command, cell_root=cell_root, output=output
                )
                try:
                    process.wait(timeout=timeout_authority.outer_deadline_seconds)
                except subprocess.TimeoutExpired as exc:
                    raise TB21HarborInfraError("tb21_harbor_timeout") from exc
        except BaseException as exc:
            primary = exc
        finally:
            remove_credential_handle(credential_handle)
            state["cleanup_attempted"] = True
            try:
                if process is not None and process.returncode is None:
                    terminate_process_group_and_cleanup(
                        process,
                        docker=self.docker,
                        cell_root=cell_root,
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                        docker_host=self.docker_host,
                    )
                else:
                    cleanup_scoped_compose_cell(
                        self.docker,
                        cell_root,
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                        docker_host=self.docker_host,
                    )
                state["cleanup_vanish_verified"] = True
            except Exception as exc:
                cleanup_error = exc
            for check in (
                lambda: revalidate_immutable_docker_image(
                    self.docker, image, inspect=self._image_inspector
                ),
                self._assert_controller_identity,
                self._assert_snapshot_identity,
                self._assert_backend_identity,
            ):
                try:
                    check()
                except Exception as exc:
                    post_errors.append(exc)

        invariant = next(
            (
                item
                for item in post_errors
                if isinstance(item, TB21HarborInvariantError)
            ),
            None,
        )
        if invariant is not None:
            raise invariant
        if post_errors:
            raise TB21HarborAuthorityError(
                "tb21_post_use_revalidation_failed"
            ) from post_errors[0]
        if cleanup_error is not None:
            raise TB21HarborInfraError("tb21_harbor_cleanup_failed") from cleanup_error
        if primary is not None:
            if isinstance(primary, BaseException) and not isinstance(
                primary, Exception
            ):
                raise primary
            if isinstance(primary, TB21HarborBackendError):
                raise primary
            if isinstance(primary, HarborExecutionError):
                raise TB21HarborInfraError("tb21_credential_setup_failed") from primary
            if isinstance(primary, OSError):
                raise TB21HarborInfraError("tb21_harbor_launch_failed") from primary
            raise TB21HarborInvariantError("tb21_backend_opaque_exception") from primary

        try:
            current_material = snapshot_task_materials(task_dir)
        except MaterialBoundaryError as exc:
            raise TB21HarborAuthorityError("tb21_runtime_task_material_drift") from exc
        if (
            current_material.public_digest != runtime_material["public_digest"]
            or current_material.evaluator_digest != runtime_material["evaluator_digest"]
            or (task_dir / "instruction.md").read_bytes() != task.instruction
            or _configured_image((task_dir / "task.toml").read_bytes())
            != image.runtime_ref
        ):
            raise TB21HarborAuthorityError("tb21_runtime_task_material_drift")
        config = _task_config((task_dir / "task.toml").read_bytes())
        raw_task = config.get("task")
        task_name = (
            raw_task.get("name", task.sample_slot.task_id)
            if isinstance(raw_task, Mapping)
            else task.sample_slot.task_id
        )
        if not isinstance(task_name, str) or not task_name:
            raise TB21HarborAuthorityError("tb21_task_name_invalid")
        if process is not None and process.returncode < 0:
            raise TB21HarborInfraError("tb21_harbor_nonzero_exit")
        # Credential evidence is a hard boundary even when Harbor itself
        # exited nonzero.  Scan the bounded known artifacts before attempting
        # native-state terminal admission.
        self._artifact_refs(official_root)
        try:
            trial = load_validated_trial_result(
                jobs_dir / "cell",
                expected_task_path=task_dir,
                expected_task_name=task_name,
                expected_agent_name=AGENT_IMPORT_PATH,
                expected_model=self.model_name,
                not_before_ns=not_before_ns,
                require_separate_verifier=False,
                tb2_native_state_verifier_only_authority=True,
            )
        except ResultAuthorityError as exc:
            raise TB21HarborAuthorityError("tb21_evaluator_authority_mismatch") from exc
        except RewardCtrfConflictError as exc:
            raise TB21HarborEvaluatorError("tb21_reward_ctrf_conflict") from exc
        except ResultProvenanceError as exc:
            if process is not None and process.returncode != 0:
                raise TB21HarborInfraError("tb21_harbor_nonzero_exit") from exc
            raise TB21HarborEvaluatorError("tb21_untrusted_grader_result") from exc
        if expected_offline_import is None or expected_offline_kwargs is None:
            raise TB21HarborInvariantError("tb21_untrusted_grader_result")
        try:
            admitted_offline = admit_offline_verifier(
                jobs_dir=jobs_dir,
                trial=trial,
                expected_import_path=expected_offline_import,
                expected_kwargs=expected_offline_kwargs,
                not_before_ns=not_before_ns,
            )
        except ResultProvenanceError as exc:
            if process is not None and process.returncode != 0:
                raise TB21HarborInfraError("tb21_harbor_nonzero_exit") from exc
            raise TB21HarborInfraError("tb21_untrusted_grader_result") from exc
        state["offline_verifier_admission"] = {
            "schema": admitted_offline["schema"],
            "task_id": admitted_offline["task_id"],
            "run_id": admitted_offline["run_id"],
            "network_mode": admitted_offline["network_mode"],
            "ctrf": admitted_offline["ctrf"],
            "reward": admitted_offline["reward"],
        }
        return trial

    def _artifact_refs(
        self,
        official_root: Path,
        *,
        require_complete: bool = False,
    ) -> dict[str, str]:
        candidates: dict[str, Path] = {
            "harbor_log": official_root / "harbor.stdout.log",
        }
        required = {"result", "reward", "ctrf", "offline_verifier"}
        job_dir = official_root / "harbor" / "cell"
        if job_dir.is_dir() and not job_dir.is_symlink():
            try:
                trial = _trial_directory(job_dir)
            except TB21HarborInvariantError:
                if require_complete:
                    raise
                trial = None
            if trial is not None:
                candidates.update(
                    {
                        "terminal": trial / "agent" / "agent-context.json",
                        "trajectory": trial / "agent" / "trajectory.json",
                        "pane": trial / "agent" / "terminus_2.pane",
                        "verifier": trial / "verifier" / "test-stdout.txt",
                        "result": trial / "result.json",
                        "reward": trial / "verifier" / "reward.txt",
                        "ctrf": trial / "verifier" / "ctrf.json",
                        "offline_verifier": (
                            trial / "verifier" / "offline-verifier-provenance.json"
                        ),
                    }
                )
        existing = tuple(
            path for path in candidates.values() if path.exists() or path.is_symlink()
        )
        try:
            scrub_credential_literal_files(
                official_root, existing, self.credential_value
            )
        except HarborExecutionError as exc:
            raise TB21HarborInvariantError("tb21_credential_found_in_evidence") from exc
        refs: dict[str, str] = {}
        for name, path in candidates.items():
            if not path.exists() and not path.is_symlink():
                continue
            verified = _validated_regular_file(path, self.layout.root)
            refs[f"{name}_ref"] = verified.relative_to(
                self.layout.root.resolve(strict=True)
            ).as_posix()
            refs[f"{name}_digest"] = file_digest(verified)
        if require_complete and any(
            f"{name}_ref" not in refs or f"{name}_digest" not in refs
            for name in required
        ):
            raise TB21HarborInvariantError("tb21_required_evidence_missing")
        return refs

    def _ensure_cell_cleanup(
        self, cell_root: Path, state: dict[str, object]
    ) -> TB21HarborInfraError | None:
        """Cover failures that happened before _run_cell reached its finally."""

        if state.get("cleanup_attempted") is True:
            return None
        state["cleanup_attempted"] = True
        try:
            cleanup_scoped_compose_cell(
                self.docker,
                cell_root,
                cleanup_grace_seconds=self.cleanup_grace_seconds,
                docker_host=self.docker_host,
            )
        except Exception as exc:
            return TB21HarborInfraError("tb21_harbor_cleanup_failed", str(exc))
        state["cleanup_vanish_verified"] = True
        return None

    def _publish_evidence(
        self,
        *,
        official_root: Path,
        state: Mapping[str, object],
        verdict: str,
        failure_kind: str | None,
        error_type: str | None,
        reward: float | None,
    ) -> dict[str, str]:
        refs = self._artifact_refs(
            official_root,
            require_complete=verdict in {"passed", "failed"},
        )
        evidence_path = official_root / "tb21-harbor-evidence.json"
        try:
            atomic_write_json(
                evidence_path,
                {
                    "schema": "tb21-harbor-cell-evidence",
                    "backend_digest": self.backend_digest,
                    "controller": self.controller.as_dict(),
                    "bridge_identity_digest": self.bridge_identity_digest,
                    "provider_route": {
                        "provider": self.provider_name,
                        "base_url": self.provider_base_url,
                        "base_url_env_name": self.provider_base_url_env_name,
                        "reasoning_effort": self.reasoning_effort,
                    },
                    "cell_id": state["cell_id"],
                    "task_id": state["task_id"],
                    "sample_index": state["sample_index"],
                    "image_identity": state.get("image_identity"),
                    "original_task_material": state.get("original_task_material"),
                    "runtime_task_material": state.get("runtime_task_material"),
                    "resolved_timeouts": state.get("resolved_timeouts"),
                    "resolved_verifier_settings": state.get(
                        "resolved_verifier_settings"
                    ),
                    "offline_verifier_binding": state.get("offline_verifier_binding"),
                    "offline_verifier_admission": state.get(
                        "offline_verifier_admission"
                    ),
                    "verdict": verdict,
                    "failure_kind": failure_kind,
                    "error_type": error_type,
                    "official_reward": reward,
                    "cleanup_vanish_verified": state.get(
                        "cleanup_vanish_verified", False
                    ),
                    "artifact_digests": {
                        key: value
                        for key, value in refs.items()
                        if key.endswith("_digest")
                    },
                    "exception_detail_recorded": False,
                },
            )
            verified = _validated_regular_file(evidence_path, self.layout.root)
            refs["evidence_ref"] = verified.relative_to(
                self.layout.root.resolve(strict=True)
            ).as_posix()
        except (OSError, TB21HarborInvariantError):
            _LOG.warning(
                "TB2 Harbor auxiliary evidence dropped: task=%s",
                state["task_id"],
            )
        return refs

    def _usage(self, trial: Mapping[str, object]) -> dict[str, object]:
        agent = trial.get("agent_result")
        agent_result = agent if isinstance(agent, Mapping) else {}
        input_tokens = _integer_usage(agent_result.get("n_input_tokens"))
        output_tokens = _integer_usage(agent_result.get("n_output_tokens"))
        cache_tokens = _integer_usage(agent_result.get("n_cache_tokens"))
        input_status = "reported" if input_tokens is not None else "missing"
        output_status = "reported" if output_tokens is not None else "missing"
        cache_status = "reported" if cache_tokens is not None else "missing"
        token_status = (
            "reported"
            if input_tokens is not None and output_tokens is not None
            else "unverified"
        )
        cost = agent_result.get("cost_usd")
        cost_usd = (
            float(cost)
            if isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(float(cost))
            and float(cost) >= 0.0
            else None
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_tokens": cache_tokens,
            "token_status": token_status,
            "input_token_status": input_status,
            "output_token_status": output_status,
            "cache_token_status": cache_status,
            "cost_usd": cost_usd,
            "cost_status": "reported" if cost_usd is not None else "missing",
            "calls": [dict(call) for call in provider_calls_from_trial(trial)],
        }

    def execute(
        self,
        candidate: GatewayCandidate,
        task: TB2CampaignTask,
    ) -> TB2TaskOutcome:
        self._resolve_runtime()
        started = time.monotonic()
        cell_id = "tb21-harbor-cell-" + uuid.uuid4().hex
        cell_root = self.layout.trusted_runtime_root / "tb21-harbor-cells" / cell_id
        official_root = (
            self.layout.root
            / "results"
            / "tb21-harbor"
            / task.sample_slot.task_id
            / cell_id
        )
        state: dict[str, object] = {
            "cell_id": cell_id,
            "task_id": task.sample_slot.task_id,
            "sample_index": task.sample_slot.sample_index,
            "cleanup_attempted": False,
            "cleanup_vanish_verified": False,
        }
        try:
            for directory in (
                cell_root,
                cell_root / "home",
                cell_root / "tmp",
                official_root,
            ):
                directory.mkdir(mode=0o700, parents=True)
            trial = self._run_cell(
                candidate,
                task,
                cell_root=cell_root,
                official_root=official_root,
                state=state,
            )
            verifier = trial.get("verifier_result")
            rewards = verifier.get("rewards") if isinstance(verifier, Mapping) else None
            raw_reward = (
                rewards.get("reward", next(iter(rewards.values())))
                if isinstance(rewards, Mapping) and rewards
                else None
            )
            if (
                not isinstance(raw_reward, (int, float))
                or isinstance(raw_reward, bool)
                or not math.isfinite(float(raw_reward))
                or not 0.0 <= float(raw_reward) <= 1.0
            ):
                raise TB21HarborEvaluatorError("tb21_grader_reward_invalid")
            reward = float(raw_reward)
            verdict = "passed" if reward >= self.pass_reward_threshold else "failed"
            failure_kind = None if verdict == "passed" else "candidate"
            error_type = (
                None if verdict == "passed" else "official_tb21_reward_below_threshold"
            )
            refs = self._publish_evidence(
                official_root=official_root,
                state=state,
                verdict=verdict,
                failure_kind=failure_kind,
                error_type=error_type,
                reward=reward,
            )
            usage = self._usage(trial)
            usage_provenance = {
                "role": "solver-agent",
                "provider": self.provider_name,
                "model": self.model_name,
                "token_source": "provider",
                "input_token_status": usage["input_token_status"],
                "output_token_status": usage["output_token_status"],
                "cache_token_status": usage["cache_token_status"],
                "cache_status": (
                    "reported_undifferentiated"
                    if usage["cache_tokens"] is not None
                    else "unknown"
                ),
                "cost_status": usage["cost_status"],
                "calls": [
                    {
                        "role": "solver-agent",
                        "provider": self.provider_name,
                        "model": self.model_name,
                        **dict(call),
                    }
                    for call in usage["calls"]
                ],
            }
            candidate_runtime_failure = _candidate_runtime_failure_for_public_feedback(
                trial,
                reward=reward,
                credential_literal=self.credential_value,
                task_id=task.sample_slot.task_id,
            )
            outcome_provenance: dict[str, object] = {
                "schema": "tb21-harbor-task-outcome",
                "backend_digest": self.backend_digest,
                "provider_route": {
                    "provider": self.provider_name,
                    "base_url": self.provider_base_url,
                    "base_url_env_name": self.provider_base_url_env_name,
                    "reasoning_effort": self.reasoning_effort,
                },
                "cell_id": cell_id,
                "official_reward": reward,
                "image_identity": state.get("image_identity"),
                "cleanup_vanish_verified": state["cleanup_vanish_verified"],
                "usage": usage_provenance,
            }
            if candidate_runtime_failure is not None:
                outcome_provenance[CANDIDATE_RUNTIME_FAILURE_KEY] = (
                    candidate_runtime_failure
                )
            return TB2TaskOutcome(
                sample_slot=task.sample_slot,
                verdict=verdict,
                failure_kind=failure_kind,
                error_type=error_type,
                wall_seconds=time.monotonic() - started,
                artifact_refs=MappingProxyType(refs),
                provenance=MappingProxyType(outcome_provenance),
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_tokens=usage["cache_tokens"],
                token_status=str(usage["token_status"]),
                input_token_status=str(usage["input_token_status"]),
                output_token_status=str(usage["output_token_status"]),
                cache_token_status=str(usage["cache_token_status"]),
                cost_usd=usage["cost_usd"],
                cost_status=str(usage["cost_status"]),
            )
        except TB21HarborBackendError as exc:
            authority_failure = isinstance(exc, TB21HarborAuthorityError)
            cleanup_failure = self._ensure_cell_cleanup(cell_root, state)
            if cleanup_failure is not None and not authority_failure:
                exc = cleanup_failure
            failure_kind = (
                "evaluator"
                if isinstance(exc, TB21HarborEvaluatorError)
                else (
                    "infra"
                    if isinstance(exc, TB21HarborAuthorityError)
                    else (
                        "backend"
                        if isinstance(exc, TB21HarborInvariantError)
                        else "infra"
                    )
                )
            )
            try:
                refs = self._publish_evidence(
                    official_root=official_root,
                    state=state,
                    verdict="inconclusive",
                    failure_kind=failure_kind,
                    error_type=exc.error_type,
                    reward=None,
                )
            except Exception:
                refs = {}
                failure_kind = "backend"
                if not isinstance(exc, TB21HarborAuthorityError):
                    exc = TB21HarborInvariantError("tb21_evidence_publication_failed")
            return TB2TaskOutcome(
                sample_slot=task.sample_slot,
                verdict="inconclusive",
                failure_kind=failure_kind,
                error_type=exc.error_type,
                wall_seconds=time.monotonic() - started,
                artifact_refs=MappingProxyType(refs),
                provenance=MappingProxyType(
                    {
                        "schema": "tb21-harbor-task-outcome",
                        "backend_digest": self.backend_digest,
                        "cell_id": cell_id,
                        "image_identity": state.get("image_identity"),
                        "exception_type": type(exc).__name__,
                        "exception_detail_recorded": False,
                    }
                ),
            )
        except Exception as exc:
            cleanup_failure = self._ensure_cell_cleanup(cell_root, state)
            if cleanup_failure is not None:
                failure_kind = "infra"
                error_type = cleanup_failure.error_type
            else:
                failure_kind = "backend"
                error_type = "tb21_backend_opaque_exception"
            return TB2TaskOutcome(
                sample_slot=task.sample_slot,
                verdict="inconclusive",
                failure_kind=failure_kind,
                error_type=error_type,
                wall_seconds=time.monotonic() - started,
                provenance=MappingProxyType(
                    {
                        "schema": "tb21-harbor-task-outcome",
                        "backend_digest": self.backend_digest,
                        "cell_id": cell_id,
                        "exception_type": type(exc).__name__,
                        "exception_detail_recorded": False,
                    }
                ),
            )


__all__ = [
    "TB21HarborAuthorityError",
    "TB21HarborExecutionBackend",
    "admit_offline_verifier",
    "admit_offline_verifier_job",
    "offline_verifier_launch_binding",
]
