"""Fail-closed provenance checks for one-cell Harbor artifacts.

The guarded Harbor path rejects stale/pre-planted output, ambiguous trial
selection, path substitution, and the common result.json-only forgery, but it
does not authenticate a writer that shares the host process.  The physical
bridge path additionally requires a fresh separate verifier and a typed
artifact envelope signed by the trusted host after the candidate is stopped.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    AGENT_IMPORT_PATH,
    AgentContextValidationError,
    ArtifactValidationError,
    PROVIDER_ACCOUNTING_METADATA_KEY,
    candidate_runtime_failure_from_context,
    verify_signed_envelope,
)
from rsi_arena.benchmarks.harbor_runtime.trial_facts import (
    ResultProvenanceError as ProviderDiagnosticsProvenanceError,
)
from rsi_arena.benchmarks.harbor_runtime.trial_facts import (
    _load_provider_diagnostics,
)


class ResultProvenanceError(ValueError):
    """Recorded Harbor artifacts do not prove a fresh, consistent cell."""


class ResultAuthorityError(ResultProvenanceError):
    """The Harbor record disagrees with evaluator-wide launch authority."""


class RewardCtrfConflictError(ResultProvenanceError):
    """Trusted reward and clear CTRF terminal evidence disagree."""


class CostAccountingProvenanceError(ResultProvenanceError):
    """Token accounting is valid, but provider cost is unverified."""

    def __init__(self, message: str, *, tokens_out: int) -> None:
        self.tokens_out = int(tokens_out)
        super().__init__(message)


@dataclass(frozen=True)
class ArtifactEnvelopeExpectation:
    """Trusted values that bind one cell's typed bridge artifact."""

    nonce: str
    codec: str
    payload_name: str
    candidate_tree_digest: str
    task_public_digest: str
    bridge_identity_digest: str
    hmac_key: bytes


_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "nonce",
        "codec",
        "payload_name",
        "payload_sha256",
        "payload_size_bytes",
        "candidate_tree_digest",
        "task_public_digest",
        "bridge_identity_digest",
        "hmac_sha256",
    }
)

_MAX_PROVENANCE_JSON_BYTES = 16 * 1024 * 1024
_MAX_CTRF_BYTES = 4 * 1024 * 1024
_MAX_REWARD_BYTES = 128


def _fresh_regular_file(path: Path, *, not_before_ns: int) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ResultProvenanceError(f"missing provenance file: {path.name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ResultProvenanceError(
            f"provenance path is not a regular file: {path.name}"
        )
    if info.st_mtime_ns < not_before_ns:
        raise ResultProvenanceError(f"stale provenance file: {path.name}")
    return info


