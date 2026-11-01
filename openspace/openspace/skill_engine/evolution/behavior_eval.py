"""Behavior evaluation gate for staged skill evolution.

This module is deliberately split into three gates:

* Contract eval checks the structured authoring contract.
* Routing eval asks the real skill selector/ranker whether the candidate routes
  on positive queries and stays out of negative near-misses.
* Replay eval is the only gate that can approve a commit. It compares the
  active revision set with the candidate revision set through an injected
  sandbox/docker runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from openspace.skill_engine.skill_utils import (
    SKILL_FILENAME,
    parse_frontmatter,
    strip_frontmatter,
)
from openspace.utils.logging import Logger

from .authoring_contract import SkillAuthoringContract, contract_from_staged

logger = Logger.get_logger(__name__)

_OUTCOMES = {"approve", "reject", "needs_human_review"}
_ARTIFACT_REF_TYPES = {
    "tool_result",
    "file_history",
    "media_ref",
    "recording_ref",
    "content_replacement",
    "runtime_snapshot",
    "agent_event",
    "background_task_result",
}
_JUDGE_REF_TYPES = {
    "tool_result",
    "execution_analysis",
    "quality_signal_ref",
    "metric_window_ref",
    "agent_event",
    "background_task_result",
}
_OPTIONAL_REPLAY_INFRA_FAILURES = {
    "missing_executable_eval_cases",
    "missing_executable_eval_evidence",
    "replay_tasks_require_external_runner",
}


@dataclass(frozen=True, slots=True)
class ContractEvalResult:
    attempted: bool = True
    passed: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ContractEvalResult":
        mapping = data if isinstance(data, Mapping) else {}
        failures = _str_list(mapping.get("failures"))
        return cls(
            attempted=bool(mapping.get("attempted", True)),
            passed=bool(mapping.get("passed", not failures)),
            failures=failures,
            warnings=_str_list(mapping.get("warnings")),
        )


@dataclass(frozen=True, slots=True)
class RoutingEvalResult:
    attempted: bool = False
    passed: bool = True
    selector: str = "none"
    candidate_skill_id: str = ""
    positive_total: int = 0
    positive_passed: int = 0
    negative_total: int = 0
    negative_passed: int = 0
    selected_skill_ids_by_query: dict[str, list[str]] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RoutingEvalResult":
        mapping = data if isinstance(data, Mapping) else {}
        failures = _str_list(mapping.get("failures"))
        return cls(
            attempted=bool(mapping.get("attempted", False)),
            passed=bool(mapping.get("passed", not failures)),
            selector=str(mapping.get("selector") or "none"),
            candidate_skill_id=str(mapping.get("candidate_skill_id") or ""),
            positive_total=int(mapping.get("positive_total") or 0),
            positive_passed=int(mapping.get("positive_passed") or 0),
            negative_total=int(mapping.get("negative_total") or 0),
            negative_passed=int(mapping.get("negative_passed") or 0),
            selected_skill_ids_by_query=_dict_of_str_lists(
                mapping.get("selected_skill_ids_by_query")
            ),
            details=_dict_or_empty(mapping.get("details")),
            failures=failures,
            warnings=_str_list(mapping.get("warnings")),
        )


@dataclass(frozen=True, slots=True)
class ReplayEvalResult:
    attempted: bool = False
    passed: bool = False
    runner: str = "none"
    replay_run_id: str = ""
    sandbox_run_id: str = ""
    judge_result_id: str = ""
    baseline_revision_set: list[str] = field(default_factory=list)
    candidate_revision_set: list[str] = field(default_factory=list)
    baseline_score: float | None = None
    candidate_score: float | None = None
    baseline_cost: float | None = None
    candidate_cost: float | None = None
    artifact_refs: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ReplayEvalResult":
        mapping = data if isinstance(data, Mapping) else {}
        failures = _str_list(mapping.get("failures"))
        passed = _bool_or_none(mapping.get("passed"))
        attempted = _bool_or_none(mapping.get("attempted"))
        return cls(
            attempted=attempted is True,
            passed=passed is True and not failures,
            runner=str(mapping.get("runner") or "none"),
            replay_run_id=str(mapping.get("replay_run_id") or ""),
            sandbox_run_id=str(mapping.get("sandbox_run_id") or ""),
            judge_result_id=str(mapping.get("judge_result_id") or ""),
            baseline_revision_set=_str_list(mapping.get("baseline_revision_set")),
            candidate_revision_set=_str_list(mapping.get("candidate_revision_set")),
            baseline_score=_float_or_none(mapping.get("baseline_score")),
            candidate_score=_float_or_none(mapping.get("candidate_score")),
            baseline_cost=_float_or_none(mapping.get("baseline_cost")),
            candidate_cost=_float_or_none(mapping.get("candidate_cost")),
            artifact_refs=_str_list(mapping.get("artifact_refs")),
            details=_normalize_replay_details(mapping),
            failures=failures,
            warnings=_str_list(mapping.get("warnings")),
        )


@dataclass(frozen=True, slots=True)
class SkillBehaviorEvalResult:
    eval_id: str
    authoring_id: str
    validation_id: str
    decision_id: str
    packet_id: str
    action_type: str
    outcome: str
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    contract_eval: ContractEvalResult = field(default_factory=ContractEvalResult)
    routing_eval: RoutingEvalResult = field(default_factory=RoutingEvalResult)
    replay_eval: ReplayEvalResult = field(default_factory=ReplayEvalResult)
    contract_snapshot: dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""
    checked_by: str = "behavior_eval"

    @property
    def passed(self) -> bool:
        return self.outcome == "approve"

    @property
    def ref_id(self) -> str:
        return f"behavior_eval:{self.eval_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["contract_eval"] = self.contract_eval.to_dict()
        data["routing_eval"] = self.routing_eval.to_dict()
        data["replay_eval"] = self.replay_eval.to_dict()
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SkillBehaviorEvalResult":
        outcome = str(data.get("outcome") or "reject").strip().lower()
        if outcome not in _OUTCOMES:
            outcome = "reject"
        routing_data = data.get("routing_eval") or data.get("trigger_eval")
        return cls(
            eval_id=str(data.get("eval_id") or ""),
            authoring_id=str(data.get("authoring_id") or ""),
            validation_id=str(data.get("validation_id") or ""),
            decision_id=str(data.get("decision_id") or ""),
            packet_id=str(data.get("packet_id") or ""),
            action_type=str(data.get("action_type") or ""),
            outcome=outcome,
            failures=_str_list(data.get("failures")),
            warnings=_str_list(data.get("warnings")),
            contract_eval=ContractEvalResult.from_mapping(data.get("contract_eval")),
            routing_eval=RoutingEvalResult.from_mapping(routing_data),
            replay_eval=ReplayEvalResult.from_mapping(data.get("replay_eval")),
            contract_snapshot=_dict_or_empty(data.get("contract_snapshot")),
            checked_at=str(data.get("checked_at") or ""),
            checked_by=str(data.get("checked_by") or "behavior_eval"),
        )


class SkillBehaviorEvaluator:
    """Commit gate for skill evolution.

    With the default strict replay setting, a commit is approved only when:
    * the authoring contract is valid;
    * routing does not reject the candidate; and
    * replay eval is attempted and passes.

    Harnesses that cannot provide an external replay runner may set
    ``require_replay_runner=False``. In that mode replay-runner availability
    failures are downgraded to warnings after contract and routing eval pass.
    """

    def __init__(
        self,
        *,
        evidence_store: Any | None = None,
        registry: Any | None = None,
        skill_store: Any | None = None,
        llm_client: Any | None = None,
        routing_selector: Any | None = None,
        replay_runner: Any | None = None,
        checked_by: str = "behavior_eval",
        enable_routing_eval: bool = True,
        require_routing_eval: bool = False,
        require_replay_runner: bool = True,
        routing_top_k: int = 2,
    ) -> None:
        self.evidence_store = evidence_store
        self.registry = registry
        self.skill_store = skill_store
        self.llm_client = llm_client
        self.routing_selector = routing_selector
        self.replay_runner = replay_runner
        self.checked_by = checked_by
        self.enable_routing_eval = bool(enable_routing_eval)
        self.require_routing_eval = bool(require_routing_eval)
        self.require_replay_runner = bool(require_replay_runner)
        self.routing_top_k = max(1, int(routing_top_k))

    async def evaluate(
        self,
        authoring: Any,
        validation: Any,
        decision: Any,
        admission: Any,
        action_packet: Any,
    ) -> SkillBehaviorEvalResult:
        eval_id = f"beval_{uuid.uuid4().hex}"
        checked_at = _utc_now()
        try:
            result = await self._evaluate(
                eval_id=eval_id,
                checked_at=checked_at,
                authoring=authoring,
                validation=validation,
                decision=decision,
                admission=admission,
                action_packet=action_packet,
            )
        except Exception as exc:
            logger.debug("Behavior eval failed internally", exc_info=True)
            result = SkillBehaviorEvalResult(
                eval_id=eval_id,
                authoring_id=str(_attr(authoring, "authoring_id") or ""),
                validation_id=str(_attr(validation, "validation_id") or ""),
                decision_id=str(_attr(decision, "decision_id") or ""),
                packet_id=str(_attr(action_packet, "packet_id") or ""),
                action_type=_action_type(decision, _attr(authoring, "staged_edit")),
                outcome="reject",
                failures=["behavior_eval_internal_error"],
                warnings=[str(exc)[:500]],
                checked_at=checked_at,
                checked_by=self.checked_by,
            )
        self._persist(result)
        return result

    async def _evaluate(
        self,
        *,
        eval_id: str,
        checked_at: str,
        authoring: Any,
        validation: Any,
        decision: Any,
        admission: Any,
        action_packet: Any,
    ) -> SkillBehaviorEvalResult:
        staged = _attr(authoring, "staged_edit")
        action = _action_type(decision, staged)
        contract = contract_from_staged(staged)
        contract_eval = self._evaluate_contract(contract, action)
        candidate_skill_id = _candidate_skill_id(staged, authoring, decision)

        failures: list[str] = []
        warnings: list[str] = []
        failures.extend(contract_eval.failures)
        warnings.extend(contract_eval.warnings)

        routing_eval = RoutingEvalResult(
            attempted=False,
            selector="not_run",
            candidate_skill_id=candidate_skill_id,
        )
        replay_eval = ReplayEvalResult(
            attempted=False,
            runner="not_run",
            baseline_revision_set=_baseline_revision_set(
                self.skill_store,
                self.registry,
            ),
            candidate_revision_set=[],
        )

        should_run_routing_eval = self.enable_routing_eval or self.require_routing_eval
        if not failures and should_run_routing_eval:
            routing_eval = await self._evaluate_routing(
                contract=contract,
                authoring=authoring,
                validation=validation,
                decision=decision,
                admission=admission,
                action_packet=action_packet,
                candidate_skill_id=candidate_skill_id,
            )
            routing_failures = list(routing_eval.failures)
            if self.require_routing_eval:
                failures.extend(routing_failures)
            else:
                warnings.extend(
                    f"optional_routing_eval_failed:{failure}"
                    for failure in routing_failures
                )
            warnings.extend(routing_eval.warnings)
            if self.require_routing_eval and not routing_eval.attempted:
                failures.append("routing_eval_not_attempted")
        elif not failures:
            routing_eval = RoutingEvalResult(
                attempted=False,
                selector="disabled",
                candidate_skill_id=candidate_skill_id,
                warnings=["routing_eval_disabled"],
            )
            warnings.extend(routing_eval.warnings)

        if not failures:
            replay_eval = await self._evaluate_replay(
                contract=contract,
                authoring=authoring,
                validation=validation,
                decision=decision,
                admission=admission,
                action_packet=action_packet,
                candidate_skill_id=candidate_skill_id,
            )
            replay_failures = list(replay_eval.failures)
            if not self.require_replay_runner:
                replay_failures, optional_replay_warnings = (
                    _filter_optional_replay_failures(replay_failures)
                )
                warnings.extend(optional_replay_warnings)
            failures.extend(replay_failures)
            warnings.extend(replay_eval.warnings)
            if not replay_eval.attempted:
                if self.require_replay_runner:
                    failures.append("missing_required_replay_runner")
                else:
                    warnings.append("optional_replay_eval_not_attempted")
            elif (
                self.require_replay_runner
                and not replay_eval.passed
                and not replay_failures
            ):
                failures.append("replay_eval_failed")
            elif (
                not self.require_replay_runner
                and not replay_eval.passed
                and not replay_failures
            ):
                warnings.append("optional_replay_eval_not_passed")

        outcome = _behavior_eval_outcome(failures)
        return SkillBehaviorEvalResult(
            eval_id=eval_id,
            authoring_id=str(_attr(authoring, "authoring_id") or ""),
            validation_id=str(_attr(validation, "validation_id") or ""),
            decision_id=str(_attr(decision, "decision_id") or ""),
            packet_id=str(_attr(action_packet, "packet_id") or ""),
            action_type=action,
            outcome=outcome,
            failures=_dedupe(failures),
            warnings=_dedupe(warnings),
            contract_eval=contract_eval,
            routing_eval=routing_eval,
            replay_eval=replay_eval,
            contract_snapshot=contract.to_dict(),
            checked_at=checked_at,
            checked_by=self.checked_by,
        )

    def _evaluate_contract(
        self,
        contract: SkillAuthoringContract,
        action_type: str,
    ) -> ContractEvalResult:
        failures = _dedupe(contract.validation_failures(action_type))
        return ContractEvalResult(
            attempted=True,
            passed=not failures,
            failures=failures,
        )

    async def _evaluate_routing(
        self,
        *,
        contract: SkillAuthoringContract,
        authoring: Any,
        validation: Any,
        decision: Any,
        admission: Any,
        action_packet: Any,
        candidate_skill_id: str,
    ) -> RoutingEvalResult:
        del validation
        positive = list(contract.eval_plan.positive_trigger_queries)
        negative = list(contract.eval_plan.negative_trigger_queries)
        if not positive and not negative:
            return RoutingEvalResult(
                attempted=False,
                selector="none",
                candidate_skill_id=candidate_skill_id,
                warnings=["routing_eval_no_queries"],
            )

        selector_name = _routing_selector_name(
            self.routing_selector,
            self.registry,
            self.llm_client,
        )
        if selector_name == "none":
            return RoutingEvalResult(
                attempted=False,
                selector="none",
                candidate_skill_id=candidate_skill_id,
                warnings=["routing_selector_unavailable"],
            )

        selected_by_query: dict[str, list[str]] = {}
        failures: list[str] = []
        warnings: list[str] = []
        positive_passed = 0
        negative_passed = 0

        for index, query in enumerate(positive):
            selected = await self._select_for_query(
                query=query,
                candidate_skill_id=candidate_skill_id,
                authoring=authoring,
                decision=decision,
                admission=admission,
                action_packet=action_packet,
            )
            selected_by_query[query] = selected
            if candidate_skill_id in selected:
                positive_passed += 1
            else:
                failures.append(f"routing_positive_missed_candidate:{index}")

        for index, query in enumerate(negative):
            selected = await self._select_for_query(
                query=query,
                candidate_skill_id=candidate_skill_id,
                authoring=authoring,
                decision=decision,
                admission=admission,
                action_packet=action_packet,
            )
            selected_by_query[query] = selected
            if candidate_skill_id not in selected:
                negative_passed += 1
            else:
                failures.append(f"routing_negative_selected_candidate:{index}")

        failures = _dedupe(failures)
        return RoutingEvalResult(
            attempted=True,
            passed=not failures,
            selector=selector_name,
            candidate_skill_id=candidate_skill_id,
            positive_total=len(positive),
            positive_passed=positive_passed,
            negative_total=len(negative),
            negative_passed=negative_passed,
            selected_skill_ids_by_query=selected_by_query,
            details={
                "routing_top_k": self.routing_top_k,
            },
            failures=failures,
            warnings=_dedupe(warnings),
        )

    async def _select_for_query(
        self,
        *,
        query: str,
        candidate_skill_id: str,
        authoring: Any,
        decision: Any,
        admission: Any,
        action_packet: Any,
    ) -> list[str]:
        selector = self.routing_selector
        if selector is not None:
            raw = await _call_routing_selector(
                selector,
                query=query,
                candidate_skill_id=candidate_skill_id,
                authoring=authoring,
                decision=decision,
                admission=admission,
                action_packet=action_packet,
                top_k=self.routing_top_k,
            )
            return _normalize_selected_ids(raw)

        registry = self.registry
        if registry is None:
            return []

        staged = _attr(authoring, "staged_edit")
        candidate_meta = _candidate_skill_meta(staged, candidate_skill_id)
        if self.llm_client is not None and hasattr(registry, "select_skills_with_llm"):
            metas = _candidate_skill_universe(registry, staged, candidate_meta)
            selected, _record = await registry.select_skills_with_llm(
                query,
                self.llm_client,
                max_skills=self.routing_top_k,
                candidate_skills=metas,
            )
            return _normalize_selected_ids(selected)

        candidates = _ranker_candidates(registry, staged, candidate_skill_id)
        if not candidates:
            return []
        ranker = getattr(registry, "ranker", None)
        if ranker is None:
            from openspace.skill_engine.skill_ranker import SkillRanker

            ranker = SkillRanker(enable_cache=False)
        ranked = ranker.hybrid_rank(query, candidates, top_k=self.routing_top_k)
        return _normalize_selected_ids(ranked)

    async def _evaluate_replay(
        self,
        *,
        contract: SkillAuthoringContract,
        authoring: Any,
        validation: Any,
        decision: Any,
        admission: Any,
        action_packet: Any,
        candidate_skill_id: str,
    ) -> ReplayEvalResult:
        baseline_revision_set = _baseline_revision_set(self.skill_store, self.registry)
        candidate_revision_set = _candidate_revision_set(
            baseline_revision_set,
            candidate_skill_id,
            _parent_skill_ids(_attr(authoring, "staged_edit"), decision),
            _action_type(decision, _attr(authoring, "staged_edit")),
        )
        if self.replay_runner is None:
            return ReplayEvalResult(
                attempted=False,
                runner="none",
                baseline_revision_set=baseline_revision_set,
                candidate_revision_set=candidate_revision_set,
            )
        runner = self.replay_runner
        try:
            method = getattr(runner, "run", None)
            kwargs = {
                "contract": contract,
                "authoring": authoring,
                "validation": validation,
                "decision": decision,
                "admission": admission,
                "action_packet": action_packet,
                "candidate_skill_id": candidate_skill_id,
                "baseline_revision_set": baseline_revision_set,
                "candidate_revision_set": candidate_revision_set,
            }
            if callable(method):
                raw = method(**kwargs)
            elif callable(runner):
                raw = runner(**kwargs)
            else:
                return ReplayEvalResult(
                    attempted=False,
                    runner=type(runner).__name__,
                    baseline_revision_set=baseline_revision_set,
                    candidate_revision_set=candidate_revision_set,
                    failures=["invalid_replay_runner"],
                )
            if hasattr(raw, "__await__"):
                raw = await raw
        except Exception as exc:
            return ReplayEvalResult(
                attempted=True,
                passed=False,
                runner=type(runner).__name__,
                baseline_revision_set=baseline_revision_set,
                candidate_revision_set=candidate_revision_set,
                failures=[f"replay_runner_error:{str(exc)[:300]}"],
            )
        return _normalize_replay_result(
            raw,
            runner_name=type(runner).__name__,
            baseline_revision_set=baseline_revision_set,
            candidate_revision_set=candidate_revision_set,
            evidence_store=self.evidence_store,
        )

    def _persist(self, result: SkillBehaviorEvalResult) -> None:
        persist = getattr(self.evidence_store, "persist_behavior_eval", None)
        if callable(persist):
            persist(result)


class SkillEvalAdapter:
    """Default eval adapter for structured skill eval plans.

    The adapter can execute deterministic static checks, but it deliberately
    refuses to approve a skill without executable replay evidence. External
    task replay/judge runners should be wired through ``SubprocessSkillReplayRunner``
    or a custom replay runner.
    """

    runner_name = "default_eval_adapter"

    def run(
        self,
        *,
        contract: SkillAuthoringContract,
        authoring: Any,
        validation: Any,
        decision: Any,
        admission: Any,
        action_packet: Any,
        candidate_skill_id: str,
        baseline_revision_set: list[str],
        candidate_revision_set: list[str],
    ) -> dict[str, Any]:
        context = build_replay_context(
            replay_run_id=f"replay_{uuid.uuid4().hex}",
            contract=contract,
            authoring=authoring,
            validation=validation,
            decision=decision,
            admission=admission,
            action_packet=action_packet,
            candidate_skill_id=candidate_skill_id,
            baseline_revision_set=baseline_revision_set,
            candidate_revision_set=candidate_revision_set,
        )
        return evaluate_replay_context(context)


class SubprocessSkillReplayRunner:
    """Run paired replay through an external process or docker container.

    The external command receives a JSON context path in
    ``OPENSPACE_REPLAY_CONTEXT`` and must print a JSON replay result to stdout.
    The command is responsible for running baseline and candidate executions in
    whatever sandbox it owns.
    """

    def __init__(
        self,
        command: str | Sequence[str],
        *,
        docker_image: str | None = None,
        timeout_s: float = 600.0,
        cwd: str | Path | None = None,
        sandbox_manager: Any | None = None,
        use_sandbox: bool = True,
        workspace_writable: bool = False,
        pythonpath_roots: Sequence[str | Path] | None = None,
    ) -> None:
        self.command = list(command) if not isinstance(command, str) else shlex.split(command)
        self.docker_image = str(docker_image or "").strip()
        self.timeout_s = float(timeout_s)
        self.cwd = Path(cwd).expanduser().resolve() if cwd else None
        self.sandbox_manager = sandbox_manager
        self.use_sandbox = bool(use_sandbox)
        self.workspace_writable = bool(workspace_writable)
        self.pythonpath_roots = [
            Path(root).expanduser().resolve()
            for root in (pythonpath_roots or [])
            if str(root or "").strip()
        ]

    async def run(
        self,
        *,
        contract: SkillAuthoringContract,
        authoring: Any,
        validation: Any,
        decision: Any,
        admission: Any,
        action_packet: Any,
        candidate_skill_id: str,
        baseline_revision_set: list[str],
        candidate_revision_set: list[str],
    ) -> dict[str, Any]:
        replay_run_id = f"replay_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="openspace_replay_") as tmp:
            tmp_path = Path(tmp)
            artifact_dir = tmp_path / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            context_path = tmp_path / "replay_context.json"
            docker_context = Path("/replay/replay_context.json")
            docker_artifact_dir = Path("/replay/artifacts")
            mounted_context = docker_context if self.docker_image else context_path
            mounted_artifact_dir = (
                docker_artifact_dir if self.docker_image else artifact_dir
            )
            workspace_dir = str(self.cwd) if self.cwd is not None else ""
            mounted_workspace_dir = (
                "/workspace" if self.docker_image and self.cwd is not None else workspace_dir
            )
            context = build_replay_context(
                replay_run_id=replay_run_id,
                contract=contract,
                authoring=authoring,
                validation=validation,
                decision=decision,
                admission=admission,
                action_packet=action_packet,
                candidate_skill_id=candidate_skill_id,
                baseline_revision_set=baseline_revision_set,
                candidate_revision_set=candidate_revision_set,
            )
            context["artifact_dir"] = str(mounted_artifact_dir)
            context["workspace_dir"] = mounted_workspace_dir
            context_path.write_text(
                json.dumps(context, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            cmd = self._command(context_path, tmp_path)
            env = self._env(
                context_path=mounted_context,
                artifact_dir=mounted_artifact_dir,
                workspace_dir=mounted_workspace_dir,
            )
            runner_mode = "docker" if self.docker_image else "subprocess"
            wrapped = None
            run_cmd = cmd
            run_env = env
            run_cwd = str(self.cwd) if self.cwd else None
            sandbox_metadata: dict[str, Any] = {"applied": False}
            if not self.docker_image and self._can_use_process_sandbox():
                try:
                    wrapped = await self.sandbox_manager.wrap_command(
                        cmd,
                        cwd=str(tmp_path),
                        env=env,
                        policy=self._sandbox_policy(tmp_path),
                    )
                    run_cmd = wrapped.argv
                    run_env = wrapped.env
                    run_cwd = wrapped.cwd
                    runner_mode = "process_sandbox"
                    sandbox_metadata = wrapped.to_metadata()
                except Exception as exc:
                    if self._sandbox_required():
                        return {
                            "passed": False,
                            "replay_run_id": replay_run_id,
                            "runner": type(self).__name__,
                            "sandbox_run_id": replay_run_id,
                            "baseline_revision_set": list(baseline_revision_set),
                            "candidate_revision_set": list(candidate_revision_set),
                            "failures": [f"process_sandbox_unavailable:{str(exc)[:300]}"],
                            "details": {
                                "runner_mode": "process_sandbox",
                                "sandbox": {"applied": False},
                            },
                        }
                    logger.debug(
                        "Evolution replay sandbox unavailable; falling back to subprocess",
                        exc_info=True,
                    )
                    runner_mode = "subprocess"
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    run_cmd,
                    cwd=run_cwd,
                    env=run_env,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_s,
                    check=False,
                )
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                payload = {
                    "passed": False,
                    "replay_run_id": replay_run_id,
                    "runner": type(self).__name__,
                    "sandbox_run_id": replay_run_id,
                    "baseline_revision_set": list(baseline_revision_set),
                    "candidate_revision_set": list(candidate_revision_set),
                    "failures": [f"replay_command_timeout:{self.timeout_s:g}s"],
                    "details": {
                        "runner_mode": runner_mode,
                        "sandbox": sandbox_metadata,
                        "timeout_s": self.timeout_s,
                        "stdout": _decode_timeout_output(exc.stdout)[-4000:],
                        "stderr": _decode_timeout_output(exc.stderr)[-4000:],
                    },
                }
                await self._cleanup_sandbox_command(wrapped)
                return payload
            except Exception as exc:
                await self._cleanup_sandbox_command(wrapped)
                return {
                    "passed": False,
                    "replay_run_id": replay_run_id,
                    "runner": type(self).__name__,
                    "sandbox_run_id": replay_run_id,
                    "baseline_revision_set": list(baseline_revision_set),
                    "candidate_revision_set": list(candidate_revision_set),
                    "failures": [f"replay_command_error:{str(exc)[:300]}"],
                    "details": {
                        "runner_mode": runner_mode,
                        "sandbox": sandbox_metadata,
                    },
                }
            if wrapped is not None:
                stderr = self._annotate_sandbox_stderr(wrapped, stderr)
                await self._cleanup_sandbox_command(wrapped)
            payload = _parse_runner_stdout(completed.stdout)
            payload.setdefault("replay_run_id", replay_run_id)
            payload.setdefault("runner", type(self).__name__)
            payload.setdefault("sandbox_run_id", replay_run_id)
            details = _dict_or_empty(payload.get("details"))
            details.update(
                {
                    "returncode": completed.returncode,
                    "stderr": stderr[-4000:],
                    "stdout": completed.stdout[-4000:],
                    "docker_image": self.docker_image,
                    "runner_mode": runner_mode,
                    "sandbox": sandbox_metadata,
                    "artifact_dir": str(artifact_dir),
                    "workspace_writable": self.workspace_writable,
                }
            )
            payload["details"] = details
            if completed.returncode != 0:
                failures = _str_list(payload.get("failures"))
                failures.append(f"replay_command_failed:{completed.returncode}")
                payload["failures"] = failures
                payload["passed"] = False
            return payload

    async def _cleanup_sandbox_command(self, wrapped: Any | None) -> None:
        if wrapped is None:
            return
        cleanup = getattr(self.sandbox_manager, "cleanup_after_command", None)
        if callable(cleanup):
            try:
                await cleanup(wrapped)
            except Exception:
                logger.debug("Failed to clean up replay sandbox command", exc_info=True)

    def _annotate_sandbox_stderr(self, wrapped: Any, stderr: str) -> str:
        annotator = getattr(
            self.sandbox_manager,
            "annotate_stderr_with_sandbox_failures",
            None,
        )
        if callable(annotator):
            try:
                return annotator(
                    wrapped.command,
                    stderr,
                    command_tag=wrapped.command_tag,
                )
            except Exception:
                logger.debug("Failed to annotate replay sandbox stderr", exc_info=True)
        return stderr

    def _env(
        self,
        *,
        context_path: Path,
        artifact_dir: Path,
        workspace_dir: str,
    ) -> dict[str, str]:
        env = dict(os.environ)
        env["OPENSPACE_REPLAY_CONTEXT"] = str(context_path)
        env["OPENSPACE_REPLAY_ARTIFACT_DIR"] = str(artifact_dir)
        env["OPENSPACE_WORKSPACE_DIR"] = workspace_dir
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        if self.pythonpath_roots:
            existing = env.get("PYTHONPATH", "")
            roots = [str(root) for root in self.pythonpath_roots]
            env["PYTHONPATH"] = os.pathsep.join([*roots, existing] if existing else roots)
        return env

    def _command(self, context_path: Path, tmp_path: Path) -> list[str]:
        if not self.docker_image:
            return list(self.command)
        mounted_context = "/replay/replay_context.json"
        cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{tmp_path}:/replay:rw",
            "-e",
            f"OPENSPACE_REPLAY_CONTEXT={mounted_context}",
            "-e",
            "OPENSPACE_REPLAY_ARTIFACT_DIR=/replay/artifacts",
            "-e",
            "OPENSPACE_WORKSPACE_DIR=/workspace",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        docker_pythonpath_roots = self._docker_pythonpath_roots()
        for host_root, mounted_root in zip(self.pythonpath_roots, docker_pythonpath_roots):
            cmd.extend(["-v", f"{host_root}:{mounted_root}:ro"])
        if docker_pythonpath_roots:
            cmd.extend(
                [
                    "-e",
                    f"PYTHONPATH={os.pathsep.join(docker_pythonpath_roots)}",
                ]
            )
        if self.cwd is not None:
            mode = "rw" if self.workspace_writable else "ro"
            cmd.extend(["-v", f"{self.cwd}:/workspace:{mode}", "-w", "/workspace"])
        cmd.extend([self.docker_image, *self.command])
        return cmd

    def _docker_pythonpath_roots(self) -> list[str]:
        return [
            f"/openspace_pythonpath/{index}"
            for index, _root in enumerate(self.pythonpath_roots)
        ]

    def _can_use_process_sandbox(self) -> bool:
        manager = self.sandbox_manager
        if not self.use_sandbox or manager is None:
            return False
        checker = getattr(manager, "is_sandboxing_enabled", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            logger.debug("Replay sandbox availability check failed", exc_info=True)
            return False

    def _sandbox_required(self) -> bool:
        manager = self.sandbox_manager
        if manager is None:
            return False
        checker = getattr(manager, "is_sandbox_required", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    def _sandbox_policy(self, tmp_path: Path) -> Any | None:
        manager = self.sandbox_manager
        if manager is None:
            return None
        try:
            policy = manager.runtime_config().policy
            allow_read = _dedupe(
                [
                    *list(getattr(policy, "allow_read", []) or []),
                    str(tmp_path),
                    *([str(self.cwd)] if self.cwd is not None else []),
                    *(str(root) for root in self.pythonpath_roots),
                ]
            )
            if self.workspace_writable:
                allow_write = _dedupe(
                    [
                        *list(getattr(policy, "allow_write", []) or []),
                        str(tmp_path),
                    ]
                )
            else:
                allow_write = [str(tmp_path)]
            return replace(policy, allow_read=allow_read, allow_write=allow_write)
        except Exception:
            logger.debug("Failed to build replay sandbox policy", exc_info=True)
            return None


def build_replay_context(
    *,
    replay_run_id: str,
    contract: SkillAuthoringContract,
    authoring: Any,
    validation: Any,
    decision: Any,
    admission: Any,
    action_packet: Any,
    candidate_skill_id: str,
    baseline_revision_set: list[str],
    candidate_revision_set: list[str],
) -> dict[str, Any]:
    return {
        "replay_run_id": replay_run_id,
        "contract": contract.to_dict(),
        "authoring_id": str(_attr(authoring, "authoring_id") or ""),
        "validation_id": str(_attr(validation, "validation_id") or ""),
        "decision_id": str(_attr(decision, "decision_id") or ""),
        "admission_id": str(_attr(admission, "admission_id") or ""),
        "packet_id": str(_attr(action_packet, "packet_id") or ""),
        "candidate_skill_id": candidate_skill_id,
        "baseline_revision_set": list(baseline_revision_set),
        "candidate_revision_set": list(candidate_revision_set),
        "staged_snapshot": _content_snapshot(_attr(authoring, "staged_edit")),
    }


def evaluate_replay_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate a replay context with the built-in micro-eval adapter."""

    replay_run_id = str(context.get("replay_run_id") or f"replay_{uuid.uuid4().hex}")
    contract = _dict_or_empty(context.get("contract"))
    eval_plan = _dict_or_empty(contract.get("eval_plan"))
    snapshot = _dict_of_strings(context.get("staged_snapshot"))
    assertions = [
        _dict_or_empty(item)
        for item in _sequence(eval_plan.get("deterministic_assertions"))
    ]
    replay_tasks = [
        _dict_or_empty(item)
        for item in _sequence(eval_plan.get("replay_tasks"))
        if _dict_or_empty(item)
    ]

    failures: list[str] = []
    warnings: list[str] = []
    assertion_results: list[dict[str, Any]] = []
    for index, assertion in enumerate(assertions):
        assertion_result = _evaluate_static_assertion(assertion, snapshot)
        assertion_result["index"] = index
        assertion_results.append(assertion_result)
        if assertion_result.get("requires_executable_evidence"):
            warnings.append(
                f"deterministic_assertion_requires_executable_evidence:{index}"
            )
        elif assertion_result.get("requires_human_review"):
            warnings.append(f"deterministic_assertion_requires_human_review:{index}")
        elif assertion_result.get("passed") is not True:
            reason = str(assertion_result.get("failure") or "assertion_failed")
            failures.append(f"deterministic_assertion_failed:{index}:{reason}")

    if replay_tasks:
        failures.append("replay_tasks_require_external_runner")
    else:
        failures.append("missing_executable_eval_cases")

    details = {
        "eval_adapter": "default_static_micro_eval",
        "assertions_total": len(assertions),
        "assertion_results": assertion_results,
        "replay_tasks_total": len(replay_tasks),
        "executable_eval_attempted": False,
        "approval_policy": "static_only_cannot_approve",
    }
    if not assertions:
        warnings.append("eval_plan_has_no_deterministic_assertions")

    return {
        "passed": False,
        "runner": "default_eval_adapter",
        "replay_run_id": replay_run_id,
        "sandbox_run_id": replay_run_id,
        "baseline_revision_set": _str_list(context.get("baseline_revision_set")),
        "candidate_revision_set": _str_list(context.get("candidate_revision_set")),
        "failures": _dedupe(failures),
        "warnings": _dedupe(warnings),
        "details": details,
    }


