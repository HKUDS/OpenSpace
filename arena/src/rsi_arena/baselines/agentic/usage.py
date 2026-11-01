"""Per-call usage extraction from Codex and Claude Code stream logs.

EdgeBench never aggregated token accounting; we require one ledger row per
LLM call.  Claude Code ``--output-format stream-json`` carries a ``usage``
object on every assistant message and a cumulative ``result`` event with
``total_cost_usd``.  The pinned Codex CLI contributes stdout usage only from
``turn.completed``.  The exact one-shot smoke may separately accept a strictly
bound run-private session cumulative record as a lower bound.  A killed or
otherwise incomplete call is never estimated from prompt length.  Stdout
parsers are tolerant: an unparseable line is skipped, never fatal — accounting
must not kill a run.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from rsi_arena.common import UsageLedger
from rsi_arena.run_layout import RunValidationError, atomic_write_json, read_jsonl

CODEX_SESSION_CUMULATIVE_TOKEN_SOURCE = "codex_session_cumulative_token_count"


@dataclass(frozen=True)
class CallUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # None means the provider/CLI did not report cost.  It is not a zero.
    cost_usd: Optional[float] = None
    call_id: Optional[str] = None
    model: Optional[str] = None


@dataclass(frozen=True)
class ClaudeMessageUsage:
    """One authoritative Claude assistant-message usage leaf.

    Token fields remain optional independently.  Missing provider observations
    are never converted to zero, and ``message_id`` is the deduplication key.
    """

    message_id: str
    model: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cache_creation_input_tokens: Optional[int]
    cache_read_input_tokens: Optional[int]

    def as_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


@dataclass(frozen=True)
class ClaudeUsageParseResult:
    """Normalized facts from one already-durable native stream."""

    messages: tuple[ClaudeMessageUsage, ...]
    total_cost_usd: Optional[float]
    cost_status: str
    session_id: Optional[str]
    malformed_line_count: int
    duplicate_message_count: int
    result_usage_policy: str = (
        "summary_tokens_not_counted;session_cumulative_cost_delta_leaf"
    )

    @property
    def parse_status(self) -> str:
        if self.malformed_line_count:
            return (
                "partial"
                if self.messages or self.total_cost_usd is not None
                else "malformed"
            )
        if self.messages or self.total_cost_usd is not None:
            return "observed"
        return "unobservable"

    def derived_totals(self) -> dict[str, object]:
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        totals: dict[str, object] = {}
        for name in fields:
            values = [getattr(message, name) for message in self.messages]
            if values and all(value is not None for value in values):
                totals[name] = sum(int(value) for value in values if value is not None)
                totals[f"{name}_status"] = "reported"
            else:
                totals[name] = None
                totals[f"{name}_status"] = "missing"
        totals["total_cost_usd"] = self.total_cost_usd
        totals["cost_status"] = self.cost_status
        return totals


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_nonnegative_int(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _optional_nonnegative_float(value: object) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0.0 else None


def parse_claude_usage(
    lines: Iterable[str],
    *,
    seen_message_ids: Optional[set[str]] = None,
) -> ClaudeUsageParseResult:
    """Parse Claude stream JSON without treating summaries as extra calls.

    Assistant ``message.id`` leaves have priority.  ``result.usage`` and
    ``result.modelUsage`` are cumulative/summary views and are deliberately not
    ledgered alongside those leaves.  ``total_cost_usd`` is retained once as a
    segment-level cost observation.  Assistant usage without its authoritative
    message identity is malformed and is not fabricated into a ledger leaf.
    """

    external_seen = seen_message_ids if seen_message_ids is not None else set()
    local_seen: set[str] = set()
    messages: list[ClaudeMessageUsage] = []
    total_cost_usd: Optional[float] = None
    session_id: Optional[str] = None
    malformed = duplicates = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        candidate_session = event.get("session_id")
        if isinstance(candidate_session, str) and candidate_session:
            if session_id is None:
                session_id = candidate_session
            elif session_id != candidate_session:
                malformed += 1
        if event.get("type") == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                malformed += 1
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            raw_id = message.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                malformed += 1
                continue
            message_id = raw_id
            if message_id in local_seen or message_id in external_seen:
                duplicates += 1
                continue
            local_seen.add(message_id)
            external_seen.add(message_id)
            model = message.get("model")
            messages.append(
                ClaudeMessageUsage(
                    message_id=message_id,
                    model=(str(model) if isinstance(model, str) and model else None),
                    input_tokens=_optional_nonnegative_int(usage.get("input_tokens")),
                    output_tokens=_optional_nonnegative_int(usage.get("output_tokens")),
                    cache_creation_input_tokens=_optional_nonnegative_int(
                        usage.get("cache_creation_input_tokens")
                    ),
                    cache_read_input_tokens=_optional_nonnegative_int(
                        usage.get("cache_read_input_tokens")
                    ),
                )
            )
        elif event.get("type") == "result":
            cost = event.get("total_cost_usd")
            if (
                isinstance(cost, (int, float))
                and not isinstance(cost, bool)
                and math.isfinite(float(cost))
                and float(cost) >= 0.0
            ):
                total_cost_usd = float(cost)
    return ClaudeUsageParseResult(
        messages=tuple(messages),
        total_cost_usd=total_cost_usd,
        cost_status="reported" if total_cost_usd is not None else "missing",
        session_id=session_id,
        malformed_line_count=malformed,
        duplicate_message_count=duplicates,
    )


class ClaudeUsageAccumulator:
    """Write Claude usage leaves once and keep segment/run totals as views."""

    def __init__(
        self,
        *,
        ledger: UsageLedger,
        run_id: str,
        session_id: str,
        summary_path: Path,
        default_model: str,
        provider: str,
    ) -> None:
        if not run_id or not session_id:
            raise ValueError("Claude usage accounting requires run/session identity")
        self._ledger = ledger
        self._run_id = str(run_id)
        self._session_id = str(session_id)
        self._summary_path = Path(summary_path)
        self._default_model = str(default_model)
        self._provider = str(provider)
        self._seen_message_ids: set[str] = set()
        self._known_call_rows: dict[str, dict[str, object]] = {}
        self._segments: list[dict[str, object]] = []
        self._last_session_cumulative_cost: Optional[float] = None
        self._restore_durable_state()

    @property
    def seen_message_ids(self) -> frozenset[str]:
        return frozenset(self._seen_message_ids)

    @property
    def next_segment_index(self) -> int:
        prefix = f"{self._session_id}:segment-"
        indexes: list[int] = []
        for row in self._known_call_rows.values():
            segment_id = row.get("segment_id")
            if isinstance(segment_id, str) and segment_id.startswith(prefix):
                suffix = segment_id.removeprefix(prefix)
                if suffix.isdecimal():
                    indexes.append(int(suffix))
        for segment in self._segments:
            segment_id = segment.get("segment_id")
            if isinstance(segment_id, str) and segment_id.startswith(prefix):
                suffix = segment_id.removeprefix(prefix)
                if suffix.isdecimal():
                    indexes.append(int(suffix))
        return max(indexes, default=-1) + 1

    def _restore_durable_state(self) -> None:
        """Rebuild idempotence and cumulative state before a native resume."""

        self._restore_derived_view()
        latest_cost: Optional[float] = None
        for row in read_jsonl(self._ledger.path):
            if row.get("run_id") != self._run_id:
                raise RunValidationError("Claude usage ledger run binding is invalid")
            call_id = row.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise RunValidationError("usage ledger call identity is missing")
            if call_id in self._known_call_rows:
                raise RunValidationError("usage ledger call identity is not unique")
            self._known_call_rows[call_id] = dict(row)
            if row.get("session_id") != self._session_id:
                continue
            scope = row.get("usage_scope")
            if scope == "message_leaf":
                message_id = row.get("message_id")
                if (
                    not isinstance(message_id, str)
                    or not message_id
                    or call_id != message_id
                    or not isinstance(row.get("segment_id"), str)
                ):
                    raise RunValidationError(
                        "Claude message usage identity is malformed"
                    )
                self._seen_message_ids.add(message_id)
            if scope in {
                "session_cumulative_cost_delta_leaf",
                "session_cumulative_cost_observation",
            }:
                observed = _optional_nonnegative_float(
                    row.get("provider_reported_cost")
                )
                if observed is None:
                    raise RunValidationError(
                        "Claude cumulative cost observation is malformed"
                    )
                latest_cost = observed
        if latest_cost is not None:
            self._last_session_cumulative_cost = latest_cost
        for segment in self._segments:
            messages = segment.get("messages")
            if not isinstance(messages, list):
                raise RunValidationError("Claude derived segment is malformed")
            for message in messages:
                if not isinstance(message, Mapping):
                    raise RunValidationError("Claude derived message is malformed")
                message_id = message.get("message_id")
                if message_id not in self._seen_message_ids:
                    raise RunValidationError(
                        "Claude derived usage is ahead of its durable ledger"
                    )

    def _restore_derived_view(self) -> None:
        if not self._summary_path.exists():
            return
        try:
            payload = json.loads(self._summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RunValidationError("Claude derived usage is malformed") from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != "rsi-arena-claude-run-usage"
            or payload.get("run_id") != self._run_id
            or payload.get("session_id") != self._session_id
            or payload.get("ledger_policy")
            != "message_token_leaves_plus_one_segment_cost_leaf"
            or set(payload)
            != {
                "schema",
                "run_id",
                "session_id",
                "ledger_policy",
                "segments",
                "derived_run_cumulative",
            }
        ):
            raise RunValidationError("Claude derived usage binding is invalid")
        raw_segments = payload.get("segments")
        cumulative = payload.get("derived_run_cumulative")
        if not isinstance(raw_segments, list) or not isinstance(cumulative, Mapping):
            raise RunValidationError("Claude derived usage is malformed")
        seen_segments: set[str] = set()
        restored: list[dict[str, object]] = []
        for raw_segment in raw_segments:
            if (
                not isinstance(raw_segment, Mapping)
                or raw_segment.get("schema") != "rsi-arena-claude-segment-usage"
                or raw_segment.get("run_id") != self._run_id
                or raw_segment.get("session_id") != self._session_id
                or not isinstance(raw_segment.get("segment_id"), str)
                or not isinstance(raw_segment.get("messages"), list)
                or not isinstance(raw_segment.get("derived_totals"), Mapping)
            ):
                raise RunValidationError("Claude derived segment is malformed")
            for message in raw_segment["messages"]:
                if (
                    not isinstance(message, Mapping)
                    or not isinstance(message.get("message_id"), str)
                    or not message.get("message_id")
                ):
                    raise RunValidationError("Claude derived message is malformed")
            segment_id = str(raw_segment["segment_id"])
            if not segment_id or segment_id in seen_segments:
                raise RunValidationError("Claude derived segment identity is invalid")
            seen_segments.add(segment_id)
            restored.append(dict(raw_segment))
        self._segments = restored
        if cumulative.get("cost_status") == "reported":
            cost = _optional_nonnegative_float(cumulative.get("total_cost_usd"))
            if cost is None:
                raise RunValidationError("Claude derived cumulative cost is malformed")
            self._last_session_cumulative_cost = cost

    def _remember_record(self, row: object) -> None:
        payload = asdict(row)  # UsageRow, kept indirect to avoid a public dependency.
        call_id = payload.get("call_id")
        assert isinstance(call_id, str) and call_id
        self._known_call_rows[call_id] = payload

    def _existing_bound_call(
        self,
        call_id: str,
        *,
        segment_id: str,
        usage_scope: str,
    ) -> Optional[dict[str, object]]:
        row = self._known_call_rows.get(call_id)
        if row is None:
            return None
        if (
            row.get("run_id") != self._run_id
            or row.get("session_id") != self._session_id
            or row.get("segment_id") != segment_id
            or row.get("usage_scope") != usage_scope
        ):
            raise RunValidationError("Claude usage call identity changed on replay")
        return row

    def record_segment(self, raw_path: Path, *, segment_id: str) -> dict[str, object]:
        """Parse one durable raw file and ledger only message/cost leaves."""

        raw_path = Path(raw_path)
        full_parse = parse_claude_usage(
            raw_path.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        if (
            full_parse.session_id is not None
            and full_parse.session_id != self._session_id
        ):
            raise RunValidationError("Claude raw stream session binding is invalid")
        raw_message_ids = {message.message_id for message in full_parse.messages}
        durable_segment_message_ids = {
            str(row["message_id"])
            for row in self._known_call_rows.values()
            if row.get("session_id") == self._session_id
            and row.get("segment_id") == segment_id
            and row.get("usage_scope") == "message_leaf"
        }
        if not durable_segment_message_ids.issubset(raw_message_ids):
            raise RunValidationError("Claude raw segment changed after usage append")
        segment_messages: list[ClaudeMessageUsage] = []
        new_messages: list[ClaudeMessageUsage] = []
        prior_message_count = 0
        for message in full_parse.messages:
            existing = self._known_call_rows.get(message.message_id)
            if message.message_id in self._seen_message_ids:
                prior_message_count += 1
                if (
                    existing is None
                    or existing.get("session_id") != self._session_id
                    or existing.get("segment_id") != segment_id
                    or existing.get("usage_scope") != "message_leaf"
                    or existing.get("message_id") != message.message_id
                    or existing.get("call_id") != message.message_id
                ):
                    # Resumed streams may repeat older session history. It is
                    # excluded from this segment, but the globally unique leaf
                    # remains bound to its original segment.
                    continue
                expected_tokens = {
                    "tokens_in": message.input_tokens,
                    "tokens_out": message.output_tokens,
                    "cache_write_tokens": message.cache_creation_input_tokens,
                    "cache_read_tokens": message.cache_read_input_tokens,
                }
                if any(
                    existing.get(field) != value
                    for field, value in expected_tokens.items()
                ):
                    raise RunValidationError(
                        "Claude message usage changed on segment replay"
                    )
                segment_messages.append(message)
                continue
            if existing is not None:
                raise RunValidationError("Claude message call identity is not unique")
            segment_messages.append(message)
            new_messages.append(message)
        parsed = ClaudeUsageParseResult(
            messages=tuple(segment_messages),
            total_cost_usd=full_parse.total_cost_usd,
            cost_status=full_parse.cost_status,
            session_id=full_parse.session_id,
            malformed_line_count=full_parse.malformed_line_count,
            duplicate_message_count=(
                full_parse.duplicate_message_count + prior_message_count
            ),
        )
        identity_status = (
            "reported"
            if parsed.session_id == self._session_id
            else "missing"
            if parsed.session_id is None
            else "mismatch"
        )
        for message in new_messages:
            token_fields: dict[str, object] = {}
            statuses = {
                "input_token_status": (
                    "reported" if message.input_tokens is not None else "missing"
                ),
                "output_token_status": (
                    "reported" if message.output_tokens is not None else "missing"
                ),
                "cache_token_status": (
                    "reported"
                    if message.cache_creation_input_tokens is not None
                    and message.cache_read_input_tokens is not None
                    else "missing"
                ),
            }
            if message.input_tokens is not None:
                token_fields["tokens_in"] = message.input_tokens
            if message.output_tokens is not None:
                token_fields["tokens_out"] = message.output_tokens
            if message.cache_creation_input_tokens is not None:
                token_fields["cache_write_tokens"] = message.cache_creation_input_tokens
            if message.cache_read_input_tokens is not None:
                token_fields["cache_read_tokens"] = message.cache_read_input_tokens
            primary_reported = (
                message.input_tokens is not None and message.output_tokens is not None
            )
            row = self._ledger.record(
                "solver-agent",
                model=message.model or self._default_model,
                provider=self._provider,
                call_id=message.message_id,
                session_id=self._session_id,
                segment_id=segment_id,
                message_id=message.message_id,
                token_source="cli",
                token_status="reported" if primary_reported else "unverified",
                cost_usd=None,
                provider_reported_cost=None,
                cost_source="unknown",
                cost_status="missing",
                cache_status=(
                    "hit"
                    if (message.cache_read_input_tokens or 0) > 0
                    else "miss"
                    if message.cache_read_input_tokens == 0
                    else "unknown"
                ),
                usage_scope="message_leaf",
                **statuses,
                **token_fields,
            )
            self._remember_record(row)
            self._seen_message_ids.add(message.message_id)
        derived_totals = parsed.derived_totals()
        if parsed.total_cost_usd is not None:
            cumulative_cost = parsed.total_cost_usd
            delta_call_id = f"{segment_id}:session-cost-delta"
            observation_call_id = f"{segment_id}:session-cost-observation"
            existing_delta = self._existing_bound_call(
                delta_call_id,
                segment_id=segment_id,
                usage_scope="session_cumulative_cost_delta_leaf",
            )
            existing_observation = self._existing_bound_call(
                observation_call_id,
                segment_id=segment_id,
                usage_scope="session_cumulative_cost_observation",
            )
            if existing_delta is not None and existing_observation is not None:
                raise RunValidationError("Claude segment has conflicting cost leaves")
            existing_cost = existing_delta or existing_observation
            if existing_cost is not None:
                observed = _optional_nonnegative_float(
                    existing_cost.get("provider_reported_cost")
                )
                if observed != cumulative_cost:
                    raise RunValidationError(
                        "Claude cumulative cost changed on segment replay"
                    )
                delta_cost = _optional_nonnegative_float(existing_cost.get("cost_usd"))
                delta_status = str(existing_cost.get("cost_status"))
            else:
                previous_cost = self._last_session_cumulative_cost
                if previous_cost is None:
                    delta_cost = cumulative_cost
                    delta_status = "reported"
                elif cumulative_cost >= previous_cost:
                    delta_cost = cumulative_cost - previous_cost
                    delta_status = "reported"
                else:
                    # A decreasing session cumulative counter is not safely
                    # attributable. Preserve the provider observation and resume
                    # deltas from this new baseline without inventing spend.
                    delta_cost = None
                    delta_status = "unverified"
            self._last_session_cumulative_cost = cumulative_cost
            if delta_cost is not None and existing_cost is None:
                row = self._ledger.record(
                    "solver-agent",
                    model=self._default_model,
                    provider=self._provider,
                    call_id=delta_call_id,
                    session_id=self._session_id,
                    segment_id=segment_id,
                    token_source="unknown",
                    token_status="missing",
                    cost_usd=delta_cost,
                    provider_reported_cost=cumulative_cost,
                    cost_source="cli",
                    cost_status="reported",
                    cache_status="unknown",
                    usage_scope="session_cumulative_cost_delta_leaf",
                )
                self._remember_record(row)
            elif delta_status == "unverified" and existing_cost is None:
                row = self._ledger.record(
                    "solver-agent",
                    model=self._default_model,
                    provider=self._provider,
                    call_id=observation_call_id,
                    session_id=self._session_id,
                    segment_id=segment_id,
                    token_source="unknown",
                    token_status="missing",
                    cost_usd=None,
                    provider_reported_cost=cumulative_cost,
                    cost_source="cli",
                    cost_status="unverified",
                    cache_status="unknown",
                    usage_scope="session_cumulative_cost_observation",
                )
                self._remember_record(row)
            derived_totals.update(
                {
                    "total_cost_usd": delta_cost,
                    "cost_status": delta_status,
                    "session_cumulative_total_cost_usd": cumulative_cost,
                }
            )
        elif not parsed.messages:
            # Persist the observation state without fabricating token/cost zeros.
            call_id = f"{segment_id}:unobservable"
            existing = self._existing_bound_call(
                call_id,
                segment_id=segment_id,
                usage_scope="segment_observation",
            )
            if existing is None:
                row = self._ledger.record(
                    "solver-agent",
                    model=self._default_model,
                    provider=self._provider,
                    call_id=call_id,
                    session_id=self._session_id,
                    segment_id=segment_id,
                    token_source="unknown",
                    token_status="missing",
                    cost_usd=None,
                    provider_reported_cost=None,
                    cost_source="unknown",
                    cost_status="missing",
                    cache_status="unknown",
                    usage_scope="segment_observation",
                )
                self._remember_record(row)
            derived_totals["session_cumulative_total_cost_usd"] = None
        else:
            derived_totals["session_cumulative_total_cost_usd"] = None
        segment = {
            "schema": "rsi-arena-claude-segment-usage",
            "run_id": self._run_id,
            "session_id": self._session_id,
            "segment_id": str(segment_id),
            "raw_stream_path": str(raw_path),
            "raw_stream_bytes": raw_path.stat().st_size,
            "parse_status": parsed.parse_status,
            "session_identity_status": identity_status,
            "malformed_line_count": parsed.malformed_line_count,
            "duplicate_message_count": parsed.duplicate_message_count,
            "result_usage_policy": parsed.result_usage_policy,
            "messages": [message.as_dict() for message in parsed.messages],
            "derived_totals": derived_totals,
        }
        for index, existing_segment in enumerate(self._segments):
            if existing_segment.get("segment_id") == segment_id:
                self._segments[index] = segment
                break
        else:
            self._segments.append(segment)
        self._write_derived_view()
        return segment

    def _write_derived_view(self) -> None:
        token_fields = (
            ("input_tokens", "tokens_in"),
            ("output_tokens", "tokens_out"),
            ("cache_creation_input_tokens", "cache_write_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
        )
        message_rows = [
            row
            for row in self._known_call_rows.values()
            if row.get("session_id") == self._session_id
            and row.get("usage_scope") == "message_leaf"
        ]
        cumulative: dict[str, object] = {}
        for name, ledger_name in token_fields:
            values = [row.get(ledger_name) for row in message_rows]
            if values and all(value is not None for value in values):
                cumulative[name] = sum(int(value) for value in values)
                cumulative[f"{name}_status"] = "reported"
            else:
                cumulative[name] = None
                cumulative[f"{name}_status"] = "missing"
        cost_statuses = [
            str(segment["derived_totals"]["cost_status"])  # type: ignore[index]
            for segment in self._segments
        ]
        cumulative_observations = [
            segment["derived_totals"].get(  # type: ignore[union-attr]
                "session_cumulative_total_cost_usd"
            )
            for segment in self._segments
        ]
        if cost_statuses and all(status == "reported" for status in cost_statuses):
            cumulative["total_cost_usd"] = cumulative_observations[-1]
            cumulative["cost_status"] = "reported"
        else:
            cumulative["total_cost_usd"] = None
            cumulative["cost_status"] = (
                "unverified" if "unverified" in cost_statuses else "missing"
            )
        payload = {
            "schema": "rsi-arena-claude-run-usage",
            "run_id": self._run_id,
            "session_id": self._session_id,
            "ledger_policy": "message_token_leaves_plus_one_segment_cost_leaf",
            "segments": self._segments,
            "derived_run_cumulative": cumulative,
        }
        atomic_write_json(self._summary_path, payload)


def iter_codex_usage(
    lines: Iterator[str],
) -> Iterator[CallUsage]:
    """Yield only completed-turn usage from pinned Codex ``exec --json``.

    ``token_count`` diagnostics are intentionally ignored.  They may describe
    a partial turn and differ across Codex surfaces, so deriving deltas would
    turn an incomplete request into an apparently measured model call.  The
    runner may separately validate the final session-cumulative checkpoint as
    one unverified, non-additive lower-bound row when there are no exact
    completed-turn rows.
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage") or {}
        if not isinstance(usage, dict) or not usage:
            continue
        call = CallUsage(
            tokens_in=_int(usage.get("input_tokens")),
            # Codex reports reasoning_output_tokens as a diagnostic subset of
            # output_tokens, not as an additional billable bucket.
            tokens_out=_int(usage.get("output_tokens")),
            cache_read_tokens=_int(usage.get("cached_input_tokens")),
        )
        if call.tokens_in or call.tokens_out or call.cache_read_tokens:
            yield call


