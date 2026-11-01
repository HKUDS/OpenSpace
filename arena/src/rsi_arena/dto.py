"""Bounded common DTOs for the run-private Arena service and UDS transport."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_STRING_CHARS = 4096
MAX_SUMMARY_CHARS = 2048
MAX_JSON_DEPTH = 8
MAX_OPAQUE_ID_BYTES = 512

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,63}\Z")


class ArenaProtocolError(ValueError):
    """A wire/common DTO is malformed or exceeds the public boundary."""


def bounded_token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ArenaProtocolError(f"{field_name} is invalid")
    return value


def bounded_opaque_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArenaProtocolError(f"{field_name} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ArenaProtocolError(f"{field_name} is invalid") from exc
    if len(encoded) > MAX_OPAQUE_ID_BYTES or any(
        unicodedata.category(char) == "Cc" for char in value
    ):
        raise ArenaProtocolError(f"{field_name} is invalid")
    return value


def bounded_text(value: object, field_name: str, *, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ArenaProtocolError(f"{field_name} is invalid")
    return value


def _validate_json(value: object, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ArenaProtocolError("JSON nesting exceeds the Arena boundary")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArenaProtocolError("JSON numbers must be finite")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise ArenaProtocolError("JSON string exceeds the Arena boundary")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > MAX_STRING_CHARS:
                raise ArenaProtocolError("JSON object key is invalid")
            _validate_json(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_json(child, depth=depth + 1)
        return
    raise ArenaProtocolError("Arena DTO contains a non-JSON value")


def _encode_frame(payload: Mapping[str, object], *, limit: int) -> bytes:
    _validate_json(payload)
    try:
        encoded = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ArenaProtocolError("Arena DTO is not serializable") from exc
    if len(encoded) > limit:
        raise ArenaProtocolError("Arena frame exceeds its byte boundary")
    return encoded


@dataclass(frozen=True)
class ArenaRequest:
    request_id: str
    op: str
    payload: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        bounded_opaque_id(self.request_id, "request_id")
        bounded_token(self.op, "op")
        if not isinstance(self.payload, Mapping):
            raise ArenaProtocolError("payload must be an object")
        copied = dict(self.payload)
        _validate_json(copied)
        object.__setattr__(self, "payload", MappingProxyType(copied))
        _encode_frame(self.as_dict(), limit=MAX_REQUEST_BYTES)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ArenaRequest":
        if not isinstance(raw, Mapping) or set(raw) != {"request_id", "op", "payload"}:
            raise ArenaProtocolError("request fields are invalid")
        return cls(
            request_id=bounded_opaque_id(raw["request_id"], "request_id"),
            op=bounded_token(raw["op"], "op"),
            payload=raw["payload"],
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ArenaRequest":
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            raise ArenaProtocolError("request frame size is invalid")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArenaProtocolError("request is not valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ArenaProtocolError("request must be a JSON object")
        return cls.from_mapping(decoded)

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "op": self.op,
            "payload": dict(self.payload),
        }

    def to_bytes(self) -> bytes:
        return _encode_frame(self.as_dict(), limit=MAX_REQUEST_BYTES)


@dataclass(frozen=True)
class ArenaError:
    kind: str
    summary: str

    def __post_init__(self) -> None:
        bounded_token(self.kind, "error.kind")
        bounded_text(self.summary, "error.summary", limit=MAX_SUMMARY_CHARS)

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "summary": self.summary}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ArenaError":
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "summary"}:
            raise ArenaProtocolError("error fields are invalid")
        return cls(
            kind=bounded_token(raw["kind"], "error.kind"),
            summary=bounded_text(
                raw["summary"], "error.summary", limit=MAX_SUMMARY_CHARS
            ),
        )


@dataclass(frozen=True)
class ArenaResponse:
    request_id: str
    ok: bool
    state: str | None = None
    result: Mapping[str, object] | None = None
    error: ArenaError | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        bounded_opaque_id(self.request_id, "request_id")
        if self.ok == (self.error is not None):
            raise ArenaProtocolError("response success/error fields disagree")
        if self.ok:
            if self.status is not None:
                raise ArenaProtocolError("successful response cannot carry status")
            if self.state not in {
                "PENDING",
                "OPEN",
                "SEALED",
                "IN_PROGRESS",
                "TERMINAL",
            }:
                raise ArenaProtocolError("successful response state is invalid")
            if self.state == "IN_PROGRESS" and self.result is not None:
                raise ArenaProtocolError("pending response result must be null")
            if (
                self.state in {"PENDING", "OPEN", "SEALED", "TERMINAL"}
                and self.result is None
            ):
                raise ArenaProtocolError("attempt lifecycle response requires a result")
        else:
            if self.state is not None or self.result is not None:
                raise ArenaProtocolError("failed response cannot carry state/result")
            if self.status is None:
                raise ArenaProtocolError("failed response requires status")
            bounded_token(self.status, "status")
        if self.result is not None:
            copied = dict(self.result)
            _validate_json(copied)
            object.__setattr__(self, "result", MappingProxyType(copied))
        _encode_frame(self.as_dict(), limit=MAX_RESPONSE_BYTES)

    @classmethod
    def success(
        cls,
        request_id: str,
        *,
        state: str,
        result: Mapping[str, object] | None,
    ) -> "ArenaResponse":
        return cls(request_id=request_id, ok=True, state=state, result=result)

    @classmethod
    def failure(
        cls,
        request_id: str,
        *,
        kind: str,
        summary: str,
        status: str = "ERROR",
    ) -> "ArenaResponse":
        return cls(
            request_id=request_id,
            ok=False,
            error=ArenaError(kind=kind, summary=summary),
            status=status,
        )

    def as_dict(self) -> dict[str, object]:
        if self.ok:
            return {
                "request_id": self.request_id,
                "ok": True,
                "state": self.state,
                "result": dict(self.result) if self.result is not None else None,
            }
        assert self.error is not None and self.status is not None
        return {
            "request_id": self.request_id,
            "ok": False,
            "status": self.status,
            "error": self.error.as_dict(),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ArenaResponse":
        if not isinstance(raw, Mapping):
            raise ArenaProtocolError("response must be a JSON object")
        if raw.get("ok") is True:
            expected = {"request_id", "ok", "state", "result"}
            state = raw.get("state")
            result = raw.get("result")
            if (
                set(raw) != expected
                or state not in {"OPEN", "SEALED", "IN_PROGRESS", "TERMINAL"}
                or (state == "IN_PROGRESS" and result is not None)
                or (
                    state in {"OPEN", "SEALED", "TERMINAL"}
                    and not isinstance(result, Mapping)
                )
            ):
                raise ArenaProtocolError("successful response fields are invalid")
            return cls.success(
                bounded_opaque_id(raw["request_id"], "request_id"),
                state=str(state),
                result=result if isinstance(result, Mapping) else None,
            )
        if raw.get("ok") is False:
            expected = {"request_id", "ok", "status", "error"}
            if set(raw) != expected or not isinstance(raw.get("error"), Mapping):
                raise ArenaProtocolError("failed response fields are invalid")
            return cls(
                request_id=bounded_opaque_id(raw["request_id"], "request_id"),
                ok=False,
                status=bounded_token(raw["status"], "status"),
                error=ArenaError.from_mapping(raw["error"]),
            )
        raise ArenaProtocolError("response ok field is invalid")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ArenaResponse":
        if not raw or len(raw) > MAX_RESPONSE_BYTES:
            raise ArenaProtocolError("response frame size is invalid")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArenaProtocolError("response is not valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ArenaProtocolError("response must be a JSON object")
        return cls.from_mapping(decoded)

    def to_bytes(self) -> bytes:
        return _encode_frame(self.as_dict(), limit=MAX_RESPONSE_BYTES)


@dataclass(frozen=True)
class DevelopmentResult:
    outcome: str
    development_evaluation_id: str
    evidence_path: str | None

    def __post_init__(self) -> None:
        if self.outcome not in {"measured", "inconclusive"}:
            raise ArenaProtocolError("development outcome is invalid")
        bounded_opaque_id(self.development_evaluation_id, "development_evaluation_id")
        if self.evidence_path is not None:
            bounded_text(self.evidence_path, "evidence_path", limit=MAX_STRING_CHARS)


@dataclass(frozen=True)
class DevelopmentBatchResult:
    """One ordered public terminal spanning the selected benchmarks in a cell."""

    benchmark_results: Mapping[str, DevelopmentResult]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.benchmark_results, Mapping)
            or not self.benchmark_results
        ):
            raise ArenaProtocolError("development batch requires benchmark results")
        copied: dict[str, DevelopmentResult] = {}
        for benchmark_id, result in self.benchmark_results.items():
            bounded_opaque_id(benchmark_id, "development benchmark_id")
            if not isinstance(result, DevelopmentResult):
                raise ArenaProtocolError("development batch result is invalid")
            copied[benchmark_id] = result
        object.__setattr__(self, "benchmark_results", MappingProxyType(copied))

    def as_dict(self) -> dict[str, object]:
        return {
            "state": "TERMINAL",
            "result": {
                "benchmark_results": [
                    {
                        "benchmark_id": benchmark_id,
                        "outcome": result.outcome,
                        "development_evaluation_id": (result.development_evaluation_id),
                        "evidence_path": result.evidence_path,
                    }
                    for benchmark_id, result in self.benchmark_results.items()
                ]
            },
        }


@dataclass(frozen=True)
class ArenaBenchmarkScore:
    benchmark_id: str
    score: float | None
    n_expected: int
    n_conclusive: int
    n_inconclusive: int
    coverage: float

    def __post_init__(self) -> None:
        bounded_opaque_id(self.benchmark_id, "benchmark_id")
        if (
            isinstance(self.n_expected, bool)
            or isinstance(self.n_conclusive, bool)
            or isinstance(self.n_inconclusive, bool)
            or not isinstance(self.n_expected, int)
            or not isinstance(self.n_conclusive, int)
            or not isinstance(self.n_inconclusive, int)
            or self.n_expected < 1
            or self.n_conclusive < 0
            or self.n_inconclusive < 0
            or self.n_conclusive + self.n_inconclusive != self.n_expected
            or isinstance(self.coverage, bool)
            or not isinstance(self.coverage, (int, float))
            or not math.isclose(
                float(self.coverage),
                self.n_conclusive / self.n_expected,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ArenaProtocolError("benchmark score/counts are invalid")
        if self.n_conclusive:
            if (
                isinstance(self.score, bool)
                or not isinstance(self.score, (int, float))
                or not math.isfinite(float(self.score))
            ):
                raise ArenaProtocolError("conclusive benchmark score is invalid")
        elif self.score is not None:
            raise ArenaProtocolError("zero-conclusive benchmark score must be null")

    def as_dict(self) -> dict[str, object]:
        return {
            "benchmark_id": self.benchmark_id,
            "score": float(self.score) if self.score is not None else None,
            "n_expected": self.n_expected,
            "n_conclusive": self.n_conclusive,
            "n_inconclusive": self.n_inconclusive,
            "coverage": float(self.coverage),
        }


@dataclass(frozen=True)
class ArenaSubmitResult:
    outcome: str
    attempt_id: str
    occurrence_key: str
    candidate_id: str
    candidate_evaluation_id: str
    role: str
    score_vector: Sequence[ArenaBenchmarkScore] = ()
    aggregate_policy: Mapping[str, str] | None = None
    aggregate_score: float | None = None
    summary: str | None = None

    def as_dict(self) -> dict[str, object]:
        outcome = bounded_token(self.outcome, "submit.outcome")
        if outcome not in {"measured", "inconclusive"}:
            raise ArenaProtocolError("submit outcome is invalid")
        role = bounded_token(self.role, "submit.role")
        if role not in {"candidate", "control", "challenger"}:
            raise ArenaProtocolError("submit role is invalid")
        scores = tuple(self.score_vector)
        if not scores:
            raise ArenaProtocolError("terminal submit lacks benchmark results")
        benchmark_ids = tuple(item.benchmark_id for item in scores)
        if len(set(benchmark_ids)) != len(benchmark_ids):
            raise ArenaProtocolError("terminal submit repeats a benchmark result")
        n_expected = sum(item.n_expected for item in scores)
        n_conclusive = sum(item.n_conclusive for item in scores)
        n_inconclusive = sum(item.n_inconclusive for item in scores)
        expected_scores = [
            float(item.score) for item in scores if item.score is not None
        ]
        expected_score = (
            sum(expected_scores) / len(expected_scores) if expected_scores else None
        )
        if outcome != ("measured" if expected_scores else "inconclusive"):
            raise ArenaProtocolError("submit outcome disagrees with benchmark scores")
        if self.aggregate_policy is None:
            if self.aggregate_score is not None:
                raise ArenaProtocolError(
                    "submit scalar requires an explicit aggregate policy"
                )
        elif dict(self.aggregate_policy) != {"kind": "macro_mean_pass_rate"}:
            raise ArenaProtocolError("submit aggregate policy is invalid")
        elif self.aggregate_score != expected_score:
            raise ArenaProtocolError(
                "submit aggregate score disagrees with benchmark scores"
            )
        result: dict[str, object] = {
            "outcome": outcome,
            "attempt_id": bounded_opaque_id(self.attempt_id, "attempt_id"),
            "occurrence_key": bounded_opaque_id(self.occurrence_key, "occurrence_key"),
            "candidate_id": bounded_opaque_id(self.candidate_id, "candidate_id"),
            "candidate_evaluation_id": bounded_opaque_id(
                self.candidate_evaluation_id, "candidate_evaluation_id"
            ),
            "role": role,
            "aggregate_policy": (
                dict(self.aggregate_policy)
                if self.aggregate_policy is not None
                else None
            ),
            "aggregate_score": self.aggregate_score,
            "counts": {
                "n_expected": n_expected,
                "n_conclusive": n_conclusive,
                "n_inconclusive": n_inconclusive,
            },
            "coverage": n_conclusive / n_expected,
            "score_vector": [item.as_dict() for item in scores],
            "usage": None,
            "evidence": None,
        }
        if self.summary is not None:
            result["summary"] = bounded_text(
                self.summary, "summary", limit=MAX_SUMMARY_CHARS
            )
        payload = {"state": "TERMINAL", "result": result}
        _validate_json(payload)
        return payload
