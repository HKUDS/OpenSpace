"""Deterministic proposal target grounding over a frozen parent repo map."""

from __future__ import annotations

from pathlib import PurePosixPath

from agentpilot.domain import (
    ParentRepositoryMap,
    ScopeKind,
    StructuredDirection,
)

from .base import RetryableProposalError


class DirectionTargetGroundingError(RetryableProposalError):
    """A parsed direction names a target absent from the frozen parent map."""


def validate_direction_targets(
    direction: StructuredDirection, repository_map: ParentRepositoryMap,
) -> None:
    if not isinstance(direction, StructuredDirection):
        raise TypeError("direction must be a StructuredDirection")
    if not isinstance(repository_map, ParentRepositoryMap):
        raise TypeError("repository_map must be a ParentRepositoryMap")
    symbols_by_path = {
        item.path: set(item.symbol_names) for item in repository_map.files
    }
    repository_paths = set(symbols_by_path)
    created_paths = {item.path for item in direction.new_paths}
    violations: list[str] = []

    for target in direction.declared_scope:
        if target.kind is ScopeKind.PACKAGE:
            prefix = PurePosixPath(target.path)
            expanded = {
                path
                for path in repository_paths
                if PurePosixPath(path).is_relative_to(prefix)
            }
            if not expanded:
                violations.append(
                    f"package {target.path!r} contains no regular files in the "
                    "parent repo map"
                )
            elif not expanded <= set(direction.resolved_scope):
                violations.append(
                    f"package {target.path!r} is not fully represented in resolved_scope"
                )
            continue

        available = symbols_by_path.get(target.path)
        if available is None:
            violations.append(
                f"file {target.path!r} is absent from the parent repo map"
            )
            continue
        if target.kind is ScopeKind.DEFINITION:
            missing = tuple(
                symbol for symbol in target.symbols if symbol not in available
            )
            if missing:
                violations.append(
                    f"file {target.path!r} has no declared symbols {list(missing)!r}"
                )

    for path in direction.resolved_scope:
        if path not in repository_paths and path not in created_paths:
            violations.append(
                f"resolved scope path {path!r} is neither in the parent map nor declared new"
            )

    for item in direction.new_paths:
        if item.path in repository_paths:
            violations.append(
                f"new path {item.path!r} already exists in the parent map"
            )
        if not any(
            PurePosixPath(item.path).is_relative_to(PurePosixPath(root))
            for root in repository_map.editable_roots
        ):
            violations.append(
                f"new path {item.path!r} is outside configured editable roots"
            )
    if violations:
        raise DirectionTargetGroundingError("; ".join(violations))


__all__ = ["DirectionTargetGroundingError", "validate_direction_targets"]