def codex_session_cumulative_usage_audit(
    cli_home: Path,
    *,
    run_id: str,
    expected_cwd: str,
    expected_model: str,
    run_started_at: object,
    run_ended_at: object,
) -> dict[str, object]:
    """Validate one run-private rollout and return final cumulative usage."""

    audit: dict[str, object] = {
        "schema": "rsi-arena-codex-session-usage",
        "run_id": str(run_id),
        "run_started_at": run_started_at,
        "run_ended_at": run_ended_at,
        "status": "missing",
        "invalid_reason": None,
        "session_id": None,
        "source": CODEX_SESSION_CUMULATIVE_TOKEN_SOURCE,
        "provenance": CODEX_SESSION_CUMULATIVE_TOKEN_SOURCE,
        "coverage": "unobservable",
        "terminal_incomplete_call_excluded": True,
        "source_rollout_relative_path": None,
        "source_rollout_absolute_path": None,
        "source_rollout_sha256": None,
        "final_record_timestamp": None,
        "token_count_event_count": 0,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "cost_status": "missing",
    }

    def invalid(reason: str) -> dict[str, object]:
        audit["status"] = "invalid"
        audit["invalid_reason"] = reason
        return audit

    if (
        not isinstance(run_started_at, (int, float))
        or isinstance(run_started_at, bool)
        or not math.isfinite(run_started_at)
        or not isinstance(run_ended_at, (int, float))
        or isinstance(run_ended_at, bool)
        or not math.isfinite(run_ended_at)
        or run_started_at > run_ended_at
    ):
        return invalid("run_time_window_invalid")

    home = Path(cli_home)
    sessions = home / "sessions"
    if not sessions.is_dir() or sessions.is_symlink():
        audit["invalid_reason"] = "sessions_directory_missing"
        return audit
    entries = list(sessions.rglob("*"))
    if any(entry.is_symlink() for entry in entries):
        return invalid("sessions_tree_contains_symlink")
    rollouts = sorted(
        entry
        for entry in entries
        if entry.name.startswith("rollout-") and entry.suffix == ".jsonl"
    )
    if len(rollouts) != 1:
        return invalid(f"expected_one_session_rollout_found_{len(rollouts)}")
    rollout = rollouts[0]
    try:
        entry = rollout.lstat()
        home_resolved = home.resolve(strict=True)
        rollout_resolved = rollout.resolve(strict=True)
    except OSError:
        return invalid("session_rollout_unreadable")
    if not stat.S_ISREG(entry.st_mode) or rollout.is_symlink():
        return invalid("session_rollout_not_regular_file")
    if not rollout_resolved.is_relative_to(home_resolved):
        return invalid("session_rollout_escapes_cli_home")

    audit["source_rollout_relative_path"] = str(rollout.relative_to(home))
    audit["source_rollout_absolute_path"] = str(rollout_resolved)
    try:
        raw_rollout = rollout.read_bytes()
        lines = raw_rollout.decode("utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError):
        return invalid("session_rollout_unreadable")
    audit["source_rollout_sha256"] = "sha256:" + hashlib.sha256(raw_rollout).hexdigest()

    session_meta: list[tuple[datetime, dict]] = []
    turn_contexts: list[tuple[datetime, dict]] = []
    token_counts: list[tuple[datetime, str, dict[str, int]]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return invalid(f"malformed_json_line_{line_number}")
        if not isinstance(event, dict):
            return invalid(f"non_object_event_line_{line_number}")
        event_type = event.get("type")
        payload = event.get("payload")
        if event_type == "session_meta":
            if not isinstance(payload, dict):
                return invalid("session_meta_payload_invalid")
            timestamp = _codex_session_timestamp(event.get("timestamp"))
            if timestamp is None:
                return invalid("session_meta_timestamp_invalid")
            session_meta.append((timestamp, payload))
        elif event_type == "turn_context":
            if not isinstance(payload, dict):
                return invalid("turn_context_payload_invalid")
            timestamp = _codex_session_timestamp(event.get("timestamp"))
            if timestamp is None:
                return invalid("turn_context_timestamp_invalid")
            turn_contexts.append((timestamp, payload))
        elif (
            event_type == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "token_count"
        ):
            timestamp = event.get("timestamp")
            parsed_timestamp = _codex_session_timestamp(timestamp)
            if parsed_timestamp is None:
                return invalid("token_count_timestamp_invalid")
            info = payload.get("info")
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            parsed_usage = _codex_cumulative_token_fields(usage)
            if parsed_usage is None:
                return invalid("token_count_usage_invalid")
            token_counts.append((parsed_timestamp, str(timestamp), parsed_usage))

    if len(session_meta) != 1:
        return invalid(f"expected_one_session_meta_found_{len(session_meta)}")
    session_timestamp, metadata = session_meta[0]
    if not run_started_at <= session_timestamp.timestamp() <= run_ended_at:
        return invalid("session_timestamp_outside_run_window")
    session_id = metadata.get("session_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or metadata.get("id") != session_id
        or session_id not in rollout.name
    ):
        return invalid("session_identity_mismatch")
    if (
        metadata.get("source") != "exec"
        or metadata.get("originator") != "codex_exec"
        or metadata.get("cwd") != expected_cwd
    ):
        return invalid("session_metadata_binding_mismatch")
    if len(turn_contexts) != 1:
        return invalid(f"expected_one_turn_context_found_{len(turn_contexts)}")
    context_timestamp, context = turn_contexts[0]
    if not run_started_at <= context_timestamp.timestamp() <= run_ended_at:
        return invalid("turn_context_timestamp_outside_run_window")
    if context.get("cwd") != expected_cwd or context.get("model") != expected_model:
        return invalid("turn_context_binding_mismatch")
    if not token_counts:
        audit["session_id"] = session_id
        audit["invalid_reason"] = "completed_token_count_missing"
        return audit

    previous_timestamp: datetime | None = None
    previous_usage: dict[str, int] | None = None
    for timestamp, _rendered, usage in token_counts:
        if not run_started_at <= timestamp.timestamp() <= run_ended_at:
            return invalid("token_count_timestamp_outside_run_window")
        if previous_timestamp is not None and timestamp < previous_timestamp:
            return invalid("token_count_timestamp_nonmonotonic")
        if previous_usage is not None and any(
            usage[name] < previous_usage[name]
            for name in _CODEX_CUMULATIVE_TOKEN_FIELDS
        ):
            return invalid("token_count_usage_nonmonotonic")
        previous_timestamp = timestamp
        previous_usage = usage

    _timestamp, rendered_timestamp, final_usage = token_counts[-1]
    audit.update(
        {
            "status": "observed_lower_bound",
            "invalid_reason": None,
            "session_id": session_id,
            "coverage": "observed_lower_bound",
            "final_record_timestamp": rendered_timestamp,
            "token_count_event_count": len(token_counts),
            **final_usage,
        }
    )
    return audit


_CODEX_CUMULATIVE_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _codex_cumulative_token_fields(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for name in _CODEX_CUMULATIVE_TOKEN_FIELDS:
        field = value.get(name)
        if not isinstance(field, int) or isinstance(field, bool) or field < 0:
            return None
        result[name] = field
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        return None
    if result["cached_input_tokens"] > result["input_tokens"]:
        return None
    if result["reasoning_output_tokens"] > result["output_tokens"]:
        return None
    return result


def _codex_session_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
