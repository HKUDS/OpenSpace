"""Authoring contract for skill-creator style evolution.

The contract turns the textual discipline from skill-creator into structured
runtime data: every drafted skill must carry intent, trigger boundaries, and an
eval plan before it can be behavior-gated and committed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


_ACTIONS = {"FIX", "DERIVED", "CAPTURED"}
_JUDGE_POLICIES = {"deterministic", "llm", "gdpval", "hybrid", "manual"}


@dataclass(frozen=True, slots=True)
class AuthoringIntentSpec:
    capability: str
    trigger_contexts: list[str] = field(default_factory=list)
    non_trigger_contexts: list[str] = field(default_factory=list)
    expected_artifacts: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    tool_dependencies: list[str] = field(default_factory=list)
    resource_plan: dict[str, Any] = field(default_factory=dict)
    parent_difference: str = ""
    observed_pattern: str = ""
    generalization_boundary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "AuthoringIntentSpec":
        mapping = data if isinstance(data, Mapping) else {}
        return cls(
            capability=str(mapping.get("capability") or "").strip(),
            trigger_contexts=_str_list(
                mapping.get("trigger_contexts")
                or mapping.get("triggers")
                or mapping.get("when_to_use")
            ),
            non_trigger_contexts=_str_list(
                mapping.get("non_trigger_contexts")
                or mapping.get("non_triggers")
                or mapping.get("when_not_to_use")
            ),
            expected_artifacts=_str_list(mapping.get("expected_artifacts")),
            success_criteria=_str_list(mapping.get("success_criteria")),
            tool_dependencies=_str_list(mapping.get("tool_dependencies")),
            resource_plan=_dict_or_empty(mapping.get("resource_plan")),
            parent_difference=str(mapping.get("parent_difference") or "").strip(),
            observed_pattern=str(mapping.get("observed_pattern") or "").strip(),
            generalization_boundary=str(
                mapping.get("generalization_boundary") or ""
            ).strip(),
        )

    def validation_failures(self, action_type: str) -> list[str]:
        action = _action(action_type)
        failures: list[str] = []
        if not self.capability:
            failures.append("missing_intent_capability")
        if not self.trigger_contexts:
            failures.append("missing_intent_trigger_contexts")
        if not self.non_trigger_contexts:
            failures.append("missing_intent_non_trigger_contexts")
        if not self.success_criteria:
            failures.append("missing_intent_success_criteria")
        if action == "DERIVED" and not self.parent_difference:
            failures.append("missing_intent_parent_difference")
        if action == "CAPTURED":
            if not self.observed_pattern:
                failures.append("missing_intent_observed_pattern")
            if not self.generalization_boundary:
                failures.append("missing_intent_generalization_boundary")
        return failures


@dataclass(frozen=True, slots=True)
class SkillReplayTask:
    prompt: str
    task_id: str = ""
    judge_policy: str = "hybrid"
    source: str = "generated"
    expected_outcome: str = ""
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | str) -> "SkillReplayTask":
        if isinstance(data, str):
            return cls(prompt=data.strip())
        mapping = data if isinstance(data, Mapping) else {}
        return cls(
            prompt=str(mapping.get("prompt") or "").strip(),
            task_id=str(mapping.get("task_id") or "").strip(),
            judge_policy=_judge_policy(mapping.get("judge_policy") or "hybrid"),
            source=str(mapping.get("source") or "generated").strip() or "generated",
            expected_outcome=str(mapping.get("expected_outcome") or "").strip(),
            artifacts=_str_list(mapping.get("artifacts")),
        )

    def validation_failures(self, index: int) -> list[str]:
        return [f"replay_task_{index}_missing_prompt"] if not self.prompt else []


@dataclass(frozen=True, slots=True)
class SkillAssertion:
    assertion_type: str
    target: str
    expected: Any = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | str) -> "SkillAssertion":
        if isinstance(data, str):
            return cls(assertion_type="manual", target=data.strip(), description=data.strip())
        mapping = data if isinstance(data, Mapping) else {}
        return cls(
            assertion_type=str(mapping.get("type") or mapping.get("assertion_type") or "").strip(),
            target=str(mapping.get("target") or "").strip(),
            expected=mapping.get("expected", True),
            description=str(mapping.get("description") or "").strip(),
        )

    def validation_failures(self, index: int) -> list[str]:
        failures: list[str] = []
        if not self.assertion_type:
            failures.append(f"assertion_{index}_missing_type")
        if not self.target and not self.description:
            failures.append(f"assertion_{index}_missing_target")
        return failures


@dataclass(frozen=True, slots=True)
class SkillEvalPlan:
    positive_trigger_queries: list[str] = field(default_factory=list)
    negative_trigger_queries: list[str] = field(default_factory=list)
    replay_tasks: list[SkillReplayTask] = field(default_factory=list)
    deterministic_assertions: list[SkillAssertion] = field(default_factory=list)
    judge_policy: str = "hybrid"
    success_criteria: list[str] = field(default_factory=list)
    baseline: str = "active"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "positive_trigger_queries": list(self.positive_trigger_queries),
            "negative_trigger_queries": list(self.negative_trigger_queries),
            "replay_tasks": [task.to_dict() for task in self.replay_tasks],
            "deterministic_assertions": [
                assertion.to_dict() for assertion in self.deterministic_assertions
            ],
            "judge_policy": self.judge_policy,
            "success_criteria": list(self.success_criteria),
            "baseline": self.baseline,
            "notes": self.notes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SkillEvalPlan":
        mapping = data if isinstance(data, Mapping) else {}
        replay_input = mapping.get("replay_tasks") or mapping.get("test_prompts") or []
        assertion_input = (
            mapping.get("deterministic_assertions")
            or mapping.get("assertions")
            or []
        )
        return cls(
            positive_trigger_queries=_str_list(
                mapping.get("positive_trigger_queries")
                or mapping.get("should_trigger")
            ),
            negative_trigger_queries=_str_list(
                mapping.get("negative_trigger_queries")
                or mapping.get("should_not_trigger")
            ),
            replay_tasks=[
                SkillReplayTask.from_mapping(item)
                for item in _sequence(replay_input)
            ],
            deterministic_assertions=[
                SkillAssertion.from_mapping(item)
                for item in _sequence(assertion_input)
            ],
            judge_policy=_judge_policy(mapping.get("judge_policy") or "hybrid"),
            success_criteria=_str_list(mapping.get("success_criteria")),
            baseline=str(mapping.get("baseline") or "active").strip() or "active",
            notes=str(mapping.get("notes") or "").strip(),
        )

    def validation_failures(self, action_type: str) -> list[str]:
        action = _action(action_type)
        failures: list[str] = []
        if not self.positive_trigger_queries:
            failures.append("missing_eval_positive_trigger_queries")
        if not self.negative_trigger_queries:
            failures.append("missing_eval_negative_trigger_queries")
        if not self.success_criteria:
            failures.append("missing_eval_success_criteria")
        if not self.replay_tasks and action in {"FIX", "DERIVED"}:
            failures.append("missing_eval_replay_tasks")
        for index, task in enumerate(self.replay_tasks):
            failures.extend(task.validation_failures(index))
        for index, assertion in enumerate(self.deterministic_assertions):
            failures.extend(assertion.validation_failures(index))
        if self.judge_policy not in _JUDGE_POLICIES:
            failures.append(f"unsupported_eval_judge_policy:{self.judge_policy}")
        return failures


@dataclass(frozen=True, slots=True)
class SkillAuthoringContract:
    intent: AuthoringIntentSpec
    eval_plan: SkillEvalPlan

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_spec": self.intent.to_dict(),
            "eval_plan": self.eval_plan.to_dict(),
        }

    @classmethod
    def from_mappings(
        cls,
        intent_spec: Mapping[str, Any] | None,
        eval_plan: Mapping[str, Any] | None,
    ) -> "SkillAuthoringContract":
        return cls(
            intent=AuthoringIntentSpec.from_mapping(intent_spec),
            eval_plan=SkillEvalPlan.from_mapping(eval_plan),
        )

    def validation_failures(self, action_type: str) -> list[str]:
        return [
            *self.intent.validation_failures(action_type),
            *self.eval_plan.validation_failures(action_type),
            *self._cross_field_failures(),
        ]

    def _cross_field_failures(self) -> list[str]:
        positive = {_normalize_query(item) for item in self.eval_plan.positive_trigger_queries}
        negative = {_normalize_query(item) for item in self.eval_plan.negative_trigger_queries}
        overlap = sorted(item for item in positive.intersection(negative) if item)
        return [f"eval_trigger_query_in_both_sets:{item[:80]}" for item in overlap]


def contract_from_staged(staged: Any) -> SkillAuthoringContract:
    return SkillAuthoringContract.from_mappings(
        _mapping_or_none(_attr(staged, "intent_spec")),
        _mapping_or_none(_attr(staged, "eval_plan")),
    )


def contract_validation_failures(staged: Any, action_type: str) -> list[str]:
    return contract_from_staged(staged).validation_failures(action_type)


def _action(value: str) -> str:
    action = str(value or "").strip().upper()
    return action if action in _ACTIONS else action


def _judge_policy(value: Any) -> str:
    policy = str(value or "hybrid").strip().lower()
    return policy if policy in _JUDGE_POLICIES else policy


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Mapping):
        return [
            f"{key}: {val}".strip()
            for key, val in value.items()
            if str(key).strip() or str(val).strip()
        ]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                text = str(
                    item.get("query")
                    or item.get("prompt")
                    or item.get("description")
                    or item
                ).strip()
            else:
                text = str(item).strip()
            if text:
                result.append(text)
        return list(dict.fromkeys(result))
    text = str(value).strip()
    return [text] if text else []


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _normalize_query(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _attr(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)
