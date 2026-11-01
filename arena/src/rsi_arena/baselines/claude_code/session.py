"""Claude Code native session, resume, and usage behavior."""

from __future__ import annotations

import json
from collections.abc import Mapping as MappingABC
from pathlib import Path
from typing import Mapping, Optional

from rsi_arena.baselines.agentic.hooks import install_claude_home
from rsi_arena.baselines.agentic.session import (
    RESUME_PROMPT,
    CliMethodSessionConfig,
    MethodIntervalResult,
    _CliMethodSession,
)
from rsi_arena.baselines.agentic.usage import ClaudeUsageAccumulator
from rsi_arena.baselines.claude_code.native import (
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_PACKAGE_IDENTITY,
    CLAUDE_CODE_VERSION,
    ClaudeNativeSession,
)
from rsi_arena.common import UsageLedger
from rsi_arena.run_layout import RunValidationError, atomic_write_json


class ClaudeMethodSession(_CliMethodSession):
    config_env_var = "CLAUDE_CONFIG_DIR"
    credential_name = ".credentials.json"

    def __init__(
        self,
        config: CliMethodSessionConfig,
        *,
        ledger: Optional[UsageLedger],
        native_session: Optional[ClaudeNativeSession] = None,
    ) -> None:
        super().__init__(config, ledger=ledger)
        self.claude_session = native_session
        self._usage: Optional[ClaudeUsageAccumulator] = None

    def bind(self, *, run_id: str) -> None:
        super().bind(run_id=run_id)
        resume_index = self.config.resume_segment_ordinal
        stored_session_id = (
            self._resume_session_id(run_id=run_id) if resume_index is not None else None
        )
        if self.claude_session is None:
            self.claude_session = ClaudeNativeSession.create(
                run_id=run_id,
                cli_path=self.config.cli_path,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
                session_id=stored_session_id or self.config.session_id,
            )
        session = self.claude_session
        if (
            session.run_id != run_id
            or (
                self.config.session_id is not None
                and session.session_id != self.config.session_id
            )
            or session.cli_path != self.config.cli_path
            or session.model != self.config.model
            or session.reasoning_effort != self.config.reasoning_effort
        ):
            raise ValueError("injected Claude session identity disagrees with runner")
        if stored_session_id is not None and session.session_id != stored_session_id:
            raise RunValidationError("Claude resume session identity changed")
        if self.ledger is not None:
            self._usage = ClaudeUsageAccumulator(
                ledger=self.ledger,
                run_id=run_id,
                session_id=session.session_id,
                summary_path=self.config.trusted_dir
                / "trajectory"
                / "claude-usage-derived.json",
                default_model=self.config.model or "claude",
                provider=self.config.provider,
            )
            if resume_index is not None:
                self._reconcile_durable_segments(resume_index)
                if self._usage.next_segment_index != resume_index:
                    raise RunValidationError(
                        "Claude native segments disagree with durable usage"
                    )
        if resume_index is not None:
            session.restore_next_segment_index(resume_index)

    def _resume_session_id(self, *, run_id: str) -> str:
        path = self.config.trusted_dir / "trajectory" / "claude-native-identity.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RunValidationError("Claude resume identity is unavailable") from exc
        expected_keys = {
            "schema",
            "package",
            "version",
            "package_identity",
            "run_id",
            "session_id",
            "model",
            "reasoning_effort",
            "resume_policy",
            "event_schema_evidence",
        }
        if (
            not isinstance(payload, MappingABC)
            or set(payload) != expected_keys
            or payload.get("schema") != "rsi-arena-claude-native-identity"
            or payload.get("package") != CLAUDE_CODE_PACKAGE
            or payload.get("version") != CLAUDE_CODE_VERSION
            or payload.get("package_identity") != CLAUDE_CODE_PACKAGE_IDENTITY
            or payload.get("run_id") != run_id
            or payload.get("model") != self.config.model
            or payload.get("reasoning_effort") != self.config.reasoning_effort
            or payload.get("resume_policy") != "explicit_session_id"
            or payload.get("event_schema_evidence") != "synthetic_fake_no_model_only"
            or not isinstance(payload.get("session_id"), str)
            or not payload.get("session_id")
        ):
            raise RunValidationError("Claude resume identity is invalid")
        session_id = str(payload["session_id"])
        if self.config.session_id is not None and self.config.session_id != session_id:
            raise RunValidationError(
                "Claude configured resume session identity changed"
            )
        return session_id

    def _reconcile_durable_segments(self, resume_index: int) -> None:
        usage = self._usage
        if usage is None:
            raise RuntimeError("Claude usage requires a bound ledger")
        roots = (
            self.config.trusted_dir / "trajectory" / "raw",
            self.config.run_dir / "segments",
        )
        for index in range(resume_index):
            name = f"segment-{index:03d}.jsonl"
            matches = tuple(path for root in roots if (path := root / name).is_file())
            if len(matches) != 1:
                raise RunValidationError(
                    "Claude resume requires one durable raw stream per segment"
                )
            usage.record_segment(
                matches[0],
                segment_id=f"{self.claude_session.session_id}:segment-{index:03d}",
            )

    def install_home(
        self,
        cli_home: Path,
        *,
        hook_path: Path,
        hook_command: str,
        secrets: Mapping[str, str],
    ) -> None:
        del secrets
        install_claude_home(
            cli_home,
            hook_path=hook_path,
            hook_command=hook_command,
            install_stop_hook=True,
        )

    def next_command(
        self, initial_prompt: str, *, continuation: Optional[bool] = None
    ) -> tuple[list[str], bool]:
        session = self.claude_session
        if session is None:
            raise RuntimeError("Claude method session is not bound")
        if continuation is None:
            continuation = self._interval_index > 0
        prompt = RESUME_PROMPT if continuation else initial_prompt
        _segment_id, command = session.next_command(
            prompt,
            resume=continuation,
            extra_args=self.config.extra_cli_args,
        )
        return command, continuation

    def bind_child_context(self, child_env: dict[str, str]) -> None:
        session = self.claude_session
        if session is None or session.current_segment_id is None:
            return
        context = session.child_context()
        child_env.update(
            {
                "RSI_ARENA_RUN_ID": context["run_id"],
                "RSI_ARENA_CLAUDE_SESSION_ID": context["session_id"],
                "RSI_ARENA_CLAUDE_SEGMENT_ID": context["segment_id"],
            }
        )
        capabilities = self._client_capabilities
        bind_context = (
            capabilities.bind_native_context if capabilities is not None else None
        )
        if bind_context is not None:
            bind_context(**context)

    def normalize_raw_output(self, raw_output_path: Path) -> float:
        session = self.claude_session
        usage = self._usage
        if session is None or usage is None:
            raise RuntimeError("Claude usage requires a bound session and ledger")
        segment_id = session.current_segment_id
        if segment_id is None:
            segment_id, _ = session.next_command(
                "synthetic usage parse",
                resume=False,
            )
        segment = usage.record_segment(raw_output_path, segment_id=segment_id)
        totals = segment["derived_totals"]
        assert isinstance(totals, Mapping)
        observed_messages = bool(segment["messages"])
        observed_cost = totals.get("total_cost_usd")
        self._saw_search_usage = (
            self._saw_search_usage or observed_messages or observed_cost is not None
        )
        if observed_cost is None:
            self._search_cost_unknown = True
            return 0.0
        return float(observed_cost)

    def current_search_cost_status(self) -> str:
        if not self._saw_search_usage:
            return "missing"
        return "missing" if self._search_cost_unknown else "reported"

    def native_session_id(self) -> Optional[str]:
        return self.claude_session.session_id if self.claude_session else None

    def native_interval_id(self) -> Optional[str]:
        return self.claude_session.current_segment_id if self.claude_session else None

    def persist_identity(self, trusted_dir: Path) -> None:
        if self.claude_session is None:
            raise RuntimeError("Claude method session is not bound")
        atomic_write_json(
            trusted_dir / "trajectory" / "claude-native-identity.json",
            self.claude_session.identity_payload(),
        )

    def interval_failure_reason(self, interval: MethodIntervalResult) -> Optional[str]:
        if interval.elapsed_s < self.config.min_segment_s:
            return "systematic_failure"
        return None

    def continuation_stop_reason(self, resume_count: int) -> Optional[str]:
        return "resume_cap" if resume_count >= self.config.max_resumes else None


__all__ = ["ClaudeMethodSession"]
