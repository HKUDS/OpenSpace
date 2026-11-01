"""Explicit Claude Code native-session construction."""

from __future__ import annotations

from pathlib import Path

from rsi_arena.baselines._sandbox import SandboxLaunch
from rsi_arena.baselines.agentic.runtime import (
    AgenticRunConfig,
    NativeSessionRunner,
    resolve_cli_method_session_config,
)
from rsi_arena.baselines.claude_code.native import ClaudeNativeSession
from rsi_arena.baselines.claude_code.session import ClaudeMethodSession
from rsi_arena.common import UsageLedger


def create_session(
    config: AgenticRunConfig,
    *,
    ledger: UsageLedger | None,
    native_session: ClaudeNativeSession | None = None,
) -> ClaudeMethodSession:
    return ClaudeMethodSession(
        resolve_cli_method_session_config(
            config,
            default_model="claude",
            run_dir=Path(config.run_dir),
            trusted_dir=Path(config.trusted_dir or config.run_dir),
        ),
        ledger=ledger,
        native_session=native_session,
    )


def create_runner(
    config: AgenticRunConfig,
    *,
    ledger: UsageLedger,
    arena_runner: object,
    native_session: ClaudeNativeSession | None = None,
) -> NativeSessionRunner:
    return NativeSessionRunner(
        config,
        ledger=ledger,
        arena_runner=arena_runner,
        method_session=create_session(
            config,
            ledger=ledger,
            native_session=native_session,
        ),
    )


def prepare_version_launch(config: AgenticRunConfig) -> SandboxLaunch:
    Path(config.run_dir).mkdir(parents=True, exist_ok=True)
    Path(config.trusted_dir or config.run_dir).mkdir(parents=True, exist_ok=True)
    return create_session(config, ledger=None).prepare_version_launch()


__all__ = [
    "AgenticRunConfig",
    "ClaudeMethodSession",
    "ClaudeNativeSession",
    "NativeSessionRunner",
    "create_runner",
    "create_session",
    "prepare_version_launch",
]