def behavior_eval_feedback(result: SkillBehaviorEvalResult) -> str:
    lines = [
        f"Behavior evaluation outcome: {result.outcome}",
        f"Behavior eval id: {result.eval_id}",
    ]
    if result.failures:
        lines.append("Failures:")
        lines.extend(f"- {item}" for item in result.failures)
    if result.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {item}" for item in result.warnings)
    lines.append(
        "Contract eval: "
        f"passed={result.contract_eval.passed}, "
        f"failures={len(result.contract_eval.failures)}"
    )
    lines.append(
        "Routing eval: "
        f"attempted={result.routing_eval.attempted}, "
        f"passed={result.routing_eval.passed}, "
        f"+{result.routing_eval.positive_passed}/{result.routing_eval.positive_total}, "
        f"-{result.routing_eval.negative_passed}/{result.routing_eval.negative_total}"
    )
    lines.append(
        "Replay eval: "
        f"attempted={result.replay_eval.attempted}, "
        f"passed={result.replay_eval.passed}, "
        f"runner={result.replay_eval.runner}"
    )
    if result.replay_eval.baseline_score is not None or result.replay_eval.candidate_score is not None:
        lines.append(
            "Replay score: "
            f"baseline={result.replay_eval.baseline_score}, "
            f"candidate={result.replay_eval.candidate_score}"
        )
    return "\n".join(lines)


