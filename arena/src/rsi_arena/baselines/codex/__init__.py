"""Codex CLI baseline extra with native resume and usage accounting."""

from __future__ import annotations

from pathlib import Path

from rsi_arena.baselines.agentic.runtime import (
    AgenticRunConfig,
    NativeSessionRunner,
    resolve_cli_method_session_config,
)
from rsi_arena.baselines.codex.launcher import resume_codex, run_codex, run_new_codex
from rsi_arena.baselines.codex.session import CodexMethodSession
from rsi_arena.common import UsageLedger


def create_session(
    config: AgenticRunConfig,
    *,
    ledger: UsageLedger | None,
) -> CodexMethodSession:
    """Create the Codex-native adapter without a method-name dispatcher."""

    return CodexMethodSession(
        resolve_cli_method_session_config(
            config,
            default_model="codex",
            run_dir=Path(config.run_dir),
            trusted_dir=Path(config.trusted_dir or config.run_dir),
        ),
        ledger=ledger,
    )


def create_runner(
    config: AgenticRunConfig,
    *,
    ledger: UsageLedger,
    arena_runner: object,
) -> NativeSessionRunner:
    return NativeSessionRunner(
        config,
        ledger=ledger,
        arena_runner=arena_runner,
        method_session=create_session(config, ledger=ledger),
    )


__all__ = [
    "AgenticRunConfig",
    "CodexMethodSession",
    "NativeSessionRunner",
    "create_runner",
    "create_session",
    "resume_codex",
    "run_codex",
    "run_new_codex",
]
