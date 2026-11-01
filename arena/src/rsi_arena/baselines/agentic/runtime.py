"""Arena-owned interval loop for Claude Code, Codex CLI, and DSH.

One continued session per run:

1. launch the CLI once with the full task prompt;
2. the injected Stop hook unconditionally blocks voluntary session ends, so
   the process only exits abnormally (crash, context death, kill);
3. abnormal exits are resumed natively (``claude --resume <session-id>`` /
   ``codex exec resume --last``); Claude keeps its ``max_resumes`` and
   ``min_segment_s`` safety valves, while Arena-owned Codex continues until
   an Arena/search hard stop;
4. termination is total recorded model-dollar-budget primary; wall clock is a
   secondary cap. Search, development, and candidate evaluation spend share
   the same durable budget authority.

Arena-owned Codex campaigns resume the same native session until the
resolved attempt cap reaches terminal state or the wall clock expires. Claude keeps
its configured native resume/cost/segment valves while using the same Arena
submit service.
DSH projects the same loop onto the official SDK ``Session.run`` lifecycle;
it has no CLI run/resume argv. Production intervals run in the shared
fail-closed bubblewrap boundary: only
the candidate workspace, private CLI state/Arena IPC, and immutable per-run
hook/Arena executables are mounted into the CLI namespace.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Protocol

from rsi_arena.baselines.agentic.session import (
    CliMethodSessionConfig,
    MethodIntervalControl,
    MethodSession,
    _MethodClientCapabilities,
)
from rsi_arena.common import UsageLedger
from rsi_arena.run_layout import (
    RUN_RESULT_SCHEMA,
    RunValidationError,
    read_jsonl,
)

MAX_RESUMES = 100  # EdgeBench MAX_RESUMES
MIN_SEGMENT_S = 1.0  # EdgeBench MIN_RUNTIME_FOR_RESUME
_ARENA_RESPONSE_COMPLETION_GRACE_S = 60.0


class ADevelopmentRunner(Protocol):
    """Small install/lifecycle surface shared by benchmark-native A services."""

    def install(
        self,
        bin_dir: Path,
        ipc_dir: Path,
        *,
        guest_ipc_dir: Optional[Path | str] = None,
    ) -> Path: ...

    def start(self) -> object: ...

    def stop(self) -> None: ...


class _ArenaSubmitMonitor:
    """Stateless runner view when Arena owns the only public submit path."""

    def __init__(self, arena_runner: ADevelopmentRunner) -> None:
        self._arena_runner = arena_runner

    @property
    def attempt_cap_exhausted(self) -> bool:
        return bool(getattr(self._arena_runner, "attempt_cap_exhausted", False))

    def _controller(self) -> object | None:
        service = getattr(self._arena_runner, "service", None)
        return getattr(service, "campaign_controller", None)

    @property
    def best_completed_submission(self) -> tuple[float, str] | None:
        """Return the first durable maximum scalar across this run."""

        controller = self._controller()
        terminal_reader = getattr(controller, "campaign_terminal_results", None)
        if not callable(terminal_reader):
            return None
        best: tuple[float, str] | None = None
        for result in terminal_reader():
            if result.outcome != "measured":
                continue
            scalar = result.aggregate_score
            evaluation_id = result.candidate_evaluation_id
            if (
                isinstance(scalar, bool)
                or not isinstance(scalar, (int, float))
                or not math.isfinite(float(scalar))
                or not isinstance(evaluation_id, str)
                or not evaluation_id
            ):
                continue
            measured = float(scalar)
            if best is None or measured > best[0]:
                best = (measured, evaluation_id)
        return best

    @property
    def successful_submission_count(self) -> int:
        """Count delivered terminal reports, not durable candidate ownership."""

        controller = self._controller()
        terminal_ids = getattr(controller, "delivered_terminal_ids", None)
        if not isinstance(terminal_ids, frozenset):
            return 0
        return len(terminal_ids)

    @property
    def n_candidate_evaluations(self) -> int:
        service = getattr(self._arena_runner, "service", None)
        controller = getattr(service, "campaign_controller", None)
        return int(getattr(controller, "candidate_evaluations_used", 0))

    def stop_admission(self) -> None:
        stop = getattr(self._arena_runner, "stop_admission", None)
        if not callable(stop):
            raise RuntimeError("Arena-owned submit runner lacks admission control")
        stop()

    def stop(self) -> None:
        return None


@dataclass(frozen=True)
class AgenticRunConfig:
    cli_path: Optional[str]
    worktree: Path
    run_dir: Path
    prompt: str
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    # ``native`` keeps the CLI's first-party auth/config behavior.
    # ``openrouter``/``openai_compatible`` change only provider configuration,
    # never the CLI loop.
    provider: str = "native"
    provider_base_url: Optional[str] = None
    provider_env_key: Optional[str] = None
    provider_name: Optional[str] = None
    # A third-party provider can make the CLI's built-in dollar estimate use
    # the wrong vendor/model price.  Positive frozen rates replace only that
    # accounting source; they do not change the CLI, prompt, or tool loop.
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    # ``None`` is the resolved run's explicit unbounded total-cost mode.  A
    # finite cap covers search, development, and candidate evaluation calls.
    budget_usd: Optional[float] = None
    wall_budget_s: float = 12 * 3600.0
    max_resumes: int = MAX_RESUMES
    min_segment_s: float = MIN_SEGMENT_S
    # Optional agent-facing cadence guard. When set, each fresh B submission
    # requires a newly published terminal A and no more than this many new A
    # publications may occur before that B submission.
    max_new_terminal_a_per_b_cycle: Optional[int] = None
    # secret env var names forwarded to the CLI (auth); everything else is
    # a minimal base environment plus the run-private CLI home
    env_passthrough: tuple[str, ...] = ()
    # Explicit derived environment values (for example Claude Code's
    # Anthropic-skin variables).  They use the same secret-aware channel as
    # passthrough values and are never serialized into the sandbox audit.
    env_overrides: tuple[tuple[str, str], ...] = ()
    extra_cli_args: tuple[str, ...] = ()
    # Optional subscription credential copied into the run-private CLI home.
    # The source is host-only and is never mounted directly.
    auth_file: Optional[Path] = None
    # The CLI's own dangerous/no-HITL flags are preserved, but this outer
    # namespace is the actual filesystem boundary.  Host execution is only a
    # deliberate unit-test/development escape hatch.
    sandbox_backend: str = "bwrap"
    bwrap_path: str = "bwrap"
    sandbox_network: str = "open"
    allow_unsafe_host: bool = False
    # Candidate-inaccessible artifact side. Campaign launchers set this to the
    # canonical trusted run leaf; it defaults to ``run_dir`` for local tests.
    trusted_dir: Optional[Path] = None
    # Formal runs fail closed when evaluation accounting is unverified.
    run_kind: str = "smoke"
    # Claude sessions are host-assigned and resumed only by this exact ID.
    # When omitted, a fresh UUID is allocated for this Runner instance.
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    # Codex process-restart resume reuses its run-private CLI home and starts
    # raw trajectory output at this next ordinal. Other adapters leave it None.
    resume_segment_ordinal: Optional[int] = None


def resolve_cli_method_session_config(
    config: AgenticRunConfig,
    *,
    default_model: str,
    run_dir: Path,
    trusted_dir: Path,
) -> CliMethodSessionConfig:
    """Resolve current CLI-only settings before entering a native adapter."""

    if not config.cli_path:
        raise ValueError("CLI session config requires an explicit CLI baseline")

    if config.provider == "openrouter":
        provider_base_url = config.provider_base_url or "https://openrouter.ai/api/v1"
        provider_env_key = config.provider_env_key or "OPENROUTER_API_KEY"
        provider_name = config.provider_name or "OpenRouter"
    elif config.provider == "openai_compatible":
        provider_base_url = str(config.provider_base_url or "").strip()
        provider_env_key = str(config.provider_env_key or "").strip()
        provider_name = config.provider_name or "OpenAI-compatible endpoint"
        if not provider_base_url or not provider_env_key:
            raise ValueError("OpenAI-compatible provider configuration is incomplete")
    else:
        provider_base_url = provider_env_key = provider_name = None
    return CliMethodSessionConfig(
        default_model=default_model,
        cli_path=config.cli_path,
        worktree=Path(config.worktree),
        run_dir=Path(run_dir),
        trusted_dir=Path(trusted_dir),
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        provider=config.provider,
        provider_base_url=provider_base_url,
        provider_env_key=provider_env_key,
        provider_name=provider_name,
        input_cost_per_million=config.input_cost_per_million,
        output_cost_per_million=config.output_cost_per_million,
        max_resumes=config.max_resumes,
        min_segment_s=config.min_segment_s,
        env_passthrough=config.env_passthrough,
        env_overrides=config.env_overrides,
        extra_cli_args=config.extra_cli_args,
        auth_file=config.auth_file,
        sandbox_backend=config.sandbox_backend,
        bwrap_path=config.bwrap_path,
        sandbox_network=config.sandbox_network,
        allow_unsafe_host=config.allow_unsafe_host,
        run_kind=config.run_kind,
        session_id=config.session_id,
        resume_segment_ordinal=config.resume_segment_ordinal,
    )


@dataclass
class RunResult:
    # budget_exhausted | cost_unverified | wall_exhausted | systematic_failure
    # | resume_cap.
    # Arena-owned campaigns additionally use attempt_cap_exhausted.
    # There is no "agent finished" reason on purpose: with the Stop hook a
    # session cannot end voluntarily, so every exit is resumed until a cap
    # or valve fires.
    stop_reason: str
    n_segments: int = 0
    resume_count: int = 0
    # Distinct candidate artifacts durably frozen at the submit boundary.
    n_submitted_candidates: int = 0
    # Submissions whose COMPLETED evaluation report was delivered successfully.
    n_submissions: int = 0
    n_candidate_evaluations: int = 0
    # None is deliberate: an unavailable/invalid evaluator is not a measured
    # zero.  Consumers must branch on evaluation_status before ranking runs.
    evaluation_status: str = "inconclusive"
    best_pass_rate: Optional[float] = None
    best_round: Optional[str] = None
    best_snapshot: Optional[Path] = None
    # Search and benchmark spend remain separately reportable even though the
    # configured cap applies to their total.
    search_cost_usd: Optional[float] = None
    search_cost_status: str = "missing"
    evaluation_cost_usd: Optional[float] = None
    # Cost state is independent of tokens and uses the shared vocabulary.
    evaluation_cost_status: str = "reported"
    total_cost_usd: Optional[float] = None
    wall_seconds: float = 0.0
    segments: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class _PreparedRuntime:
    server: _ArenaSubmitMonitor
    arena_runner: ADevelopmentRunner


def _method_client_capabilities(
    arena_runner: ADevelopmentRunner,
) -> _MethodClientCapabilities:
    install = getattr(arena_runner, "install", None)
    if not callable(install):
        raise TypeError("Arena runner lacks public client installation")

    def client_environment() -> Mapping[str, str]:
        environment = getattr(arena_runner, "client_environment", {})
        if not isinstance(environment, Mapping):
            raise TypeError("Arena client environment must be a mapping")
        return MappingProxyType(dict(environment))

    def sandbox_mounts() -> tuple[tuple[Path, Path], ...]:
        return tuple(getattr(arena_runner, "sandbox_mounts", ()))

    binder = getattr(arena_runner, "bind_native_context", None)
    if binder is not None and not callable(binder):
        raise TypeError("Arena native-context binder must be callable")
    return _MethodClientCapabilities(
        install=install,
        client_environment=client_environment,
        sandbox_mounts=sandbox_mounts,
        bind_native_context=binder,
    )


class NativeSessionRunner:
    def __init__(
        self,
        config: AgenticRunConfig,
        *,
        ledger: UsageLedger,
        arena_runner: ADevelopmentRunner,
        method_session: MethodSession,
    ):
        if not bool(getattr(arena_runner, "handles_submit", False)):
            raise ValueError("agentic runner requires the Arena submit owner")
        self._config = config
        self._ledger = ledger
        self._arena_runner = arena_runner
        self._run_dir = Path(config.run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._separate_trusted_dir = config.trusted_dir is not None
        self._trusted_dir = Path(config.trusted_dir or config.run_dir)
        self._trusted_dir.mkdir(parents=True, exist_ok=True)
        injected_run_id = getattr(method_session, "run_id", None)
        run_id = str(config.run_id or injected_run_id or f"run-{uuid.uuid4()}")
        if injected_run_id is not None and str(injected_run_id) != run_id:
            raise ValueError("runner/config run identity disagrees")
        self._run_id = run_id
        self._method_session = method_session
        self._method_session.bind(run_id=run_id)

    # -- assembly ------------------------------------------------------------

    def _prepare(self) -> _PreparedRuntime:
        try:
            capabilities = _method_client_capabilities(self._arena_runner)
            self._method_session.prepare(capabilities)
            self._arena_runner.start()
        except BaseException:
            try:
                self._method_session.close()
            finally:
                self._arena_runner.stop()
            raise
        return _PreparedRuntime(
            server=_ArenaSubmitMonitor(self._arena_runner),
            arena_runner=self._arena_runner,
        )

    # -- accounting ----------------------------------------------------------

    def _structured_evaluation_cost(self) -> tuple[Optional[float], str]:
        """Read trusted development and candidate-evaluation cost leaves."""

        total = 0.0
        status = "reported"
        observed = False
        candidate_ids = {
            str(row.get("candidate_id", ""))
            for row in read_jsonl(self._trusted_dir / "candidates.jsonl")
        }
        for row in read_jsonl(self._ledger.path):
            if row.get("spend_kind") != "evaluation":
                continue
            is_development = row.get("usage_scope") == "development"
            if not is_development and str(row.get("candidate_id", "")) not in (
                candidate_ids
            ):
                continue
            observed = True
            row_status = str(row.get("cost_status", "unverified"))
            if row_status not in {"reported", "missing", "unverified"}:
                row_status = "unverified"
            value = row.get("accounting_cost_usd", row.get("cost_usd"))
            cost = _optional_nonnegative_finite_cost(value)
            if row_status == "reported" and cost is None:
                row_status = "unverified"
            status = _merge_cost_status(status, row_status)
            if row_status == "reported":
                assert cost is not None
                total += cost
        if not observed:
            return None, "missing"
        return (total, "reported") if status == "reported" else (None, status)

    def _budget_accounting(self) -> tuple[float | None, str]:
        """Return the durable all-call total used by the finite dollar cap."""

        total = 0.0
        for row in read_jsonl(self._ledger.path):
            value = row.get("accounting_cost_usd", row.get("cost_usd"))
            cost = _optional_nonnegative_finite_cost(value)
            if cost is None:
                return None, "unverified"
            total += cost
        return total, "reported"

    # -- main loop -----------------------------------------------------------

    def run(self) -> RunResult:
        config = self._config
        early_started = time.monotonic()
        server = _ArenaSubmitMonitor(self._arena_runner)
        budget_cost, budget_status = self._budget_accounting()
        if config.budget_usd is not None and (
            budget_status != "reported"
            or budget_cost is None
            or budget_cost >= config.budget_usd
        ):
            search_cost_usd, search_cost_status = self._stored_search_cost_summary()
            return self._finish_result(
                RunResult(
                    stop_reason=(
                        "budget_exhausted"
                        if budget_status == "reported"
                        else "cost_unverified"
                    )
                ),
                server=server,
                started=early_started,
                search_cost_usd=search_cost_usd,
                search_cost_status=search_cost_status,
            )
        if server.attempt_cap_exhausted:
            search_cost_usd, search_cost_status = self._stored_search_cost_summary()
            return self._finish_result(
                RunResult(stop_reason="attempt_cap_exhausted"),
                server=server,
                started=early_started,
                search_cost_usd=search_cost_usd,
                search_cost_status=search_cost_status,
            )

        self._method_session.reset_usage()
        prompt_file = self._run_dir / "prompt.md"
        prompt_file.write_text(config.prompt, encoding="utf-8")
        segments_dir = (
            self._trusted_dir / "trajectory" / "raw"
            if self._separate_trusted_dir
            else self._run_dir / "segments"
        )
        segments_dir.mkdir(parents=True, exist_ok=True)
        self._method_session.persist_identity(self._trusted_dir)
        runtime = self._prepare()
        server = runtime.server

        result = RunResult(stop_reason="unknown")
        started = time.monotonic()
        run_started_at = time.time()
        spent_usd = 0.0
        resume_count = 0

        try:
            while True:
                remaining_wall = config.wall_budget_s - (time.monotonic() - started)
                if remaining_wall <= 0:
                    result.stop_reason = "wall_exhausted"
                    break
                interval = self._method_session.run_prompt_interval(
                    initial_prompt=config.prompt,
                    raw_output_dir=segments_dir,
                    control=MethodIntervalControl(
                        wall_deadline=time.monotonic() + remaining_wall,
                        attempt_cap_exhausted=lambda: server.attempt_cap_exhausted,
                        completion_grace_s=_ARENA_RESPONSE_COMPLETION_GRACE_S,
                    ),
                )
                if interval.cost_usd is not None:
                    spent_usd += interval.cost_usd
                result.segments.append(interval.segment_record())
                result.n_segments += 1

                if interval.timed_out:
                    try:
                        server.stop_admission()
                    finally:
                        result.stop_reason = "wall_exhausted"
                    break
                interval_failure = self._method_session.interval_failure_reason(
                    interval
                )
                if interval_failure is not None:
                    result.stop_reason = interval_failure
                    break
                if server.attempt_cap_exhausted:
                    result.stop_reason = "attempt_cap_exhausted"
                    break
                if config.budget_usd is not None:
                    budget_cost, budget_status = self._budget_accounting()
                    if budget_status != "reported" or budget_cost is None:
                        result.stop_reason = "cost_unverified"
                        break
                    if budget_cost >= config.budget_usd:
                        result.stop_reason = "budget_exhausted"
                        break
                continuation_stop = self._method_session.continuation_stop_reason(
                    resume_count
                )
                if continuation_stop is not None:
                    result.stop_reason = continuation_stop
                    break
                resume_count += 1
        finally:
            try:
                self._method_session.close()
            finally:
                server.stop()
                runtime.arena_runner.stop()

        run_ended_at = time.time()
        self._method_session.finalize_session_usage(
            run_started_at=run_started_at,
            run_ended_at=run_ended_at,
        )
        search_cost_usd, search_cost_status = self._method_session.search_cost_summary(
            spent_usd
        )
        result.resume_count = resume_count
        return self._finish_result(
            result,
            server=server,
            started=started,
            search_cost_usd=search_cost_usd,
            search_cost_status=search_cost_status,
        )

    def _result_path(self) -> Path:
        return (
            self._trusted_dir / "results" / "run-result.json"
            if self._separate_trusted_dir
            else self._run_dir / "result.json"
        )

    def _stored_search_cost_summary(self) -> tuple[Optional[float], str]:
        """Recover only a schema- and run-bound durable search-cost summary."""

        path = self._result_path()
        if not path.exists():
            return None, "missing"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RunValidationError("agentic run result is malformed") from exc
        if not isinstance(payload, Mapping):
            raise RunValidationError("agentic run result is not a JSON object")
        if (
            payload.get("schema") != RUN_RESULT_SCHEMA
            or payload.get("run_id") != self._run_id
        ):
            raise RunValidationError("agentic run result identity changed")

        status = payload.get("search_cost_status")
        if status not in {"reported", "missing", "unverified"}:
            raise RunValidationError("agentic run result search cost is malformed")
        value = payload.get("search_cost_usd")
        cost = (
            float(value)
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0.0
            else None
        )
        if status == "reported" and cost is None:
            raise RunValidationError("agentic run result search cost is malformed")
        if status == "missing" and value is not None:
            raise RunValidationError("agentic run result search cost is malformed")
        if status == "unverified" and value is not None and cost is None:
            raise RunValidationError("agentic run result search cost is malformed")
        return cost, str(status)

    def _finish_result(
        self,
        result: RunResult,
        *,
        server: _ArenaSubmitMonitor,
        started: float,
        search_cost_usd: Optional[float],
        search_cost_status: str,
    ) -> RunResult:
        """Close out one runner invocation from already-durable Arena facts."""

        arena_trusted_submission = getattr(
            self._arena_runner,
            "trusted_last_submission_result",
            None,
        )
        result.n_submissions = server.successful_submission_count
        result.n_submitted_candidates = server.n_candidate_evaluations
        result.n_candidate_evaluations = server.n_candidate_evaluations
        best = server.best_completed_submission
        arena_summary = arena_trusted_submission
        if best is not None:
            result.best_pass_rate, result.best_round = best
        if best is not None:
            result.evaluation_status = "measured"
        elif arena_summary is not None:
            terminal = arena_summary.get("result")
            if (
                arena_summary.get("state") == "TERMINAL"
                and isinstance(terminal, Mapping)
                and terminal.get("outcome") in {"measured", "inconclusive"}
            ):
                result.evaluation_status = str(terminal["outcome"])
        evaluation_cost_usd, evaluation_cost_status = self._structured_evaluation_cost()
        result.search_cost_usd = search_cost_usd
        result.search_cost_status = search_cost_status
        result.evaluation_cost_usd = (
            evaluation_cost_usd if evaluation_cost_status == "reported" else None
        )
        result.evaluation_cost_status = evaluation_cost_status
        total_cost_usd, total_cost_status = self._budget_accounting()
        result.total_cost_usd = (
            total_cost_usd if total_cost_status == "reported" else None
        )
        result.wall_seconds = time.monotonic() - started
        result_path = self._result_path()
        result_payload = {
            "schema": RUN_RESULT_SCHEMA,
            "run_id": self._run_id,
            **{k: v for k, v in result.__dict__.items() if k != "best_snapshot"},
            "best_snapshot": (
                str(result.best_snapshot) if result.best_snapshot else None
            ),
        }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result_payload, indent=2, default=str))
        return result


def _optional_nonnegative_finite_cost(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cost) or cost < 0.0:
        return None
    return cost


def _merge_cost_status(left: str, right: str) -> str:
    order = {"reported": 0, "missing": 1, "unverified": 2}
    return left if order[left] >= order[right] else right
