"""Trusted model-usage normalization for BrowseComp-Plus."""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

USAGE_SIDECAR_ENV = "RSI_ARENA_BROWSECOMP_USAGE_SIDECAR"
_USAGE_SIDECAR_SCHEMA = "browsecomp-model-usage-call"


def _token(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer or null")
    return value


def _cost(value: object) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError("model cost must be a finite non-negative number or null")
    return float(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")
    return value.strip()


@dataclass(frozen=True)
class ModelUsage:
    """Provider-authored accounting for one logical model role."""

    role: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _text(self.role, "model usage role"))
        object.__setattr__(
            self, "provider", _text(self.provider, "model usage provider")
        )
        object.__setattr__(self, "model", _text(self.model, "model usage model"))
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
        ):
            object.__setattr__(
                self,
                field_name,
                _token(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "cost_usd", _cost(self.cost_usd))

    @classmethod
    def from_mapping(cls, value: object) -> ModelUsage | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("model usage must be an object")
        required = {"role", "provider", "model"}
        optional = {
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cost_usd",
        }
        if not required.issubset(value) or set(value) - required - optional:
            raise ValueError("model usage fields are invalid")
        return cls(
            role=value["role"],  # type: ignore[arg-type]
            provider=value["provider"],  # type: ignore[arg-type]
            model=value["model"],  # type: ignore[arg-type]
            input_tokens=value.get("input_tokens"),  # type: ignore[arg-type]
            output_tokens=value.get("output_tokens"),  # type: ignore[arg-type]
            cache_read_tokens=value.get("cache_read_tokens"),  # type: ignore[arg-type]
            cost_usd=value.get("cost_usd"),  # type: ignore[arg-type]
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": self.cost_usd,
        }


def _attribute(source: object, *names: str) -> object:
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def usage_from_openai_response(
    response: object,
    *,
    role: str,
    provider: str,
    model: str,
) -> ModelUsage | None:
    """Read only provider-reported fields from an OpenAI-compatible response."""

    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    cache_read = _attribute(details, "cached_tokens") if details is not None else None
    cost = _attribute(usage, "cost", "cost_usd")
    if cost is None:
        cost = _attribute(response, "cost", "cost_usd")
    return ModelUsage(
        role=role,
        provider=provider,
        model=model,
        input_tokens=_attribute(usage, "prompt_tokens", "input_tokens"),  # type: ignore[arg-type]
        output_tokens=_attribute(usage, "completion_tokens", "output_tokens"),  # type: ignore[arg-type]
        cache_read_tokens=cache_read,  # type: ignore[arg-type]
        cost_usd=cost,  # type: ignore[arg-type]
    )


def aggregate_model_usage(
    values: Sequence[ModelUsage | None],
) -> Mapping[str, object] | None:
    """Aggregate complete fields while retaining role-level accounting facts."""

    observed = tuple(value for value in values if value is not None)
    if not observed:
        return None

    def total(field_name: str) -> int | float | None:
        fields = tuple(getattr(value, field_name) for value in observed)
        if any(value is None for value in fields):
            return None
        return sum(fields)  # type: ignore[arg-type]

    input_tokens = total("input_tokens")
    output_tokens = total("output_tokens")
    cache_read_tokens = total("cache_read_tokens")
    cost_usd = total("cost_usd")
    providers = {value.provider for value in observed}
    models = {value.model for value in observed}
    roles = {value.role for value in observed}
    return MappingProxyType(
        {
            "role": next(iter(roles)) if len(roles) == 1 else "verifier/judge",
            "provider": next(iter(providers)) if len(providers) == 1 else "multiple",
            "model": next(iter(models)) if len(models) == 1 else "multiple",
            "token_source": "provider",
            "input_token_status": (
                "reported" if input_tokens is not None else "missing"
            ),
            "output_token_status": (
                "reported" if output_tokens is not None else "missing"
            ),
            "cache_token_status": (
                "reported" if cache_read_tokens is not None else "missing"
            ),
            "cache_status": (
                "hit"
                if isinstance(cache_read_tokens, int) and cache_read_tokens > 0
                else "miss"
                if cache_read_tokens is not None
                else "unknown"
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cost_usd": cost_usd,
            "cost_status": "reported" if cost_usd is not None else "missing",
            "calls": [value.as_dict() for value in observed],
        }
    )


def model_usage_calls_from_mapping(value: object) -> tuple[ModelUsage, ...]:
    """Decode an ordered provider-call list without accepting aggregate aliases."""

    if value is None:
        return ()
    if not isinstance(value, Mapping) or set(value) != {"calls"}:
        raise ValueError("model usage must contain exactly an ordered calls list")
    raw_calls = value["calls"]
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ValueError("model usage calls must be a non-empty list")
    calls: list[ModelUsage] = []
    for raw in raw_calls:
        decoded = ModelUsage.from_mapping(raw)
        if decoded is None:  # pragma: no cover - list items cannot be None
            raise ValueError("model usage call is empty")
        calls.append(decoded)
    return tuple(calls)


class PartialModelUsageError(TimeoutError):
    """A runner failed after one or more provider calls were durably reported."""

    def __init__(self, message: str, usage_calls: Sequence[ModelUsage]) -> None:
        super().__init__(message)
        self.usage_calls = tuple(usage_calls)


def append_usage_sidecar(
    path: str | os.PathLike[str], *, call_id: str, usage: ModelUsage
) -> None:
    """Append one fsync'd, stable-id provider call, idempotently."""

    if not isinstance(call_id, str) or not call_id:
        raise ValueError("BrowseComp usage call_id must be non-empty")
    if not isinstance(usage, ModelUsage):
        raise TypeError("BrowseComp usage sidecar requires ModelUsage")
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("BrowseComp usage sidecar path must be absolute")
    row = {
        "schema": _USAGE_SIDECAR_SCHEMA,
        "call_id": call_id,
        "usage": usage.as_dict(),
    }
    encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > 16 * 1024:
        raise ValueError("BrowseComp usage sidecar row is too large")
    existing = _usage_sidecar_rows(target) if target.exists() else {}
    prior = existing.get(call_id)
    if prior is not None:
        if prior != row:
            raise ValueError("BrowseComp usage call changed for its stable identity")
        return
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _usage_sidecar_rows(path: Path) -> dict[str, dict[str, object]]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("BrowseComp usage sidecar is not a regular file")
    if info.st_size > 16 * 1024 * 1024:
        raise ValueError("BrowseComp usage sidecar exceeds its byte limit")
    rows: dict[str, dict[str, object]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            raw = json.loads(line)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"schema", "call_id", "usage"}
                or raw.get("schema") != _USAGE_SIDECAR_SCHEMA
                or not isinstance(raw.get("call_id"), str)
            ):
                raise ValueError("BrowseComp usage sidecar row is invalid")
            call_id = str(raw["call_id"])
            ModelUsage.from_mapping(raw["usage"])
            if call_id in rows and rows[call_id] != raw:
                raise ValueError("BrowseComp usage sidecar call identity is duplicated")
            rows[call_id] = raw
    return rows


def read_usage_sidecar(path: str | os.PathLike[str]) -> tuple[ModelUsage, ...]:
    """Read the ordered, unique calls available after any worker disposition."""

    target = Path(path)
    if not target.exists():
        return ()
    decoded: list[ModelUsage] = []
    for raw in _usage_sidecar_rows(target).values():
        usage = ModelUsage.from_mapping(raw["usage"])
        if usage is None:  # pragma: no cover - validated above
            raise ValueError("BrowseComp usage sidecar call is empty")
        decoded.append(usage)
    return tuple(decoded)


__all__ = [
    "ModelUsage",
    "PartialModelUsageError",
    "USAGE_SIDECAR_ENV",
    "append_usage_sidecar",
    "aggregate_model_usage",
    "model_usage_calls_from_mapping",
    "read_usage_sidecar",
    "usage_from_openai_response",
]
