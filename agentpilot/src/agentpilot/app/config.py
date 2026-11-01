"""Strict policy configuration.

This parser does not silently translate historical policy modes.  A run must
choose one complete carrier and provide every algorithm-defining parameter.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from agentpilot.domain import PolicyMethod
from agentpilot.policy.memory.ranker import MEMORY_RANK_MAX_CANDIDATES


class PolicyConfigError(ValueError):
    pass


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyConfigError(f"{field_name} must be a table")
    return value


def _only(mapping: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise PolicyConfigError(f"unknown {field_name} keys: {sorted(unknown)}")


def _text(mapping: Mapping[str, Any], name: str, field_name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PolicyConfigError(f"{field_name}.{name} must be non-empty")
    return value.strip()


def _integer(
    mapping: Mapping[str, Any], name: str, field_name: str, *, minimum: int
) -> int:
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PolicyConfigError(f"{field_name}.{name} must be >= {minimum}")
    return value


def _number(mapping: Mapping[str, Any], name: str, field_name: str) -> float:
    value = mapping.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise PolicyConfigError(f"{field_name}.{name} must be finite")
    return float(value)


@dataclass(frozen=True)
class EffectivenessConfig:
    statistic: str


@dataclass(frozen=True)
class NeighborhoodConfig:
    exploration_c: float
    min_observations: int
    fallback_levels: tuple[str, ...]
    solver_tolerance: float
    solver_max_iterations: int
    tie_tolerance: float


@dataclass(frozen=True)
class CooldownConfig:
    max_slot_generation_failures: int
    slot_generation_cooldown_opportunities: int
    max_retryable_skips: int
    skip_cooldown_opportunities: int


@dataclass(frozen=True)
class SemanticConfig:
    provider: str
    model: str
    calibration_bundle_id: str
    encoder_identity: str
    dimension: int
    metric: str
    threshold_id: str
    tau_repeat: float
    tau_arm: float
    threshold_boundary_margin: float
    nearest_arm_tie_tolerance: float


@dataclass(frozen=True)
class CompletionConfig:
    provider: str
    model: str
    max_output_tokens: int
    temperature: float
    disable_reasoning: bool


@dataclass(frozen=True)
class ProposerConfig(CompletionConfig):
    max_proposer_turns: int
    max_trace_reads: int
    max_repository_reads: int
    max_read_bytes: int
    max_scope_refs: int
    max_new_paths: int
    investigation: bool = False
    research_strategy: str = "focused"
    guide_enabled: bool = True
    investigation_scope: str = "all_failures"
    group_repeated_tasks: bool = True
    same_task_pass_examples: int = 1
    max_read_calls: int | None = None
    max_context_bytes: int = 1048576
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class UCBPolicyConfig:
    base_seed: int
    direction_slate_size: int
    discover_quota: int
    max_slot_generation_attempts: int
    discover_exclusion_limit: int
    max_planning_attempts: int
    cooldown: CooldownConfig
    effectiveness: EffectivenessConfig
    neighborhood: NeighborhoodConfig
    semantic: SemanticConfig
    proposer: ProposerConfig


@dataclass(frozen=True)
class MemoryPolicyConfig:
    direction_slate_size: int
    max_chars: int
    max_planning_attempts: int
    proposer: ProposerConfig
    ranker: CompletionConfig
    summarizer: CompletionConfig


@dataclass(frozen=True)
class PolicyConfig:
    method: PolicyMethod
    ucb: UCBPolicyConfig | None = None
    memory: MemoryPolicyConfig | None = None


def _parse_effectiveness(value: Any) -> EffectivenessConfig:
    table = _mapping(value, "policy.effectiveness")
    _only(table, {"statistic"}, "policy.effectiveness")
    statistic = _text(table, "statistic", "policy.effectiveness")
    if statistic not in {"point", "lower_bound"}:
        raise PolicyConfigError(
            "policy.effectiveness.statistic must be point or lower_bound"
        )
    return EffectivenessConfig(statistic=statistic)


def _parse_neighborhood(value: Any) -> NeighborhoodConfig:
    table = _mapping(value, "policy.neighborhood")
    _only(
        table,
        {
            "exploration_c",
            "min_observations",
            "fallback_levels",
            "solver_tolerance",
            "solver_max_iterations",
            "tie_tolerance",
        },
        "policy.neighborhood",
    )
    exploration_c = _number(table, "exploration_c", "policy.neighborhood")
    if exploration_c < 0.0:
        raise PolicyConfigError("policy.neighborhood.exploration_c must be >= 0")
    levels = table.get("fallback_levels")
    if (
        not isinstance(levels, list)
        or not levels
        or any(not isinstance(item, str) or not item.strip() for item in levels)
        or len(set(levels)) != len(levels)
    ):
        raise PolicyConfigError(
            "policy.neighborhood.fallback_levels must be unique non-empty strings"
        )
    solver_tolerance = _number(table, "solver_tolerance", "policy.neighborhood")
    if not 0.0 < solver_tolerance < 1.0:
        raise PolicyConfigError(
            "policy.neighborhood.solver_tolerance must be in (0, 1)"
        )
    tie_tolerance = _number(table, "tie_tolerance", "policy.neighborhood")
    if tie_tolerance < 0.0:
        raise PolicyConfigError("policy.neighborhood.tie_tolerance must be >= 0")
    return NeighborhoodConfig(
        exploration_c=exploration_c,
        min_observations=_integer(
            table, "min_observations", "policy.neighborhood", minimum=1
        ),
        fallback_levels=tuple(item.strip() for item in levels),
        solver_tolerance=solver_tolerance,
        solver_max_iterations=_integer(
            table, "solver_max_iterations", "policy.neighborhood", minimum=1
        ),
        tie_tolerance=tie_tolerance,
    )


def _parse_semantic(value: Any) -> SemanticConfig:
    table = _mapping(value, "policy.semantic")
    _only(
        table,
        {
            "provider",
            "model",
            "calibration_bundle_id",
            "encoder_identity",
            "dimension",
            "metric",
            "threshold_id",
            "tau_repeat",
            "tau_arm",
            "threshold_boundary_margin",
            "nearest_arm_tie_tolerance",
        },
        "policy.semantic",
    )
    metric = _text(table, "metric", "policy.semantic")
    if metric != "cosine":
        raise PolicyConfigError("policy.semantic.metric must be cosine")
    tau_repeat = _number(table, "tau_repeat", "policy.semantic")
    tau_arm = _number(table, "tau_arm", "policy.semantic")
    threshold_boundary_margin = _number(
        table, "threshold_boundary_margin", "policy.semantic"
    )
    nearest_arm_tie_tolerance = _number(
        table, "nearest_arm_tie_tolerance", "policy.semantic"
    )
    if not 0.0 <= tau_repeat < tau_arm <= 2.0:
        raise PolicyConfigError(
            "semantic thresholds must satisfy 0 <= tau_repeat < tau_arm <= 2"
        )
    if threshold_boundary_margin < 0.0:
        raise PolicyConfigError(
            "policy.semantic.threshold_boundary_margin must be >= 0"
        )
    if nearest_arm_tie_tolerance < 0.0:
        raise PolicyConfigError(
            "policy.semantic.nearest_arm_tie_tolerance must be >= 0"
        )
    if threshold_boundary_margin >= tau_repeat:
        raise PolicyConfigError(
            "semantic threshold boundary margin leaves no repeat band"
        )
    if tau_arm + threshold_boundary_margin >= 2.0:
        raise PolicyConfigError(
            "semantic threshold boundary margin leaves no distinct band"
        )
    if tau_repeat + threshold_boundary_margin >= tau_arm - threshold_boundary_margin:
        raise PolicyConfigError(
            "semantic threshold boundary margins leave no same-arm band"
        )
    return SemanticConfig(
        provider=_text(table, "provider", "policy.semantic"),
        model=_text(table, "model", "policy.semantic"),
        calibration_bundle_id=_text(table, "calibration_bundle_id", "policy.semantic"),
        encoder_identity=_text(table, "encoder_identity", "policy.semantic"),
        dimension=_integer(table, "dimension", "policy.semantic", minimum=1),
        metric=metric,
        threshold_id=_text(table, "threshold_id", "policy.semantic"),
        tau_repeat=tau_repeat,
        tau_arm=tau_arm,
        threshold_boundary_margin=threshold_boundary_margin,
        nearest_arm_tie_tolerance=nearest_arm_tie_tolerance,
    )


def _completion_values(
    table: Mapping[str, Any], field: str
) -> dict[str, str | int | float | bool]:
    temperature = _number(table, "temperature", field)
    if temperature < 0.0:
        raise PolicyConfigError(f"{field}.temperature must be >= 0")
    disable_reasoning = table.get("disable_reasoning")
    if not isinstance(disable_reasoning, bool):
        raise PolicyConfigError(f"{field}.disable_reasoning must be boolean")
    return {
        "provider": _text(table, "provider", field),
        "model": _text(table, "model", field),
        "max_output_tokens": _integer(
            table, "max_output_tokens", field, minimum=1
        ),
        "temperature": temperature,
        "disable_reasoning": disable_reasoning,
    }


def _parse_completion(value: Any, field: str) -> CompletionConfig:
    table = _mapping(value, field)
    _only(
        table,
        {
            "provider",
            "model",
            "max_output_tokens",
            "temperature",
            "disable_reasoning",
        },
        field,
    )
    return CompletionConfig(**_completion_values(table, field))


def _parse_proposer(value: Any) -> ProposerConfig:
    field = "policy.proposer"
    table = _mapping(value, field)
    _only(
        table,
        {
            "provider",
            "model",
            "max_output_tokens",
            "temperature",
            "disable_reasoning",
            "max_proposer_turns",
            "max_trace_reads",
            "max_repository_reads",
            "max_read_bytes",
            "max_scope_refs",
            "max_new_paths",
            "investigation",
            "research_strategy",
            "guide_enabled",
            "investigation_scope",
            "group_repeated_tasks",
            "same_task_pass_examples",
            "max_read_calls",
            "max_context_bytes",
            "reasoning_effort",
        },
        field,
    )
    integer_specs = {
        "max_proposer_turns": (1, None),
        "max_trace_reads": (0, None),
        "max_repository_reads": (0, None),
        "max_read_bytes": (0, None),
        "max_scope_refs": (1, 8),
        "max_new_paths": (1, 4),
    }
    parsed: dict[str, int] = {}
    for name, (minimum, maximum) in integer_specs.items():
        parsed[name] = _integer(table, name, field, minimum=minimum)
        if maximum is not None and parsed[name] > maximum:
            raise PolicyConfigError(
                f"{field}.{name} cannot exceed {maximum}"
            )
    investigation = table.get("investigation", False)
    if not isinstance(investigation, bool):
        raise PolicyConfigError(f"{field}.investigation must be boolean")
    if investigation and parsed["max_proposer_turns"] < 4:
        raise PolicyConfigError(f"{field}.investigation requires at least four turns")
    strategy = table.get("research_strategy", "focused")
    if not isinstance(strategy, str) or strategy not in {"focused", "interleaved", "mechanism", "counterexample"}:
        raise PolicyConfigError(f"{field}.research_strategy is unsupported")
    if strategy != "focused" and not investigation:
        raise PolicyConfigError(f"{field}.research_strategy requires investigation")
    guide_enabled = table.get("guide_enabled", True)
    if not isinstance(guide_enabled, bool):
        raise PolicyConfigError(f"{field}.guide_enabled must be boolean")
    investigation_scope = table.get("investigation_scope", "all_failures")
    if not isinstance(investigation_scope, str) or investigation_scope not in {"all_failures", "single_case"}:
        raise PolicyConfigError(f"{field}.investigation_scope must be all_failures or single_case")
    grouped = table.get("group_repeated_tasks", True)
    if not isinstance(grouped, bool):
        raise PolicyConfigError(f"{field}.group_repeated_tasks must be boolean")
    pass_examples = _integer({"same_task_pass_examples": table.get("same_task_pass_examples", 1)},
                            "same_task_pass_examples", field, minimum=0)
    max_read_calls = (_integer(table, "max_read_calls", field, minimum=0)
                      if "max_read_calls" in table else None)
    max_context_bytes = _integer(
        {"max_context_bytes": table.get("max_context_bytes", 1048576)},
        "max_context_bytes", field, minimum=16384,
    )
    effort = table.get("reasoning_effort")
    if effort is not None:
        if not isinstance(effort, str) or effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            raise PolicyConfigError(f"{field}.reasoning_effort is unsupported")
        if table.get("disable_reasoning") is not False:
            raise PolicyConfigError(f"{field}.reasoning_effort requires disable_reasoning=false")
    return ProposerConfig(
        **_completion_values(table, field),
        **parsed,
        investigation=investigation,
        research_strategy=strategy,
        guide_enabled=guide_enabled,
        investigation_scope=investigation_scope,
        group_repeated_tasks=grouped,
        same_task_pass_examples=pass_examples,
        max_read_calls=max_read_calls,
        max_context_bytes=max_context_bytes,
        reasoning_effort=effort,
    )


def _parse_cooldown(value: Any) -> CooldownConfig:
    table = _mapping(value, "policy.cooldown")
    names = {
        "max_slot_generation_failures",
        "slot_generation_cooldown_opportunities",
        "max_retryable_skips",
        "skip_cooldown_opportunities",
    }
    _only(table, names, "policy.cooldown")
    return CooldownConfig(
        **{name: _integer(table, name, "policy.cooldown", minimum=1) for name in names}
    )


def _parse_ucb(table: Mapping[str, Any]) -> PolicyConfig:
    _only(
        table,
        {
            "method",
            "base_seed",
            "direction_slate_size",
            "discover_quota",
            "max_slot_generation_attempts",
            "discover_exclusion_limit",
            "max_planning_attempts",
            "cooldown",
            "effectiveness",
            "neighborhood",
            "semantic",
            "proposer",
        },
        "policy",
    )
    slate_size = _integer(table, "direction_slate_size", "policy", minimum=1)
    quota = _integer(table, "discover_quota", "policy", minimum=1)
    if quota > slate_size:
        raise PolicyConfigError("discover_quota cannot exceed direction_slate_size")
    return PolicyConfig(
        method=PolicyMethod.UCB,
        ucb=UCBPolicyConfig(
            base_seed=_integer(table, "base_seed", "policy", minimum=0),
            direction_slate_size=slate_size,
            discover_quota=quota,
            max_slot_generation_attempts=_integer(
                table,
                "max_slot_generation_attempts",
                "policy",
                minimum=1,
            ),
            discover_exclusion_limit=_integer(
                table,
                "discover_exclusion_limit",
                "policy",
                minimum=1,
            ),
            max_planning_attempts=_integer(
                table,
                "max_planning_attempts",
                "policy",
                minimum=1,
            ),
            cooldown=_parse_cooldown(table.get("cooldown")),
            effectiveness=_parse_effectiveness(table.get("effectiveness")),
            neighborhood=_parse_neighborhood(table.get("neighborhood")),
            semantic=_parse_semantic(table.get("semantic")),
            proposer=_parse_proposer(table.get("proposer")),
        ),
    )


def _parse_memory(table: Mapping[str, Any]) -> PolicyConfig:
    _only(
        table,
        {
            "method",
            "direction_slate_size",
            "max_planning_attempts",
            "memory",
            "proposer",
        },
        "policy",
    )
    memory = _mapping(table.get("memory"), "policy.memory")
    _only(
        memory,
        {"max_chars", "ranker", "summarizer"},
        "policy.memory",
    )
    slate_size = _integer(table, "direction_slate_size", "policy", minimum=1)
    if slate_size > MEMORY_RANK_MAX_CANDIDATES:
        raise PolicyConfigError(
            "memory direction_slate_size cannot exceed the ranking candidate "
            f"bound of {MEMORY_RANK_MAX_CANDIDATES}"
        )
    ranker = _parse_completion(memory.get("ranker"), "policy.memory.ranker")
    summarizer = _parse_completion(
        memory.get("summarizer"), "policy.memory.summarizer"
    )
    return PolicyConfig(
        method=PolicyMethod.MEMORY,
        memory=MemoryPolicyConfig(
            direction_slate_size=slate_size,
            max_chars=_integer(memory, "max_chars", "policy.memory", minimum=512),
            max_planning_attempts=_integer(
                table,
                "max_planning_attempts",
                "policy",
                minimum=1,
            ),
            proposer=_parse_proposer(table.get("proposer")),
            ranker=ranker,
            summarizer=summarizer,
        ),
    )


def parse_policy_config(value: Mapping[str, Any]) -> PolicyConfig:
    table = _mapping(value, "policy")
    method_text = _text(table, "method", "policy")
    try:
        method = PolicyMethod(method_text)
    except ValueError as exc:
        raise PolicyConfigError("policy.method must be ucb or memory") from exc
    return _parse_ucb(table) if method is PolicyMethod.UCB else _parse_memory(table)


def load_policy_config(path: str | Path) -> PolicyConfig:
    source = Path(path)
    try:
        document = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyConfigError(f"cannot load policy config from {source}") from exc
    _only(document, {"policy"}, "config root")
    return parse_policy_config(_mapping(document.get("policy"), "policy"))


__all__ = [
    "CompletionConfig",
    "CooldownConfig",
    "EffectivenessConfig",
    "MemoryPolicyConfig",
    "NeighborhoodConfig",
    "PolicyConfig",
    "PolicyConfigError",
    "ProposerConfig",
    "SemanticConfig",
    "UCBPolicyConfig",
    "load_policy_config",
    "parse_policy_config",
]
