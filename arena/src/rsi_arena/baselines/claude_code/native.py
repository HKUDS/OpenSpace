"""Claude Code native session identity and explicit segment/resume commands."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional, Sequence

CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code"
CLAUDE_CODE_VERSION = "2.1.159"
CLAUDE_CODE_PACKAGE_IDENTITY = f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}"
CLAUDE_AIME_FAKE_NO_MODEL_READINESS = "CLAUDE_AIME_FAKE_NO_MODEL_WIRING_READY"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


def _identity(value: str, field: str) -> str:
    result = str(value)
    if not _ID.fullmatch(result):
        raise ValueError(f"invalid Claude {field}")
    return result


@dataclass
class ClaudeNativeSession:
    """Host-assigned explicit Claude session and monotonic segment identity."""

    run_id: str
    session_id: str
    cli_path: str
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    _next_segment_index: int = 0
    current_segment_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.run_id = _identity(self.run_id, "run_id")
        self.session_id = _identity(self.session_id, "session_id")
        if not isinstance(self.cli_path, str) or not self.cli_path:
            raise ValueError("Claude session requires a CLI executable reference")
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str)
            or not self.reasoning_effort.strip()
        ):
            raise ValueError("Claude reasoning effort must be a non-empty string")

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        cli_path: str,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> "ClaudeNativeSession":
        return cls(
            run_id=run_id,
            session_id=session_id or str(uuid.uuid4()),
            cli_path=cli_path,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def next_command(
        self,
        prompt: str,
        *,
        resume: bool,
        extra_args: Sequence[str] = (),
    ) -> tuple[str, list[str]]:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Claude segment prompt must be non-empty")
        if any(argument == "--continue" for argument in extra_args):
            raise ValueError("Claude native adapter forbids --continue")
        segment_id = f"{self.session_id}:segment-{self._next_segment_index:03d}"
        self._next_segment_index += 1
        self.current_segment_id = segment_id
        session_args = (
            ["--resume", self.session_id]
            if resume
            else ["--session-id", self.session_id]
        )
        command = [
            self.cli_path,
            *session_args,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self.model:
            command.extend(("--model", self.model))
        if self.reasoning_effort:
            command.extend(("--effort", self.reasoning_effort))
        command.extend(map(str, extra_args))
        return segment_id, command

    def restore_next_segment_index(self, next_segment_index: int) -> None:
        """Resume allocation at the first raw segment not durably observed."""

        if (
            isinstance(next_segment_index, bool)
            or not isinstance(next_segment_index, int)
            or next_segment_index < 0
        ):
            raise ValueError("Claude next segment index must be non-negative")
        if self.current_segment_id is not None:
            raise RuntimeError("cannot restore an active Claude native session")
        if self._next_segment_index not in {0, next_segment_index}:
            raise RuntimeError("Claude native segment allocation already advanced")
        self._next_segment_index = next_segment_index

    def child_context(self) -> dict[str, str]:
        if self.current_segment_id is None:
            raise RuntimeError("Claude segment has not been allocated")
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "segment_id": self.current_segment_id,
        }

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema": "rsi-arena-claude-native-identity",
            "package": CLAUDE_CODE_PACKAGE,
            "version": CLAUDE_CODE_VERSION,
            "package_identity": CLAUDE_CODE_PACKAGE_IDENTITY,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "resume_policy": "explicit_session_id",
            "event_schema_evidence": "synthetic_fake_no_model_only",
        }


__all__ = [
    "CLAUDE_AIME_FAKE_NO_MODEL_READINESS",
    "CLAUDE_CODE_PACKAGE_IDENTITY",
    "CLAUDE_CODE_VERSION",
    "ClaudeNativeSession",
]
