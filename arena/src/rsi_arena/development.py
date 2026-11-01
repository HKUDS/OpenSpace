"""Run-scoped development owner and benchmark-owned adapter contract.

The owner acquires one temporary stable view of the current candidate, calls
one trusted benchmark adapter, and publishes only benchmark-approved public
bytes under ``.rsi-arena/development``.  The mutable mirror is never read back as
task, evaluator, result, usage, or trajectory authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from rsi_arena.common import ROLES, UsageLedger, snapshot_worktree
from rsi_arena.run_layout import (
    RunLayout,
    RunLayoutError,
    RunValidationError,
    _fsync_directory,
    _load_run_manifest,
    atomic_write_bytes,
    file_digest,
    read_jsonl,
)

from .dto import DevelopmentResult, bounded_opaque_id

_MIRROR_SCHEMA = "rsi-arena-development-manifest"
_INDEX_SCHEMA = "rsi-arena-development-index"
_RESULT_SCHEMA = "rsi-arena-development-public-result"
_USAGE_SCHEMA = "rsi-arena-development-public-usage"
_TRAJECTORY_SCHEMA = "rsi-arena-development-public-trajectory"
_NATIVE_TRACE_MAX_BYTES = 32 * 1024 * 1024
_NATIVE_PANE_MAX_BYTES = 8 * 1024 * 1024
_RESULT_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "campaign_id",
        "method",
        "benchmark_id",
        "development_evaluation_id",
        "state",
        "task_ids",
        "stable_code_view",
        "result",
    }
)
_DELIVERY_CHANNEL = "method_visible_development_mirror"
_DELIVERY_SCHEMA = "campaign-development-delivery"
_DELIVERY_FIELDS = (
    "development_material",
    "development_result",
    "development_usage",
    "development_trajectory",
    "development_artifacts",
)
_DELIVERY_CHILD_FIELDS = frozenset(
    {
        "benchmark_id",
        "overall_status",
        "supported_fields",
        "delivered_fields",
        "delivery_channels",
        "rendered_artifact_digests",
        "status",
        "delivery_records",
        "development_mirror",
        "development_task_ids",
    }
)
_SINGULAR_DELIVERY_FIELDS = (_DELIVERY_CHILD_FIELDS - {"benchmark_id", "status"}) | {
    "development_benchmark_id"
}
_DEVELOPMENT_DELIVERY_LOCK = threading.RLock()
_FAILED_TASK_BRIEF_LIMIT = 6
_FAILED_TASK_BRIEF_TEXT_LIMIT = 2_048
_FAILED_TASK_BRIEF_BYTES_LIMIT = 4_096
_FAILED_TASK_BRIEFS_BYTES_LIMIT = 24_576
_LOG = logging.getLogger(__name__)


def _is_sha256_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def replay_delivered_development_results(
    layout: RunLayout,
    *,
    benchmark_task_ids: Mapping[str, Sequence[str]],
) -> tuple[Mapping[str, object], ...]:
    """Return only canonical public results with a digest-bound delivery.

    This reader never constructs ``RunScopedDevelopmentOperations`` and never
    creates the publication or mutable mirror directories.
    """

    path = layout.root / "development-delivery-manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunValidationError("development delivery manifest is invalid") from exc
    manifest_fields = {
        "schema",
        "run_id",
        "method",
        "status",
        "benchmarks",
        "campaign_freeze_id",
    }
    run_manifest = _load_run_manifest(layout.root)
    freeze = run_manifest["campaign_run_freeze"]
    assert isinstance(freeze, Mapping)
    if (
        not isinstance(payload, dict)
        or frozenset(payload) != frozenset(manifest_fields)
        or payload.get("schema") != _DELIVERY_SCHEMA
        or payload.get("run_id") != layout.run_id
        or payload.get("method") != layout.method
        or payload.get("status") not in {"not_recorded", "delivered"}
        or not isinstance(payload.get("benchmarks"), Mapping)
        or set(payload["benchmarks"]) != set(benchmark_task_ids)
        or payload.get("campaign_freeze_id") != freeze.get("campaign_freeze_id")
    ):
        raise RunValidationError("development delivery manifest identity is invalid")

    publications: dict[tuple[str, str], tuple[Path, dict[str, object]]] = {}
    publication_root = layout.root / "development" / "publications"
    if publication_root.exists():
        result_paths = set(publication_root.glob("*/result.json"))
        result_paths.update(publication_root.glob("*/*/result.json"))
        for result_path in sorted(result_paths):
            try:
                info = result_path.lstat()
                raw = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RunValidationError(
                    "development public publication is invalid"
                ) from exc
            benchmark_id = raw.get("benchmark_id") if isinstance(raw, Mapping) else None
            evaluation_id = (
                raw.get("development_evaluation_id")
                if isinstance(raw, Mapping)
                else None
            )
            expected_tasks = benchmark_task_ids.get(str(benchmark_id))
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not isinstance(raw, dict)
                or set(raw) != _RESULT_FIELDS
                or raw.get("schema") != _RESULT_SCHEMA
                or raw.get("run_id") != layout.run_id
                or raw.get("campaign_id") != layout.campaign
                or raw.get("method") != layout.method
                or expected_tasks is None
                or not isinstance(evaluation_id, str)
                or not evaluation_id
                or result_path.parent.name != evaluation_id
                or raw.get("state") != "TERMINAL"
                or not isinstance(raw.get("task_ids"), list)
                or not raw["task_ids"]
                or len(set(map(str, raw["task_ids"]))) != len(raw["task_ids"])
                or not set(map(str, raw["task_ids"])).issubset(set(expected_tasks))
                or not isinstance(raw.get("result"), Mapping)
                or raw["result"].get("outcome") not in {"measured", "inconclusive"}
            ):
                raise RunValidationError(
                    "development public publication source changed identity"
                )
            key = (str(benchmark_id), evaluation_id)
            if key in publications:
                raise RunValidationError(
                    "development publication identity is duplicate"
                )
            publications[key] = (result_path, raw)

    delivered: list[Mapping[str, object]] = []
    seen_evaluations: set[tuple[str, str]] = set()
    has_delivery_record = False
    benchmarks = payload["benchmarks"]
    assert isinstance(benchmarks, Mapping)
    for benchmark_id, expected_tasks in benchmark_task_ids.items():
        raw_delivery = benchmarks.get(benchmark_id)
        if (
            not isinstance(raw_delivery, Mapping)
            or set(raw_delivery) != _DELIVERY_CHILD_FIELDS
        ):
            raise RunValidationError(
                "development delivery benchmark binding is invalid"
            )
        records = raw_delivery.get("delivery_records")
        supported_fields = raw_delivery.get("supported_fields")
        delivered_fields = raw_delivery.get("delivered_fields")
        delivery_channels = raw_delivery.get("delivery_channels")
        rendered = raw_delivery.get("rendered_artifact_digests")
        statuses = raw_delivery.get("status")
        development_tasks = raw_delivery.get("development_task_ids")
        if (
            raw_delivery.get("benchmark_id") != benchmark_id
            or raw_delivery.get("overall_status") not in {"not_recorded", "delivered"}
            or not isinstance(records, list)
            or any(not isinstance(record, Mapping) for record in records)
            or not isinstance(supported_fields, list)
            or any(field not in _DELIVERY_FIELDS for field in supported_fields)
            or len(set(supported_fields)) != len(supported_fields)
            or not isinstance(delivered_fields, list)
            or any(field not in _DELIVERY_FIELDS for field in delivered_fields)
            or len(set(delivered_fields)) != len(delivered_fields)
            or not set(delivered_fields).issubset(set(supported_fields))
            or not isinstance(delivery_channels, Mapping)
            or set(delivery_channels) != set(delivered_fields)
            or any(
                not isinstance(channel, str) or not channel
                for channel in delivery_channels.values()
            )
            or not isinstance(statuses, Mapping)
            or set(statuses) != set(delivered_fields)
            or any(status != "delivered" for status in statuses.values())
            or not isinstance(rendered, list)
            or any(not _is_sha256_identity(digest) for digest in rendered)
            or bool(records) != (raw_delivery.get("overall_status") == "delivered")
            or development_tasks not in ([], list(map(str, expected_tasks)))
        ):
            raise RunValidationError("development delivery benchmark state is invalid")
        has_delivery_record = has_delivery_record or bool(records)
        for ordinal, record in enumerate(records, start=1):
            assert isinstance(record, Mapping)
            fields = record.get("fields")
            evaluation_id = record.get("development_evaluation_id")
            task_ids = record.get("task_ids")
            record_digests = record.get("rendered_artifact_digests")
            if (
                record.get("ordinal") != ordinal
                or record.get("operation")
                not in {"materialize", "evaluation", "native_aggregate"}
                or not isinstance(fields, list)
                or not fields
                or any(field not in _DELIVERY_FIELDS for field in fields)
                or len(set(fields)) != len(fields)
                or not isinstance(task_ids, list)
                or not task_ids
                or any(not isinstance(task_id, str) for task_id in task_ids)
                or len(set(map(str, task_ids))) != len(task_ids)
                or not set(map(str, task_ids)).issubset(set(expected_tasks))
                or not isinstance(record_digests, list)
                or any(
                    not _is_sha256_identity(digest) or digest not in rendered
                    for digest in record_digests
                )
                or not isinstance(record.get("channel"), str)
                or not record.get("channel")
                or isinstance(record.get("delivered_at"), bool)
                or not isinstance(record.get("delivered_at"), (int, float))
                or not math.isfinite(float(record["delivered_at"]))
                or (
                    record.get("operation") == "materialize"
                    and evaluation_id is not None
                )
                or (
                    record.get("operation") != "materialize"
                    and (not isinstance(evaluation_id, str) or not evaluation_id)
                )
            ):
                raise RunValidationError("development delivery receipt is malformed")
            if "development_result" not in fields:
                continue
            key = (benchmark_id, str(evaluation_id))
            publication = publications.get(key)
            if (
                record.get("operation") != "evaluation"
                or key in seen_evaluations
                or publication is None
                or list(map(str, task_ids))
                != list(map(str, publication[1].get("task_ids", ())))
            ):
                raise RunValidationError("development delivery receipt is misbound")
            assert publication is not None
            result_path, result = publication
            result_digest = file_digest(result_path)
            if result_digest not in record_digests or result_digest not in rendered:
                raise RunValidationError(
                    "development delivery result digest is misbound"
                )
            seen_evaluations.add(key)
            delivered.append(
                MappingProxyType(
                    {
                        **dict(result),
                        "delivery_timestamp": float(record["delivered_at"]),
                    }
                )
            )
    if payload.get("status") != (
        "delivered" if has_delivery_record else "not_recorded"
    ):
        raise RunValidationError("development delivery campaign status is invalid")
    return tuple(delivered)


def _json_mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    try:
        encoded = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must contain bounded JSON-compatible data") from exc
    if not isinstance(decoded, dict):  # pragma: no cover - guarded above
        raise TypeError(f"{field} must be a mapping")
    return MappingProxyType(decoded)


def _artifact_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("development public artifact path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("development public artifact path is invalid")
    return path.as_posix()


def _bounded_feedback_value(value: object, *, text_limit: int) -> object:
    if isinstance(value, str):
        if len(value) <= text_limit:
            return value
        return value[: max(0, text_limit - 1)].rstrip() + "…"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_feedback_value(item, text_limit=text_limit)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_feedback_value(item, text_limit=text_limit) for item in value[:32]
        ]
    return value


def _bounded_failed_task_brief(value: Mapping[str, object]) -> dict[str, object]:
    for text_limit in (_FAILED_TASK_BRIEF_TEXT_LIMIT, 1_024, 512, 256):
        bounded = _bounded_feedback_value(value, text_limit=text_limit)
        if not isinstance(bounded, dict):  # pragma: no cover - mapping above
            raise TypeError("failed task brief must be a mapping")
        try:
            encoded = json.dumps(
                bounded,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("failed task brief must contain JSON data") from exc
        if len(encoded) <= _FAILED_TASK_BRIEF_BYTES_LIMIT:
            return bounded
    raise ValueError("failed task brief exceeds its bounded feedback budget")


def _failed_task_briefs(
    *,
    adapter: "DevelopmentAdapter",
    materials: Sequence["DevelopmentMaterial"],
    result: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Join measured zeroes to frozen public development material."""

    raw_results = result.get("task_results")
    formatter = getattr(adapter, "format_failed_task_brief", None)
    if not isinstance(raw_results, list) or not callable(formatter):
        return ()
    material_by_id = {material.task_id: material for material in materials}
    failed: list[tuple[str, Mapping[str, object]]] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        task_id = raw.get("task_id")
        score = raw.get("score")
        if (
            not isinstance(task_id, str)
            or task_id not in material_by_id
            or raw.get("outcome") != "measured"
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or score != 0
        ):
            continue
        failed.append((task_id, raw))

    briefs: list[Mapping[str, object]] = []
    total_bytes = 2
    for task_id, task_result in sorted(failed, key=lambda item: item[0])[
        :_FAILED_TASK_BRIEF_LIMIT
    ]:
        formatted = formatter(material_by_id[task_id])
        if not isinstance(formatted, Mapping):
            raise RunValidationError(
                "development adapter returned an invalid failed-task brief"
            )
        brief: dict[str, object] = {
            "task_id": task_id,
            "score": 0,
            **dict(formatted),
        }
        for field in ("diagnostic", "summary"):
            value = task_result.get(field)
            if value is not None:
                brief[field] = value
        bounded = _bounded_failed_task_brief(brief)
        encoded_size = len(
            json.dumps(
                bounded,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        separator_size = 1 if briefs else 0
        if (
            total_bytes + separator_size + encoded_size
            > _FAILED_TASK_BRIEFS_BYTES_LIMIT
        ):
            break
        briefs.append(MappingProxyType(bounded))
        total_bytes += separator_size + encoded_size
    return tuple(briefs)


@dataclass(frozen=True)
class DevelopmentArtifact:
    """One benchmark-approved public development artifact."""

    path: str
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _artifact_path(self.path))
        if not isinstance(self.content, bytes):
            raise TypeError("development public artifact content must be bytes")


@dataclass(frozen=True)
class DevelopmentMaterial:
    """Public material for exactly one frozen development task."""

    task_id: str
    task: Mapping[str, object]
    artifacts: tuple[DevelopmentArtifact, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "task_id", bounded_opaque_id(self.task_id, "material.task_id")
        )
        object.__setattr__(self, "task", _json_mapping(self.task, "material.task"))
        artifacts = tuple(self.artifacts)
        if any(not isinstance(item, DevelopmentArtifact) for item in artifacts):
            raise TypeError("development public material artifacts are invalid")
        if len({item.path for item in artifacts}) != len(artifacts):
            raise ValueError(
                "development public material artifact paths must be unique"
            )
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True)
class DevelopmentExecution:
    """Benchmark-approved public projection of one trusted development lifecycle.

    The benchmark adapter owns the canonical evaluator and its complete trusted
    evidence ledgers.  These fields contain only the neutral bytes that the
    campaign permits the shared owner to publish to the method mirror.
    """

    outcome: str
    result: Mapping[str, object]
    usage: Mapping[str, object]
    trajectory: tuple[Mapping[str, object], ...]
    artifacts: tuple[DevelopmentArtifact, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in {"measured", "inconclusive"}:
            raise ValueError("development execution outcome is invalid")
        object.__setattr__(
            self, "result", _json_mapping(self.result, "development result")
        )
        if self.result.get("outcome") != self.outcome:
            raise ValueError(
                "development result outcome must match the execution outcome"
            )
        object.__setattr__(
            self, "usage", _json_mapping(self.usage, "development usage")
        )
        object.__setattr__(
            self,
            "trajectory",
            tuple(
                _json_mapping(item, "development trajectory event")
                for item in self.trajectory
            ),
        )
        artifacts = tuple(self.artifacts)
        if any(not isinstance(item, DevelopmentArtifact) for item in artifacts):
            raise TypeError("development execution artifacts are invalid")
        if len({item.path for item in artifacts}) != len(artifacts):
            raise ValueError("development execution artifact paths must be unique")
        object.__setattr__(self, "artifacts", artifacts)


def _development_usage_calls(
    usage: Mapping[str, object],
    *,
    development_evaluation_id: str,
    benchmark_id: str,
    task_ids: tuple[str, ...],
) -> tuple[tuple[str, str, Mapping[str, object]], ...]:
    """Normalize benchmark-owned A usage into the one run usage ledger."""

    raw_tasks = usage.get("tasks")
    if not isinstance(raw_tasks, list) or any(
        not isinstance(item, Mapping) for item in raw_tasks
    ):
        raise RunValidationError("development usage tasks are invalid")
    normalized: list[tuple[str, str, Mapping[str, object]]] = []
    seen_tasks: set[str] = set()
    allowed_tasks = set(task_ids)
    for raw_task in raw_tasks:
        assert isinstance(raw_task, Mapping)
        task_id = raw_task.get("task_id")
        if (
            not isinstance(task_id, str)
            or not task_id
            or task_id not in allowed_tasks
            or task_id in seen_tasks
        ):
            raise RunValidationError("development usage task binding is invalid")
        seen_tasks.add(task_id)
        raw_calls = raw_task.get("calls")
        if isinstance(raw_calls, list) and all(
            isinstance(call, Mapping) for call in raw_calls
        ):
            calls = tuple(raw_calls)
        else:
            raise RunValidationError(
                "development usage must contain explicit model-call facts"
            )
        if not calls and (
            any(
                raw_task.get(name) is not None
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_tokens",
                    "provider_reported_cost",
                    "cost_usd",
                )
            )
            or raw_task.get("cost_status") == "reported"
        ):
            raise RunValidationError(
                "development aggregate usage lacks model-call facts"
            )
        for ordinal, call in enumerate(calls, start=1):
            assert isinstance(call, Mapping)
            normalized.append(
                _development_usage_call(
                    usage,
                    raw_task,
                    call,
                    development_evaluation_id=development_evaluation_id,
                    benchmark_id=benchmark_id,
                    task_id=task_id,
                    ordinal=ordinal,
                    call_count=len(calls),
                )
            )
    return tuple(normalized)