def _behavior_eval_outcome(failures: list[str]) -> str:
    if not failures:
        return "approve"
    if all(_needs_human_review_failure(item) for item in failures):
        return "needs_human_review"
    return "reject"


def _needs_human_review_failure(failure: str) -> bool:
    text = str(failure or "")
    return text in {
        "missing_required_replay_runner",
        "replay_eval_not_attempted",
        "missing_executable_eval_cases",
        "missing_executable_eval_evidence",
        "missing_baseline_revision_set",
        "missing_candidate_revision_set",
        "replay_tasks_require_external_runner",
    }


def _filter_optional_replay_failures(
    failures: list[str],
) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    warnings: list[str] = []
    for failure in failures:
        text = str(failure or "")
        if text in _OPTIONAL_REPLAY_INFRA_FAILURES:
            warnings.append(f"optional_replay_eval_skipped:{text}")
            continue
        kept.append(failure)
    return kept, warnings


def _normalize_replay_result(
    raw: Any,
    *,
    runner_name: str,
    baseline_revision_set: list[str],
    candidate_revision_set: list[str],
    evidence_store: Any | None = None,
) -> ReplayEvalResult:
    if isinstance(raw, ReplayEvalResult):
        raw_failures = _replay_revision_set_failures(
            raw.baseline_revision_set,
            raw.candidate_revision_set,
            baseline_revision_set=baseline_revision_set,
            candidate_revision_set=candidate_revision_set,
        )
        raw_failures.extend(
            _replay_evidence_ref_failures(raw.to_dict(), evidence_store)
        )
        raw_failures.extend(_replay_task_result_failures(raw.to_dict()))
        if raw.passed and not _replay_result_has_verified_executable_evidence(
            raw.to_dict(),
            evidence_store,
        ):
            raw_failures.append("missing_executable_eval_evidence")
        raw_failures = _dedupe([*raw.failures, *raw_failures])
        if raw_failures:
            return ReplayEvalResult(
                attempted=raw.attempted,
                passed=False,
                runner=raw.runner,
                replay_run_id=raw.replay_run_id,
                sandbox_run_id=raw.sandbox_run_id,
                judge_result_id=raw.judge_result_id,
                baseline_revision_set=list(raw.baseline_revision_set),
                candidate_revision_set=list(raw.candidate_revision_set),
                baseline_score=raw.baseline_score,
                candidate_score=raw.candidate_score,
                baseline_cost=raw.baseline_cost,
                candidate_cost=raw.candidate_cost,
                artifact_refs=list(raw.artifact_refs),
                details=dict(raw.details),
                failures=raw_failures,
                warnings=list(raw.warnings),
            )
        return raw
    if isinstance(raw, bool):
        failures = [] if not raw else ["missing_executable_eval_evidence"]
        if not raw:
            failures.append("replay_runner_returned_false")
        return ReplayEvalResult(
            attempted=True,
            runner=runner_name,
            replay_run_id=f"replay_{uuid.uuid4().hex}",
            passed=False,
            baseline_revision_set=list(baseline_revision_set),
            candidate_revision_set=list(candidate_revision_set),
            failures=_dedupe(failures),
        )
    if not isinstance(raw, Mapping):
        return ReplayEvalResult(
            attempted=True,
            runner=runner_name,
            replay_run_id=f"replay_{uuid.uuid4().hex}",
            passed=False,
            baseline_revision_set=list(baseline_revision_set),
            candidate_revision_set=list(candidate_revision_set),
            failures=["invalid_replay_result"],
        )
    failures = _str_list(raw.get("failures"))
    warnings = _str_list(raw.get("warnings"))
    has_baseline_revision_set = "baseline_revision_set" in raw
    has_candidate_revision_set = "candidate_revision_set" in raw
    returned_baseline_revision_set = (
        _str_list(raw.get("baseline_revision_set")) if has_baseline_revision_set else []
    )
    returned_candidate_revision_set = (
        _str_list(raw.get("candidate_revision_set")) if has_candidate_revision_set else []
    )
    if not has_baseline_revision_set:
        failures.append("missing_baseline_revision_set")
    if not has_candidate_revision_set:
        failures.append("missing_candidate_revision_set")
    if has_baseline_revision_set and has_candidate_revision_set:
        failures.extend(
            _replay_revision_set_failures(
                returned_baseline_revision_set,
                returned_candidate_revision_set,
                baseline_revision_set=baseline_revision_set,
                candidate_revision_set=candidate_revision_set,
            )
        )
    failures.extend(_replay_evidence_ref_failures(raw, evidence_store))
    failures.extend(_replay_task_result_failures(raw))
    baseline_score = _float_or_none(raw.get("baseline_score"))
    candidate_score = _float_or_none(raw.get("candidate_score"))
    details = _normalize_replay_details(raw)
    passed_raw = raw.get("passed")
    if passed_raw is None:
        passed = False
        failures.append("replay_result_missing_pass_signal")
    else:
        parsed_passed = _bool_or_none(passed_raw)
        if parsed_passed is None:
            passed = False
            failures.append("invalid_replay_pass_signal")
        else:
            passed = parsed_passed
            if not passed and _should_report_replay_runner_failed(raw, failures):
                failures.append("replay_runner_reported_failed")
    if (
        baseline_score is not None
        and candidate_score is not None
        and candidate_score + 1e-9 < baseline_score
    ):
        passed = False
        failures.append("candidate_score_regressed")
    if passed and not _replay_result_has_verified_executable_evidence(
        raw,
        evidence_store,
    ):
        passed = False
        failures.append("missing_executable_eval_evidence")
    failures = _dedupe(failures)
    return ReplayEvalResult(
        attempted=True,
        runner=str(raw.get("runner") or runner_name),
        replay_run_id=str(raw.get("replay_run_id") or f"replay_{uuid.uuid4().hex}"),
        sandbox_run_id=str(raw.get("sandbox_run_id") or ""),
        judge_result_id=str(raw.get("judge_result_id") or ""),
        baseline_revision_set=returned_baseline_revision_set,
        candidate_revision_set=returned_candidate_revision_set,
        passed=passed and not failures,
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        baseline_cost=_float_or_none(raw.get("baseline_cost")),
        candidate_cost=_float_or_none(raw.get("candidate_cost")),
        artifact_refs=_str_list(raw.get("artifact_refs")),
        details=details,
        failures=failures,
        warnings=_dedupe(warnings),
    )


