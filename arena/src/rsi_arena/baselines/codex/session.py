"""Codex-native session, resume, and usage behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from rsi_arena.baselines._sandbox import atomic_private_copy
from rsi_arena.baselines.agentic.hooks import install_codex_home
from rsi_arena.baselines.agentic.session import (
    RESUME_PROMPT,
    CliMethodSessionConfig,
    _CliMethodSession,
)
from rsi_arena.baselines.agentic.usage import (
    CODEX_SESSION_CUMULATIVE_TOKEN_SOURCE,
    codex_session_cumulative_usage_audit,
    iter_codex_usage,
)
from rsi_arena.common import UsageLedger


class CodexMethodSession(_CliMethodSession):
    config_env_var = "CODEX_HOME"
    credential_name = "auth.json"

    def __init__(
        self,
        config: CliMethodSessionConfig,
        *,
        ledger: Optional[UsageLedger],
    ) -> None:
        super().__init__(config, ledger=ledger)

    def install_home(
        self,
        cli_home: Path,
        *,
        hook_path: Path,
        hook_command: str,
        secrets: Mapping[str, str],
    ) -> None:
        config = self.config
        install_codex_home(
            cli_home,
            hook_path=hook_path,
            hook_command=hook_command,
            provider_model=(config.model if config.provider != "native" else None),
            provider_name=config.provider_name,
            provider_base_url=config.provider_base_url,
            provider_env_key=config.provider_env_key,
            reasoning_effort=config.reasoning_effort,
            secret_env_names=tuple(sorted(secrets)),
            install_stop_hook=True,
        )

    def copy_auth_sidecars(self, source: Path, cli_home: Path) -> None:
        dotenv = source.parent / ".env"
        if dotenv.is_file():
            atomic_private_copy(dotenv, cli_home / ".env")

    def next_command(
        self, initial_prompt: str, *, continuation: Optional[bool] = None
    ) -> tuple[list[str], bool]:
        if continuation is None:
            continuation = self._resume_existing_session or self._interval_index > 0
        prompt = RESUME_PROMPT if continuation else initial_prompt
        command = [self.config.cli_path, "exec"]
        if continuation:
            command.extend(("resume", "--last"))
        command.extend(("--json", "--dangerously-bypass-approvals-and-sandbox"))
        prompt_index = len(command)
        command.append(prompt)
        if self.config.model:
            command[prompt_index:prompt_index] = ["-m", self.config.model]
        return [*command, *self.config.extra_cli_args], continuation

    def normalize_raw_output(self, raw_output_path: Path) -> float:
        if not raw_output_path.exists():
            return 0.0
        lines = raw_output_path.read_text(errors="replace").splitlines()
        cost = 0.0
        frozen_prices = (
            self.config.input_cost_per_million > 0
            and self.config.output_cost_per_million > 0
        )
        for ordinal, call in enumerate(iter_codex_usage(iter(lines)), start=1):
            if frozen_prices:
                call_cost = (
                    call.tokens_in * self.config.input_cost_per_million
                    + call.tokens_out * self.config.output_cost_per_million
                ) / 1_000_000
                if not (
                    call.tokens_in
                    or call.tokens_out
                    or call.cache_read_tokens
                    or call.cache_write_tokens
                ):
                    continue
            else:
                call_cost = call.cost_usd
            self._saw_search_usage = True
            provider_cost = call.cost_usd
            if provider_cost is None:
                self._search_cost_unknown = True
            if call_cost is not None:
                cost += call_cost
            if self.ledger is None:
                raise RuntimeError("Codex usage requires a bound usage ledger")
            self.ledger.record(
                "solver-agent",
                model=self.config.model or self.config.default_model,
                provider=self.config.provider,
                call_id=f"codex:{raw_output_path.stem}:model-call:{ordinal}",
                segment_id=f"codex:{raw_output_path.stem}",
                usage_scope="model_call",
                tokens_in=call.tokens_in,
                tokens_out=call.tokens_out,
                cache_read_tokens=call.cache_read_tokens,
                cache_write_tokens=call.cache_write_tokens,
                cost_usd=call_cost,
                token_source="cli",
                token_status="reported",
                provider_reported_cost=provider_cost,
                cost_source=(
                    "frozen_price_estimate"
                    if frozen_prices
                    else "cli"
                    if provider_cost is not None
                    else "unknown"
                ),
                cost_status="reported" if provider_cost is not None else "missing",
                cache_status=(
                    "hit" if call.cache_read_tokens else "miss_or_unreported"
                ),
            )
        return cost

    def current_search_cost_status(self) -> str:
        if not self._saw_search_usage:
            return "missing"
        if self._observed_session_usage_lower_bound:
            return "missing"
        if self._search_cost_unknown:
            frozen_prices = (
                self.config.input_cost_per_million > 0
                and self.config.output_cost_per_million > 0
            )
            return "unverified" if frozen_prices else "missing"
        return "reported"

    def search_cost_summary(self, spent_usd: float) -> tuple[Optional[float], str]:
        if self._observed_session_usage_lower_bound:
            return None, "missing"
        frozen_prices = (
            self.config.input_cost_per_million > 0
            and self.config.output_cost_per_million > 0
        )
        return (
            (
                spent_usd
                if self._saw_search_usage
                and (frozen_prices or not self._search_cost_unknown)
                else None
            ),
            self.current_search_cost_status(),
        )

    def finalize_session_usage(
        self, *, run_started_at: object, run_ended_at: object
    ) -> None:
        if self._saw_search_usage:
            return
        expected_cwd = (
            str(self.config.worktree.expanduser().resolve())
            if self.config.sandbox_backend.strip().lower() == "host"
            else "/workspace"
        )
        if self.run_id is None:
            raise RuntimeError("Codex usage finalization requires run identity")
        audit = codex_session_cumulative_usage_audit(
            self.config.run_dir / "cli-home",
            run_id=self.run_id,
            expected_cwd=expected_cwd,
            expected_model=str(self.config.model or ""),
            run_started_at=run_started_at,
            run_ended_at=run_ended_at,
        )
        if audit["status"] != "observed_lower_bound":
            return
        self._saw_search_usage = True
        self._search_cost_unknown = True
        self._observed_session_usage_lower_bound = True
        if self.ledger is None:
            raise RuntimeError("Codex usage requires a bound usage ledger")
        self.ledger.record(
            "solver-agent",
            model=self.config.model or self.config.default_model,
            provider=self.config.provider,
            call_id=str(audit["session_id"]),
            tokens_in=int(audit["input_tokens"]),
            tokens_out=int(audit["output_tokens"]),
            cache_read_tokens=int(audit["cached_input_tokens"]),
            cost_usd=None,
            token_source=CODEX_SESSION_CUMULATIVE_TOKEN_SOURCE,
            token_status="unverified",
            usage_scope="observed_lower_bound",
            provider_reported_cost=None,
            cost_source="unknown",
            cost_status="missing",
            cache_status=("hit" if int(audit["cached_input_tokens"]) else "miss"),
        )


__all__ = ["CodexMethodSession"]
