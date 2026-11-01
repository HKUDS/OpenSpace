"""Benchmark-neutral fixed-size sampling for paired evaluation.

Selection randomness and measurement randomness deliberately live in separate
APIs.  A selection seed chooses which finite-population units enter a draw;
the measurement seed for a unit/replicate is derived only from the evaluation
scope, unit identity, and replicate index.  This separation is required for a
design-based Horvitz--Thompson analysis.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import statistics
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, TypeVar

from agentpilot.domain.common import deep_freeze, require_text

SAMPLING_METHOD_IDENTITY = "balanced-product-stratified"
MARGINAL_PROPENSITY_METHOD_IDENTITY = "exact-balanced-product"
JOINT_PROPENSITY_METHOD_IDENTITY = "exact-balanced-product-joint"
MEASUREMENT_SEED_METHOD_IDENTITY = "scope-task-replicate"

_T = TypeVar("_T")


@dataclass(frozen=True)
class SamplingUnit:
    """One member of a finite evaluation population.

    ``stratum`` is an adapter-provided balancing label. For a competition it
    may be a problem cluster, but the sampling implementation has no
    benchmark-specific knowledge.
    """

    unit_id: str
    stratum: str

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ValueError("unit_id must be non-empty")
        if not self.stratum:
            raise ValueError("stratum must be non-empty")


@dataclass(frozen=True)
class SamplingDraw:
    """A realized draw together with its frozen exact sampling design."""

    selected: tuple[SamplingUnit, ...]
    marginal_inclusion: Mapping[str, float]
    selected_joint_inclusion: Mapping[tuple[str, str], float]
    selection_seed: int
    sampling_method: str = SAMPLING_METHOD_IDENTITY
    marginal_method: str = MARGINAL_PROPENSITY_METHOD_IDENTITY
    joint_method: str = JOINT_PROPENSITY_METHOD_IDENTITY

    def __post_init__(self) -> None:
        selected = tuple(self.selected)
        if any(not isinstance(unit, SamplingUnit) for unit in selected):
            raise TypeError("selected must contain SamplingUnit values")
        selected_ids = tuple(unit.unit_id for unit in selected)
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("sampling draw contains duplicate selected unit ids")
        if isinstance(self.selection_seed, bool) or not isinstance(
            self.selection_seed, int
        ):
            raise ValueError("selection_seed must be an integer")
        expected_methods = {
            "sampling_method": SAMPLING_METHOD_IDENTITY,
            "marginal_method": MARGINAL_PROPENSITY_METHOD_IDENTITY,
            "joint_method": JOINT_PROPENSITY_METHOD_IDENTITY,
        }
        for name, expected in expected_methods.items():
            if getattr(self, name) != expected:
                raise ValueError(f"{name} must be {expected!r}")

        if not isinstance(self.marginal_inclusion, Mapping):
            raise TypeError("marginal_inclusion must be a mapping")
        marginal: dict[str, float] = {}
        for raw_unit_id, raw_probability in self.marginal_inclusion.items():
            unit_id = require_text(raw_unit_id, "marginal unit id")
            if unit_id in marginal:
                raise ValueError("marginal inclusion contains duplicate unit ids")
            probability = float(raw_probability)
            if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                raise ValueError("marginal inclusion probabilities must be in [0, 1]")
            marginal[unit_id] = probability
        if selected_ids:
            if any(probability <= 0.0 for probability in marginal.values()):
                raise ValueError(
                    "non-empty fixed-size designs need positive marginals"
                )
        elif any(probability != 0.0 for probability in marginal.values()):
            raise ValueError("empty fixed-size designs need zero marginals")
        if not math.isclose(
            sum(marginal.values()),
            float(len(selected_ids)),
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("marginal inclusion sum differs from fixed draw size")
        for unit_id in selected_ids:
            if marginal.get(unit_id, 0.0) <= 0.0:
                raise ValueError(
                    "selected units need strictly positive marginal inclusion"
                )

        if not isinstance(self.selected_joint_inclusion, Mapping):
            raise TypeError("selected_joint_inclusion must be a mapping")
        expected_pairs = set(itertools.combinations(sorted(selected_ids), 2))
        joint: dict[tuple[str, str], float] = {}
        for raw_pair, raw_probability in self.selected_joint_inclusion.items():
            if not isinstance(raw_pair, tuple) or len(raw_pair) != 2:
                raise ValueError("joint inclusion keys must be two-item tuples")
            first = require_text(raw_pair[0], "joint unit id")
            second = require_text(raw_pair[1], "joint unit id")
            if first >= second:
                raise ValueError("joint inclusion keys must be canonical ordered pairs")
            if (first, second) in joint:
                raise ValueError("joint inclusion contains duplicate pairs")
            probability = float(raw_probability)
            lower = max(
                0.0,
                marginal.get(first, 0.0) + marginal.get(second, 0.0) - 1.0,
            )
            if (
                not math.isfinite(probability)
                or probability <= 0.0
                or probability < lower - 1e-12
                or probability
                > min(marginal.get(first, 0.0), marginal.get(second, 0.0))
            ):
                raise ValueError("selected joint inclusion probability is invalid")
            joint[(first, second)] = probability
        if set(joint) != expected_pairs:
            raise ValueError(
                "selected joint inclusion must exactly cover selected unit pairs"
            )

        object.__setattr__(self, "selected", selected)
        object.__setattr__(self, "marginal_inclusion", deep_freeze(marginal))
        object.__setattr__(self, "selected_joint_inclusion", deep_freeze(joint))

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(unit.unit_id for unit in self.selected)


@dataclass(frozen=True)
class MeasurementCell:
    """A deterministic measurement slot for one selected unit."""

    unit_id: str
    replicate_index: int
    seed: int


def _nonnegative_integer(value: int, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a nonnegative integer")
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a nonnegative integer") from exc
    if converted != value or converted < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return converted


def measurement_seed(scope_identity: str, unit_id: str, replicate_index: int) -> int:
    """Name a measurement slot with a stable 63-bit value.

    Despite the name this does not seed anything. Benchmarks are free to feed it
    to a generator they control, but nothing here requires them to, and the
    adapter cannot: its solver is an agentic loop whose randomness lives in the
    provider and in the container it observes, neither of which accepts a seed.
    Treat the value as a replicate's identity, and do not read a recorded seed as
    evidence that a measurement is reproducible.
    """

    if not scope_identity:
        raise ValueError("scope_identity must be non-empty")
    if not unit_id:
        raise ValueError("unit_id must be non-empty")
    index = _nonnegative_integer(replicate_index, name="replicate_index")
    payload = json.dumps(
        {
            "method": MEASUREMENT_SEED_METHOD_IDENTITY,
            "replicate_index": index,
            # Preserve the checkpoint's seed schedule while exposing the more
            # general ``unit_id`` vocabulary at this module boundary.
            "scope": scope_identity,
            "task_id": unit_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return random.Random(payload).randrange(1 << 63)


def build_measurement_cells(
    selected: Sequence[SamplingUnit],
    *,
    scope_identity: str,
    replicates: int,
) -> tuple[MeasurementCell, ...]:
    """Build the fixed measurement schedule for a realized selection."""

    count = _nonnegative_integer(replicates, name="replicates")
    if count < 1:
        raise ValueError("replicates must be an integer >= 1")
    return tuple(
        MeasurementCell(
            unit_id=unit.unit_id,
            replicate_index=index,
            seed=measurement_seed(scope_identity, unit.unit_id, index),
        )
        for unit in selected
        for index in range(count)
    )


def _validated_population(
    units: Sequence[SamplingUnit],
    weights: Optional[Mapping[str, float]],
) -> tuple[SamplingUnit, ...]:
    ordered = tuple(sorted(units, key=lambda unit: (unit.stratum, unit.unit_id)))
    ids = [unit.unit_id for unit in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("sampling unit ids must be unique")
    if weights is not None:
        missing = sorted(set(ids) - set(weights))
        if missing:
            raise KeyError(f"sampling weights missing unit ids: {missing!r}")
        for unit_id in ids:
            value = float(weights[unit_id])
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    "sampling weights must be finite and strictly positive"
                )
    return ordered


def _normalised_weights(
    items: Sequence[_T],
    weights: Optional[Mapping[str, float]],
    key: Callable[[_T], str],
) -> list[float]:
    raw = [1.0 if weights is None else float(weights[key(item)]) for item in items]
    if any(not math.isfinite(value) or value <= 0.0 for value in raw):
        raise ValueError("sampling weights must be finite and strictly positive")
    scale = max(raw, default=1.0)
    return [value / scale for value in raw]


def _logaddexp(left: float, right: float) -> float:
    if left == -math.inf:
        return right
    if right == -math.inf:
        return left
    high, low = (left, right) if left >= right else (right, left)
    return high + math.log1p(math.exp(low - high))


def _log_elementary_suffix(log_values: Sequence[float], size: int) -> list[list[float]]:
    """Return log elementary-symmetric-polynomial suffixes."""

    n_items = len(log_values)
    suffix = [[-math.inf] * (size + 1) for _ in range(n_items + 1)]
    suffix[n_items][0] = 0.0
    for index in range(n_items - 1, -1, -1):
        suffix[index][0] = 0.0
        for count in range(1, min(size, n_items - index) + 1):
            suffix[index][count] = _logaddexp(
                suffix[index + 1][count],
                log_values[index] + suffix[index + 1][count - 1],
            )
    return suffix


def _product_weight_sample(
    items: Sequence[_T],
    *,
    size: int,
    weights: Optional[Mapping[str, float]],
    rng: random.Random,
    key: Callable[[_T], str],
) -> list[_T]:
    """Sample fixed-size subsets with probability proportional to weight product."""

    ordered = list(items)
    n_items = len(ordered)
    target = max(0, min(size, n_items))
    if target == 0:
        return []
    if target == n_items:
        return ordered
    values = _normalised_weights(ordered, weights, key)
    log_values = [math.log(value) for value in values]
    suffix = _log_elementary_suffix(log_values, target)
    if not math.isfinite(suffix[0][target]):
        raise ValueError("sampling design has zero normalising mass")

    picked: list[_T] = []
    remaining = target
    for index, item in enumerate(ordered):
        if remaining == 0:
            break
        items_left = n_items - index
        if remaining == items_left:
            picked.extend(ordered[index:])
            remaining = 0
            break
        log_probability = (
            log_values[index]
            + suffix[index + 1][remaining - 1]
            - suffix[index][remaining]
        )
        if rng.random() < math.exp(min(0.0, log_probability)):
            picked.append(item)
            remaining -= 1
    if remaining:
        raise RuntimeError("product-weight sampler failed to fill its quota")
    return picked


def _product_inclusion_probabilities(
    items: Sequence[_T],
    *,
    size: int,
    weights: Optional[Mapping[str, float]],
    key: Callable[[_T], str],
) -> dict[str, float]:
    """Compute exact first-order inclusion probabilities for a product design."""

    ordered = list(items)
    n_items = len(ordered)
    target = max(0, min(size, n_items))
    if target == 0:
        return {key(item): 0.0 for item in ordered}
    if target == n_items:
        return {key(item): 1.0 for item in ordered}
    values = _normalised_weights(ordered, weights, key)
    log_values = [math.log(value) for value in values]

    prefix = [[-math.inf] * (target + 1) for _ in range(n_items + 1)]
    prefix[0][0] = 0.0
    for index, log_value in enumerate(log_values):
        prefix[index + 1][0] = 0.0
        for count in range(1, min(target, index + 1) + 1):
            prefix[index + 1][count] = _logaddexp(
                prefix[index][count],
                log_value + prefix[index][count - 1],
            )
    suffix = _log_elementary_suffix(log_values, target)
    denominator = prefix[n_items][target]
    if not math.isfinite(denominator):
        raise ValueError("sampling design has zero normalising mass")

    result: dict[str, float] = {}
    for index, item in enumerate(ordered):
        log_excluded = -math.inf
        for left_count in range(target):
            right_count = target - 1 - left_count
            if left_count <= index and right_count <= n_items - index - 1:
                log_excluded = _logaddexp(
                    log_excluded,
                    prefix[index][left_count] + suffix[index + 1][right_count],
                )
        log_probability = log_values[index] + log_excluded - denominator
        result[key(item)] = math.exp(min(0.0, log_probability))
    return result


def _product_joint_inclusion_probability(
    items: Sequence[_T],
    *,
    size: int,
    weights: Optional[Mapping[str, float]],
    key: Callable[[_T], str],
    first_key: str,
    second_key: str,
) -> float:
    """Compute exact second-order inclusion probability for one item pair."""

    if first_key == second_key:
        raise ValueError("joint inclusion requires two distinct items")
    ordered = list(items)
    keyed = {key(item): item for item in ordered}
    if len(keyed) != len(ordered):
        raise ValueError("sampling design item keys must be unique")
    if first_key not in keyed or second_key not in keyed:
        raise KeyError("joint-inclusion item is absent from the design")
    n_items = len(ordered)
    target = max(0, min(size, n_items))
    if target < 2:
        return 0.0
    if target == n_items:
        return 1.0

    values = _normalised_weights(ordered, weights, key)
    log_by_key = {key(item): math.log(value) for item, value in zip(ordered, values)}
    retained = [
        log_by_key[key(item)]
        for item in ordered
        if key(item) not in {first_key, second_key}
    ]
    excluded = _log_elementary_suffix(retained, target - 2)[0][target - 2]
    denominator = _log_elementary_suffix(list(log_by_key.values()), target)[0][target]
    if not math.isfinite(denominator):
        raise ValueError("sampling design has zero normalising mass")
    if not math.isfinite(excluded):
        return 0.0
    log_probability = (
        log_by_key[first_key] + log_by_key[second_key] + excluded - denominator
    )
    return math.exp(min(0.0, log_probability))


def _grouped(
    population: Sequence[SamplingUnit],
) -> dict[str, list[SamplingUnit]]:
    groups: dict[str, list[SamplingUnit]] = {}
    for unit in population:
        groups.setdefault(unit.stratum, []).append(unit)
    return groups


def _stratum_weights(
    groups: Mapping[str, Sequence[SamplingUnit]],
    weights: Optional[Mapping[str, float]],
) -> Optional[dict[str, float]]:
    if weights is None:
        return None
    return {
        stratum: statistics.fmean(weights[unit.unit_id] for unit in group)
        for stratum, group in groups.items()
    }


def _quota_design(
    groups: Mapping[str, Sequence[SamplingUnit]],
    target: int,
    group_weights: Optional[Mapping[str, float]],
) -> tuple[dict[str, int], dict[str, float], int]:
    """Water-fill strata, then allocate remainder by an exact product design."""

    strata = sorted(groups)
    pool_size = sum(len(groups[stratum]) for stratum in strata)
    target = max(0, min(target, pool_size))
    level = 0
    while True:
        current = sum(min(len(groups[stratum]), level) for stratum in strata)
        if current == target:
            break
        next_total = sum(min(len(groups[stratum]), level + 1) for stratum in strata)
        if next_total > target or next_total == current:
            break
        level += 1
    base = {stratum: min(len(groups[stratum]), level) for stratum in strata}
    remainder = target - sum(base.values())
    eligible = [stratum for stratum in strata if base[stratum] < len(groups[stratum])]
    extra_probability = {stratum: 0.0 for stratum in strata}
    if remainder:
        extra_probability.update(
            _product_inclusion_probabilities(
                eligible,
                size=remainder,
                weights=group_weights,
                key=lambda stratum: stratum,
            )
        )
    return base, extra_probability, remainder


def design_propensities(
    units: Sequence[SamplingUnit],
    *,
    size: int,
    weights: Optional[Mapping[str, float]] = None,
) -> dict[str, float]:
    """Return exact marginal inclusion probabilities for every population unit."""

    target = _nonnegative_integer(size, name="size")
    population = _validated_population(units, weights)
    if not population:
        return {}
    target = min(target, len(population))
    groups = _grouped(population)
    group_weights = _stratum_weights(groups, weights)
    base, extra_probability, _ = _quota_design(groups, target, group_weights)

    result: dict[str, float] = {}
    for stratum in sorted(groups):
        group = groups[stratum]
        low = _product_inclusion_probabilities(
            group,
            size=base[stratum],
            weights=weights,
            key=lambda unit: unit.unit_id,
        )
        rho = extra_probability[stratum]
        high = (
            _product_inclusion_probabilities(
                group,
                size=base[stratum] + 1,
                weights=weights,
                key=lambda unit: unit.unit_id,
            )
            if rho > 0.0
            else low
        )
        for unit in group:
            result[unit.unit_id] = (1.0 - rho) * low[unit.unit_id] + rho * high[
                unit.unit_id
            ]
    return result


def design_joint_propensities(
    units: Sequence[SamplingUnit],
    *,
    size: int,
    pairs: Sequence[tuple[str, str]],
    weights: Optional[Mapping[str, float]] = None,
) -> dict[tuple[str, str], float]:
    """Return exact joint inclusion probabilities for requested unit pairs."""

    target = _nonnegative_integer(size, name="size")
    population = _validated_population(units, weights)
    if not population:
        return {}
    target = min(target, len(population))
    groups = _grouped(population)
    unit_stratum = {unit.unit_id: unit.stratum for unit in population}
    group_weights = _stratum_weights(groups, weights)
    base, extra_probability, remainder = _quota_design(groups, target, group_weights)
    eligible = [
        stratum for stratum in sorted(groups) if base[stratum] < len(groups[stratum])
    ]

    first_cache: dict[tuple[str, int], dict[str, float]] = {}

    def conditional_first(stratum: str, quota: int) -> dict[str, float]:
        cache_key = (stratum, quota)
        if cache_key not in first_cache:
            first_cache[cache_key] = _product_inclusion_probabilities(
                groups[stratum],
                size=quota,
                weights=weights,
                key=lambda unit: unit.unit_id,
            )
        return first_cache[cache_key]

    extra_joint_cache: dict[tuple[str, str], float] = {}

    def extra_joint(first: str, second: str) -> float:
        pair = tuple(sorted((first, second)))
        if pair not in extra_joint_cache:
            extra_joint_cache[pair] = (
                _product_joint_inclusion_probability(
                    eligible,
                    size=remainder,
                    weights=group_weights,
                    key=lambda stratum: stratum,
                    first_key=first,
                    second_key=second,
                )
                if first in eligible and second in eligible
                else 0.0
            )
        return extra_joint_cache[pair]

    result: dict[tuple[str, str], float] = {}
    for raw_first, raw_second in pairs:
        if raw_first == raw_second:
            raise ValueError("joint inclusion requires distinct unit ids")
        first, second = sorted((raw_first, raw_second))
        if first not in unit_stratum or second not in unit_stratum:
            raise KeyError("joint-inclusion unit is absent from the population")
        first_stratum = unit_stratum[first]
        second_stratum = unit_stratum[second]
        if first_stratum == second_stratum:
            rho = extra_probability[first_stratum]
            low = _product_joint_inclusion_probability(
                groups[first_stratum],
                size=base[first_stratum],
                weights=weights,
                key=lambda unit: unit.unit_id,
                first_key=first,
                second_key=second,
            )
            high = (
                _product_joint_inclusion_probability(
                    groups[first_stratum],
                    size=base[first_stratum] + 1,
                    weights=weights,
                    key=lambda unit: unit.unit_id,
                    first_key=first,
                    second_key=second,
                )
                if rho > 0.0
                else low
            )
            value = (1.0 - rho) * low + rho * high
        else:
            rho_first = extra_probability[first_stratum]
            rho_second = extra_probability[second_stratum]
            rho_both = extra_joint(first_stratum, second_stratum)
            quota_probabilities = {
                (0, 0): 1.0 - rho_first - rho_second + rho_both,
                (1, 0): rho_first - rho_both,
                (0, 1): rho_second - rho_both,
                (1, 1): rho_both,
            }
            value = 0.0
            for (first_extra, second_extra), probability in quota_probabilities.items():
                if probability <= 0.0:
                    continue
                first_probability = conditional_first(
                    first_stratum, base[first_stratum] + first_extra
                )[first]
                second_probability = conditional_first(
                    second_stratum, base[second_stratum] + second_extra
                )[second]
                value += probability * first_probability * second_probability
        result[(first, second)] = min(1.0, max(0.0, value))
    return result


def draw_stratified_sample(
    units: Sequence[SamplingUnit],
    *,
    size: int,
    selection_seed: int,
    weights: Optional[Mapping[str, float]] = None,
) -> SamplingDraw:
    """Draw one exact balanced product sample and freeze its propensities."""

    target = _nonnegative_integer(size, name="size")
    if isinstance(selection_seed, bool) or not isinstance(selection_seed, int):
        raise ValueError("selection_seed must be an integer")
    population = _validated_population(units, weights)
    target = min(target, len(population))
    groups = _grouped(population)
    group_weights = _stratum_weights(groups, weights)
    base, _, remainder = _quota_design(groups, target, group_weights)
    rng = random.Random(selection_seed)
    eligible = [
        stratum for stratum in sorted(groups) if base[stratum] < len(groups[stratum])
    ]
    extra_strata = set(
        _product_weight_sample(
            eligible,
            size=remainder,
            weights=group_weights,
            rng=rng,
            key=lambda stratum: stratum,
        )
    )
    selected: list[SamplingUnit] = []
    for stratum in sorted(groups):
        quota = base[stratum] + (1 if stratum in extra_strata else 0)
        selected.extend(
            _product_weight_sample(
                groups[stratum],
                size=quota,
                weights=weights,
                rng=rng,
                key=lambda unit: unit.unit_id,
            )
        )
    if len(selected) != target:
        raise RuntimeError("balanced sampler did not fill the requested draw")

    marginals = design_propensities(population, size=target, weights=weights)
    selected_pairs = list(
        itertools.combinations(sorted(unit.unit_id for unit in selected), 2)
    )
    joints = design_joint_propensities(
        population,
        size=target,
        pairs=selected_pairs,
        weights=weights,
    )
    return SamplingDraw(
        selected=tuple(selected),
        marginal_inclusion=marginals,
        selected_joint_inclusion=joints,
        selection_seed=selection_seed,
    )


__all__ = [
    "JOINT_PROPENSITY_METHOD_IDENTITY",
    "MARGINAL_PROPENSITY_METHOD_IDENTITY",
    "MEASUREMENT_SEED_METHOD_IDENTITY",
    "SAMPLING_METHOD_IDENTITY",
    "MeasurementCell",
    "SamplingDraw",
    "SamplingUnit",
    "build_measurement_cells",
    "design_joint_propensities",
    "design_propensities",
    "draw_stratified_sample",
    "measurement_seed",
]