def _replay_revision_set_failures(
    returned_baseline_revision_set: list[str],
    returned_candidate_revision_set: list[str],
    *,
    baseline_revision_set: list[str],
    candidate_revision_set: list[str],
) -> list[str]:
    failures: list[str] = []
    if not _same_revision_set(returned_baseline_revision_set, baseline_revision_set):
        failures.append("baseline_revision_set_mismatch")
    if not _same_revision_set(returned_candidate_revision_set, candidate_revision_set):
        failures.append("candidate_revision_set_mismatch")
    return failures


def _should_report_replay_runner_failed(
    raw: Mapping[str, Any],
    failures: list[str],
) -> bool:
    if not failures:
        return True
    if _runner_executable_replay_attempted(raw):
        return True
    return not all(_needs_human_review_failure(failure) for failure in failures)


def _runner_executable_replay_attempted(raw: Mapping[str, Any]) -> bool:
    details = _dict_or_empty(raw.get("details"))
    if _bool_or_none(raw.get("executable_eval_attempted")) is True:
        return True
    if _bool_or_none(details.get("executable_eval_attempted")) is True:
        return True
    for item in _replay_task_result_mappings(raw):
        if (
            _bool_or_none(item.get("attempted")) is True
            or _bool_or_none(item.get("executable_eval_attempted")) is True
        ):
            return True
    return False