def _digest_fresh_regular_file(path: Path, *, not_before_ns: int) -> tuple[int, str]:
    """Stream a fresh regular file through a no-follow descriptor."""

    _fresh_regular_file(path, not_before_ns=not_before_ns)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ResultProvenanceError(f"unreadable provenance file: {path.name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mtime_ns < not_before_ns:
            raise ResultProvenanceError(
                f"provenance path is not a fresh regular file: {path.name}"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or size != after.st_size:
            raise ResultProvenanceError(
                f"provenance file changed while being read: {path.name}"
            )
        return size, "sha256:" + digest.hexdigest()
    finally:
        os.close(fd)


def _load_json(
    path: Path,
    *,
    not_before_ns: int,
    max_bytes: int = _MAX_PROVENANCE_JSON_BYTES,
) -> dict:
    info = _fresh_regular_file(path, not_before_ns=not_before_ns)
    if info.st_size > max_bytes:
        raise ResultProvenanceError(f"provenance JSON is too large: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultProvenanceError(f"unreadable provenance JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ResultProvenanceError(f"provenance JSON is not an object: {path.name}")
    return value


def _same_path(raw, expected: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    return Path(raw).expanduser().resolve() == expected


def _single_dict(items, *, label: str) -> dict:
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ResultProvenanceError(f"expected exactly one {label}")
    return items[0]


def _finite_number(value, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultProvenanceError(f"{label} is not numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ResultProvenanceError(f"{label} is not finite")
    return parsed


def _extract_reward(trial: dict) -> float:
    verifier = trial.get("verifier_result")
    if not isinstance(verifier, dict):
        raise ResultProvenanceError("trial has no verifier_result")
    rewards = verifier.get("rewards")
    if not isinstance(rewards, dict) or not rewards:
        raise ResultProvenanceError("trial has no verifier rewards")
    value = rewards.get("reward", next(iter(rewards.values())))
    reward = _finite_number(value, label="verifier reward")
    if not 0.0 <= reward <= 1.0:
        raise ResultProvenanceError("verifier reward is outside the [0, 1] scale")
    return reward


def _validate_ctrf_reward(ctrf: Mapping[str, object], reward: float) -> None:
    """Reject only a clear reward/CTRF terminal contradiction.

    CTRF producers may omit detailed summaries, so absence remains neutral.
    When pytest-json-ctrf supplies its standard integer summary, an observed
    failure proves a zero and an all-passed suite proves a one.
    """

    results = ctrf.get("results")
    summary = results.get("summary") if isinstance(results, Mapping) else None
    if not isinstance(summary, Mapping):
        return
    tests = summary.get("tests")
    passed = summary.get("passed")
    failed = summary.get("failed")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (tests, passed, failed)
    ):
        return
    ctrf_reward: float | None = None
    if failed > 0:
        ctrf_reward = 0.0
    elif tests > 0 and passed == tests:
        ctrf_reward = 1.0
    if ctrf_reward is not None and reward != ctrf_reward:
        raise RewardCtrfConflictError(
            "verifier reward conflicts with the CTRF terminal summary"
        )


def _normalized_native_state_accounting(trial: dict, stats: dict) -> dict:
    """Keep only mutually consistent optional Harbor accounting observations."""

    raw_agent_result = trial.get("agent_result")
    if not isinstance(raw_agent_result, Mapping):
        return {**trial, "agent_result": None}
    agent_result = dict(raw_agent_result)
    for name in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
        trial_value = agent_result.get(name)
        job_value = stats.get(name)
        matching_value: int | None = None
        if (
            isinstance(trial_value, (int, float))
            and not isinstance(trial_value, bool)
            and isinstance(job_value, (int, float))
            and not isinstance(job_value, bool)
            and math.isfinite(float(trial_value))
            and math.isfinite(float(job_value))
            and float(trial_value) >= 0.0
            and float(job_value) >= 0.0
            and float(trial_value).is_integer()
            and float(job_value).is_integer()
            and int(trial_value) == int(job_value)
        ):
            matching_value = int(trial_value)
        agent_result[name] = matching_value

    trial_cost = agent_result.get("cost_usd")
    job_cost = stats.get("cost_usd")
    matching_cost: float | None = None
    if (
        isinstance(trial_cost, (int, float))
        and not isinstance(trial_cost, bool)
        and isinstance(job_cost, (int, float))
        and not isinstance(job_cost, bool)
        and math.isfinite(float(trial_cost))
        and math.isfinite(float(job_cost))
        and float(trial_cost) >= 0.0
        and float(job_cost) >= 0.0
        and math.isclose(
            float(trial_cost),
            float(job_cost),
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
    ):
        matching_cost = float(trial_cost)
    agent_result["cost_usd"] = matching_cost
    return {**trial, "agent_result": agent_result}


def load_validated_artifact_envelope(
    trial_dir: Path,
    *,
    expectation: ArtifactEnvelopeExpectation,
    not_before_ns: int,
) -> dict:
    """Authenticate the bridge envelope and the exact payload it names."""

    convention_dir = Path(trial_dir) / "artifacts" / "logs" / "artifacts"
    envelope_path = convention_dir / "rsi-arena-envelope.json"
    envelope = _load_json(envelope_path, not_before_ns=not_before_ns)
    if set(envelope) != _ENVELOPE_FIELDS:
        raise ResultProvenanceError("artifact envelope has missing or unknown fields")
    try:
        unsigned = verify_signed_envelope(envelope, expectation.hmac_key)
    except (ArtifactValidationError, TypeError, ValueError) as exc:
        raise ResultProvenanceError("artifact envelope authentication failed") from exc

    expected_fields: Mapping[str, str] = {
        "nonce": expectation.nonce,
        "codec": expectation.codec,
        "payload_name": expectation.payload_name,
        "candidate_tree_digest": expectation.candidate_tree_digest,
        "task_public_digest": expectation.task_public_digest,
        "bridge_identity_digest": expectation.bridge_identity_digest,
    }
    for name, expected in expected_fields.items():
        if unsigned.get(name) != expected:
            raise ResultProvenanceError(
                f"artifact envelope {name} does not match the scheduled cell"
            )

    payload_name = unsigned.get("payload_name")
    if (
        not isinstance(payload_name, str)
        or not payload_name
        or payload_name in {".", ".."}
        or "/" in payload_name
        or "\\" in payload_name
    ):
        raise ResultProvenanceError("artifact envelope payload name is unsafe")
    payload_path = convention_dir / payload_name
    actual_size, digest = _digest_fresh_regular_file(
        payload_path, not_before_ns=not_before_ns
    )
    payload_size = unsigned.get("payload_size_bytes")
    if (
        isinstance(payload_size, bool)
        or not isinstance(payload_size, int)
        or payload_size < 0
        or payload_size != actual_size
    ):
        raise ResultProvenanceError("artifact payload size does not match envelope")
    if unsigned.get("payload_sha256") != digest:
        raise ResultProvenanceError("artifact payload digest does not match envelope")

    manifest_path = Path(trial_dir) / "artifacts" / "manifest.json"
    _fresh_regular_file(manifest_path, not_before_ns=not_before_ns)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultProvenanceError("unreadable Harbor artifact manifest") from exc
    # Harbor writes ArtifactManifest.to_json_data() as a top-level list.
    if not isinstance(manifest, list) or not any(
        isinstance(entry, dict)
        and entry.get("source") == "/logs/artifacts"
        and entry.get("status") == "ok"
        for entry in manifest
    ):
        raise ResultProvenanceError(
            "Harbor artifact manifest does not record the convention transfer"
        )
    return envelope


def load_validated_trial_result(
    job_dir: Path,
    *,
    expected_task_path: Path,
    expected_task_name: str,
    expected_agent_name: str,
    expected_model: str,
    not_before_ns: int,
    require_separate_verifier: bool = False,
    artifact_expectation: ArtifactEnvelopeExpectation | None = None,
    native_state_verifier_only_authority: bool = False,
) -> dict:
    """Load the unique trial only when the whole Harbor record is coherent."""
    job_dir = Path(job_dir)
    expected_task_path = Path(expected_task_path).expanduser().resolve()
    try:
        info = job_dir.lstat()
    except FileNotFoundError as exc:
        raise ResultProvenanceError("Harbor job directory is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ResultProvenanceError("Harbor job path is not a real directory")

    job_config = _load_json(job_dir / "config.json", not_before_ns=not_before_ns)
    job_lock = _load_json(job_dir / "lock.json", not_before_ns=not_before_ns)
    job_result = _load_json(job_dir / "result.json", not_before_ns=not_before_ns)

    if (
        job_config.get("job_name") != "cell"
        or job_config.get("n_concurrent_trials") != 1
    ):
        raise ResultProvenanceError("job configuration is not a one-cell run")
    config_task = _single_dict(job_config.get("tasks"), label="configured task")
    config_agent = _single_dict(job_config.get("agents"), label="configured agent")
    if not _same_path(config_task.get("path"), expected_task_path):
        raise ResultProvenanceError("job task path does not match scheduled task")
    if config_agent.get("name") != expected_agent_name:
        raise ResultAuthorityError("job agent does not match scheduled agent")
    if config_agent.get("model_name") != expected_model:
        raise ResultAuthorityError("job model does not match scheduled model")

    lock_trial = _single_dict(job_lock.get("trials"), label="locked trial")
    lock_task = lock_trial.get("task")
    lock_agent = lock_trial.get("agent")
    if not isinstance(lock_task, dict) or not isinstance(lock_agent, dict):
        raise ResultProvenanceError("lock omits task or agent identity")
    if not _same_path(lock_task.get("path"), expected_task_path):
        raise ResultProvenanceError("locked task path does not match scheduled task")
    digest = lock_task.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ResultProvenanceError("locked task has no content digest")
    if lock_agent.get("name") != expected_agent_name:
        raise ResultAuthorityError("locked agent does not match scheduled agent")
    if lock_agent.get("model_name") != expected_model:
        raise ResultAuthorityError("locked model does not match scheduled model")
    verifier_lock = lock_trial.get("verifier")
    if not isinstance(verifier_lock, dict) or verifier_lock.get("disable") is not False:
        raise ResultAuthorityError("verification was disabled in the locked trial")
    if (
        require_separate_verifier
        and verifier_lock.get("environment_mode") != "separate"
    ):
        raise ResultAuthorityError(
            "locked trial did not use a separate verifier environment"
        )

    try:
        job_id = str(UUID(str(job_result.get("id"))))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ResultProvenanceError("job result has no valid id") from exc
    stats = job_result.get("stats")
    if not isinstance(stats, dict):
        raise ResultProvenanceError("job result has no stats")
    expected_counts = {
        "n_completed_trials": 1,
        "n_running_trials": 0,
        "n_pending_trials": 0,
        "n_cancelled_trials": 0,
    }
    if any(stats.get(name) != value for name, value in expected_counts.items()):
        raise ResultProvenanceError("job did not finish exactly one trial")
    n_errored_trials = stats.get("n_errored_trials")
    if not native_state_verifier_only_authority and n_errored_trials not in (0, 1):
        raise ResultProvenanceError("job has an unsupported errored-trial count")

    trial_dirs = []
    for child in job_dir.iterdir():
        try:
            child_info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(child_info.st_mode):
            raise ResultProvenanceError("symlink found in Harbor job directory")
        if stat.S_ISDIR(child_info.st_mode) and (child / "result.json").exists():
            trial_dirs.append(child)
    if len(trial_dirs) != 1:
        raise ResultProvenanceError("job does not contain exactly one trial result")
    trial_dir = trial_dirs[0]
    trial = _load_json(trial_dir / "result.json", not_before_ns=not_before_ns)
    trial_config = _load_json(trial_dir / "config.json", not_before_ns=not_before_ns)
    _load_json(trial_dir / "lock.json", not_before_ns=not_before_ns)

    if expected_agent_name == AGENT_IMPORT_PATH:
        try:
            provider_facts = _load_provider_diagnostics(
                trial_dir, not_before_ns=not_before_ns
            )
        except ProviderDiagnosticsProvenanceError as exc:
            raise ResultProvenanceError(
                "trusted provider diagnostics are invalid"
            ) from exc
        if provider_facts is None:
            raise ResultProvenanceError(
                "trusted provider diagnostics are required for the isolated agent"
            )
        raw_provider, _, _, _, provider_calls = provider_facts
        agent_result = trial.get("agent_result")
        if isinstance(agent_result, Mapping):
            normalized_agent = dict(agent_result)
            metadata = normalized_agent.get("metadata")
            normalized_metadata = (
                dict(metadata) if isinstance(metadata, Mapping) else {}
            )
            normalized_metadata[PROVIDER_ACCOUNTING_METADATA_KEY] = {
                "accounting": raw_provider["accounting"],
                "reliability": raw_provider["provider"],
                "calls": [call.as_dict() for call in provider_calls],
            }
            normalized_agent["metadata"] = normalized_metadata
            trial = {**trial, "agent_result": normalized_agent}

    if trial.get("trial_name") != trial_dir.name:
        raise ResultProvenanceError("trial name does not match its directory")
    if trial.get("task_name") != expected_task_name:
        raise ResultProvenanceError("trial task name does not match scheduled task")
    if (
        require_separate_verifier
        and trial.get("verifier_environment_mode") != "separate"
    ):
        raise ResultAuthorityError(
            "trial result did not record a separate verifier environment"
        )
    embedded = trial.get("config")
    if not isinstance(embedded, dict):
        raise ResultProvenanceError("trial result omits its configuration")
    embedded_task = embedded.get("task")
    embedded_agent = embedded.get("agent")
    if not isinstance(embedded_task, dict) or not isinstance(embedded_agent, dict):
        raise ResultProvenanceError("trial configuration omits task or agent")
    if not _same_path(embedded_task.get("path"), expected_task_path):
        raise ResultProvenanceError("trial task path does not match scheduled task")
    if embedded_agent.get("name") != expected_agent_name:
        raise ResultAuthorityError("trial agent does not match scheduled agent")
    if embedded_agent.get("model_name") != expected_model:
        raise ResultAuthorityError("trial model does not match scheduled model")
    if str(embedded.get("job_id")) != job_id:
        raise ResultProvenanceError("trial is not bound to the recorded job")

    # The standalone trial config is a second Harbor-written record.  Binding
    # it too catches result.json-only fabrication or path substitution.
    standalone_task = trial_config.get("task")
    standalone_agent = trial_config.get("agent")
    if not isinstance(standalone_task, dict) or not isinstance(standalone_agent, dict):
        raise ResultProvenanceError("standalone trial config omits task or agent")
    if not _same_path(standalone_task.get("path"), expected_task_path):
        raise ResultProvenanceError("standalone trial task path mismatch")
    if standalone_agent.get("name") != expected_agent_name:
        raise ResultAuthorityError("standalone trial agent mismatch")
    if standalone_agent.get("model_name") != expected_model:
        raise ResultAuthorityError("standalone trial model mismatch")
    if str(trial_config.get("job_id")) != job_id:
        raise ResultProvenanceError("standalone trial is not bound to the job")

    reward = _extract_reward(trial)
    reward_path = trial_dir / "verifier" / "reward.txt"
    reward_info = _fresh_regular_file(reward_path, not_before_ns=not_before_ns)
    if reward_info.st_size > _MAX_REWARD_BYTES:
        raise ResultProvenanceError("verifier reward artifact is too large")
    try:
        verifier_reward = float(reward_path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ResultProvenanceError("verifier reward artifact is unreadable") from exc
    if not math.isfinite(verifier_reward) or verifier_reward != reward:
        raise ResultProvenanceError(
            "result reward does not match verifier reward artifact"
        )

    if native_state_verifier_only_authority:
        if require_separate_verifier or artifact_expectation is not None:
            raise ResultAuthorityError(
                "native-state authority cannot replace a typed artifact boundary"
            )
        ctrf = _load_json(
            trial_dir / "verifier" / "ctrf.json",
            not_before_ns=not_before_ns,
            max_bytes=_MAX_CTRF_BYTES,
        )
        _validate_ctrf_reward(ctrf, reward)
        return _normalized_native_state_accounting(trial, stats)

    exception_info = trial.get("exception_info")
    agent_result = trial.get("agent_result")
    try:
        candidate_failure = candidate_runtime_failure_from_context(agent_result)
    except AgentContextValidationError as exc:
        raise ResultProvenanceError(
            "candidate runtime failure marker is invalid"
        ) from exc
    if candidate_failure is not None:
        # The fixed bridge returned normally after preserving an exception
        # at a proven candidate-owned boundary.  The marker is diagnostic;
        # trusted verifier artifacts below remain the score authority.
        if (
            expected_agent_name != AGENT_IMPORT_PATH
            or n_errored_trials != 0
            or exception_info is not None
        ):
            raise ResultProvenanceError(
                "candidate runtime marker conflicts with Harbor error accounting"
            )
    if n_errored_trials == 1:
        if (
            not isinstance(exception_info, dict)
            or exception_info.get("exception_type") != "AgentTimeoutError"
        ):
            raise ResultProvenanceError("errored trial is not a recorded agent timeout")
    elif exception_info is not None:
        raise ResultProvenanceError(
            "trial exception does not match job error accounting"
        )

    trusted_zero_after_agent_timeout = n_errored_trials == 1 and reward == 0.0
    trusted_signed_nonzero_after_agent_timeout = (
        n_errored_trials == 1 and reward != 0.0 and artifact_expectation is not None
    )
    trusted_zero_without_envelope = trusted_zero_after_agent_timeout or (
        candidate_failure is not None and reward == 0.0
    )

    if artifact_expectation is not None:
        if not require_separate_verifier:
            raise ResultProvenanceError(
                "typed bridge artifact requires separate-verifier provenance"
            )
        envelope_path = (
            trial_dir / "artifacts" / "logs" / "artifacts" / "rsi-arena-envelope.json"
        )
        try:
            envelope_path.lstat()
            envelope_present = True
        except FileNotFoundError:
            envelope_present = False
        # A typed agent deadline or candidate failure marker may prevent the
        # normal answer envelope from being emitted.  The narrow trusted-zero
        # case does not need that candidate artifact to justify score 0:
        # Harbor's result and verifier reward artifact already agree.  If an
        # envelope did appear, it must still authenticate normally; non-zero
        # scores never receive this exception.
        if not trusted_zero_without_envelope or envelope_present:
            load_validated_artifact_envelope(
                trial_dir,
                expectation=artifact_expectation,
                not_before_ns=not_before_ns,
            )

    trusted_score_after_agent_timeout = (
        trusted_zero_after_agent_timeout or trusted_signed_nonzero_after_agent_timeout
    )
    trusted_score_with_optional_usage = (
        trusted_score_after_agent_timeout or candidate_failure is not None
    )
    if trusted_score_with_optional_usage and isinstance(agent_result, dict):
        trial_cost = agent_result.get("cost_usd")
        job_cost = stats.get("cost_usd")
        matching_cost: float | None = None
        if (
            isinstance(trial_cost, (int, float))
            and not isinstance(trial_cost, bool)
            and isinstance(job_cost, (int, float))
            and not isinstance(job_cost, bool)
            and math.isfinite(float(trial_cost))
            and math.isfinite(float(job_cost))
            and float(trial_cost) >= 0.0
            and float(job_cost) >= 0.0
            and math.isclose(
                float(trial_cost),
                float(job_cost),
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        ):
            matching_cost = float(trial_cost)
        normalized_agent_result = dict(agent_result)
        normalized_agent_result["cost_usd"] = matching_cost
        trial = {**trial, "agent_result": normalized_agent_result}
        agent_result = normalized_agent_result

    if trusted_score_with_optional_usage and (
        not isinstance(agent_result, dict)
        or agent_result.get("n_output_tokens") is None
        or stats.get("n_output_tokens") is None
    ):
        # Final AgentContext usage is accounting evidence, not score
        # authority.  A timeout or candidate failure marker commonly leaves it
        # absent/null, so preserve the verified score and record missing usage.
        # When both output-token records exist, retain the existing strict
        # accounting cross-check below.
        return trial

    if not isinstance(agent_result, dict):
        raise ResultProvenanceError("trial has no agent accounting record")
    if "n_output_tokens" not in agent_result or "n_output_tokens" not in stats:
        raise ResultProvenanceError(
            "Harbor record omits required n_output_tokens accounting"
        )
    trial_tokens = _finite_number(
        agent_result["n_output_tokens"], label="n_output_tokens"
    )
    job_tokens = _finite_number(stats["n_output_tokens"], label="job n_output_tokens")
    if trial_tokens < 0.0 or job_tokens < 0.0:
        raise ResultProvenanceError("n_output_tokens accounting is negative")
    if not trial_tokens.is_integer() or not job_tokens.is_integer():
        raise ResultProvenanceError("n_output_tokens accounting is not integral")
    if not math.isclose(trial_tokens, job_tokens, rel_tol=1e-9, abs_tol=1e-12):
        raise ResultProvenanceError(
            "trial n_output_tokens does not match job accounting"
        )
    tokens_out = int(trial_tokens)

    if trusted_score_with_optional_usage:
        # Cost is already normalized above.  Missing, invalid, or mismatched
        # cost changes only accounting coverage; it cannot revoke the trusted
        # verifier score.
        return trial

    trial_cost = agent_result.get("cost_usd")
    job_cost = stats.get("cost_usd")
    if trial_cost is None and job_cost is None:
        return trial
    if trial_cost is None or job_cost is None:
        raise CostAccountingProvenanceError(
            "trial and job disagree about cost_usd accounting value",
            tokens_out=tokens_out,
        )
    try:
        trial_cost_value = _finite_number(trial_cost, label="cost_usd")
        job_cost_value = _finite_number(job_cost, label="job cost_usd")
    except ResultProvenanceError as exc:
        raise CostAccountingProvenanceError(str(exc), tokens_out=tokens_out) from exc
    if trial_cost_value < 0.0 or job_cost_value < 0.0:
        raise CostAccountingProvenanceError(
            "cost_usd accounting is negative", tokens_out=tokens_out
        )
    if not math.isclose(trial_cost_value, job_cost_value, rel_tol=1e-9, abs_tol=1e-12):
        raise CostAccountingProvenanceError(
            "trial cost_usd does not match job accounting", tokens_out=tokens_out
        )
    return trial


def provider_calls_from_trial(
    trial: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Return only call facts installed from the trusted provider sidecar."""

    agent_result = trial.get("agent_result")
    metadata = (
        agent_result.get("metadata") if isinstance(agent_result, Mapping) else None
    )
    provider = (
        metadata.get(PROVIDER_ACCOUNTING_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    raw_calls = provider.get("calls") if isinstance(provider, Mapping) else None
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, list) or any(
        not isinstance(call, Mapping) for call in raw_calls
    ):
        raise ResultProvenanceError("trusted provider call facts are malformed")
    return tuple(dict(call) for call in raw_calls)
