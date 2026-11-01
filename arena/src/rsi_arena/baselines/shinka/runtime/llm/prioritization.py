import numpy as np
from abc import ABC, abstractmethod
from typing import Optional, Union, Sequence, List, Any, Dict
from scipy.special import logsumexp
from rich.table import Table
from rich.console import Console
import rich.box
import pickle
from pathlib import Path

from rsi_arena.baselines.shinka.runtime.local_openai_config import parse_local_openai_model

Arm = Union[int, str]
Subset = Optional[Union[np.ndarray, Sequence[Arm]]]


def _logadd(x_log, y_log, w1=1.0, w2=1.0):
    x = np.asarray(x_log, dtype=float) + np.log(w1)
    y = np.asarray(y_log, dtype=float) + np.log(w2)
    a = np.stack([x, y], axis=0)
    return logsumexp(a, axis=0)


def _logdiffexp(a_log, b_log):
    a = np.asarray(a_log, float)
    b = np.asarray(b_log, float)
    d = a - b
    with np.errstate(over="ignore", invalid="ignore"):
        v = a + np.log1p(-np.exp(-d))
    return np.where(d >= 0, v, -np.inf)


def _logexpm1(z):
    z = np.asarray(z, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(z > 50.0, z, np.log(np.expm1(z)))


def _truncate_middle(text: str, max_width: int) -> str:
    if len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]

    left_width = (max_width - 3) // 2
    right_width = max_width - 3 - left_width
    return f"{text[:left_width]}...{text[-right_width:]}"


def _format_arm_display_name(name: Arm, max_width: int) -> str:
    text = str(name)
    if not isinstance(name, str):
        return _truncate_middle(text, max_width)

    try:
        local_match = parse_local_openai_model(name)
    except ValueError:
        local_match = None

    if local_match is not None:
        text = f"local/{local_match.api_model_name}"
    elif name.startswith("openrouter/"):
        text = name
    else:
        text = name.split("/")[-1]

    return _truncate_middle(text, max_width)