def _same_revision_set(left: list[str], right: list[str]) -> bool:
    return sorted(_str_list(left)) == sorted(_str_list(right))


def _replay_result_has_verified_executable_evidence(
    raw: Any,
    evidence_store: Any | None,
) -> bool:
    if _replay_result_has_executable_evidence(raw):
        return True
    if not isinstance(raw, Mapping):
        return False
    getter = getattr(evidence_store, "get_ref", None)
    if not callable(getter):
        return False
    for ref_id in _top_level_replay_artifact_refs(raw):
        if not _replay_ref_failures(
            getter,
            ref_id,
            allowed_types=_ARTIFACT_REF_TYPES,
            missing_reason="missing_replay_artifact_ref",
            invalid_type_reason="invalid_replay_artifact_ref_type",
        ):
            return True
    for ref_id in _top_level_replay_judge_refs(raw):
        if not _replay_ref_failures(
            getter,
            ref_id,
            allowed_types=_JUDGE_REF_TYPES,
            missing_reason="missing_replay_judge_ref",
            invalid_type_reason="invalid_replay_judge_ref_type",
        ):
            return True
    return False


def _normalize_replay_details(raw: Mapping[str, Any]) -> dict[str, Any]:
    details = _dict_or_empty(raw.get("details"))
    task_results = _replay_task_result_mappings(raw)
    if task_results:
        details["replay_task_results"] = task_results
    return details