def _development_usage_call(
    root: Mapping[str, object],
    task: Mapping[str, object],
    call: Mapping[str, object],
    *,
    development_evaluation_id: str,
    benchmark_id: str,
    task_id: str,
    ordinal: int,
    call_count: int,
) -> tuple[str, str, Mapping[str, object]]:
    raw_role = str(call.get("role", task.get("role", "solver-agent")))
    role = "solver-agent" if raw_role == "development_task_worker" else raw_role
    if role not in ROLES:
        raise RunValidationError("development usage role is invalid")
    provider = str(call.get("provider", task.get("provider", "unknown"))).strip()
    model = str(
        call.get("model", task.get("model", root.get("model", "unknown")))
    ).strip()
    if not provider or not model:
        raise RunValidationError("development usage provider/model is invalid")
    logical_ordinal = call.get("ordinal", ordinal)
    if (
        isinstance(logical_ordinal, bool)
        or not isinstance(logical_ordinal, int)
        or logical_ordinal != ordinal
    ):
        raise RunValidationError("development usage call ordinal is invalid")
    provider_call_status = call.get("status", "completed")
    if provider_call_status not in {
        "completed",
        "in_flight",
        "response_oversized",
        "transport_failed",
    }:
        raise RunValidationError("development provider call status is invalid")
    provider_id_fields: dict[str, str | None] = {}
    for source_name, target_name in (
        ("response_id", "provider_response_id"),
        ("generation_id", "provider_generation_id"),
    ):
        value = call.get(source_name)
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", value) is None
        ):
            raise RunValidationError("development provider call identity is invalid")
        provider_id_fields[target_name] = value

    tokens_in = _optional_usage_count(call.get("input_tokens"), "input_tokens")
    tokens_out = _optional_usage_count(call.get("output_tokens"), "output_tokens")
    cache_read = _optional_usage_count(
        call.get("cache_read_tokens"), "cache_read_tokens"
    )
    cache_tokens = _optional_usage_count(call.get("cache_tokens"), "cache_tokens")
    token_status = (
        "reported"
        if tokens_in is not None and tokens_out is not None
        else "unverified"
        if tokens_in is not None
        or tokens_out is not None
        or cache_read is not None
        or cache_tokens is not None
        else "missing"
    )
    cost_value = call.get("cost_usd", call.get("provider_reported_cost"))
    if cost_value is None and call is task:
        cost_value = task.get("provider_reported_cost")
    cost = _optional_usage_cost(cost_value)
    if cost_value is not None and cost is None:
        raise RunValidationError("development provider call cost is invalid")
    declared_cost_status = str(
        call.get("cost_status", task.get("cost_status", "missing"))
    )
    if declared_cost_status not in {"reported", "missing", "unverified"}:
        raise RunValidationError("development usage cost status is invalid")
    if declared_cost_status == "reported" and cost is None:
        raise RunValidationError("reported development cost is unavailable")
    cost_status = "reported" if cost is not None else declared_cost_status
    raw_wall = task.get("wall_seconds", 0.0) if ordinal == 1 else 0.0
    wall_seconds = _optional_usage_cost(raw_wall)
    if wall_seconds is None:
        raise RunValidationError("development usage wall time is invalid")

    fields: dict[str, object] = {
        "evaluation_id": development_evaluation_id,
        "benchmark_id": benchmark_id,
        "task_id": task_id,
        "request_id": f"{development_evaluation_id}:{benchmark_id}:{task_id}",
        "call_id": (
            f"{development_evaluation_id}:{benchmark_id}:{task_id}:model-call:{ordinal}"
        ),
        "logical_call_ordinal": logical_ordinal,
        "provider_response_id": provider_id_fields["provider_response_id"],
        "provider_generation_id": provider_id_fields["provider_generation_id"],
        "provider_call_status": provider_call_status,
        "wall_seconds": wall_seconds if call_count else 0.0,
        "spend_kind": "evaluation",
        "usage_scope": "development",
        "provider": provider,
        "token_source": str(
            call.get("token_source", task.get("token_source", "provider"))
        ),
        "token_status": token_status,
        "input_token_status": "reported" if tokens_in is not None else "missing",
        "output_token_status": "reported" if tokens_out is not None else "missing",
        "cache_token_status": (
            "reported"
            if cache_read is not None or cache_tokens is not None
            else "missing"
        ),
        "cache_status": str(
            call.get("cache_status", task.get("cache_status", "unknown"))
        ),
        "cost_usd": cost,
        "provider_reported_cost": cost if cost_status == "reported" else None,
        "cost_source": "provider" if cost_status == "reported" else "unknown",
        "cost_status": cost_status,
    }
    if tokens_in is not None:
        fields["tokens_in"] = tokens_in
    if tokens_out is not None:
        fields["tokens_out"] = tokens_out
    if cache_read is not None:
        fields["cache_read_tokens"] = cache_read
    if cache_tokens is not None:
        fields["cache_tokens"] = cache_tokens
    return role, model, MappingProxyType(fields)


