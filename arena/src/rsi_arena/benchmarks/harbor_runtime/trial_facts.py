"""Fresh one-job/one-trial Harbor provenance for binary measurements."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping
from uuid import UUID

from .controller import HarborAgentInvocation
from .provider_broker import (
    PROVIDER_DIAGNOSTICS_NAME,
    PROVIDER_RELIABILITY_DIAGNOSTICS_SCHEMA,
    PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA,
    ProviderCallAccounting,
    ProviderReliabilitySnapshot,
    provider_wait_dominated_deadline,
)
from .trial_files import (
    TrialFileBoundaryError,
    TrialFileMissing,
    read_bounded_trial_file,
)

_PROVIDER_DIAGNOSTICS_MAX_BYTES = 16 * 1024 * 1024


class ResultProvenanceError(ValueError):
    """Harbor output does not prove one fresh, coherent measurement cell."""


class ResultAuthorityError(ResultProvenanceError):
    """The Harbor record disagrees with evaluator-wide launch authority."""


class TrialAgentRuntimeError(ResultProvenanceError):
    """The trusted trial record reports that its agent runtime failed."""

    def __init__(
        self,
        recorded_exception_type: str,
        *,
        provider_diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        self.recorded_exception_type = recorded_exception_type
        self.provider_diagnostics = provider_diagnostics
        super().__init__(f"Harbor agent runtime failed ({recorded_exception_type})")


@dataclass(frozen=True, slots=True)
class HarborTrialFacts:
    reward: float
    artifact_payload: bytes | None
    artifact_state: str
    trial_name: str
    task_name: str
    #: True when Harbor stopped the agent at its deadline instead of the agent
    #: deciding it was finished.  A graded reward is still recorded for the
    #: state frozen at that deadline, so this is the only record separating
    #: "the scaffold ran out of time" from "the scaffold finished and was
    #: wrong" -- two failures that ask for opposite repairs.
    agent_timed_out: bool = False
    #: True when the deadline was reached with the provider still refusing calls.
    #: The reward carried alongside is not the candidate's result -- the agent
    #: never got to finish -- so a cell reading this must decline to measure
    #: instead of scoring the zero.
    provider_upstream_failed: bool = False
    #: Trusted content-free broker counters/durations, suitable for operator UI
    #: and bounded CellMeasurement diagnostics. Candidate code cannot write the
    #: trial-parent file from its sandbox.
    provider_diagnostics: Mapping[str, object] | None = None
    #: Strict per-logical-response facts.  These are suitable for one ledger
    #: row per actual model call; the aggregate counters above are not.
    provider_calls: tuple[ProviderCallAccounting, ...] = ()

    def __post_init__(self) -> None:
        if self.reward not in (0.0, 1.0):
            raise ValueError("reward must be exactly zero or one")
        if self.artifact_state not in {
            "present",
            "missing",
            "oversized",
            "not_required",
        }:
            raise ValueError("artifact_state is invalid")
        if not isinstance(self.agent_timed_out, bool):
            raise TypeError("agent_timed_out must be a boolean")
        if not isinstance(self.provider_upstream_failed, bool):
            raise TypeError("provider_upstream_failed must be a boolean")
        if self.provider_upstream_failed and not self.agent_timed_out:
            raise ValueError("upstream failure is only recorded at the deadline")
        if self.provider_diagnostics is not None and not isinstance(
            self.provider_diagnostics, Mapping
        ):
            raise TypeError("provider diagnostics must be a mapping or null")
        calls = tuple(self.provider_calls)
        if any(not isinstance(call, ProviderCallAccounting) for call in calls):
            raise TypeError("provider calls must be accounting facts")
        object.__setattr__(self, "provider_calls", calls)


_PROVIDER_INTEGER_FIELDS = frozenset(
    {
        "logical_requests_started",
        "logical_requests_finished",
        "upstream_attempts_started",
        "upstream_attempts_finished",
        "responses_started",
        "responses_finished",
        "transport_failures",
        "terminal_transport_failures",
        "safe_retries",
        "open_requests",
        "open_without_response",
        "open_reading_response",
        "first_byte_samples",
        "local_rejections",
    }
)
_PROVIDER_FLOAT_FIELDS = frozenset(
    {
        "total_upstream_wait_seconds",
        "terminal_transport_wait_seconds",
        "nonproductive_wait_seconds",
        "first_byte_total_seconds",
        "upstream_timeout_seconds",
    }
)
_PROVIDER_OPTIONAL_FLOAT_FIELDS = frozenset(
    {
        "oldest_open_request_seconds",
        "oldest_open_attempt_seconds",
        "first_byte_max_seconds",
        "last_progress_age_seconds",
        "last_transport_failure_age_seconds",
        "last_terminal_transport_failure_age_seconds",
    }
)
_PROVIDER_SNAPSHOT_FIELDS = (
    {"schema"}
    | _PROVIDER_INTEGER_FIELDS
    | _PROVIDER_FLOAT_FIELDS
    | _PROVIDER_OPTIONAL_FLOAT_FIELDS
    | {"local_rejection_counts", "last_rejected_fields"}
)
_PROVIDER_REJECTION_REASONS = frozenset(
    {
        "authorization",
        "content_length",
        "content_type",
        "settled_cost_stop",
        "incomplete_body",
        "model_mismatch",
        "payload_budget",
        "request_body_timeout",
        "request_budget",
        "request_size",
        "request_validation",
        "route",
        "tool_mismatch",
        "transfer_encoding",
        "unsupported_fields",
    }
)
_PROVIDER_ACCOUNTING_INTEGER_FIELDS = frozenset(
    {
        "requests",
        "request_bytes",
        "response_bytes",
        "input_tokens",
        "cache_tokens",
        "output_tokens",
    }
)
_PROVIDER_ACCOUNTING_FIELDS = _PROVIDER_ACCOUNTING_INTEGER_FIELDS | {"cost_usd"}
_PROVIDER_CALL_FIELDS = {
    "ordinal",
    "status",
    "response_id",
    "generation_id",
    "input_tokens",
    "cache_tokens",
    "output_tokens",
    "cost_usd",
}
_PROVIDER_CALL_STATUSES = {
    "in_flight",
    "completed",
    "response_oversized",
    "transport_failed",
    "response_rejected",
}
_PROVIDER_CALL_DIAGNOSTIC_FIELDS = {
    "finish_reason",
    "reasoning_tokens",
    "http_status",
    "provider_error_code",
    "started_at",
    "elapsed_seconds",
    "request_max_tokens",
    "request_max_tokens_source",
    "request_reasoning",
    "response_artifact",
    "response_sha256",
    "response_complete",
    "request_group",
    "retryable_refusal",
    "configured_route",
    "provider_name",
}
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")


def _validated_call_diagnostics(raw: Mapping[str, object]) -> dict[str, object]:
    result = {name: raw.get(name) for name in _PROVIDER_CALL_DIAGNOSTIC_FIELDS}
    result['retryable_refusal'] = raw.get('retryable_refusal', False)
    if not isinstance(result['retryable_refusal'], bool):
        raise ResultProvenanceError('provider refusal marker is invalid')
    group = result['request_group']
    if group is not None and (type(group) is not int or not 1 <= group <= raw['ordinal']):
        raise ResultProvenanceError('provider request group is invalid')
    for key in ('configured_route', 'provider_name'):
        value = result[key]
        if value is not None and (not isinstance(value, str) or len(value) > 1024
                                 or any(ord(c) < 32 for c in value)):
            raise ResultProvenanceError('provider route label is invalid')
    for name in ("reasoning_tokens", "http_status", "request_max_tokens"):
        value = result[name]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < (0 if name == "reasoning_tokens" else 1)
        ):
            raise ResultProvenanceError(f"provider call {name} is invalid")
    status = result["http_status"]
    if status is not None and not 100 <= status <= 599:
        raise ResultProvenanceError("provider HTTP status is invalid")
    error_code = result['provider_error_code']
    if error_code is not None and (type(error_code) is not int or not 400 <= error_code <= 599):
        raise ResultProvenanceError("provider body error code is invalid")
    finish = result["finish_reason"]
    if finish is not None and (
        not isinstance(finish, str) or not _PROVIDER_ID.fullmatch(finish)
    ):
        raise ResultProvenanceError("provider finish reason is invalid")
    started = result["started_at"]
    if started is not None:
        try:
            stamp = (
                datetime.fromisoformat(started) if isinstance(started, str) else None
            )
        except ValueError:
            stamp = None
        if stamp is None or stamp.tzinfo is None:
            raise ResultProvenanceError("provider start time is invalid")
    elapsed = result["elapsed_seconds"]
    if elapsed is not None and (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ResultProvenanceError("provider call duration is invalid")
    if result["request_max_tokens_source"] not in (None, "explicit", "default"):
        raise ResultProvenanceError("provider output limit source is invalid")
    reasoning = result["request_reasoning"]
    if reasoning is not None:
        if not isinstance(reasoning, dict) or set(reasoning) - {
            "enabled",
            "exclude",
            "effort",
            "max_tokens",
            "other_fields_omitted",
        }:
            raise ResultProvenanceError("provider reasoning settings are invalid")
        for name in ("enabled", "exclude", "other_fields_omitted"):
            if name in reasoning and not isinstance(reasoning[name], bool):
                raise ResultProvenanceError("provider reasoning toggle is invalid")
        if "effort" in reasoning and reasoning["effort"] not in (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ):
            raise ResultProvenanceError("provider reasoning effort is invalid")
        maximum = reasoning.get("max_tokens")
        if "max_tokens" in reasoning and (
            isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0
        ):
            raise ResultProvenanceError("provider reasoning token bound is invalid")
    artifact = result["response_artifact"]
    digest = result["response_sha256"]
    complete = result["response_complete"]
    if any(item is not None for item in (artifact, digest, complete)):
        if (
            not isinstance(artifact, str)
            or not re.fullmatch(r"provider-responses/call-[0-9]{6}\.json", artifact)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(complete, bool)
        ):
            raise ResultProvenanceError(
                "provider response artifact identity is invalid"
            )
    return result


def _validated_provider_calls(
    value: object,
    *,
    accounting: Mapping[str, object],
    provider: Mapping[str, object],
) -> tuple[ProviderCallAccounting, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or any(
            not isinstance(item, dict)
            or not _PROVIDER_CALL_FIELDS <= set(item)
            or set(item) - _PROVIDER_CALL_FIELDS - _PROVIDER_CALL_DIAGNOSTIC_FIELDS
            for item in value
        )
    ):
        raise ResultProvenanceError("provider calls have an invalid shape")
    calls: list[ProviderCallAccounting] = []
    for expected_ordinal, raw in enumerate(value, start=1):
        assert isinstance(raw, dict)
        ordinal = raw["ordinal"]
        status = raw["status"]
        if ordinal != expected_ordinal or isinstance(ordinal, bool):
            raise ResultProvenanceError("provider call ordinals are not canonical")
        if status not in _PROVIDER_CALL_STATUSES:
            raise ResultProvenanceError("provider call status is invalid")
        identities: dict[str, str | None] = {}
        for name in ("response_id", "generation_id"):
            item = raw[name]
            if item is not None and (
                not isinstance(item, str) or _PROVIDER_ID.fullmatch(item) is None
            ):
                raise ResultProvenanceError("provider response identity is invalid")
            identities[name] = item
        counts: dict[str, int | None] = {}
        for name in ("input_tokens", "cache_tokens", "output_tokens"):
            item = raw[name]
            if item is not None and (
                isinstance(item, bool) or not isinstance(item, int) or item < 0
            ):
                raise ResultProvenanceError("provider call token count is invalid")
            counts[name] = item
        if (
            counts["input_tokens"] is not None
            and counts["cache_tokens"] is not None
            and counts["cache_tokens"] > counts["input_tokens"]
        ):
            raise ResultProvenanceError(
                "provider call cache tokens exceed input tokens"
            )
        raw_cost = raw["cost_usd"]
        if raw_cost is not None and (
            isinstance(raw_cost, bool)
            or not isinstance(raw_cost, (int, float))
            or not math.isfinite(float(raw_cost))
            or float(raw_cost) < 0.0
        ):
            raise ResultProvenanceError("provider call cost is invalid")
        if status == "transport_failed" and any(
            raw[name] is not None
            for name in ("input_tokens", "cache_tokens", "output_tokens", "cost_usd")
        ):
            raise ResultProvenanceError(
                "unanswered provider call carries response accounting"
            )
        calls.append(
            ProviderCallAccounting(
                ordinal=ordinal,
                status=str(status),
                response_id=identities["response_id"],
                generation_id=identities["generation_id"],
                input_tokens=counts["input_tokens"],
                cache_tokens=counts["cache_tokens"],
                output_tokens=counts["output_tokens"],
                cost_usd=None if raw_cost is None else float(raw_cost),
                **_validated_call_diagnostics(raw),
            )
        )
    if len(calls) != provider["logical_requests_started"]:
        raise ResultProvenanceError("provider calls disagree with logical requests")
    if accounting["requests"] < len(calls):
        raise ResultProvenanceError("provider calls exceed accepted requests")
    response_statuses = {"completed", "response_oversized"}
    if (
        sum(call.status in response_statuses for call in calls)
        != provider["responses_finished"]
    ):
        raise ResultProvenanceError("provider calls disagree with completed responses")
    if (
        sum(call.status != "in_flight" for call in calls)
        != provider["logical_requests_finished"]
    ):
        raise ResultProvenanceError("provider calls disagree with finished requests")
    if (
        sum(call.status == "in_flight" for call in calls) != provider["open_requests"]
        or provider["logical_requests_started"] - provider["logical_requests_finished"]
        != provider["open_requests"]
    ):
        raise ResultProvenanceError("provider calls disagree with open requests")
    for name in ("response_id", "generation_id"):
        identities = [
            getattr(call, name) for call in calls if getattr(call, name) is not None
        ]
        if len(identities) != len(set(identities)):
            raise ResultProvenanceError("provider response identity is duplicate")
    complete_costs = all(
        call.status == "completed" and call.cost_usd is not None for call in calls
    )
    aggregate_cost = accounting["cost_usd"]
    if calls and complete_costs:
        assert all(call.cost_usd is not None for call in calls)
        if aggregate_cost is None or not math.isclose(
            float(aggregate_cost),
            sum(float(call.cost_usd) for call in calls),
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ResultProvenanceError("provider call costs disagree with aggregate")
    elif aggregate_cost is not None:
        raise ResultProvenanceError("partial provider calls carry an aggregate cost")
    for call_field, aggregate_field in (
        ("input_tokens", "input_tokens"),
        ("cache_tokens", "cache_tokens"),
        ("output_tokens", "output_tokens"),
    ):
        values = [getattr(call, call_field) for call in calls]
        if calls and all(
            call.status == "completed" and value is not None
            for call, value in zip(calls, values)
        ):
            if (
                sum(int(value) for value in values if value is not None)
                != accounting[aggregate_field]
            ):
                raise ResultProvenanceError(
                    "provider call tokens disagree with aggregate"
                )
    return tuple(calls)


def _classify_trial_exception(
    *, n_errored_trials: object, exception_info: object
) -> bool:
    """Return the timeout marker or reject a non-measurement execution error."""

    if n_errored_trials == 1:
        recorded_type = (
            exception_info.get("exception_type")
            if isinstance(exception_info, Mapping)
            else None
        )
        if recorded_type == "ProgramRuntimeError":
            raise TrialAgentRuntimeError(recorded_type)
        if recorded_type != "AgentTimeoutError":
            raise ResultProvenanceError("errored trial is not a recorded agent timeout")
        return True
    if exception_info is not None:
        raise ResultProvenanceError("trial recorded an execution exception")
    return False


def _load_provider_diagnostics(
    trial_dir: Path, *, not_before_ns: int
) -> (
    tuple[
        dict[str, object],
        ProviderReliabilitySnapshot,
        bool,
        float,
        tuple[ProviderCallAccounting, ...],
    ]
    | None
):
    try:
        payload = read_bounded_trial_file(
            trial_dir,
            PROVIDER_DIAGNOSTICS_NAME,
            maximum=_PROVIDER_DIAGNOSTICS_MAX_BYTES,
            not_before_ns=not_before_ns,
        )
    except TrialFileMissing:
        return None
    except TrialFileBoundaryError as exc:
        raise ResultProvenanceError(
            "provider diagnostics cross the file boundary"
        ) from exc
    try:
        raw = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResultProvenanceError("provider diagnostics JSON is unreadable") from exc
    if not isinstance(raw, dict):
        raise ResultProvenanceError("provider diagnostics are not an object")
    if (
        set(raw) - {"configured_max_output_tokens", "configured_max_total_output_tokens",
                    "configured_provider_timeout_seconds", "model_name", "configured_provider_only", "cost_stop_enabled"}
        != {
            "schema",
            "worker_deadline_reached",
            "worker_timeout_seconds",
            "accounting",
            "calls",
            "provider",
        }
        or raw.get("schema") != PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA
    ):
        raise ResultProvenanceError("provider diagnostics have an invalid shape")
    if "cost_stop_enabled" in raw and not isinstance(raw["cost_stop_enabled"], bool):
        raise ResultProvenanceError("provider cost-stop setting is invalid")
    worker_deadline = raw["worker_deadline_reached"]
    model_name = raw.get("model_name")
    if model_name is not None and (
        not isinstance(model_name, str)
        or re.fullmatch(r"openrouter/[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,499}", model_name)
        is None
    ):
        raise ResultProvenanceError("provider configured model identity is invalid")
    provider_only = raw.get("configured_provider_only")
    if provider_only is not None and (
        not isinstance(provider_only, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9/_-]{0,127}(?:,[a-z0-9][a-z0-9/_-]{0,127}){0,7}", provider_only) is None
    ):
        raise ResultProvenanceError("configured provider route is invalid")
    maximum = raw.get("configured_max_output_tokens")
    if maximum is not None and (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 0 < maximum <= 262_144
    ):
        raise ResultProvenanceError("configured provider output limit is invalid")
    total_maximum = raw.get("configured_max_total_output_tokens")
    if total_maximum is not None and (
        isinstance(total_maximum, bool)
        or not isinstance(total_maximum, int)
        or not (maximum or 1) <= total_maximum <= 16_777_216
    ):
        raise ResultProvenanceError("configured provider total output limit is invalid")
    configured_timeout = raw.get("configured_provider_timeout_seconds")
    if configured_timeout is not None and (
        isinstance(configured_timeout, bool)
        or not isinstance(configured_timeout, (int, float))
        or not math.isfinite(float(configured_timeout))
        or not 0 < configured_timeout <= 86_400
    ):
        raise ResultProvenanceError("configured provider timeout is invalid")
    worker_timeout = raw["worker_timeout_seconds"]
    accounting = raw["accounting"]
    calls = raw["calls"]
    provider = raw["provider"]
    if not isinstance(worker_deadline, bool):
        raise ResultProvenanceError("provider worker deadline marker is invalid")
    if (
        isinstance(worker_timeout, bool)
        or not isinstance(worker_timeout, (int, float))
        or not math.isfinite(float(worker_timeout))
        or not 0 < float(worker_timeout) <= 86_400
    ):
        raise ResultProvenanceError("provider worker timeout is invalid")
    if not isinstance(provider, dict) or set(provider) != _PROVIDER_SNAPSHOT_FIELDS:
        raise ResultProvenanceError(
            "provider reliability snapshot has an invalid shape"
        )
    if (
        not isinstance(accounting, dict)
        or set(accounting) != _PROVIDER_ACCOUNTING_FIELDS
    ):
        raise ResultProvenanceError("provider accounting has an invalid shape")
    for name in _PROVIDER_ACCOUNTING_INTEGER_FIELDS:
        value = accounting[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResultProvenanceError("provider accounting counter is invalid")
    accounting_cost = accounting["cost_usd"]
    if accounting_cost is not None and (
        isinstance(accounting_cost, bool)
        or not isinstance(accounting_cost, (int, float))
        or not math.isfinite(float(accounting_cost))
        or float(accounting_cost) < 0
    ):
        raise ResultProvenanceError("provider accounting cost is invalid")
    if provider.get("schema") != PROVIDER_RELIABILITY_DIAGNOSTICS_SCHEMA:
        raise ResultProvenanceError("provider reliability schema differs")
    for name in _PROVIDER_INTEGER_FIELDS:
        value = provider[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResultProvenanceError("provider reliability counter is invalid")
    for name in _PROVIDER_FLOAT_FIELDS:
        value = provider[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ResultProvenanceError("provider reliability duration is invalid")
    if not 0 < float(provider["upstream_timeout_seconds"]) <= 86_400:
        raise ResultProvenanceError("provider upstream timeout is invalid")
    for name in _PROVIDER_OPTIONAL_FLOAT_FIELDS:
        value = provider[name]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ResultProvenanceError("provider optional duration is invalid")
    rejection_counts = provider["local_rejection_counts"]
    if (
        not isinstance(rejection_counts, dict)
        or len(rejection_counts) > len(_PROVIDER_REJECTION_REASONS)
        or any(
            reason not in _PROVIDER_REJECTION_REASONS
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            for reason, count in rejection_counts.items()
        )
        or sum(rejection_counts.values()) != provider["local_rejections"]
    ):
        raise ResultProvenanceError("provider rejection counters are invalid")
    rejected_fields = provider["last_rejected_fields"]
    if (
        not isinstance(rejected_fields, list)
        or len(rejected_fields) > 16
        or rejected_fields != sorted(set(rejected_fields))
        or any(
            not isinstance(field, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", field) is None
            for field in rejected_fields
        )
    ):
        raise ResultProvenanceError("provider rejected fields are invalid")
    if (
        provider["logical_requests_finished"] > provider["logical_requests_started"]
        or provider["upstream_attempts_finished"]
        > provider["upstream_attempts_started"]
        or provider["responses_finished"] > provider["responses_started"]
        or provider["terminal_transport_failures"] > provider["transport_failures"]
        or provider["terminal_transport_wait_seconds"]
        > provider["total_upstream_wait_seconds"]
        or provider["nonproductive_wait_seconds"]
        > provider["total_upstream_wait_seconds"]
        or provider["terminal_transport_wait_seconds"]
        > provider["nonproductive_wait_seconds"]
        or (
            provider["terminal_transport_wait_seconds"] > 0
            and provider["terminal_transport_failures"] == 0
        )
        or provider["open_without_response"] + provider["open_reading_response"]
        > provider["open_requests"]
    ):
        raise ResultProvenanceError("provider reliability counters are incoherent")
    try:
        snapshot = ProviderReliabilitySnapshot(
            **{**provider, "last_rejected_fields": tuple(rejected_fields)}
        )
    except TypeError as exc:  # Exact shape above should make this unreachable.
        raise ResultProvenanceError("provider reliability snapshot is invalid") from exc
    validated_calls = _validated_provider_calls(
        calls,
        accounting=accounting,
        provider=provider,
    )
    return raw, snapshot, worker_deadline, float(worker_timeout), validated_calls


def _fresh_regular_file(path: Path, *, not_before_ns: int) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ResultProvenanceError(
            "required Harbor provenance file is missing"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ResultProvenanceError("Harbor provenance path is not a regular file")
    if info.st_mtime_ns < not_before_ns:
        raise ResultProvenanceError("Harbor provenance file predates this cell")
    return info


def _load_json(path: Path, *, not_before_ns: int) -> dict[str, object]:
    _fresh_regular_file(path, not_before_ns=not_before_ns)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultProvenanceError("Harbor provenance JSON is unreadable") from exc
    if not isinstance(value, dict):
        raise ResultProvenanceError("Harbor provenance JSON is not an object")
    return value


def _same_path(raw: object, expected: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return Path(raw).expanduser().resolve(strict=True) == expected
    except (OSError, RuntimeError):
        return False


def _single_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, list)
        or len(value) != 1
        or not isinstance(value[0], Mapping)
    ):
        raise ResultProvenanceError(f"expected exactly one {label}")
    return value[0]


def _agent_matches(raw: object, expected: HarborAgentInvocation, *, label: str) -> None:
    if not isinstance(raw, Mapping):
        raise ResultAuthorityError(f"{label} omits the agent configuration")
    if raw.get("name") != expected.agent_name:
        raise ResultAuthorityError(f"{label} agent identity differs")
    if raw.get("model_name") != expected.model_name:
        raise ResultAuthorityError(f"{label} model identity differs")
    configured_env = raw.get("env", {})
    if not isinstance(configured_env, Mapping):
        raise ResultAuthorityError(f"{label} agent environment is malformed")
    expected_env = {name: f"${{{name}}}" for name in expected.secret_env_names}
    if dict(configured_env) != expected_env:
        raise ResultAuthorityError(f"{label} agent environment differs")
    configured_kwargs = raw.get("kwargs", {})
    if not isinstance(configured_kwargs, Mapping):
        raise ResultAuthorityError(f"{label} agent kwargs are malformed")
    if dict(configured_kwargs) != dict(expected.agent_kwargs):
        raise ResultAuthorityError(f"{label} agent kwargs differ")


def _extract_reward(trial: Mapping[str, object]) -> float:
    verifier = trial.get("verifier_result")
    if not isinstance(verifier, Mapping):
        raise ResultProvenanceError("trial has no verifier result")
    rewards = verifier.get("rewards")
    if not isinstance(rewards, Mapping) or set(rewards) != {"reward"}:
        raise ResultProvenanceError("trial verifier rewards are not canonical")
    value = rewards["reward"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultProvenanceError("trial reward is not numeric")
    reward = float(value)
    if not math.isfinite(reward) or reward not in (0.0, 1.0):
        raise ResultProvenanceError("trial reward is not exact binary reward")
    return reward


def _reward_file(trial_dir: Path, *, not_before_ns: int) -> float:
    path = trial_dir / "verifier/reward.txt"
    _fresh_regular_file(path, not_before_ns=not_before_ns)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ResultProvenanceError("verifier reward file is unreadable") from exc
    if raw not in {b"0", b"0\n", b"1", b"1\n"}:
        raise ResultProvenanceError("verifier reward file is not canonical")
    return float(int(raw.strip()))


def _validate_ctrf(trial_dir: Path, *, not_before_ns: int, reward: float) -> None:
    ctrf = _load_json(trial_dir / "verifier/ctrf.json", not_before_ns=not_before_ns)
    results = ctrf.get("results")
    if not isinstance(results, Mapping):
        raise ResultProvenanceError("verifier CTRF results are missing")
    summary = results.get("summary")
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
    implied = None
    if failed > 0:
        implied = 0.0
    elif tests > 0 and passed == tests:
        implied = 1.0
    if implied is not None and implied != reward:
        raise ResultProvenanceError("verifier reward conflicts with CTRF summary")


def _validate_artifact_manifest(trial_dir: Path, *, not_before_ns: int) -> None:
    path = trial_dir / "artifacts/manifest.json"
    _fresh_regular_file(path, not_before_ns=not_before_ns)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultProvenanceError("artifact manifest is unreadable") from exc
    entries = manifest
    if isinstance(manifest, dict) and set(manifest) == {"entries"}:
        entries = manifest["entries"]
    if not isinstance(entries, list):
        raise ResultProvenanceError("artifact manifest is malformed")
    # "empty" is as conclusive as "ok": both say Harbor really did collect the
    # convention directory, so a missing answer means the agent never wrote one
    # rather than that we failed to look.  Any other status leaves the transfer
    # itself in doubt and must not be read as evidence about the agent.
    matching = [
        entry
        for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("source") == "/logs/artifacts"
        and entry.get("status") in {"ok", "empty"}
    ]
    if len(matching) != 1:
        raise ResultProvenanceError(
            "artifact manifest does not prove one convention transfer"
        )


def _load_answer_artifact(
    trial_dir: Path,
    *,
    not_before_ns: int,
    artifact_path: str,
    maximum_bytes: int,
) -> tuple[bytes | None, str]:
    relative = artifact_path.lstrip("/")
    path = trial_dir / "artifacts" / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, "missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ResultProvenanceError("answer artifact is not a regular file")
    if info.st_mtime_ns < not_before_ns:
        raise ResultProvenanceError("answer artifact predates this cell")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResultProvenanceError(
            "answer artifact could not be opened safely"
        ) from exc
    try:
        before = os.fstat(descriptor)
        payload = os.read(descriptor, maximum_bytes + 1)
        after = os.fstat(descriptor)
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
        ):
            raise ResultProvenanceError("answer artifact changed while being read")
    finally:
        os.close(descriptor)
    if before.st_size > maximum_bytes:
        return payload, "oversized"
    if len(payload) != before.st_size:
        raise ResultProvenanceError("answer artifact was not read completely")
    return payload, "present"


def load_validated_trial_facts(
    job_dir: str | Path,
    *,
    expected_task_path: str | Path,
    expected_task_name: str,
    expected_invocation: HarborAgentInvocation,
    not_before_ns: int,
    require_separate_verifier: bool = True,
    require_artifact_manifest: bool = True,
    require_ctrf: bool = False,
    artifact_path: str | None = None,
    artifact_max_bytes: int | None = None,
) -> HarborTrialFacts:
    """Validate all independent Harbor records before trusting its reward."""

    root = Path(job_dir)
    task_path = Path(expected_task_path).resolve(strict=True)
    try:
        root_info = root.lstat()
    except FileNotFoundError as exc:
        raise ResultProvenanceError("Harbor job directory is missing") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ResultProvenanceError("Harbor job path is not a real directory")
    if not isinstance(expected_task_name, str) or not expected_task_name:
        raise ValueError("expected_task_name must be non-empty text")
    if not isinstance(expected_invocation, HarborAgentInvocation):
        raise TypeError("expected_invocation must be a HarborAgentInvocation")
    if isinstance(not_before_ns, bool) or not isinstance(not_before_ns, int):
        raise TypeError("not_before_ns must be an integer")
    if require_artifact_manifest and (
        not isinstance(artifact_path, str)
        or not artifact_path
        or isinstance(artifact_max_bytes, bool)
        or not isinstance(artifact_max_bytes, int)
        or artifact_max_bytes < 0
    ):
        raise ValueError(
            "artifact validation requires a path and non-negative byte cap"
        )

    job_config = _load_json(root / "config.json", not_before_ns=not_before_ns)
    job_lock = _load_json(root / "lock.json", not_before_ns=not_before_ns)
    job_result = _load_json(root / "result.json", not_before_ns=not_before_ns)
    if (
        job_config.get("job_name") != "cell"
        or job_config.get("n_concurrent_trials") != 1
        or job_config.get("n_attempts", 1) != 1
    ):
        raise ResultProvenanceError("job configuration is not one cell attempt")
    configured_task = _single_mapping(job_config.get("tasks"), label="configured task")
    configured_agent = _single_mapping(
        job_config.get("agents"), label="configured agent"
    )
    if not _same_path(configured_task.get("path"), task_path):
        raise ResultAuthorityError("configured task path differs")
    _agent_matches(configured_agent, expected_invocation, label="configured")

    locked_trial = _single_mapping(job_lock.get("trials"), label="locked trial")
    locked_task = locked_trial.get("task")
    if not isinstance(locked_task, Mapping) or not _same_path(
        locked_task.get("path"), task_path
    ):
        raise ResultAuthorityError("locked task path differs")
    task_digest = locked_task.get("digest")
    if (
        not isinstance(task_digest, str)
        or not task_digest.startswith("sha256:")
        or len(task_digest) != 71
    ):
        raise ResultProvenanceError("locked task digest is invalid")
    _agent_matches(locked_trial.get("agent"), expected_invocation, label="locked")
    locked_verifier = locked_trial.get("verifier")
    if (
        not isinstance(locked_verifier, Mapping)
        or locked_verifier.get("disable") is not False
        or (
            require_separate_verifier
            and locked_verifier.get("environment_mode") != "separate"
        )
    ):
        raise ResultAuthorityError("locked verifier is not enabled and separate")

    try:
        job_id = str(UUID(str(job_result.get("id"))))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ResultProvenanceError("job result id is invalid") from exc
    stats = job_result.get("stats")
    expected_stats = {
        "n_completed_trials": 1,
        "n_running_trials": 0,
        "n_pending_trials": 0,
        "n_cancelled_trials": 0,
    }
    if not isinstance(stats, Mapping) or any(
        stats.get(key) != value for key, value in expected_stats.items()
    ):
        raise ResultProvenanceError("job did not complete exactly one clean trial")
    # Harbor keeps going to the verifier after AgentTimeoutError, so such a
    # trial is both completed and errored while still carrying a graded reward
    # for the state frozen at the agent deadline.  Accepting that one shape is
    # deferred until the trial record below proves the error really was a
    # timeout; every other errored-trial shape stays rejected.
    n_errored_trials = stats.get("n_errored_trials")
    if n_errored_trials not in (0, 1):
        raise ResultProvenanceError("job has an unsupported errored-trial count")

    trial_dirs: list[Path] = []
    for child in root.iterdir():
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ResultProvenanceError("Harbor job directory contains a symlink")
        if stat.S_ISDIR(info.st_mode) and (child / "result.json").exists():
            trial_dirs.append(child)
    if len(trial_dirs) != 1:
        raise ResultProvenanceError("job does not contain exactly one trial result")
    trial_dir = trial_dirs[0]
    trial = _load_json(trial_dir / "result.json", not_before_ns=not_before_ns)
    trial_config = _load_json(trial_dir / "config.json", not_before_ns=not_before_ns)
    _load_json(trial_dir / "lock.json", not_before_ns=not_before_ns)

    if trial.get("trial_name") != trial_dir.name:
        raise ResultProvenanceError("trial name differs from its directory")
    if trial.get("task_name") != expected_task_name:
        raise ResultAuthorityError("trial task name differs")
    if (
        require_separate_verifier
        and trial.get("verifier_environment_mode") != "separate"
    ):
        raise ResultAuthorityError("trial verifier authority differs")
    exception_info = trial.get("exception_info")
    loaded_provider = _load_provider_diagnostics(
        trial_dir,
        not_before_ns=not_before_ns,
    )
    provider_diagnostics: dict[str, object] | None = None
    provider_calls: tuple[ProviderCallAccounting, ...] = ()
    if loaded_provider is not None:
        provider_diagnostics = loaded_provider[0]
        provider_calls = loaded_provider[4]
        if provider_diagnostics.get("model_name") not in (
            None,
            expected_invocation.model_name,
        ):
            raise ResultAuthorityError(
                "provider configured model differs from invocation"
            )
    # Only Harbor's ordinary timeout shape is accepted as a measurement. It
    # continues to artifact collection and the verifier; connector exceptions
    # do not. Provider attribution is derived separately from trusted telemetry.
    try:
        agent_timed_out = _classify_trial_exception(
            n_errored_trials=n_errored_trials,
            exception_info=exception_info,
        )
    except TrialAgentRuntimeError as exc:
        raise TrialAgentRuntimeError(
            exc.recorded_exception_type,
            provider_diagnostics=provider_diagnostics,
        ) from exc
    if loaded_provider is None and not expected_invocation.apparatus_only:
        raise ResultProvenanceError(
            "provider diagnostics are required for a real agent"
        )

    provider_upstream_failed = False
    if loaded_provider is not None:
        (
            _,
            provider_snapshot,
            worker_deadline_reached,
            worker_timeout_seconds,
            _,
        ) = loaded_provider
        agent_timed_out = agent_timed_out or worker_deadline_reached
        if agent_timed_out:
            provider_upstream_failed = provider_wait_dominated_deadline(
                provider_snapshot,
                worker_timeout_seconds=worker_timeout_seconds,
            )

    embedded = trial.get("config")
    if not isinstance(embedded, Mapping):
        raise ResultProvenanceError("trial result omits embedded configuration")
    for label, config in (("embedded", embedded), ("standalone", trial_config)):
        if str(config.get("job_id")) != job_id:
            raise ResultProvenanceError(f"{label} trial is not bound to the job")
        raw_task = config.get("task")
        if not isinstance(raw_task, Mapping) or not _same_path(
            raw_task.get("path"), task_path
        ):
            raise ResultAuthorityError(f"{label} trial task differs")
        _agent_matches(config.get("agent"), expected_invocation, label=label)

    reward = _extract_reward(trial)
    if _reward_file(trial_dir, not_before_ns=not_before_ns) != reward:
        raise ResultProvenanceError("verifier reward file differs from trial result")
    if require_ctrf:
        _validate_ctrf(trial_dir, not_before_ns=not_before_ns, reward=reward)
    if require_artifact_manifest:
        _validate_artifact_manifest(trial_dir, not_before_ns=not_before_ns)
        assert artifact_path is not None
        assert artifact_max_bytes is not None
        artifact, artifact_state = _load_answer_artifact(
            trial_dir,
            not_before_ns=not_before_ns,
            artifact_path=artifact_path,
            maximum_bytes=artifact_max_bytes,
        )
    else:
        artifact, artifact_state = None, "not_required"
    return HarborTrialFacts(
        reward=reward,
        artifact_payload=artifact,
        artifact_state=artifact_state,
        trial_name=trial_dir.name,
        task_name=expected_task_name,
        agent_timed_out=agent_timed_out,
        provider_upstream_failed=provider_upstream_failed,
        provider_diagnostics=provider_diagnostics,
        provider_calls=provider_calls,
    )


__all__ = [
    "HarborTrialFacts",
    "ResultAuthorityError",
    "ResultProvenanceError",
    "TrialAgentRuntimeError",
    "load_validated_trial_facts",
]