def _replay_task_result_failures(raw: Mapping[str, Any]) -> list[str]:
    if _allows_failed_replay_tasks(raw):
        return []
    failures: list[str] = []
    for index, item in enumerate(_replay_task_result_mappings(raw)):
        task_id = str(item.get("task_id") or item.get("id") or index).strip()
        passed = _bool_or_none(item.get("passed"))
        status = str(item.get("status") or "").strip().lower()
        if passed is False:
            failures.append(f"replay_task_failed:{task_id}")
        elif status in {"failed", "error"}:
            failures.append(f"replay_task_{status}:{task_id}")
        for assertion_index, assertion in enumerate(
            _replay_assertion_result_mappings(item)
        ):
            assertion_id = str(
                assertion.get("assertion_id")
                or assertion.get("id")
                or assertion.get("index")
                or assertion_index
            ).strip()
            assertion_passed = _bool_or_none(assertion.get("passed"))
            assertion_status = str(assertion.get("status") or "").strip().lower()
            if assertion_passed is False:
                failures.append(
                    f"replay_task_assertion_failed:{task_id}:{assertion_id}"
                )
            elif assertion_status in {"failed", "error"}:
                failures.append(
                    f"replay_task_assertion_{assertion_status}:{task_id}:{assertion_id}"
                )
    return _dedupe(failures)


def _replay_assertion_result_mappings(task_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (
            _dict_or_empty(result)
            for result in _sequence(task_result.get("assertion_results"))
        )
        if item
    ]


def _allows_failed_replay_tasks(raw: Mapping[str, Any]) -> bool:
    details = _dict_or_empty(raw.get("details"))
    for key in (
        "task_result_aggregation",
        "replay_task_aggregation",
        "task_aggregation_policy",
        "aggregation_policy",
    ):
        value = str(raw.get(key) or details.get(key) or "").strip().lower()
        if value in {
            "allow_failed_tasks",
            "allow_partial",
            "partial_ok",
            "top_level_authoritative",
            "custom",
        }:
            return True
    return False


def _replay_evidence_ref_failures(
    raw: Mapping[str, Any],
    evidence_store: Any | None,
) -> list[str]:
    getter = getattr(evidence_store, "get_ref", None)
    if not callable(getter):
        return []
    failures: list[str] = []
    for ref_id in _replay_artifact_refs(raw):
        failures.extend(
            _replay_ref_failures(
                getter,
                ref_id,
                allowed_types=_ARTIFACT_REF_TYPES,
                missing_reason="missing_replay_artifact_ref",
                invalid_type_reason="invalid_replay_artifact_ref_type",
            )
        )
    for ref_id in _replay_judge_refs(raw):
        failures.extend(
            _replay_ref_failures(
                getter,
                ref_id,
                allowed_types=_JUDGE_REF_TYPES,
                missing_reason="missing_replay_judge_ref",
                invalid_type_reason="invalid_replay_judge_ref_type",
            )
        )
    return _dedupe(failures)