class BanditBase(ABC):
    def __init__(
        self,
        n_arms: Optional[int] = None,
        seed: Optional[int] = None,
        arm_names: Optional[List[str]] = None,
        auto_decay: Optional[float] = None,
        shift_by_baseline: bool = True,
        shift_by_parent: bool = True,
    ):
        self.rng = np.random.default_rng(seed)

        if arm_names is None and n_arms is None:
            raise ValueError("provide n_arms or arm_names")
        if arm_names is not None:
            if n_arms is not None and int(n_arms) != len(arm_names):
                raise ValueError("len(arm_names) must equal n_arms")
            self._arm_names = list(arm_names)
            self._name_to_idx = {n: i for i, n in enumerate(self._arm_names)}
            self._n_arms = len(self._arm_names)
        else:
            self._arm_names = None
            self._name_to_idx = {}
            self._n_arms = int(n_arms)

        self._baseline = 0.0
        self._shift_by_baseline = bool(shift_by_baseline)
        self._shift_by_parent = bool(shift_by_parent)
        if auto_decay is not None and not (0.0 < auto_decay <= 1.0):
            raise ValueError("auto_decay must be in (0, 1]")
        self._auto_decay = auto_decay

    @property
    def n_arms(self) -> int:
        return self._n_arms

    def set_baseline_score(
        self,
        baseline: float,
    ) -> None:
        self._baseline = float(baseline)

    def _resolve_arm(self, arm: Arm) -> int:
        # allows updating by int index or string name
        if isinstance(arm, int):
            return int(arm)
        if self._arm_names is None:
            try:
                return int(arm)
            except Exception as e:
                raise ValueError("string arm requires arm_names") from e
        if arm not in self._name_to_idx:
            raise ValueError(f"unknown arm name '{arm}'")
        return self._name_to_idx[arm]

    def _resolve_subset(self, subset: Subset) -> np.ndarray:
        if subset is None:
            return np.arange(self.n_arms, dtype=np.int64)
        if isinstance(subset, np.ndarray) and np.issubdtype(subset.dtype, np.integer):
            return subset.astype(np.int64)
        idxs = [self._resolve_arm(a) for a in subset]
        return np.asarray(idxs, dtype=np.int64)

    def _maybe_decay(self) -> None:
        if self._auto_decay is not None:
            self.decay(self._auto_decay)

    @abstractmethod
    def update_submitted(
        self,
        arm: Arm,
    ) -> float:
        raise NotImplementedError

    @abstractmethod
    def update(
        self,
        arm: Arm,
        reward: Optional[float],
        baseline: Optional[float] = None,
    ) -> None:
        raise NotImplementedError

    def update_cost(
        self,
        arm: Arm,
        cost: float,
    ) -> float:
        # optional method to update cost associated with an arm
        return 0.0

    @abstractmethod
    def posterior(
        self,
        subset: Subset = None,
        samples: Optional[int] = None,
        **kwargs: Any,
    ) -> np.ndarray:
        raise NotImplementedError

    def select_llm(
        self,
        subset: Subset = None,
        samples: Optional[int] = None,
        **kwargs: Any,
    ) -> tuple[np.ndarray, np.ndarray]:
        # return one hot vector of selection probabilities per arm after
        # sampling from posterior
        probabilities = self.posterior(subset=subset, samples=samples, **kwargs)
        one_hot = np.zeros(self.n_arms, dtype=np.float64)
        samples = self.rng.choice(
            self.n_arms,
            size=1,
            p=probabilities,
        )
        one_hot[samples[0]] = 1.0
        return one_hot, probabilities

    @abstractmethod
    def decay(self, factor: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def print_summary(self, console: Optional[Console] = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """Get the internal state of the bandit for serialization."""
        raise NotImplementedError

    @abstractmethod
    def set_state(self, state: Dict[str, Any]) -> None:
        """Restore the internal state of the bandit from serialization."""
        raise NotImplementedError

    def save_state(self, path: Union[str, Path]) -> None:
        """Save bandit state to a pickle file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = self.get_state()
        with open(path, "wb") as f:
            pickle.dump(state, f)

    def load_state(self, path: Union[str, Path]) -> None:
        """Load bandit state from a pickle file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Bandit state file not found: {path}")
        with open(path, "rb") as f:
            state = pickle.load(f)
        self.set_state(state)

    def _state_arm_names(self) -> Optional[List[str]]:
        if self._arm_names is None:
            return None
        return list(self._arm_names)

    def _align_state_array(
        self,
        state: Dict[str, Any],
        key: str,
        default: np.ndarray,
    ) -> np.ndarray:
        if key not in state:
            return default.copy()

        saved = np.asarray(state[key], dtype=default.dtype)
        if saved.ndim != 1:
            raise ValueError(f"bandit state field '{key}' must be one-dimensional")

        aligned = default.copy()
        saved_arm_names = state.get("arm_names")
        if saved_arm_names is not None and self._arm_names is not None:
            saved_name_to_idx = {
                name: i for i, name in enumerate(saved_arm_names) if i < saved.size
            }
            for i, name in enumerate(self._arm_names):
                saved_i = saved_name_to_idx.get(name)
                if saved_i is not None:
                    aligned[i] = saved[saved_i]
            return aligned

        n = min(saved.size, aligned.size)
        if n > 0:
            aligned[:n] = saved[:n]
        return aligned

    def _state_arm_layout_matches(self, state: Dict[str, Any], key: str) -> bool:
        saved_arm_names = state.get("arm_names")
        if saved_arm_names is not None and self._arm_names is not None:
            return list(saved_arm_names) == self._arm_names
        if key not in state:
            return True
        return np.asarray(state[key]).size == self.n_arms

    def _restore_observation_range(self, state: Dict[str, Any]) -> None:
        if self._state_arm_layout_matches(state, "s"):
            self._obs_max = state["obs_max"]
            self._obs_min = state["obs_min"]
            return

        seen = self.divs > 0.0
        means = self._mean()[seen]
        finite_means = means[np.isfinite(means)]

        if self.asymmetric_scaling:
            if self.use_exponential_scaling:
                self._obs_min = -np.inf
                self._obs_max = (
                    float(finite_means.max()) if finite_means.size > 0 else -np.inf
                )
            else:
                self._obs_min = 0.0
                self._obs_max = (
                    max(0.0, float(finite_means.max()))
                    if finite_means.size > 0
                    else 0.0
                )
            return

        self._obs_max = float(finite_means.max()) if finite_means.size > 0 else -np.inf
        self._obs_min = float(finite_means.min()) if finite_means.size > 0 else np.inf


class AsymmetricUCB(BanditBase):
    # asymmetric ucb1 with ε-exploration and adaptive scaling
    def __init__(
        self,
        n_arms: Optional[int] = None,
        seed: Optional[int] = None,
        exploration_coef: float = 1.0,
        epsilon: float = 0.2,
        arm_names: Optional[List[str]] = None,
        auto_decay: Optional[float] = 0.95,
        shift_by_baseline: bool = True,
        shift_by_parent: bool = True,
        adaptive_scale: bool = True,
        asymmetric_scaling: bool = True,
        exponential_base: Optional[float] = 1.0,
        cost_aware_coef: float = 0.0,
        cost_exploration_coef: Optional[float] = 0.1,
        cost_power: float = 1.0,
        cost_ref_percentile: float = 50.0,
    ):
        super().__init__(
            n_arms=n_arms,
            seed=seed,
            arm_names=arm_names,
            auto_decay=auto_decay,
            shift_by_baseline=shift_by_baseline,
            shift_by_parent=shift_by_parent,
        )
        if asymmetric_scaling:
            assert shift_by_baseline or shift_by_parent, (
                "asymmetric scaling requires at least one of "
                "shift_by_baseline or shift_by_parent to be True"
            )
        if not (0.0 <= epsilon <= 1.0):
            raise ValueError("epsilon must be in [0, 1]")
        if not (0.0 <= cost_aware_coef <= 1.0):
            raise ValueError("cost_aware_coefficient must be in [0, 1]")
        if cost_power <= 0.0:
            raise ValueError("cost_power must be > 0")
        if not (0.0 <= cost_ref_percentile <= 100.0):
            raise ValueError("cost_ref_percentile must be in [0, 100]")
        self.c = float(exploration_coef)
        self.epsilon = float(epsilon)
        self.adaptive_scale = bool(adaptive_scale)
        self.asymmetric_scaling = bool(asymmetric_scaling)
        self.exponential_base = exponential_base
        self.cost_aware_coefficient = float(cost_aware_coef)
        self.cost_power = float(cost_power)
        self.cost_ref_percentile = float(cost_ref_percentile)
        if cost_exploration_coef is None:
            self.cost_exploration_coef = self.c
        else:
            self.cost_exploration_coef = float(cost_exploration_coef)

        self.use_exponential_scaling = self.exponential_base is not None

        # if none, no exponential scaling
        if self.exponential_base is not None:
            assert self.exponential_base > 0.0, "exponential_base must be > 0"
            self.exponential_base = float(exponential_base)

        n = self.n_arms
        self.n_submitted = np.zeros(n, dtype=np.float64)
        self.n_completed = np.zeros(n, dtype=np.float64)
        self.n_costs = np.zeros(n, dtype=np.float64)
        self.total_costs = np.zeros(n, dtype=np.float64)
        if self.use_exponential_scaling:
            self.s = np.full(n, -np.inf, dtype=np.float64)
        else:
            self.s = np.zeros(n, dtype=np.float64)
        self.divs = np.zeros(n, dtype=np.float64)

        if self.asymmetric_scaling:
            if self.use_exponential_scaling:
                self._obs_max = -np.inf
                self._obs_min = -np.inf
            else:
                self._obs_min = 0.0
                self._obs_max = 0.0
        else:
            self._obs_max = -np.inf
            self._obs_min = np.inf

        self.max_cost_observed = -np.inf
        self.min_cost_observed = np.inf

    @property
    def n(self) -> np.ndarray:
        return np.maximum(self.n_submitted, self.n_completed)

    def _add_to_reward(self, r: float, value: float, coeff_r=1, coeff_value=1) -> float:
        if self.use_exponential_scaling:
            out, sign = logsumexp(
                [r, value],
                b=[coeff_r, coeff_value],
                return_sign=True,
            )
        else:
            out = coeff_r * r + coeff_value * value
        return out

    def _multiply_reward(self, r: float, value: float) -> float:
        if self.use_exponential_scaling:
            assert value > 0, "Multipliers in log space must be > 0"
            out = r + np.log(value)
        else:
            out = r * value
        return out

    def _mean(self) -> np.ndarray:
        denom = np.maximum(self.divs, 1e-7)
        if self.use_exponential_scaling:
            return self.s - np.log(denom)
        else:
            return self.s / denom

    def _update_obs_range(self, r: float) -> None:
        if r > self._obs_max:
            self._obs_max = r
        if not (self.use_exponential_scaling and self.asymmetric_scaling):
            if r < self._obs_min:
                self._obs_min = r

    def _have_obs_range(self) -> bool:
        if self.use_exponential_scaling and self.asymmetric_scaling:
            return np.isfinite(self._obs_max)
        return (
            np.isfinite(self._obs_min)
            and np.isfinite(self._obs_max)
            and (self._obs_max - self._obs_min) > 0.0
        )

    def _impute_worst_reward(self) -> float:
        if self.asymmetric_scaling:
            return -np.inf if self.use_exponential_scaling else 0.0

        seen = self.n > 0
        if not np.any(seen):
            return 0.0

        denom = np.maximum(self.divs[seen], 1e-7)
        mu = self.s[seen] / denom
        mu_min = float(mu.min())
        if mu.size >= 2:
            s = float(mu.std(ddof=1))
            sigma = 1.0 if (not np.isfinite(s) or s <= 0.0) else s
        else:
            sigma = 1.0
        return mu_min - sigma

    def _normalized_means(self, idx):
        if not self.adaptive_scale or not self._have_obs_range():
            m = self._mean()[idx]
            return np.exp(m) if self.use_exponential_scaling else m
        elif self.use_exponential_scaling and self.asymmetric_scaling:
            mlog = self._mean()[idx]
            return np.exp(mlog - self._obs_max)
        elif self.use_exponential_scaling:
            means_log = self._mean()[idx]
            rng_log = _logdiffexp(self._obs_max, self._obs_min)
            num_log = _logdiffexp(means_log, self._obs_min)
            return np.exp(num_log - rng_log)
        else:
            means = self._mean()[idx]
            rng = max(self._obs_max - self._obs_min, 1e-9)
            return (means - self._obs_min) / rng

    def update_submitted(
        self,
        arm: Arm,
    ) -> float:
        arm = self._resolve_arm(arm)
        self.n_submitted[arm] += 1.0
        return self.n[arm]

    def update(self, arm, reward, baseline=None):
        i = self._resolve_arm(arm)
        is_real = reward is not None
        r_raw = float(reward) if is_real else self._impute_worst_reward()

        if self._shift_by_parent and self._shift_by_baseline:
            baseline = (
                self._baseline if baseline is None else max(baseline, self._baseline)
            )
        elif self._shift_by_baseline:
            baseline = self._baseline
        elif not self._shift_by_parent:
            baseline = 0.0
        if baseline is None:
            raise ValueError("baseline required when shifting is active")

        r = r_raw - baseline

        if self.asymmetric_scaling:
            r = max(r, 0.0)

        self.divs[i] += 1.0
        self.n_completed[i] += 1.0

        if self.use_exponential_scaling and self.asymmetric_scaling:
            z = r * self.exponential_base
            if self._shift_by_baseline:
                contrib_log = _logexpm1(z)
            else:
                contrib_log = z
            self.s[i] = _logadd(self.s[i], contrib_log)
            if self.adaptive_scale and is_real:
                self._update_obs_range(contrib_log)
        else:
            self.s[i] += r
            if self.adaptive_scale and is_real:
                self._update_obs_range(r)

        self._maybe_decay()
        return r, baseline

    def update_cost(
        self,
        arm: Arm,
        cost: float,
    ) -> float:
        i = self._resolve_arm(arm)
        c = float(cost)
        self.total_costs[i] += c
        self.n_costs[i] += 1.0

        if c > self.max_cost_observed:
            self.max_cost_observed = c
        if c < self.min_cost_observed:
            self.min_cost_observed = c

        return c

    def _normalized_cost_ratio(
        self,
        idx: np.ndarray,
        num: float,
        n_cost_bonus: Optional[np.ndarray] = None,
        denom_floor_min: float = 1e-7,
    ) -> Optional[np.ndarray]:
        if not (
            np.isfinite(self.max_cost_observed) and np.isfinite(self.min_cost_observed)
        ):
            return None
        if self.max_cost_observed < self.min_cost_observed:
            return None

        n_cost = np.maximum(self.n_costs[idx], 1.0)
        mean_costs = self.total_costs[idx] / n_cost

        cost_range = self.max_cost_observed - self.min_cost_observed

        if n_cost_bonus is None:
            n_cost_bonus = n_cost

        # optimistic "cheapness": use a lower confidence bound on cost
        cost_bonus = (
            self.cost_exploration_coef * cost_range * np.sqrt(num / n_cost_bonus)
        )

        # use small floor to prevent division by zero, but don't artificially
        # equalize costs by using min_cost_observed as floor
        denom_floor = denom_floor_min
        cost_denom = np.maximum(mean_costs - cost_bonus, denom_floor)

        have_cost = self.n_costs[idx] > 0.0

        # use percentile of observed costs as reference to avoid imbalance when
        # interpolating between reward-only and reward/cost scaling
        # higher percentile (e.g. 100=max) more aggressively favors cheap models
        if np.any(have_cost):
            cost_ref = float(
                np.percentile(mean_costs[have_cost], self.cost_ref_percentile)
            )
        else:
            cost_ref = denom_floor
        cost_ref = max(cost_ref, denom_floor)

        return cost_ref / cost_denom

    def _restore_cost_range(self, state: Dict[str, Any]) -> None:
        if (
            self._state_arm_layout_matches(state, "n_costs")
            and "min_cost_observed" in state
            and "max_cost_observed" in state
        ):
            self.min_cost_observed = float(state["min_cost_observed"])
            self.max_cost_observed = float(state["max_cost_observed"])
            return

        have_cost = self.n_costs > 0.0
        if not np.any(have_cost):
            self.min_cost_observed = np.inf
            self.max_cost_observed = -np.inf
            return

        mean_costs = self.total_costs[have_cost] / self.n_costs[have_cost]
        finite_costs = mean_costs[np.isfinite(mean_costs)]
        if finite_costs.size == 0:
            self.min_cost_observed = np.inf
            self.max_cost_observed = -np.inf
            return

        self.min_cost_observed = float(finite_costs.min())
        self.max_cost_observed = float(finite_costs.max())

    def posterior(self, subset=None, samples=None):
        idx = self._resolve_subset(subset)
        if samples is None or int(samples) <= 1:
            n_sub = self.n[idx]
            probs = np.zeros(self._n_arms, dtype=np.float64)

            # Handle empty subset
            if idx.size == 0:
                return probs

            if np.all(n_sub <= 0.0):
                p = np.ones(idx.size) / idx.size
                probs[idx] = p
                return probs

            unseen = np.where(n_sub <= 0.0)[0]
            if unseen.size > 0:
                p = np.ones(unseen.size) / unseen.size
                probs[idx[unseen]] = p
                return probs

            t = float(self.n.sum())
            base = self._normalized_means(idx)
            num = 2.0 * np.log(max(t, 2.0))
            base_bonus = np.sqrt(num / n_sub)
            bonus = self.c * base_bonus
            scores = base + bonus

            if self.cost_aware_coefficient > 0.0:
                cost_ratio = self._normalized_cost_ratio(idx, num)
                if cost_ratio is not None:
                    # normalize cost_ratio to [0, 1] for stable additive blending
                    cost_ratio_max = np.maximum(cost_ratio.max(), 1e-9)
                    cost_ratio_norm = cost_ratio / cost_ratio_max
                    # apply cost_power to amplify cost differences
                    # power > 1 more aggressively favors cheap models
                    cost_ratio_scaled = np.power(cost_ratio_norm, self.cost_power)
                    # additive blend: at k=1, only cost matters; at k=0, only reward
                    k = self.cost_aware_coefficient
                    scores = (1.0 - k) * scores + k * cost_ratio_scaled

            winners = np.where(scores == scores.max())[0]
            rem = idx.size - winners.size
            p_sub = np.zeros(idx.size, dtype=np.float64)
            if rem == 0:
                p_sub[:] = 1.0 / idx.size
            else:
                p_sub[winners] = (1.0 - self.epsilon) / winners.size
                mask = np.ones(idx.size, dtype=bool)
                mask[winners] = False
                p_sub[mask] = self.epsilon / rem
            probs[idx] = p_sub
            return probs
        else:
            return self._posterior_batch(idx, samples)

    def _posterior_batch(self, idx: np.ndarray, k: int) -> np.ndarray:
        A = idx.size
        probs = np.zeros(self._n_arms, dtype=np.float64)
        if k <= 0 or A == 0:
            return probs

        n_sub = self.n[idx].astype(np.float64)
        v = np.zeros(A, dtype=np.int64)

        if np.all(n_sub <= 0.0):
            p = np.ones(A, dtype=np.float64) / A
            probs[idx] = p
            return probs

        unseen = np.where(n_sub <= 0.0)[0]
        if unseen.size > 0:
            if k >= unseen.size:
                v[unseen] += 1
                k -= unseen.size
            else:
                take = int(k)
                sel = self.rng.choice(unseen, size=take, replace=False)
                v[sel] += 1
                k = 0
            if k == 0:
                alloc = v.astype(np.float64)
                probs[idx] = alloc / alloc.sum()
                return probs

        base = self._normalized_means(idx)
        t0 = float(self.n.sum())
        step = int(v.sum()) + 1

        # simulate remaining k virtual pulls with epsilon-greedy
        while k > 0:
            num = 2.0 * np.log(max(t0 + step, 2.0))
            den = np.maximum(n_sub + v, 1.0)
            base_bonus = np.sqrt(num / den)
            scores = base + self.c * base_bonus

            if self.cost_aware_coefficient > 0.0:
                n_cost = np.maximum(self.n_costs[idx], 1.0)
                n_cost_bonus = n_cost + v
                cost_ratio = self._normalized_cost_ratio(
                    idx,
                    num,
                    n_cost_bonus=n_cost_bonus,
                )
                if cost_ratio is not None:
                    # normalize cost_ratio to [0, 1] for stable additive blending
                    cost_ratio_max = np.maximum(cost_ratio.max(), 1e-9)
                    cost_ratio_norm = cost_ratio / cost_ratio_max
                    # apply cost_power to amplify cost differences
                    # power > 1 more aggressively favors cheap models
                    cost_ratio_scaled = np.power(cost_ratio_norm, self.cost_power)
                    # additive blend: at k=1, only cost matters; at k=0, only reward
                    k = self.cost_aware_coefficient
                    scores = (1.0 - k) * scores + k * cost_ratio_scaled

            winners = np.where(scores == scores.max())[0]
            p = np.zeros(A, dtype=np.float64)
            if winners.size == A:
                p[:] = 1.0 / A
            else:
                p[winners] = (1.0 - self.epsilon) / winners.size
                mask = np.ones(A, dtype=bool)
                mask[winners] = False
                others = np.where(mask)[0]
                if others.size > 0:
                    p[others] = self.epsilon / others.size

            i = int(self.rng.choice(A, p=p))
            v[i] += 1
            step += 1
            k -= 1

        alloc = v.astype(np.float64)
        probs[idx] = alloc / alloc.sum()
        return probs

    def decay(self, factor: float) -> None:
        if not (0.0 < factor <= 1.0):
            raise ValueError("factor must be in (0, 1]")
        self.divs = self.divs * factor
        one_minus_factor = 1.0 - factor
        if self.use_exponential_scaling and self.asymmetric_scaling:
            # shrink in exp space to match original score scale
            s = self.s
            with np.errstate(divide="ignore", invalid="ignore"):
                log1p_term = np.where(
                    s > 0.0,
                    s + np.log(one_minus_factor + np.exp(-s)),
                    np.log1p(one_minus_factor * np.exp(s)),
                )
                self.s = s + np.log(factor) - log1p_term

            if self.adaptive_scale and np.isfinite(self._obs_max):
                means_log = self._mean()
                mmax = float(np.max(means_log))
                om = self._obs_max
                log1p_obs = (
                    om + np.log(one_minus_factor + np.exp(-om))
                    if om > 0.0
                    else np.log1p(one_minus_factor * np.exp(om))
                )
                obs_new = om + np.log(factor) - log1p_obs
                self._obs_max = max(obs_new, mmax)
        else:
            self.s = self.s * factor
            if self.adaptive_scale and self._have_obs_range():
                means = self._mean()
                self._obs_max = max(
                    self._obs_max * factor + one_minus_factor * np.max(means),
                    np.max(means),
                )
                self._obs_min = min(
                    self._obs_min * factor + one_minus_factor * np.min(means),
                    np.min(means),
                )

    def print_summary(self, console: Optional[Console] = None) -> None:
        names = self._arm_names or [str(i) for i in range(self._n_arms)]
        post = self.posterior()
        n = self.n.astype(int)
        idx = np.arange(self._n_arms)

        # exploitation and exploration components
        exploitation = self._normalized_means(idx)
        t = float(self.n.sum())
        num = 2.0 * np.log(max(t, 2.0))
        n_sub = np.maximum(self.n[idx], 1.0)
        exploration = self.c * np.sqrt(num / n_sub)
        score_raw = exploitation + exploration

        have_costs = (
            np.isfinite(self.min_cost_observed)
            and np.isfinite(self.max_cost_observed)
            and (self.max_cost_observed >= self.min_cost_observed)
        )

        n_costs = self.n_costs.astype(int)
        tot_cost = self.total_costs.astype(np.float64)

        mean_costs = np.zeros(self._n_arms, dtype=np.float64)
        score_cost = np.full(self._n_arms, np.nan, dtype=np.float64)
        score_used = score_raw.copy()

        with np.errstate(divide="ignore", invalid="ignore"):
            mask_cost = self.n_costs > 0.0
            mean_costs[mask_cost] = tot_cost[mask_cost] / self.n_costs[mask_cost]

        if have_costs:
            cost_ratio = self._normalized_cost_ratio(
                idx,
                num,
                denom_floor_min=1e-3,
            )
            if cost_ratio is not None:
                # normalize cost_ratio to [0, 1] for stable additive blending
                cost_ratio_max = np.maximum(cost_ratio.max(), 1e-9)
                cost_ratio_norm = cost_ratio / cost_ratio_max
                cost_ratio_scaled = np.power(cost_ratio_norm, self.cost_power)
                score_cost = cost_ratio_scaled

                # additive blend: at k=1, only cost matters; at k=0, only reward
                k = float(self.cost_aware_coefficient)
                score_used = (1.0 - k) * score_raw + k * cost_ratio_scaled

        # Create header information
        exp_base_str = (
            f"{self.exponential_base:.3f}"
            if self.exponential_base is not None
            else "None"
        )
        header_info = (
            f"AsymmetricUCB (c={self.c:.3f}, eps={self.epsilon:.3f}, "
            f"adaptive={self.adaptive_scale}, asym={self.asymmetric_scaling}, "
            f"exp_base={exp_base_str}, shift_base={self._shift_by_baseline}, "
            f"shift_parent={self._shift_by_parent}, "
            f"log_sum={self.use_exponential_scaling}, "
            f"cost_k={self.cost_aware_coefficient:.3f}, "
            f"cost_c={self.cost_exploration_coef:.3f}, "
            f"cost_pow={self.cost_power:.2f}, "
            f"cost_pct={self.cost_ref_percentile:.0f})"
        )

        additional_info = []
        if self._auto_decay is not None:
            additional_info.append(f"auto_decay={self._auto_decay:.3f}")
        additional_info.append(f"baseline={self._baseline:.6f}")

        if np.isfinite(self._obs_min) and np.isfinite(self._obs_max):
            if self.use_exponential_scaling:
                obs_min = np.exp(self._obs_min)
                obs_max = np.exp(self._obs_max)
            else:
                obs_min = self._obs_min
                obs_max = self._obs_max
            rng = obs_max - obs_min
            additional_info.append(
                f"obs_range=[{obs_min:.6f},{obs_max:.6f}] (w={rng:.6f})"
            )

        # Create rich table
        table = Table(
            title=header_info,
            box=rich.box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            width=120,
        )

        # Add columns
        table.add_column("arm", style="white", width=24)
        table.add_column("n", justify="right", style="green", width=4)
        table.add_column("n_cost", justify="right", style="green", width=7)
        table.add_column("tot_cost", justify="right", style="yellow", width=8)
        table.add_column("mean_cost", justify="right", style="yellow", width=8)
        table.add_column("exploit", justify="right", style="magenta", width=8)
        table.add_column("explore", justify="right", style="cyan", width=8)
        table.add_column("score_raw", justify="right", style="white", width=8)
        table.add_column("score_cost", justify="right", style="white", width=6)
        table.add_column("score", justify="right", style="bold white", width=8)
        table.add_column("post", justify="right", style="bright_green", width=6)

        # Add rows
        for i, name in enumerate(names):
            display_name = _format_arm_display_name(name, max_width=25)

            if n_costs[i] > 0:
                mean_cost_str = f"{mean_costs[i]:.4f}"
            else:
                mean_cost_str = "-"

            if have_costs:
                score_cost_str = f"{score_cost[i]:.4f}"
            else:
                score_cost_str = "-"

            table.add_row(
                display_name,
                f"{n[i]:d}",
                f"{n_costs[i]:d}",
                f"{tot_cost[i]:.4f}",
                mean_cost_str,
                f"{exploitation[i]:.4f}",
                f"{exploration[i]:.4f}",
                f"{score_raw[i]:.4f}",
                score_cost_str,
                f"{score_used[i]:.4f}",
                f"{post[i]:.4f}",
            )

        # Print directly to console
        _console = console or Console()
        _console.print(table)

    def get_state(self) -> Dict[str, Any]:
        """Get the internal state for serialization."""
        return {
            "arm_names": self._state_arm_names(),
            "n_submitted": self.n_submitted.copy(),
            "n_completed": self.n_completed.copy(),
            "s": self.s.copy(),
            "divs": self.divs.copy(),
            "baseline": self._baseline,
            "obs_max": self._obs_max,
            "obs_min": self._obs_min,
            "n_costs": self.n_costs.copy(),
            "total_costs": self.total_costs.copy(),
            "min_cost_observed": self.min_cost_observed,
            "max_cost_observed": self.max_cost_observed,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """Restore the internal state from serialization."""
        self.n_submitted = self._align_state_array(
            state, "n_submitted", self.n_submitted
        )
        self.n_completed = self._align_state_array(
            state, "n_completed", self.n_completed
        )
        self.s = self._align_state_array(state, "s", self.s)
        self.divs = self._align_state_array(state, "divs", self.divs)
        self._baseline = state["baseline"]
        self._restore_observation_range(state)
        self.n_costs = self._align_state_array(state, "n_costs", self.n_costs)
        self.total_costs = self._align_state_array(
            state, "total_costs", self.total_costs
        )
        self._restore_cost_range(state)


class FixedSampler(BanditBase):
    # samples from fixed prior probabilities; no learning or decay
    def __init__(
        self,
        n_arms: Optional[int] = None,
        seed: Optional[int] = None,
        prior_probs: Optional[np.ndarray] = None,
        arm_names: Optional[List[str]] = None,
        auto_decay: Optional[float] = None,
        shift_by_baseline: bool = True,
        shift_by_parent: bool = True,
        **kwargs: Any,
    ):
        super().__init__(
            n_arms=n_arms,
            seed=seed,
            arm_names=arm_names,
            auto_decay=auto_decay,
            shift_by_baseline=shift_by_baseline,
            shift_by_parent=shift_by_parent,
        )
        n = self.n_arms
        if prior_probs is None:
            self.p = np.full(n, 1.0 / n, dtype=np.float64)
        else:
            p = np.asarray(prior_probs, dtype=np.float64)
            if p.ndim != 1 or p.size != n:
                raise ValueError("prior_probs must be length n_arms")
            if np.any(p < 0.0):
                raise ValueError("prior_probs must be >= 0")
            s = p.sum()
            if s <= 0.0:
                raise ValueError("prior_probs must sum to > 0")
            self.p = p / s

        self.n_pulls = np.zeros(n, dtype=np.float64)
        self.n_costs = np.zeros(n, dtype=np.float64)
        self.total_costs = np.zeros(n, dtype=np.float64)

    def update_submitted(
        self,
        arm: Arm,
    ) -> float:
        return 0.0

    def update(
        self,
        arm: Arm,
        reward: Optional[float],
        baseline: Optional[float] = None,
    ) -> tuple[float, float]:
        i = self._resolve_arm(arm)
        self.n_pulls[i] += 1.0
        self._maybe_decay()
        return 0.0, baseline

    def update_cost(
        self,
        arm: Arm,
        cost: float,
    ) -> float:
        i = self._resolve_arm(arm)
        c = float(cost)
        self.total_costs[i] += c
        self.n_costs[i] += 1.0
        return c

    def posterior(
        self,
        subset: Subset = None,
        samples: Optional[int] = None,
    ) -> np.ndarray:
        # return fixed selection probabilities per arm
        if subset is None:
            return self.p.copy()
        idx = self._resolve_subset(subset)

        # Handle empty subset
        if idx.size == 0:
            return np.zeros(self.n_arms, dtype=np.float64)

        probs = self.p[idx]
        s = probs.sum()
        if s <= 0.0:
            raise ValueError("subset probs sum to 0")
        probs = probs / s
        out = np.zeros(self.n_arms, dtype=np.float64)
        out[idx] = probs
        return out

    def decay(self, factor: float) -> None:
        return None

    def print_summary(self, console: Optional[Console] = None) -> None:
        names = self._arm_names or [str(i) for i in range(self._n_arms)]
        post = self.posterior()
        n = self.n_pulls.astype(int)
        tot_cost = self.total_costs.astype(np.float64)

        # Create rich table
        table = Table(
            title="FixedSampler (fixed prior probs)",
            box=rich.box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            width=120,  # Match display.py table width
        )

        # Add columns
        table.add_column("arm", style="white", width=28)
        table.add_column("n", justify="right", style="green")
        table.add_column("tot_cost", justify="right", style="yellow")
        table.add_column("base", justify="right", style="blue")
        table.add_column("prob", justify="right", style="bright_green")

        # Add rows
        for i, name in enumerate(names):
            display_name = _format_arm_display_name(name, max_width=28)
            table.add_row(
                display_name,
                f"{n[i]:d}",
                f"{tot_cost[i]:.4f}",
                f"{self._baseline:.4f}",
                f"{post[i]:.4f}",
            )

        # Print directly to console
        _console = console or Console()
        _console.print(table)

    def get_state(self) -> Dict[str, Any]:
        """Get the internal state for serialization."""
        return {
            "arm_names": self._state_arm_names(),
            "baseline": self._baseline,
            "p": self.p.copy(),
            "n_pulls": self.n_pulls.copy(),
            "n_costs": self.n_costs.copy(),
            "total_costs": self.total_costs.copy(),
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """Restore the internal state from serialization."""
        self._baseline = state["baseline"]
        self.p = self._align_state_array(state, "p", self.p)
        p_sum = float(self.p.sum())
        if p_sum <= 0.0:
            raise ValueError("loaded fixed sampler probabilities must sum to > 0")
        self.p = self.p / p_sum
        self.n_pulls = self._align_state_array(state, "n_pulls", self.n_pulls)
        self.n_costs = self._align_state_array(state, "n_costs", self.n_costs)
        self.total_costs = self._align_state_array(
            state, "total_costs", self.total_costs
        )


class ThompsonSampler(BanditBase):
    def __init__(
        self,
        n_arms: Optional[int] = None,
        seed: Optional[int] = None,
        epsilon: float = 0.1,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
        arm_names: Optional[List[str]] = None,
        auto_decay: Optional[float] = 0.95,
        shift_by_baseline: bool = True,
        shift_by_parent: bool = True,
        adaptive_scale: bool = True,
        asymmetric_scaling: bool = True,
        exponential_base: Optional[float] = 1.0,
    ):
        super().__init__(
            n_arms=n_arms,
            seed=seed,
            arm_names=arm_names,
            auto_decay=auto_decay,
            shift_by_baseline=shift_by_baseline,
            shift_by_parent=shift_by_parent,
        )
        if not (0.0 <= epsilon <= 1.0):
            raise ValueError("epsilon must be in [0, 1]")
        self.epsilon = float(epsilon)
        self.adaptive_scale = bool(adaptive_scale)
        self.asymmetric_scaling = bool(asymmetric_scaling)
        self.exponential_base = exponential_base

        self.use_exponential_scaling = self.exponential_base is not None

        if self.exponential_base is not None:
            assert self.exponential_base > 0.0, "exponential_base must be > 0"
            self.exponential_base = float(exponential_base)

        n = self.n_arms
        self.n_submitted = np.zeros(n, dtype=np.float64)
        self.n_completed = np.zeros(n, dtype=np.float64)

        if self.use_exponential_scaling:
            self.s = np.full(n, -np.inf, dtype=np.float64)
        else:
            self.s = np.zeros(n, dtype=np.float64)
        self.divs = np.zeros(n, dtype=np.float64)

        if prior_alpha <= 0.0 or prior_beta <= 0.0:
            raise ValueError("priors must be > 0")
        self.a_prior = float(prior_alpha)
        self.b_prior = float(prior_beta)
        self.alpha = np.full(n, self.a_prior, dtype=np.float64)
        self.beta = np.full(n, self.b_prior, dtype=np.float64)

        if self.asymmetric_scaling:
            if self.use_exponential_scaling:
                self._obs_max = -np.inf
                self._obs_min = -np.inf
            else:
                self._obs_min = 0.0
                self._obs_max = 0.0
        else:
            self._obs_max = -np.inf
            self._obs_min = np.inf

    @property
    def n(self) -> np.ndarray:
        return np.maximum(self.n_submitted, self.n_completed)

    def _mean(self) -> np.ndarray:
        denom = np.maximum(self.divs, 1e-7)
        if self.use_exponential_scaling:
            return self.s - np.log(denom)
        else:
            return self.s / denom

    def _have_obs_range(self) -> bool:
        if self.use_exponential_scaling and self.asymmetric_scaling:
            return np.isfinite(self._obs_max)
        return (
            np.isfinite(self._obs_min)
            and np.isfinite(self._obs_max)
            and (self._obs_max - self._obs_min) > 0.0
        )

    def _impute_worst_reward(self) -> float:
        if self.asymmetric_scaling:
            return -np.inf if self.use_exponential_scaling else 0.0

        seen = self.n > 0
        if not np.any(seen):
            return 0.0

        denom = np.maximum(self.divs[seen], 1e-7)
        mu = self.s[seen] / denom
        mu_min = float(mu.min())
        if mu.size >= 2:
            s = float(mu.std(ddof=1))
            sigma = 1.0 if (not np.isfinite(s) or s <= 0.0) else s
        else:
            sigma = 1.0
        return mu_min - sigma

    def reshift_in_range(self, contrib: float, is_real: bool) -> float:
        # maps to 0-1
        if self.use_exponential_scaling and self.asymmetric_scaling:
            if np.isfinite(self._obs_max):
                return float(np.exp(contrib - self._obs_max))
            return 0.5 if is_real else 0.0
        if (
            np.isfinite(self._obs_min)
            and np.isfinite(self._obs_max)
            and (self._obs_max - self._obs_min) > 0.0
        ):
            rng = max(self._obs_max - self._obs_min, 1e-9)
            u = (contrib - self._obs_min) / rng
            return float(np.clip(u, 0.0, 1.0))
        return float(1.0 / (1.0 + np.exp(-contrib)))

    def update_submitted(
        self,
        arm: Arm,
    ) -> float:
        i = self._resolve_arm(arm)
        self.n_submitted[i] += 1.0
        return self.n[i]

    def update(
        self,
        arm: Arm,
        reward: Optional[float],
        baseline: Optional[float] = None,
    ) -> tuple[float, float]:
        i = self._resolve_arm(arm)
        is_real = reward is not None
        r_raw = float(reward) if is_real else self._impute_worst_reward()

        if self._shift_by_parent and self._shift_by_baseline:
            baseline = (
                self._baseline if baseline is None else max(baseline, self._baseline)
            )
        elif self._shift_by_baseline:
            baseline = self._baseline
        elif not self._shift_by_parent:
            baseline = 0.0
        if baseline is None:
            raise ValueError("baseline required when shifting is active")

        r = r_raw - baseline
        if self.asymmetric_scaling:
            r = max(r, 0.0)

        self.divs[i] += 1.0
        self.n_completed[i] += 1.0

        if self.use_exponential_scaling and self.asymmetric_scaling:
            z = r * self.exponential_base
            contrib = _logexpm1(z) if self._shift_by_baseline else z
            self.s[i] = _logadd(self.s[i], contrib)
            if self.adaptive_scale and is_real:
                if contrib > self._obs_max:
                    self._obs_max = contrib
        else:
            contrib = r
            self.s[i] += r
            if self.adaptive_scale and is_real:
                if contrib > self._obs_max:
                    self._obs_max = contrib
                if (
                    not (self.use_exponential_scaling and self.asymmetric_scaling)
                    and contrib < self._obs_min
                ):
                    self._obs_min = contrib

        # beta update
        u = 0.0 if not is_real else self.reshift_in_range(contrib, is_real=True)
        self.alpha[i] += u
        self.beta[i] += 1.0 - u

        self._maybe_decay()
        return r, baseline

    def posterior(self, subset=None, samples=None):
        idx = self._resolve_subset(subset)
        probs = np.zeros(self._n_arms, dtype=np.float64)
        A = idx.size
        if A == 0:
            return probs

        # check for completely unseen arms
        n_sub = self.n_completed[idx]
        unseen = np.where(n_sub <= 0.0)[0]
        if unseen.size > 0:
            n_sub = self.n[idx]
            # Handle empty subset
            if idx.size == 0:
                return probs

            if np.all(n_sub <= 0.0):
                p = np.ones(idx.size) / idx.size
                probs[idx] = p
                return probs
            # return uniform probability over unseen subset only
            p = np.zeros(idx.size, dtype=np.float64)
            p[unseen] = 1.0 / unseen.size
            out = np.zeros(self._n_arms, dtype=np.float64)
            out[idx] = p
            return out

        if samples is None or int(samples) <= 1:
            theta = self.rng.beta(self.alpha[idx], self.beta[idx])
            winners = np.where(theta == theta.max())[0]
            p = np.zeros(A, dtype=np.float64)
            if winners.size == A:
                p[:] = 1.0 / A
            else:
                p[winners] = (1.0 - self.epsilon) / winners.size
                mask = np.ones(A, dtype=bool)
                mask[winners] = False
                others = np.where(mask)[0]
                if others.size > 0:
                    p[others] = self.epsilon / others.size
            probs[idx] = p
            return probs
        else:
            raise NotImplementedError

    def decay(self, factor: float) -> None:
        if not (0.0 < factor <= 1.0):
            raise ValueError("factor must be in (0, 1]")
        self.divs = self.divs * factor
        one_minus = 1.0 - factor

        if self.use_exponential_scaling and self.asymmetric_scaling:
            s = self.s
            log1p_term = np.where(
                s > 0.0,
                s + np.log(one_minus + np.exp(-s)),
                np.log1p(one_minus * np.exp(s)),
            )
            self.s = s + np.log(factor) - log1p_term

            if self.adaptive_scale and np.isfinite(self._obs_max):
                means_log = self._mean()
                mmax = float(np.max(means_log))
                om = self._obs_max
                log1p_obs = (
                    om + np.log(one_minus + np.exp(-om))
                    if om > 0.0
                    else np.log1p(one_minus * np.exp(om))
                )
                obs_new = om + np.log(factor) - log1p_obs
                self._obs_max = max(obs_new, mmax)
        else:
            self.s = self.s * factor
            if self.adaptive_scale and self._have_obs_range():
                means = self._mean()
                self._obs_max = max(
                    self._obs_max * factor + one_minus * np.max(means),
                    np.max(means),
                )
                self._obs_min = min(
                    self._obs_min * factor + one_minus * np.min(means),
                    np.min(means),
                )

        # decay back to prior
        self.alpha = self.a_prior + factor * (self.alpha - self.a_prior)
        self.beta = self.b_prior + factor * (self.beta - self.b_prior)

    def print_summary(self, console: Optional[Console] = None) -> None:
        names = self._arm_names or [str(i) for i in range(self._n_arms)]
        post = self.posterior()
        n = self.n.astype(int)
        mean = self._mean()
        if self.use_exponential_scaling:
            mean_disp = mean
            mean_label = "log mean"
        else:
            mean_disp = mean
            mean_label = "mean"

        a = self.alpha
        b = self.beta
        exploit = a / np.maximum(a + b, 1e-9)
        explore = np.sqrt(
            (a * b) / (np.maximum(a + b, 1e-9) ** 2 * np.maximum(a + b + 1.0, 1e-9))
        )
        score = exploit + explore

        exp_base_str = (
            f"{self.exponential_base:.3f}"
            if self.exponential_base is not None
            else "None"
        )
        header_info = (
            "ThompsonSampler ("
            f"a_prior={self.a_prior:.3f}, b_prior={self.b_prior:.3f}, eps={self.epsilon:.3f}, "
            f"adaptive={self.adaptive_scale}, asym={self.asymmetric_scaling}, "
            f"exp_base={exp_base_str}, shift_base={self._shift_by_baseline}, "
            f"shift_parent={self._shift_by_parent}, "
            f"log_sum={self.use_exponential_scaling})"
        )

        table = Table(
            title=header_info,
            box=rich.box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            width=120,
        )
        table.add_column("arm", style="white", width=24)
        table.add_column("n", justify="right", style="green")
        table.add_column("div", justify="right", style="yellow")
        table.add_column(mean_label, justify="right", style="blue")
        table.add_column("exploit", justify="right", style="magenta")
        table.add_column("explore", justify="right", style="cyan")
        table.add_column("score", justify="right", style="bold white")
        table.add_column("post", justify="right", style="bright_green")

        for i, name in enumerate(names):
            display_name = _format_arm_display_name(name, max_width=25)
            table.add_row(
                display_name,
                f"{n[i]:d}",
                f"{self.divs[i]:.3f}",
                f"{mean_disp[i]:.4f}",
                f"{exploit[i]:.4f}",
                f"{explore[i]:.4f}",
                f"{score[i]:.4f}",
                f"{post[i]:.4f}",
            )

        _console = console or Console()
        _console.print(table)

    def get_state(self) -> Dict[str, Any]:
        """Get the internal state for serialization."""
        return {
            "arm_names": self._state_arm_names(),
            "n_submitted": self.n_submitted.copy(),
            "n_completed": self.n_completed.copy(),
            "s": self.s.copy(),
            "divs": self.divs.copy(),
            "alpha": self.alpha.copy(),
            "beta": self.beta.copy(),
            "baseline": self._baseline,
            "obs_max": self._obs_max,
            "obs_min": self._obs_min,
        }

    def set_state(self, state: Dict[str, Any]) -> None:
        """Restore the internal state from serialization."""
        self.n_submitted = self._align_state_array(
            state, "n_submitted", self.n_submitted
        )
        self.n_completed = self._align_state_array(
            state, "n_completed", self.n_completed
        )
        self.s = self._align_state_array(state, "s", self.s)
        self.divs = self._align_state_array(state, "divs", self.divs)
        self.alpha = self._align_state_array(state, "alpha", self.alpha)
        self.beta = self._align_state_array(state, "beta", self.beta)
        self._baseline = state["baseline"]
        self._restore_observation_range(state)
