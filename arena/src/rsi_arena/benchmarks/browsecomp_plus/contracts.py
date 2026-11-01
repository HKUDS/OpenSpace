"""Minimal public and trusted catalog contracts for BrowseComp-Plus."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

EXPECTED_TASK_COUNT = 830
DEVELOPMENT_TASK_COUNT = 249
HELD_OUT_TASK_COUNT = 581


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _unique_strings(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    normalized = tuple(_nonempty(value, label) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} contains duplicate values")
    return normalized


@dataclass(frozen=True)
class PublicTask:
    """Candidate-visible task material; answers are structurally absent."""

    query_id: str
    query: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_id", _nonempty(self.query_id, "query_id"))
        object.__setattr__(self, "query", _nonempty(self.query, "query"))

    def as_dict(self) -> dict[str, str]:
        return {"query_id": self.query_id, "query": self.query}


@dataclass(frozen=True)
class TrustedTask:
    """Evaluator-owned task material; no public serialization is provided."""

    query_id: str
    query: str
    answer: str
    evidence_docids: tuple[str, ...]
    gold_docids: tuple[str, ...]
    negative_docids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_id", _nonempty(self.query_id, "query_id"))
        object.__setattr__(self, "query", _nonempty(self.query, "query"))
        object.__setattr__(self, "answer", _nonempty(self.answer, "answer"))
        for field_name in ("evidence_docids", "gold_docids", "negative_docids"):
            object.__setattr__(
                self,
                field_name,
                _unique_strings(getattr(self, field_name), field_name),
            )

    @property
    def public(self) -> PublicTask:
        return PublicTask(query_id=self.query_id, query=self.query)


@dataclass(frozen=True)
class HeldOutBoundary:
    """Candidate-visible descriptor with no held-out membership or material."""

    count: int

    def __post_init__(self) -> None:
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise TypeError("held-out count must be an integer")
        if self.count != HELD_OUT_TASK_COUNT:
            raise ValueError("held-out count must be exactly 581")

    def as_dict(self) -> dict[str, int]:
        return {"count": self.count}


@dataclass(frozen=True)
class PublicCatalog:
    development: tuple[PublicTask, ...]
    held_out: HeldOutBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.development, tuple) or any(
            not isinstance(task, PublicTask) for task in self.development
        ):
            raise TypeError("development must be a tuple of PublicTask")
        if len(self.development) != DEVELOPMENT_TASK_COUNT:
            raise ValueError(
                "public development catalog must contain exactly 249 tasks"
            )
        if len({task.query_id for task in self.development}) != len(self.development):
            raise ValueError("public development catalog contains duplicate query IDs")
        if not isinstance(self.held_out, HeldOutBoundary):
            raise TypeError("held_out must be a HeldOutBoundary")

    def as_dict(self) -> dict[str, object]:
        return {
            "development": [task.as_dict() for task in self.development],
            "held_out": self.held_out.as_dict(),
        }


@dataclass(frozen=True)
class TrustedCatalog:
    """Private owner of the complete split; no public serialization is provided."""

    public: PublicCatalog
    development: tuple[TrustedTask, ...]
    held_out: tuple[TrustedTask, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.public, PublicCatalog):
            raise TypeError("public must be a PublicCatalog")
        for field_name in ("development", "held_out"):
            tasks = getattr(self, field_name)
            if not isinstance(tasks, tuple) or any(
                not isinstance(task, TrustedTask) for task in tasks
            ):
                raise TypeError(f"{field_name} must be a tuple of TrustedTask")
        if len(self.development) != DEVELOPMENT_TASK_COUNT:
            raise ValueError("trusted development catalog must contain 249 tasks")
        if len(self.held_out) != HELD_OUT_TASK_COUNT:
            raise ValueError("trusted held-out catalog must contain 581 tasks")
        development_ids = {task.query_id for task in self.development}
        held_out_ids = {task.query_id for task in self.held_out}
        if len(development_ids) != DEVELOPMENT_TASK_COUNT:
            raise ValueError("trusted development catalog contains duplicate query IDs")
        if len(held_out_ids) != HELD_OUT_TASK_COUNT:
            raise ValueError("trusted held-out catalog contains duplicate query IDs")
        if development_ids & held_out_ids:
            raise ValueError("development and held-out catalogs overlap")
        if tuple(task.public for task in self.development) != self.public.development:
            raise ValueError("trusted and public development catalogs disagree")

    @property
    def tasks_by_id(self) -> Mapping[str, TrustedTask]:
        return MappingProxyType(
            {task.query_id: task for task in self.development + self.held_out}
        )