def _replay_ref_failures(
    getter: Any,
    ref_id: str,
    *,
    allowed_types: set[str],
    missing_reason: str,
    invalid_type_reason: str,
) -> list[str]:
    text = str(ref_id or "").strip()
    if not text:
        return []
    try:
        ref = getter(text)
    except Exception as exc:
        logger.debug("Failed to load replay evidence ref %s", text, exc_info=True)
        return [f"{missing_reason}:{text}:{str(exc)[:80]}"]
    if ref is None:
        return [f"{missing_reason}:{text}"]
    ref_type = str(_attr(ref, "ref_type") or "")
    if ref_type not in allowed_types:
        return [f"{invalid_type_reason}:{text}:{ref_type or 'missing'}"]
    return []


def _replay_artifact_refs(raw: Mapping[str, Any]) -> list[str]:
    refs = _top_level_replay_artifact_refs(raw)
    for item in _replay_task_result_mappings(raw):
        refs.extend(_str_list(item.get("artifact_refs")))
    return _dedupe(refs)


def _top_level_replay_artifact_refs(raw: Mapping[str, Any]) -> list[str]:
    details = _dict_or_empty(raw.get("details"))
    return _dedupe([
        *_str_list(raw.get("artifact_refs")),
        *_str_list(details.get("artifact_refs")),
    ])


def _replay_judge_refs(raw: Mapping[str, Any]) -> list[str]:
    refs = _top_level_replay_judge_refs(raw)
    for item in _replay_task_result_mappings(raw):
        refs.append(str(item.get("judge_result_id") or ""))
    return _dedupe(refs)


def _top_level_replay_judge_refs(raw: Mapping[str, Any]) -> list[str]:
    details = _dict_or_empty(raw.get("details"))
    return _dedupe([
        str(raw.get("judge_result_id") or ""),
        str(details.get("judge_result_id") or ""),
    ])


def _replay_task_result_mappings(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    details = _dict_or_empty(raw.get("details"))
    items = [
        *_sequence(raw.get("replay_task_results")),
        *_sequence(details.get("replay_task_results")),
    ]
    return [
        item
        for item in (_dict_or_empty(result) for result in items)
        if item
    ]


def _routing_selector_name(
    selector: Any | None,
    registry: Any | None,
    llm_client: Any | None,
) -> str:
    if selector is not None:
        return type(selector).__name__
    if registry is not None and llm_client is not None and hasattr(registry, "select_skills_with_llm"):
        return "registry_llm_selector"
    if registry is not None:
        return "registry_ranker"
    return "none"


async def _call_routing_selector(selector: Any, **kwargs: Any) -> Any:
    for name in ("select", "route", "evaluate", "run"):
        method = getattr(selector, name, None)
        if callable(method):
            raw = method(**kwargs)
            if hasattr(raw, "__await__"):
                raw = await raw
            return raw
    if callable(selector):
        raw = selector(**kwargs)
        if hasattr(raw, "__await__"):
            raw = await raw
        return raw
    return []


def _candidate_skill_universe(
    registry: Any,
    staged: Any,
    candidate_meta: Any,
) -> list[Any]:
    parent_ids = set(_parent_skill_ids(staged, None))
    skills = [
        meta
        for meta in list(registry.list_skills())
        if str(getattr(meta, "skill_id", "")) not in parent_ids
    ]
    return [*skills, candidate_meta]


def _candidate_skill_meta(staged: Any, candidate_skill_id: str) -> Any:
    from openspace.skill_engine.registry import SkillMeta

    skill_text = _content_snapshot(staged).get(SKILL_FILENAME, "")
    frontmatter = parse_frontmatter(skill_text)
    name = str(
        _attr(staged, "proposed_name")
        or frontmatter.get("name")
        or candidate_skill_id
    )
    description = str(
        _attr(staged, "proposed_description")
        or frontmatter.get("description")
        or name
    )
    skill_path = _staged_skill_path(staged)
    return SkillMeta(
        skill_id=candidate_skill_id,
        name=name,
        description=description,
        path=skill_path,
        display_name=str(frontmatter.get("name") or name),
        source="evolution_candidate",
        loaded_from="staging",
        user_invocable=not bool(frontmatter.get("disable-model-invocation")),
        disable_model_invocation=bool(frontmatter.get("disable-model-invocation")),
        when_to_use=str(frontmatter.get("when_to_use") or frontmatter.get("when-to-use") or "") or None,
        raw_frontmatter=dict(frontmatter),
    )


def _ranker_candidates(
    registry: Any,
    staged: Any,
    candidate_skill_id: str,
) -> list[Any]:
    from openspace.skill_engine.skill_ranker import SkillCandidate

    parent_ids = set(_parent_skill_ids(staged, None))
    candidates: list[Any] = []
    for meta in list(registry.list_skills()):
        skill_id = str(getattr(meta, "skill_id", "") or "")
        if skill_id in parent_ids:
            continue
        body = ""
        loader = getattr(registry, "load_skill_content", None)
        if callable(loader):
            body = str(loader(skill_id) or "")
        candidates.append(
            SkillCandidate(
                skill_id=skill_id,
                name=str(getattr(meta, "name", "") or ""),
                description=str(getattr(meta, "description", "") or ""),
                body=body,
            )
        )
    skill_text = _content_snapshot(staged).get(SKILL_FILENAME, "")
    frontmatter = parse_frontmatter(skill_text)
    body = strip_frontmatter(skill_text)
    candidates.append(
        SkillCandidate(
            skill_id=candidate_skill_id,
            name=str(_attr(staged, "proposed_name") or frontmatter.get("name") or ""),
            description=str(
                _attr(staged, "proposed_description")
                or frontmatter.get("description")
                or ""
            ),
            body=body,
            source="evolution_candidate",
        )
    )
    return candidates


def _normalize_selected_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        for key in ("selected", "selected_skill_ids", "skill_ids", "skills"):
            if key in raw:
                return _normalize_selected_ids(raw.get(key))
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple, set)):
        ids: list[str] = []
        for item in raw:
            if isinstance(item, str):
                ids.append(item)
                continue
            skill_id = _attr(item, "skill_id")
            if skill_id:
                ids.append(str(skill_id))
        return _dedupe(ids)
    skill_id = _attr(raw, "skill_id")
    return [str(skill_id)] if skill_id else []