def _optional_usage_count(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunValidationError(f"development usage {label} is invalid")
    return value


def _optional_usage_cost(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


class DevelopmentAdapter(Protocol):
    """One trusted, benchmark-owned development implementation."""

    @property
    def benchmark_id(self) -> str: ...

    @property
    def frozen_task_ids(self) -> Sequence[str]: ...

    def public_material(self, task_id: str) -> DevelopmentMaterial: ...

    def format_failed_task_brief(
        self, material: DevelopmentMaterial
    ) -> Mapping[str, object]: ...

    def execute(
        self,
        stable_code_view: Path,
        task_ids: tuple[str, ...],
        *,
        development_evaluation_id: str,
        stable_code_view_id: str,
    ) -> DevelopmentExecution: ...


class DevelopmentOperations(Protocol):
    """The fixed service-facing development run/run-set owner seam."""

    @property
    def layout(self) -> RunLayout: ...

    @property
    def frozen_task_ids(self) -> tuple[str, ...]: ...

    def run(
        self, task_id: str, *, candidate_workspace: Path | None = None
    ) -> DevelopmentResult: ...

    def run_set(
        self,
        task_ids: Sequence[str],
        *,
        candidate_workspace: Path | None = None,
    ) -> DevelopmentResult: ...

    def run_all(
        self, *, candidate_workspace: Path | None = None
    ) -> DevelopmentResult: ...

    def freeze_public_result(self, result: DevelopmentResult) -> DevelopmentResult: ...


class NativeDevelopmentOperations(DevelopmentOperations, Protocol):
    """Trusted development projection/delivery seam for a native MethodRunner."""

    def trusted_native_projection(
        self, result: DevelopmentResult
    ) -> Mapping[str, object]: ...

    def trusted_native_feedback_projection(
        self, result: DevelopmentResult
    ) -> Mapping[str, object]: ...

    def trusted_native_trace_artifacts(
        self, result: DevelopmentResult
    ) -> Mapping[str, Mapping[str, object]]: ...

    def record_native_aggregate_delivery(
        self,
        *,
        development_evaluation_id: str,
        development_outcome: str,
        native_request_id: str,
        rendered: str,
        response_fields: Sequence[str],
        channel: str,
        recipient: str,
    ) -> None: ...


class RunScopedDevelopmentOperations:
    """Execute development tasks against a temporary candidate view."""

    def __init__(
        self,
        *,
        layout: RunLayout,
        adapter: DevelopmentAdapter,
        workspace: Path | None = None,
        candidate_root: Path | None = None,
        mirror_root: Path | None = None,
        benchmark_namespace: str | None = None,
        usage_ledger: UsageLedger | None = None,
    ) -> None:
        self.layout = layout
        self.adapter = adapter
        self.workspace = layout.assert_workspace_isolated(
            workspace or layout.method_root / "workspace"
        )
        self.candidate_root = layout.assert_workspace_isolated(
            candidate_root or self.workspace
        )
        try:
            self.candidate_root.relative_to(self.workspace)
        except ValueError as exc:
            raise RunLayoutError(
                "development candidate root must be contained by the workspace"
            ) from exc
        self.benchmark_id = bounded_opaque_id(
            adapter.benchmark_id, "development benchmark_id"
        )
        self.benchmark_namespace = (
            bounded_opaque_id(benchmark_namespace, "development benchmark namespace")
            if benchmark_namespace is not None
            else None
        )
        self._benchmark_namespace_segment = (
            _benchmark_segment(self.benchmark_namespace)
            if self.benchmark_namespace is not None
            else None
        )
        task_ids = tuple(
            bounded_opaque_id(task_id, "development task_id")
            for task_id in adapter.frozen_task_ids
        )
        if not task_ids or len(set(task_ids)) != len(task_ids):
            raise RunValidationError("development adapter frozen membership is invalid")
        self.frozen_task_ids = task_ids
        self._task_id_set = frozenset(task_ids)
        expected_mirror = self.workspace / ".rsi-arena" / "development"
        if self._benchmark_namespace_segment is not None:
            expected_mirror /= self._benchmark_namespace_segment
        self.mirror_root = Path(mirror_root or expected_mirror).expanduser().absolute()
        if self.mirror_root != expected_mirror:
            if self._benchmark_namespace_segment is None:
                raise RunLayoutError(
                    "development mirror must use workspace/.rsi-arena/development"
                )
            raise RunLayoutError("development mirror changed its run-owned identity")
        self._publication_root = layout.root / "development" / "publications"
        self._views_root = layout.trusted_runtime_root / "development-views"
        if self._benchmark_namespace_segment is not None:
            self._publication_root /= self._benchmark_namespace_segment
            self._views_root /= self._benchmark_namespace_segment
        self._lock = threading.RLock()
        self._usage_ledger = usage_ledger
        self._materials = self._capture_materials()
        self._initial_files = self._render_initial_files()
        self._publication_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._views_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._index = self._load_publication_index()
        self._frozen_public_evidence: dict[str, str | None] = {}
        self._reconcile_usage_publications()
        self._initialize_mirror()

    @property
    def evidence_root(self) -> Path:
        """Host-side public mirror root used for service confinement checks."""

        return self.mirror_root

    def run(
        self, task_id: str, *, candidate_workspace: Path | None = None
    ) -> DevelopmentResult:
        return self.run_set((task_id,), candidate_workspace=candidate_workspace)

    def run_all(self, *, candidate_workspace: Path | None = None) -> DevelopmentResult:
        return self.run_set(
            self.frozen_task_ids, candidate_workspace=candidate_workspace
        )

    def run_set(
        self,
        task_ids: Sequence[str],
        *,
        candidate_workspace: Path | None = None,
    ) -> DevelopmentResult:
        tasks = tuple(
            bounded_opaque_id(task_id, "development task_id") for task_id in task_ids
        )
        if (
            not tasks
            or len(set(tasks)) != len(tasks)
            or not set(tasks).issubset(self._task_id_set)
        ):
            raise RunValidationError(
                "development request escaped frozen benchmark membership"
            )
        candidate_root = self.candidate_root
        if candidate_workspace is not None:
            candidate_root = self.layout.assert_workspace_isolated(
                Path(candidate_workspace)
            )
            try:
                candidate_root.relative_to(self.workspace)
            except ValueError as exc:
                raise RunLayoutError(
                    "development candidate root must be contained by the workspace"
                ) from exc
        with self._lock:
            return self._execute(
                tasks, candidate_root=candidate_root, allow_safe_symlinks=True
            )

    def run_trusted_native_set(
        self,
        task_ids: Sequence[str],
        *,
        candidate_workspace: Path,
    ) -> DevelopmentResult:
        """Snapshot an Arena-owned materialization while preserving safe links."""

        tasks = tuple(
            bounded_opaque_id(task_id, "development task_id") for task_id in task_ids
        )
        if (
            not tasks
            or len(set(tasks)) != len(tasks)
            or not set(tasks).issubset(self._task_id_set)
        ):
            raise RunValidationError(
                "native development request escaped frozen benchmark membership"
            )
        candidate_root = self.layout.assert_workspace_isolated(candidate_workspace)
        try:
            candidate_root.relative_to(self.workspace)
        except ValueError as exc:
            raise RunLayoutError(
                "native development candidate root must be contained by the workspace"
            ) from exc
        with self._lock:
            return self._execute(
                tasks,
                candidate_root=candidate_root,
                allow_safe_symlinks=True,
            )

    def trusted_native_projection(
        self, result: DevelopmentResult
    ) -> Mapping[str, object]:
        """Read one immutable run-owned publication, never the mutable mirror."""

        evaluation_id = bounded_opaque_id(
            result.development_evaluation_id, "development_evaluation_id"
        )
        publication = self._publication_root / evaluation_id
        try:
            publication_info = publication.lstat()
            result_path = publication / "result.json"
            result_info = result_path.lstat()
        except OSError as exc:
            raise RunValidationError(
                "canonical development publication is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(publication_info.st_mode)
            or not stat.S_ISDIR(publication_info.st_mode)
            or stat.S_ISLNK(result_info.st_mode)
            or not stat.S_ISREG(result_info.st_mode)
        ):
            raise RunValidationError("canonical development publication is invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(result_path, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RunValidationError(
                "canonical development publication is unreadable"
            ) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != _RESULT_FIELDS
            or payload.get("schema") != _RESULT_SCHEMA
            or payload.get("run_id") != self.layout.run_id
            or payload.get("campaign_id") != self.layout.campaign
            or payload.get("method") != self.layout.method
            or payload.get("benchmark_id") != self.benchmark_id
            or payload.get("development_evaluation_id") != evaluation_id
            or payload.get("state") != "TERMINAL"
            or not isinstance(payload.get("result"), Mapping)
            or payload["result"].get("outcome") != result.outcome
        ):
            raise RunValidationError(
                "canonical development publication changed identity"
            )
        return MappingProxyType(payload)

    @staticmethod
    def _read_native_trace_file(path: Path, *, maximum: int) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                    raise OSError("native trace artifact violates its file boundary")
                payload = stream.read(maximum + 1)
        except OSError as exc:
            raise RunValidationError(
                "canonical development trace artifact is invalid"
            ) from exc
        if len(payload) > maximum:
            raise RunValidationError(
                "canonical development trace artifact exceeds its byte bound"
            )
        return payload

    def trusted_native_trace_artifacts(
        self, result: DevelopmentResult
    ) -> Mapping[str, Mapping[str, object]]:
        """Return digest-checked Harbor traces from the immutable publication."""

        projection = self.trusted_native_projection(result)
        evaluation_id = str(projection["development_evaluation_id"])
        publication = self._publication_root / evaluation_id
        trajectory_path = publication / "trajectory.jsonl"
        try:
            raw_events = self._read_native_trace_file(
                trajectory_path, maximum=_NATIVE_TRACE_MAX_BYTES
            )
        except RunValidationError:
            if not trajectory_path.exists():
                return MappingProxyType({})
            raise
        task_ids = tuple(map(str, projection.get("task_ids", ())))
        selected: dict[str, Mapping[str, object]] = {}
        for line in raw_events.splitlines():
            try:
                event = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RunValidationError(
                    "canonical development trajectory is unreadable"
                ) from exc
            if not isinstance(event, Mapping):
                raise RunValidationError(
                    "canonical development trajectory event is invalid"
                )
            if event.get("event") != "development_task_agent_trajectory":
                continue
            task_id = event.get("task_id")
            partial = event.get("partial")
            status = event.get("status")
            artifact_path = event.get("artifact_path")
            digest = event.get("artifact_digest")
            expected_path = f"artifacts/{task_id}/agent-trajectory.json"
            provider_path = event.get("provider_diagnostics_path")
            provider_digest = event.get("provider_diagnostics_digest")
            if (
                not isinstance(task_id, str)
                or task_id not in task_ids
                or task_id in selected
                or not isinstance(partial, bool)
                or status not in {"completed", "timeout"}
                or partial != (status == "timeout")
                or (artifact_path is None and provider_path is None)
            ):
                raise RunValidationError(
                    "canonical development trace binding is invalid"
                )
            trajectory = None
            if artifact_path is not None:
                if artifact_path != expected_path:
                    raise RunValidationError("canonical development trace path changed")
                trajectory = self._read_native_trace_file(
                    publication / expected_path,
                    maximum=_NATIVE_TRACE_MAX_BYTES,
                )
                if "sha256:" + hashlib.sha256(trajectory).hexdigest() != digest:
                    raise RunValidationError(
                        "canonical development trace digest changed"
                    )
            elif digest is not None:
                raise RunValidationError("canonical development trace lacks its file")
            provider_diagnostics = None
            if provider_path is not None:
                if provider_path != f"artifacts/{task_id}/provider-diagnostics.json":
                    raise RunValidationError(
                        "canonical provider diagnostics path changed"
                    )
                provider_bytes = self._read_native_trace_file(
                    publication / provider_path, maximum=512 * 1024
                )
                if (
                    "sha256:" + hashlib.sha256(provider_bytes).hexdigest()
                    != provider_digest
                ):
                    raise RunValidationError(
                        "canonical provider diagnostics digest changed"
                    )
                try:
                    provider_diagnostics = json.loads(provider_bytes)
                except (UnicodeError, ValueError) as exc:
                    raise RunValidationError(
                        "canonical provider diagnostics are unreadable"
                    ) from exc
                if not isinstance(provider_diagnostics, dict):
                    raise RunValidationError(
                        "canonical provider diagnostics are invalid"
                    )
            elif provider_digest is not None:
                raise RunValidationError(
                    "canonical provider diagnostics lack their file"
                )
            pane_path = publication / "artifacts" / task_id / "terminus-pane.txt"
            pane = (
                self._read_native_trace_file(pane_path, maximum=_NATIVE_PANE_MAX_BYTES)
                if pane_path.exists()
                else None
            )
            selected[task_id] = MappingProxyType(
                {
                    "trajectory": trajectory,
                    "pane": pane,
                    "partial": partial,
                    "artifact_digest": digest,
                    "provider_diagnostics": provider_diagnostics,
                }
            )
        return MappingProxyType(dict(sorted(selected.items())))

    def trusted_native_feedback_projection(
        self, result: DevelopmentResult
    ) -> Mapping[str, object]:
        """Enrich native-only feedback without changing canonical publication."""

        projection = self.trusted_native_projection(result)
        public_result = projection.get("result")
        if not isinstance(public_result, Mapping):  # pragma: no cover - validated above
            raise RunValidationError("canonical development publication lacks a result")
        enriched_result = dict(public_result)
        enriched_result["failed_task_briefs"] = [
            dict(brief)
            for brief in _failed_task_briefs(
                adapter=self.adapter,
                materials=self._materials,
                result=public_result,
            )
        ]
        return MappingProxyType({**dict(projection), "result": enriched_result})

    def freeze_public_result(self, result: DevelopmentResult) -> DevelopmentResult:
        """Bind one public response to canonical authority and optional mirror state."""

        with self._lock:
            projection = self.trusted_native_projection(result)
            evaluation_id = str(projection["development_evaluation_id"])
            expected = self.mirror_root / "evaluations" / evaluation_id
            if result.evidence_path is not None:
                supplied = Path(result.evidence_path).expanduser().absolute()
                if ".." in supplied.parts or supplied != expected:
                    raise RunValidationError(
                        "development public evidence binding changed"
                    )
            if evaluation_id in self._frozen_public_evidence:
                evidence_path = self._frozen_public_evidence[evaluation_id]
                return DevelopmentResult(result.outcome, evaluation_id, evidence_path)

            evidence_path: str | None = None
            if result.evidence_path is not None:
                try:
                    arena = self.workspace / ".rsi-arena"
                    evaluations = self.mirror_root / "evaluations"
                    result_path = expected / "result.json"
                    index_path = self.mirror_root / "index.json"
                    directories = (arena, self.mirror_root, evaluations, expected)
                    if any(
                        stat.S_ISLNK(path.lstat().st_mode)
                        or not stat.S_ISDIR(path.lstat().st_mode)
                        for path in directories
                    ):
                        raise RunLayoutError(
                            "development optional mirror directory is invalid"
                        )
                    result_info = result_path.lstat()
                    index_info = index_path.lstat()
                    if (
                        stat.S_ISLNK(result_info.st_mode)
                        or not stat.S_ISREG(result_info.st_mode)
                        or stat.S_ISLNK(index_info.st_mode)
                        or not stat.S_ISREG(index_info.st_mode)
                    ):
                        raise RunLayoutError(
                            "development optional mirror file is invalid"
                        )
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    result_descriptor = os.open(result_path, flags)
                    with os.fdopen(result_descriptor, "rb") as stream:
                        mirror_result = stream.read()
                    index_descriptor = os.open(index_path, flags)
                    with os.fdopen(index_descriptor, "rb") as stream:
                        mirror_index = stream.read()
                    if (
                        mirror_result != _json_bytes(dict(projection))
                        or mirror_index != self._index_bytes()
                    ):
                        raise RunLayoutError(
                            "development optional mirror content changed"
                        )
                except (OSError, RunLayoutError):
                    _LOG.warning(
                        "development optional mirror dropped: evaluation=%s",
                        evaluation_id,
                    )
                else:
                    evidence_path = str(expected)
            self._frozen_public_evidence[evaluation_id] = evidence_path
            return DevelopmentResult(result.outcome, evaluation_id, evidence_path)

    def record_native_aggregate_delivery(
        self,
        *,
        development_evaluation_id: str,
        development_outcome: str,
        native_request_id: str,
        rendered: str,
        response_fields: Sequence[str],
        channel: str,
        recipient: str,
    ) -> None:
        """Record selected-parent-only delivery by development evaluation ID."""

        request_id = bounded_opaque_id(native_request_id, "native_request_id")
        actual_fields = tuple(map(str, response_fields))
        if (
            not rendered
            or not actual_fields
            or any(not field for field in actual_fields)
            or len(set(actual_fields)) != len(actual_fields)
            or not channel
            or not recipient
        ):
            raise ValueError("native development delivery evidence is incomplete")
        with self._lock, _DEVELOPMENT_DELIVERY_LOCK:
            projection = self.trusted_native_projection(
                DevelopmentResult(development_outcome, development_evaluation_id, None)
            )
            evaluation_id = str(projection["development_evaluation_id"])
            task_ids = tuple(map(str, projection.get("task_ids", ())))
            digest = "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()
            _manifest, _benchmarks, delivery = self._delivery_manifest()
            records = delivery.get("delivery_records", [])
            if not isinstance(records, list):
                raise RunValidationError("development delivery records are invalid")
            expected = {
                "operation": "native_aggregate",
                "development_evaluation_id": evaluation_id,
                "native_request_id": request_id,
                "task_ids": list(task_ids),
                "fields": ["development_artifacts"],
                "rendered_artifact_digests": [digest],
                "response_fields": list(actual_fields),
                "channel": channel,
                "recipient": recipient,
            }
            matching = [
                row
                for row in records
                if isinstance(row, Mapping)
                and row.get("operation") == "native_aggregate"
                and row.get("development_evaluation_id") == evaluation_id
                and row.get("native_request_id") == request_id
            ]
            if matching:
                observed = dict(matching[0])
                observed.pop("ordinal", None)
                observed.pop("delivered_at", None)
                if len(matching) != 1 or observed != expected:
                    raise RunValidationError(
                        "native development delivery binding changed"
                    )
                return
            self._record_delivery(
                fields=("development_artifacts",),
                paths=(),
                operation="native_aggregate",
                evaluation_id=evaluation_id,
                task_ids=task_ids,
                rendered_digests=(digest,),
                response_fields=actual_fields,
                channel=channel,
                native_request_id=request_id,
                recipient=recipient,
            )

    def record_public_response_delivered(
        self,
        result: DevelopmentResult,
        *,
        rendered: str,
        response_fields: Sequence[str],
        channel: str,
        recipient: str,
    ) -> None:
        """Record dynamic fields only after publishing the terminal response."""

        actual_fields = tuple(map(str, response_fields))
        if (
            not rendered
            or not actual_fields
            or any(not field for field in actual_fields)
            or len(set(actual_fields)) != len(actual_fields)
            or not channel
            or not recipient
        ):
            raise ValueError("public development delivery evidence is incomplete")
        with self._lock:
            projection = self.trusted_native_projection(result)
            evaluation_id = str(projection["development_evaluation_id"])
            task_ids = tuple(map(str, projection.get("task_ids", ())))
            publication = self._publication_root / evaluation_id
            fields = ["development_result"]
            optional_digests: list[str] = []
            result_path = publication / "result.json"
            try:
                optional_digests.append(file_digest(result_path))
            except OSError:
                _LOG.warning(
                    "development result delivery digest dropped: evaluation=%s",
                    evaluation_id,
                )
            usage_path = publication / "usage.json"
            if usage_path.is_file():
                try:
                    optional_digests.append(file_digest(usage_path))
                except OSError:
                    _LOG.warning(
                        "development usage delivery digest dropped: evaluation=%s",
                        evaluation_id,
                    )
                else:
                    fields.append("development_usage")
            trajectory_path = publication / "trajectory.jsonl"
            if trajectory_path.is_file():
                try:
                    optional_digests.append(file_digest(trajectory_path))
                except OSError:
                    _LOG.warning(
                        "development trajectory JSONL dropped: evaluation=%s stage=%s",
                        evaluation_id,
                        "delivery-digest",
                    )
                else:
                    fields.append("development_trajectory")
            artifact_paths = sorted(
                path
                for path in (publication / "artifacts").rglob("*")
                if path.is_file()
            )
            artifact_delivered = False
            for artifact_path in artifact_paths:
                relative = artifact_path.relative_to(
                    publication / "artifacts"
                ).as_posix()
                task_id = _trajectory_bundle_task(relative, task_ids)
                try:
                    optional_digests.append(file_digest(artifact_path))
                except OSError:
                    _LOG.warning(
                        "development artifact delivery digest dropped: "
                        "task=%s file=%s stage=%s",
                        task_id or "none",
                        relative,
                        "delivery-digest",
                    )
                else:
                    artifact_delivered = True
            if artifact_delivered:
                fields.append("development_artifacts")
            response_digest = "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()
            self._record_delivery(
                fields=tuple(fields),
                paths=(),
                operation="evaluation",
                evaluation_id=evaluation_id,
                task_ids=task_ids,
                rendered_digests=(*optional_digests, response_digest),
                response_fields=actual_fields,
                channel=channel,
                recipient=recipient,
            )

    def _capture_materials(self) -> tuple[DevelopmentMaterial, ...]:
        materials: list[DevelopmentMaterial] = []
        for task_id in self.frozen_task_ids:
            material = self.adapter.public_material(task_id)
            if not isinstance(material, DevelopmentMaterial):
                raise RunValidationError(
                    "development adapter returned invalid public material"
                )
            if material.task_id != task_id:
                raise RunValidationError(
                    "development public material changed task identity"
                )
            materials.append(material)
        return tuple(materials)

    def _render_initial_files(self) -> Mapping[str, bytes]:
        files: dict[str, bytes] = {}
        tasks: list[dict[str, object]] = []
        for material in self._materials:
            segment = _task_segment(material.task_id)
            task_path = f"materials/{segment}/task.json"
            files[task_path] = _json_bytes(
                {
                    "schema": "rsi-arena-development-task-material",
                    "run_id": self.layout.run_id,
                    "benchmark_id": self.benchmark_id,
                    "task_id": material.task_id,
                    "material": dict(material.task),
                }
            )
            for artifact in material.artifacts:
                files[f"materials/{segment}/artifacts/{artifact.path}"] = (
                    artifact.content
                )
            tasks.append(
                {
                    "task_id": material.task_id,
                    "benchmark_id": self.benchmark_id,
                    "material_path": task_path,
                    "artifacts_path": f"materials/{segment}/artifacts",
                }
            )
        files["manifest.json"] = _json_bytes(
            {
                "schema": _MIRROR_SCHEMA,
                "run_id": self.layout.run_id,
                "campaign_id": self.layout.campaign,
                "method": self.layout.method,
                "benchmark_id": self.benchmark_id,
                "task_ids": list(self.frozen_task_ids),
                "tasks": tasks,
                "authority": "method-mutable-copy-only",
            }
        )
        return MappingProxyType(files)

    def _initialize_mirror(self) -> None:
        mirror = self._prepare_mirror_root()
        if mirror != self.mirror_root:
            raise RunLayoutError("development mirror identity changed")
        _ensure_child_directory(mirror, "materials")
        _ensure_child_directory(mirror, "evaluations")
        for relative, content in self._initial_files.items():
            destination = _safe_mirror_destination(mirror, relative)
            _write_if_missing(destination, content)
        index_path = mirror / "index.json"
        _write_if_missing(index_path, self._index_bytes())
        if not self._delivery_has("development_material"):
            self._record_delivery(
                fields=("development_material",),
                paths=tuple(
                    _safe_mirror_destination(mirror, relative)
                    for relative in self._initial_files
                ),
                operation="materialize",
                evaluation_id=None,
                task_ids=self.frozen_task_ids,
            )

    def _execute(
        self,
        task_ids: tuple[str, ...],
        *,
        candidate_root: Path,
        allow_safe_symlinks: bool = False,
    ) -> DevelopmentResult:
        evaluation_id = f"development-evaluation-{uuid.uuid4().hex}"
        view_id = f"development-view-{uuid.uuid4().hex}"
        view = self._views_root / view_id
        try:
            snapshot_worktree(
                candidate_root,
                view,
                allow_safe_symlinks=allow_safe_symlinks,
            )
            execution = self.adapter.execute(
                view,
                task_ids,
                development_evaluation_id=evaluation_id,
                stable_code_view_id=view_id,
            )
            if not isinstance(execution, DevelopmentExecution):
                raise RunValidationError(
                    "development adapter returned invalid public evidence"
                )
            evidence = self._publish_evaluation(
                evaluation_id=evaluation_id,
                view_id=view_id,
                task_ids=task_ids,
                execution=execution,
            )
            return DevelopmentResult(
                execution.outcome,
                evaluation_id,
                str(evidence) if evidence is not None else None,
            )
        finally:
            _remove_path(view)

    def _publish_evaluation(
        self,
        *,
        evaluation_id: str,
        view_id: str,
        task_ids: tuple[str, ...],
        execution: DevelopmentExecution,
    ) -> Path | None:
        result_bytes = _json_bytes(
            {
                "schema": _RESULT_SCHEMA,
                "run_id": self.layout.run_id,
                "campaign_id": self.layout.campaign,
                "method": self.layout.method,
                "benchmark_id": self.benchmark_id,
                "development_evaluation_id": evaluation_id,
                "state": "TERMINAL",
                "task_ids": list(task_ids),
                "stable_code_view": {
                    "view_id": view_id,
                    "durable_candidate": False,
                },
                "result": dict(execution.result),
            }
        )
        usage_bytes = _json_bytes(
            {
                "schema": _USAGE_SCHEMA,
                "run_id": self.layout.run_id,
                "development_evaluation_id": evaluation_id,
                "benchmark_id": self.benchmark_id,
                "usage": dict(execution.usage),
            }
        )
        trajectory = tuple(
            {
                **dict(event),
                "schema": _TRAJECTORY_SCHEMA,
                "run_id": self.layout.run_id,
                "development_evaluation_id": evaluation_id,
                "benchmark_id": self.benchmark_id,
            }
            for event in execution.trajectory
        )
        trajectory_artifact_tasks = _trajectory_artifact_tasks(
            task_ids,
            trajectory,
            execution.artifacts,
        )
        publication, canonical_artifact_paths, canonical_trajectory = (
            self._publish_evidence_directory(
                self._publication_root,
                evaluation_id,
                result_bytes=result_bytes,
                usage_bytes=usage_bytes,
                trajectory=trajectory,
                artifacts=execution.artifacts,
                trajectory_artifact_tasks=trajectory_artifact_tasks,
                stage="canonical-publication",
            )
        )
        self._record_usage_publication(publication)
        mirror_artifacts = [
            artifact
            for artifact in execution.artifacts
            if artifact.path in canonical_artifact_paths
        ]
        mirror_trajectory_artifact_tasks = {
            path: trajectory_artifact_tasks[path]
            for path in canonical_artifact_paths
            if path in trajectory_artifact_tasks
        }
        public: Path | None = None
        try:
            evaluations = self._prepare_mirror_evaluations()
            public, _mirror_artifact_paths, _mirror_trajectory = (
                self._publish_evidence_directory(
                    evaluations,
                    evaluation_id,
                    result_bytes=(publication / "result.json").read_bytes(),
                    usage_bytes=(
                        (publication / "usage.json").read_bytes()
                        if (publication / "usage.json").is_file()
                        else None
                    ),
                    trajectory=canonical_trajectory,
                    artifacts=tuple(mirror_artifacts),
                    trajectory_artifact_tasks=mirror_trajectory_artifact_tasks,
                    stage="mirror-publication",
                )
            )
        except (OSError, RunLayoutError):
            _LOG.warning(
                "development mirror publication dropped: evaluation=%s", evaluation_id
            )
        if public is None:
            return None
        record = {
            "development_evaluation_id": evaluation_id,
            "outcome": execution.outcome,
            "task_ids": list(task_ids),
            "evidence_path": f"evaluations/{evaluation_id}",
        }
        self._index.append(record)
        try:
            self._publish_index()
        except OSError:
            _LOG.warning(
                "development public index update dropped: evaluation=%s", evaluation_id
            )
            return None
        return public

    def _reconcile_usage_publications(self) -> None:
        """Repair missing global usage rows from immutable A terminals."""

        if self._usage_ledger is None:
            return
        for publication in sorted(self._publication_root.iterdir()):
            if publication.name.startswith("."):
                continue
            info = publication.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RunValidationError(
                    "canonical development publication is not a directory"
                )
            self._record_usage_publication(publication)

    def _record_usage_publication(self, publication: Path) -> None:
        ledger = self._usage_ledger
        if ledger is None:
            return
        result_path = publication / "result.json"
        usage_path = publication / "usage.json"
        try:
            result_info = result_path.lstat()
            usage_info = usage_path.lstat()
            if (
                stat.S_ISLNK(result_info.st_mode)
                or not stat.S_ISREG(result_info.st_mode)
                or stat.S_ISLNK(usage_info.st_mode)
                or not stat.S_ISREG(usage_info.st_mode)
            ):
                raise OSError("development usage authority is not regular")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            usage = json.loads(usage_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RunValidationError(
                "canonical development usage publication is invalid"
            ) from exc
        if (
            not isinstance(result, Mapping)
            or result.get("schema") != _RESULT_SCHEMA
            or result.get("run_id") != self.layout.run_id
            or result.get("method") != self.layout.method
            or result.get("benchmark_id") != self.benchmark_id
            or result.get("development_evaluation_id") != publication.name
            or result.get("state") != "TERMINAL"
            or not isinstance(result.get("task_ids"), list)
            or not isinstance(usage, Mapping)
            or usage.get("schema") != _USAGE_SCHEMA
            or usage.get("run_id") != self.layout.run_id
            or usage.get("benchmark_id") != self.benchmark_id
            or usage.get("development_evaluation_id") != publication.name
            or not isinstance(usage.get("usage"), Mapping)
        ):
            raise RunValidationError(
                "canonical development usage publication changed identity"
            )
        task_ids = tuple(map(str, result["task_ids"]))
        calls = _development_usage_calls(
            usage["usage"],
            development_evaluation_id=publication.name,
            benchmark_id=self.benchmark_id,
            task_ids=task_ids,
        )
        existing_by_call: dict[str, Mapping[str, object]] = {}
        for row in read_jsonl(ledger.path):
            call_id = row.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise RunValidationError("usage call identity is invalid")
            if call_id in existing_by_call:
                raise RunValidationError("usage call identity is duplicate")
            existing_by_call[call_id] = row
        for role, model, fields in calls:
            call_id = str(fields["call_id"])
            existing = existing_by_call.get(call_id)
            if existing is not None:
                expected = {"role": role, "model": model, **fields}
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise RunValidationError("development usage call changed on replay")
                continue
            ledger.record(role, model=model, **fields)

    @staticmethod
    def _publish_evidence_directory(
        parent: Path,
        evaluation_id: str,
        *,
        result_bytes: bytes,
        usage_bytes: bytes | None,
        trajectory: tuple[Mapping[str, object], ...] | None,
        artifacts: tuple[DevelopmentArtifact, ...],
        trajectory_artifact_tasks: Mapping[str, str],
        stage: str,
    ) -> tuple[
        Path,
        frozenset[str],
        tuple[Mapping[str, object], ...] | None,
    ]:
        staging = parent / f".{evaluation_id}.{uuid.uuid4().hex}.tmp"
        destination = parent / evaluation_id
        staging.mkdir(mode=0o700)
        try:
            atomic_write_bytes(staging / "result.json", result_bytes)
            if usage_bytes is None:
                raise RunLayoutError("development terminal requires usage authority")
            atomic_write_bytes(staging / "usage.json", usage_bytes)
            os.rename(staging, destination)
            _fsync_directory(destination.parent)
        except BaseException:
            _remove_path(staging)
            raise
        successful_artifact_paths: set[str] = set()
        for artifact in artifacts:
            task_id = trajectory_artifact_tasks.get(artifact.path)
            artifact_path = destination / "artifacts" / artifact.path
            try:
                atomic_write_bytes(artifact_path, artifact.content)
            except OSError:
                _discard_failed_atomic_write(artifact_path)
                _LOG.warning(
                    "development auxiliary artifact dropped: task=%s file=%s stage=%s",
                    task_id or "none",
                    artifact.path,
                    stage,
                )
            else:
                successful_artifact_paths.add(artifact.path)
        published_trajectory: tuple[Mapping[str, object], ...] | None = None
        if trajectory is not None:
            filtered_trajectory = _filter_trajectory_events(
                trajectory,
                successful_artifact_paths,
            )
            trajectory_path = destination / "trajectory.jsonl"
            trajectory_bytes = b"".join(
                _json_bytes(event) for event in filtered_trajectory
            )
            try:
                atomic_write_bytes(trajectory_path, trajectory_bytes)
            except OSError:
                _discard_failed_atomic_write(trajectory_path)
                _LOG.warning(
                    "development trajectory JSONL dropped: evaluation=%s stage=%s",
                    evaluation_id,
                    stage,
                )
            else:
                published_trajectory = filtered_trajectory
        return (
            destination,
            frozenset(successful_artifact_paths),
            published_trajectory,
        )

    def _prepare_mirror_evaluations(self) -> Path:
        mirror = self._prepare_mirror_root()
        if mirror != self.mirror_root:
            raise RunLayoutError("development mirror identity changed")
        return _ensure_child_directory(mirror, "evaluations")

    def _prepare_mirror_root(self) -> Path:
        arena = _ensure_child_directory(self.workspace, ".rsi-arena")
        mirror = _ensure_child_directory(arena, "development")
        if self._benchmark_namespace_segment is not None:
            mirror = _ensure_child_directory(mirror, self._benchmark_namespace_segment)
        return mirror

    def _load_publication_index(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for publication in sorted(self._publication_root.iterdir()):
            info = publication.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RunValidationError(
                    "development public publication source is invalid"
                )
            result_path = publication / "result.json"
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RunValidationError(
                    "development public publication source is invalid"
                ) from exc
            if (
                not isinstance(result, dict)
                or set(result) != _RESULT_FIELDS
                or result.get("schema") != _RESULT_SCHEMA
                or result.get("run_id") != self.layout.run_id
                or result.get("benchmark_id") != self.benchmark_id
                or result.get("development_evaluation_id") != publication.name
                or result.get("state") != "TERMINAL"
                or not isinstance(result.get("result"), Mapping)
                or result["result"].get("outcome") not in {"measured", "inconclusive"}
                or not isinstance(result.get("task_ids"), list)
                or not set(map(str, result["task_ids"])).issubset(self._task_id_set)
            ):
                raise RunValidationError(
                    "development public publication source changed identity"
                )
            mirror_publication = self.mirror_root / "evaluations" / publication.name
            mirror_result = mirror_publication / "result.json"
            try:
                mirror_publication_info = mirror_publication.lstat()
                mirror_result_info = mirror_result.lstat()
            except OSError:
                continue
            if (
                stat.S_ISLNK(mirror_publication_info.st_mode)
                or not stat.S_ISDIR(mirror_publication_info.st_mode)
                or stat.S_ISLNK(mirror_result_info.st_mode)
                or not stat.S_ISREG(mirror_result_info.st_mode)
            ):
                continue
            records.append(
                {
                    "development_evaluation_id": publication.name,
                    "outcome": result["result"]["outcome"],
                    "task_ids": list(map(str, result["task_ids"])),
                    "evidence_path": f"evaluations/{publication.name}",
                }
            )
        return records

    def _index_bytes(self) -> bytes:
        return _json_bytes(
            {
                "schema": _INDEX_SCHEMA,
                "run_id": self.layout.run_id,
                "evaluations": list(self._index),
            }
        )

    def _publish_index(self) -> None:
        _ensure_child_directory(self.mirror_root, "evaluations")
        atomic_write_bytes(self.mirror_root / "index.json", self._index_bytes())

    def _delivery_has(self, field: str) -> bool:
        with _DEVELOPMENT_DELIVERY_LOCK:
            _payload, _benchmarks, delivery = self._delivery_manifest()
            delivered = delivery.get("delivered_fields")
            if not isinstance(delivered, list) or any(
                not isinstance(item, str) for item in delivered
            ):
                raise RunValidationError("development delivered fields are invalid")
            return field in delivered

    def _delivery_manifest(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        path = self.layout.root / "development-delivery-manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunValidationError(
                "development delivery manifest is invalid"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != _DELIVERY_SCHEMA
            or payload.get("run_id") != self.layout.run_id
            or payload.get("method") != self.layout.method
            or payload.get("status") not in {"not_recorded", "delivered"}
            or any(field in payload for field in _SINGULAR_DELIVERY_FIELDS)
        ):
            raise RunValidationError(
                "development delivery manifest identity is invalid"
            )
        benchmarks = payload.get("benchmarks")
        if not isinstance(benchmarks, Mapping):
            raise RunValidationError(
                "development delivery manifest identity is invalid"
            )
        raw_delivery = benchmarks.get(self.benchmark_id)
        if (
            not isinstance(raw_delivery, Mapping)
            or set(raw_delivery) != _DELIVERY_CHILD_FIELDS
        ):
            raise RunValidationError(
                "development delivery benchmark binding is invalid"
            )
        delivery = dict(raw_delivery)
        task_ids = delivery.get("development_task_ids")
        mirror = delivery.get("development_mirror")
        expected_mirror = self.mirror_root.relative_to(self.workspace).as_posix()
        if (
            delivery.get("benchmark_id") != self.benchmark_id
            or task_ids not in ([], list(self.frozen_task_ids))
            or mirror not in (None, expected_mirror)
        ):
            raise RunValidationError("development delivery benchmark identity changed")
        return payload, dict(benchmarks), delivery

    def _record_delivery(
        self,
        *,
        fields: Sequence[str],
        paths: Sequence[Path],
        operation: str,
        evaluation_id: str | None,
        task_ids: Sequence[str],
        rendered_digests: Sequence[str] = (),
        response_fields: Sequence[str] = (),
        channel: str = _DELIVERY_CHANNEL,
        native_request_id: str | None = None,
        recipient: str | None = None,
    ) -> None:
        with _DEVELOPMENT_DELIVERY_LOCK:
            payload, benchmarks, delivery = self._delivery_manifest()
            supported = delivery.get("supported_fields")
            delivered = delivery.get("delivered_fields")
            channels = delivery.get("delivery_channels")
            statuses = delivery.get("status")
            digests = delivery.get("rendered_artifact_digests")
            records = delivery.get("delivery_records")
            if (
                not isinstance(supported, list)
                or not isinstance(delivered, list)
                or not isinstance(channels, Mapping)
                or not isinstance(statuses, Mapping)
                or not isinstance(digests, list)
                or not isinstance(records, list)
                or any(not isinstance(item, str) for item in supported)
                or any(not isinstance(item, str) for item in delivered)
                or any(not isinstance(item, str) for item in digests)
                or any(not isinstance(item, Mapping) for item in records)
            ):
                raise RunValidationError(
                    "development delivery benchmark state is invalid"
                )
            supported = list(supported)
            delivered = list(delivered)
            channels = dict(channels)
            statuses = dict(statuses)
            digests = list(digests)
            records = [dict(item) for item in records]
            for field in _DELIVERY_FIELDS:
                if field not in supported:
                    supported.append(field)
            for field in fields:
                if field not in _DELIVERY_FIELDS:
                    raise RunValidationError(
                        "development delivery field is unsupported"
                    )
                if field not in delivered:
                    delivered.append(field)
                channels[field] = channel
                statuses[field] = "delivered"
            rendered = [file_digest(item) for item in paths]
            rendered.extend(map(str, rendered_digests))
            for digest in rendered:
                if digest not in digests:
                    digests.append(digest)
            expected: dict[str, object] = {
                "operation": operation,
                "development_evaluation_id": evaluation_id,
                "task_ids": list(task_ids),
                "fields": list(fields),
                "rendered_artifact_digests": rendered,
                "channel": channel,
            }
            if response_fields:
                expected["response_fields"] = list(map(str, response_fields))
            if native_request_id is not None:
                expected["native_request_id"] = native_request_id
            if recipient is not None:
                expected["recipient"] = recipient
            matching = [
                row
                for row in records
                if row.get("operation") == operation
                and row.get("development_evaluation_id") == evaluation_id
                and row.get("native_request_id") == native_request_id
            ]
            if matching:
                observed = dict(matching[0])
                observed.pop("ordinal", None)
                if len(matching) != 1 or observed != expected:
                    raise RunValidationError("development delivery binding changed")
                return
            records.append(
                {
                    "ordinal": len(records) + 1,
                    "delivered_at": time.time(),
                    **expected,
                }
            )
            delivery.update(
                {
                    "overall_status": "delivered",
                    "supported_fields": supported,
                    "delivered_fields": delivered,
                    "delivery_channels": channels,
                    "rendered_artifact_digests": digests,
                    "status": statuses,
                    "delivery_records": records,
                    "development_mirror": self.mirror_root.relative_to(
                        self.workspace
                    ).as_posix(),
                    "development_task_ids": list(self.frozen_task_ids),
                }
            )
            benchmarks[self.benchmark_id] = delivery
            payload["benchmarks"] = benchmarks
            payload["status"] = "delivered"
            self.layout.write_manifest("development-delivery-manifest.json", payload)


def _trajectory_artifact_tasks(
    task_ids: Sequence[str],
    trajectory: Sequence[Mapping[str, object]],
    artifacts: Sequence[DevelopmentArtifact],
) -> dict[str, str]:
    artifact_paths = {artifact.path for artifact in artifacts}
    tasks: dict[str, str] = {}
    for event in trajectory:
        if event.get("event") != "development_task_agent_trajectory":
            continue
        task_id = event.get("task_id")
        artifact_path = event.get("artifact_path")
        if not isinstance(task_id, str) or task_id not in task_ids:
            continue
        expected = f"artifacts/{task_id}/agent-trajectory.json"
        relative = f"{task_id}/agent-trajectory.json"
        if artifact_path == expected and relative in artifact_paths:
            tasks[relative] = task_id
        provider_relative = f"{task_id}/provider-diagnostics.json"
        if (
            event.get("provider_diagnostics_path") == f"artifacts/{provider_relative}"
            and provider_relative in artifact_paths
        ):
            tasks[provider_relative] = task_id
    for task_id in task_ids:
        pane_path = f"{task_id}/terminus-pane.txt"
        if pane_path in artifact_paths:
            tasks[pane_path] = task_id
        for artifact_path in artifact_paths:
            if re.fullmatch(
                re.escape(task_id) + r"/provider-responses/call-[0-9]{6}\.json",
                artifact_path,
            ):
                tasks[artifact_path] = task_id
    return tasks


def _filter_trajectory_events(
    trajectory: Sequence[Mapping[str, object]],
    successful_trajectory_paths: set[str],
) -> tuple[Mapping[str, object], ...]:
    filtered: list[Mapping[str, object]] = []
    for event in trajectory:
        if event.get("event") == "development_task_agent_trajectory":
            event = dict(event)
            available = False
            for path_key, digest_key in (
                ("artifact_path", "artifact_digest"),
                ("provider_diagnostics_path", "provider_diagnostics_digest"),
            ):
                path = event.get(path_key)
                if (
                    isinstance(path, str)
                    and path.startswith("artifacts/")
                    and path.removeprefix("artifacts/") in successful_trajectory_paths
                ):
                    available = True
                elif path_key in event:
                    event[path_key] = None
                    event[digest_key] = None
            if not available:
                continue
        filtered.append(event)
    return tuple(filtered)


def _trajectory_bundle_task(relative: str, task_ids: Sequence[str]) -> str | None:
    for task_id in task_ids:
        if re.fullmatch(
            re.escape(task_id) + r"/provider-responses/call-[0-9]{6}\.json", relative
        ):
            return task_id
        if relative in {
            f"{task_id}/agent-trajectory.json",
            f"{task_id}/terminus-pane.txt",
            f"{task_id}/provider-diagnostics.json",
        }:
            return task_id
    return None


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


def _task_segment(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:20]
    return f"task-{digest}"


def _benchmark_segment(benchmark_id: str) -> str:
    digest = hashlib.sha256(benchmark_id.encode("utf-8")).hexdigest()[:20]
    return f"benchmark-{digest}"


def _ensure_child_directory(parent: Path, name: str) -> Path:
    parent = Path(parent).resolve(strict=True)
    child = parent / name
    try:
        info = child.lstat()
    except FileNotFoundError:
        child.mkdir(mode=0o700)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RunLayoutError("development mirror directory is not a real directory")
    resolved = child.resolve(strict=True)
    if resolved.parent != parent:
        raise RunLayoutError("development mirror directory escaped method state")
    return resolved


def _safe_mirror_destination(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise RunLayoutError("development mirror publication path is invalid")
    parent = root
    for part in parts[:-1]:
        parent = _ensure_child_directory(parent, part)
    return parent / parts[-1]


def _write_if_missing(path: Path, content: bytes) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        atomic_write_bytes(path, content)
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RunLayoutError("development mirror control file is not a regular file")


def _remove_path(path: Path) -> None:
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        Path(path).unlink()


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


__all__ = [
    "DevelopmentAdapter",
    "DevelopmentArtifact",
    "DevelopmentExecution",
    "DevelopmentMaterial",
    "DevelopmentOperations",
    "NativeDevelopmentOperations",
    "RunScopedDevelopmentOperations",
]