def _parse_runner_stdout(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        return {"passed": False, "failures": ["replay_runner_empty_stdout"]}
    try:
        parsed = json.loads(text)
        return dict(parsed) if isinstance(parsed, Mapping) else {
            "passed": False,
            "failures": ["replay_runner_stdout_not_object"],
        }
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                return dict(parsed) if isinstance(parsed, Mapping) else {}
            except Exception:
                pass
    return {"passed": False, "failures": ["replay_runner_invalid_json"]}


def _candidate_skill_id(staged: Any, authoring: Any, decision: Any) -> str:
    for value in (
        _attr(staged, "proposed_skill_id"),
        _attr(staged, "skill_id"),
        _attr(decision, "proposed_skill_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    name = str(_attr(staged, "proposed_name") or "skill").strip() or "skill"
    authoring_id = str(_attr(authoring, "authoring_id") or uuid.uuid4().hex)
    return f"{name}__candidate_{authoring_id[-8:]}"


def _baseline_revision_set(skill_store: Any | None, registry: Any | None) -> list[str]:
    if skill_store is not None:
        loader = getattr(skill_store, "load_active", None)
        if callable(loader):
            try:
                active = loader()
                if isinstance(active, Mapping):
                    return sorted(str(key) for key in active.keys())
            except Exception:
                logger.debug("Failed to load active skill revision set", exc_info=True)
    if registry is not None:
        try:
            return sorted(
                str(getattr(meta, "skill_id", "") or "")
                for meta in registry.list_skills()
                if getattr(meta, "skill_id", None)
            )
        except Exception:
            logger.debug("Failed to load registry revision set", exc_info=True)
    return []


def _candidate_revision_set(
    baseline: list[str],
    candidate_skill_id: str,
    parent_skill_ids: list[str],
    action_type: str,
) -> list[str]:
    ids = list(baseline)
    if action_type == "FIX":
        parent_set = set(parent_skill_ids)
        ids = [item for item in ids if item not in parent_set]
    ids.append(candidate_skill_id)
    return sorted(_dedupe(ids))


def _parent_skill_ids(staged: Any, decision: Any | None) -> list[str]:
    ids = _str_list(_attr(staged, "parent_skill_ids"))
    if not ids:
        ids = _str_list(_attr(staged, "target_skill_ids"))
    if not ids and decision is not None:
        ids = _str_list(_attr(decision, "target_skill_ids")) or _str_list(
            _attr(decision, "target_skills")
        )
    return _dedupe(ids)


def _staged_skill_path(staged: Any) -> Path:
    target_dir = str(_attr(staged, "target_dir") or "").strip()
    if target_dir:
        return Path(target_dir).expanduser().resolve() / SKILL_FILENAME
    staging_dir = str(_attr(staged, "staging_dir") or "").strip()
    proposed_name = str(_attr(staged, "proposed_name") or "candidate").strip()
    if staging_dir:
        return Path(staging_dir).expanduser().resolve() / "proposed" / proposed_name / SKILL_FILENAME
    return Path.cwd() / ".openspace" / "evolution" / "staged" / proposed_name / SKILL_FILENAME


def _content_snapshot(staged: Any) -> dict[str, str]:
    value = _attr(staged, "content_snapshot")
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(val) for key, val in value.items()}


def _action_type(decision: Any, staged: Any) -> str:
    raw = (
        _attr(staged, "action_type")
        or _attr(decision, "proposed_action")
        or _attr(decision, "action_type")
        or ""
    )
    return str(raw).strip().upper()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attr(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Mapping):
        return [str(item) for item in value.values() if str(item)]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dict_of_str_lists(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _str_list(item) for key, item in value.items()}


def _dict_of_strings(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _evaluate_static_assertion(
    assertion: Mapping[str, Any],
    snapshot: Mapping[str, str],
) -> dict[str, Any]:
    assertion_type = _normalize_assertion_type(
        assertion.get("type") or assertion.get("assertion_type")
    )
    target = str(assertion.get("target") or "").strip()
    expected = assertion.get("expected", True)
    skill_text = (
        str(snapshot.get(SKILL_FILENAME) or "")
        or str(snapshot.get("SKILL.md") or "")
    )
    frontmatter = parse_frontmatter(skill_text) if skill_text else {}
    body = strip_frontmatter(skill_text) if skill_text else ""
    result: dict[str, Any] = {
        "type": assertion_type,
        "target": target,
        "expected": expected,
        "evidence_type": "static",
    }

    if assertion_type in {"file_exists", "skill_file_exists"}:
        exists = bool(target and target in snapshot)
        if not exists and target and target not in {SKILL_FILENAME, "SKILL.md"}:
            return {
                **result,
                "passed": None,
                "observed": exists,
                "requires_executable_evidence": True,
            }
        passed = exists if bool(expected) else not exists
        return {**result, "passed": passed, "observed": exists}

    if assertion_type in {"skill_file_contains", "file_contains"}:
        content = _snapshot_content(snapshot, target or SKILL_FILENAME)
        needles = _expected_needles(expected, fallback=target)
        passed = bool(content) and all(needle in content for needle in needles)
        return {**result, "passed": passed, "observed": bool(content), "needles": needles}

    if assertion_type in {"skill_file_not_contains", "file_not_contains"}:
        content = _snapshot_content(snapshot, target or SKILL_FILENAME)
        needles = _expected_needles(expected, fallback=target)
        passed = bool(content) and all(needle not in content for needle in needles)
        return {**result, "passed": passed, "observed": bool(content), "needles": needles}

    if assertion_type == "body_contains":
        needles = _expected_needles(expected, fallback=target)
        passed = bool(body) and all(needle in body for needle in needles)
        return {**result, "passed": passed, "observed": bool(body), "needles": needles}

    if assertion_type == "body_not_contains":
        needles = _expected_needles(expected, fallback=target)
        passed = bool(body) and all(needle not in body for needle in needles)
        return {**result, "passed": passed, "observed": bool(body), "needles": needles}

    if assertion_type in {"frontmatter_has", "header_has"}:
        observed = frontmatter.get(target)
        passed = bool(observed) if bool(expected) else not bool(observed)
        return {**result, "passed": passed, "observed": observed}

    if assertion_type in {"frontmatter_equals", "header_equals"}:
        observed = frontmatter.get(target)
        passed = str(observed or "") == str(expected)
        return {**result, "passed": passed, "observed": observed}

    if assertion_type in {"frontmatter_contains", "header_contains"}:
        observed = str(frontmatter.get(target) or "")
        needles = _expected_needles(expected)
        passed = bool(observed) and all(needle in observed for needle in needles)
        return {**result, "passed": passed, "observed": observed, "needles": needles}

    if assertion_type == "skill_name_equals":
        observed = str(frontmatter.get("name") or "")
        expected_text = str(expected if expected is not True else target)
        passed = bool(observed) and observed == expected_text
        return {**result, "passed": passed, "observed": observed}

    if assertion_type == "skill_description_contains":
        observed = str(frontmatter.get("description") or "")
        needles = _expected_needles(expected, fallback=target)
        passed = bool(observed) and all(needle in observed for needle in needles)
        return {**result, "passed": passed, "observed": observed, "needles": needles}

    if assertion_type in {"artifact_valid", "error_absent"}:
        return {
            **result,
            "passed": None,
            "requires_executable_evidence": True,
        }

    if assertion_type == "manual":
        return {
            **result,
            "passed": None,
            "requires_human_review": True,
        }

    return {
        **result,
        "passed": False,
        "failure": f"unsupported_assertion_type:{assertion_type or 'missing'}",
    }


def _normalize_assertion_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _snapshot_content(snapshot: Mapping[str, str], target: str) -> str:
    if target in snapshot:
        return str(snapshot.get(target) or "")
    normalized = target.lstrip("./")
    for key, value in snapshot.items():
        if str(key).lstrip("./") == normalized:
            return str(value)
    return ""


def _expected_needles(expected: Any, *, fallback: str = "") -> list[str]:
    if isinstance(expected, bool):
        return [fallback] if fallback else []
    if isinstance(expected, str):
        text = expected.strip()
        return [text] if text else ([fallback] if fallback else [])
    needles = _str_list(expected)
    return needles or ([fallback] if fallback else [])


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    return None


def _replay_result_has_executable_evidence(raw: Any) -> bool:
    if not isinstance(raw, Mapping):
        return False
    for item in _replay_task_result_mappings(raw):
        if _replay_task_result_has_executable_evidence(item):
            return True
    return False


def _replay_task_result_has_executable_evidence(item: Mapping[str, Any]) -> bool:
    task_id = str(item.get("task_id") or item.get("id") or "").strip()
    if not task_id:
        return False
    attempted = (
        _bool_or_none(item.get("attempted")) is True
        or _bool_or_none(item.get("executable_eval_attempted")) is True
    )
    if not attempted:
        return False
    if _bool_or_none(item.get("passed")) is not None:
        return True
    status = str(item.get("status") or "").strip().lower()
    if status in {"passed", "failed", "error"}:
        return True
    if str(item.get("judge_result_id") or "").strip():
        return True
    if _str_list(item.get("artifact_refs")):
        return True
    if (
        _float_or_none(item.get("baseline_score")) is not None
        and _float_or_none(item.get("candidate_score")) is not None
    ):
        return True
    if _sequence(item.get("assertion_results")):
        return True
    return False


def _decode_timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _dedupe(values: list[Any]) -> list[str]:
    return [item for item in dict.fromkeys(str(value) for value in values if value) if item]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
