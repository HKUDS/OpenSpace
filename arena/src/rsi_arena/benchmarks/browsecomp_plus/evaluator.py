"""Injected, side-effect-free BrowseComp-Plus task evaluator."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .contracts import TrustedTask
from .retrieval import RetrievalTrajectory
from .usage import ModelUsage

_JUDGE_PROMPT_TEMPLATE = """
Judge whether the following [response] to [question] is correct or not based on
the precise and unambiguous [correct_answer] below.

[question]: {question}
[response]: {response}
[correct_answer]: {correct_answer}

Return these fields.  The final correctness field must be exactly `correct: yes`
or `correct: no` on its own line.

extracted_final_answer: The exact answer extracted from [response].
reasoning: Explain only whether that answer is semantically equivalent to
[correct_answer], allowing harmless string variations and a small numerical
margin where appropriate.
correct: yes or no
confidence: The confidence percentage extracted from [response].
""".strip()

_FINAL_ANSWER_PATTERN = re.compile(
    r"\A\s*Explanation:\s*(?P<explanation>.+?)\r?\n"
    r"Exact Answer:[ \t]*(?P<answer>[^\r\n]+)\r?\n"
    r"Confidence:[ \t]*(?P<confidence>(?:100(?:\.0+)?|\d{1,2}(?:\.\d+)?))%\s*\Z",
    re.DOTALL,
)
_BRACKET_PATTERN = re.compile(r"\[([^\[\]]+)\]")
_FULLWIDTH_CITATION_PATTERN = re.compile(r"cite([^]+)")
_EVALUATION_SIGNALS = frozenset(
    {
        "candidate_incomplete",
        "invalid_final_format",
        "judged_incorrect",
        "judged_correct",
    }
)


@dataclass(frozen=True)
class ParsedFinalAnswer:
    explanation: str
    exact_answer: str
    confidence: float
    cited_docids: tuple[str, ...]


@dataclass(frozen=True)
class JudgeRequest:
    """Trusted in-memory request; it must never be placed in a public artifact."""

    prompt: str


@dataclass(frozen=True)
class JudgeResponse:
    """Sanitized judge terminal returned by a trusted router.

    ``retry_count`` is the number of router-level retries actually performed,
    not the configured attempt limit; retries internal to a provider SDK are
    not observable here. ``http_status`` is present only when the router
    observed an HTTP response status. ``text`` is the model's message content,
    never a provider response object.
    """

    text: str | None
    terminal_reason: str
    http_status: int | None
    retry_count: int
    usage: ModelUsage | None = None

    def __post_init__(self) -> None:
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("judge response text must be a string or null")
        if not isinstance(self.terminal_reason, str) or not self.terminal_reason:
            raise ValueError("judge terminal reason must be non-empty")
        if self.http_status is not None and (
            isinstance(self.http_status, bool) or not isinstance(self.http_status, int)
        ):
            raise TypeError("judge HTTP status must be an integer or null")
        if (
            isinstance(self.retry_count, bool)
            or not isinstance(self.retry_count, int)
            or self.retry_count < 0
        ):
            raise ValueError("judge retry count must be a non-negative integer")
        if self.usage is not None and not isinstance(self.usage, ModelUsage):
            raise TypeError("judge usage must be ModelUsage or null")
        if self.usage is not None and self.usage.role != "verifier/judge":
            raise ValueError("judge usage has the wrong role")


class Judge(Protocol):
    def __call__(self, request: JudgeRequest) -> object: ...


def _normalize_judge_response(value: object) -> JudgeResponse:
    """Normalize the single operator-authored judge callable boundary."""

    if isinstance(value, JudgeResponse):
        return value
    if isinstance(value, str):
        return JudgeResponse(value, "completed", None, 0)
    if isinstance(value, Mapping) and set(value) in (
        {
            "text",
            "terminal_reason",
            "http_status",
            "retry_count",
        },
        {
            "text",
            "terminal_reason",
            "http_status",
            "retry_count",
            "usage",
        },
    ):
        return JudgeResponse(
            text=value["text"],  # type: ignore[arg-type]
            terminal_reason=value["terminal_reason"],  # type: ignore[arg-type]
            http_status=value["http_status"],  # type: ignore[arg-type]
            retry_count=value["retry_count"],  # type: ignore[arg-type]
            usage=ModelUsage.from_mapping(value.get("usage")),
        )
    raise TypeError("judge returned an invalid terminal")


@dataclass(frozen=True)
class TaskEvaluation:
    """Trusted evaluation result with explicitly sanitized public serialization."""

    outcome: str
    score: int | None
    correctness: int | None
    retrieval_recall: float
    citation_precision: float
    citation_recall: float
    tool_call_count: int
    evaluation_signal: str | None
    judge: JudgeResponse = field(repr=False)

    def __post_init__(self) -> None:
        if self.outcome not in {"measured", "inconclusive"}:
            raise ValueError("task evaluation outcome is invalid")
        if self.outcome == "measured":
            if self.score not in {0, 1} or self.correctness != self.score:
                raise ValueError(
                    "measured correctness/score must be the same binary value"
                )
            if (
                not isinstance(self.evaluation_signal, str)
                or self.evaluation_signal not in _EVALUATION_SIGNALS
            ):
                raise ValueError("measured evaluation signal is invalid")
            expected_score = int(self.evaluation_signal == "judged_correct")
            if self.score != expected_score:
                raise ValueError("measured evaluation signal disagrees with score")
        elif self.score is not None or self.correctness is not None:
            raise ValueError("inconclusive evaluation must not carry a score")
        elif self.evaluation_signal is not None:
            raise ValueError("inconclusive evaluation must not carry a signal")
        for field_name in (
            "retrieval_recall",
            "citation_precision",
            "citation_recall",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, float) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{field_name} must be a float in [0, 1]")
        if (
            isinstance(self.tool_call_count, bool)
            or not isinstance(self.tool_call_count, int)
            or self.tool_call_count < 0
        ):
            raise ValueError("tool_call_count must be a non-negative integer")
        if not isinstance(self.judge, JudgeResponse):
            raise TypeError("task evaluation judge terminal is invalid")

    def development_judge_diagnostics(self) -> dict[str, object]:
        """Return the structured diagnostics approved for development."""

        return {
            "terminal_reason": self.judge.terminal_reason,
            "http_status": self.judge.http_status,
            "retry_count": self.judge.retry_count,
        }

    def as_public_dict(self) -> dict[str, object]:
        public: dict[str, object] = {
            "schema": "browsecomp-plus-task-evaluation",
            "outcome": self.outcome,
            "score": self.score,
            "metrics": {
                "tool_call_count": self.tool_call_count,
            },
        }
        if self.evaluation_signal is not None:
            public["evaluation_signal"] = self.evaluation_signal
        return public


def extract_citations(response_text: str) -> tuple[str, ...]:
    """Extract official numeric ``[docid]`` citation groups, once each."""

    if not isinstance(response_text, str) or not response_text:
        return ()
    seen: dict[str, None] = {}
    groups = _BRACKET_PATTERN.findall(response_text)
    groups.extend(_FULLWIDTH_CITATION_PATTERN.findall(response_text))
    for group in groups:
        if re.fullmatch(r"\s*\d+(?:\s*,\s*\d+)*\s*", group) is None:
            continue
        for docid in re.findall(r"\d+", group):
            seen.setdefault(docid, None)
    return tuple(seen)


def parse_final_answer(response_text: object) -> ParsedFinalAnswer | None:
    """Parse the required Explanation/Exact Answer/Confidence terminal format."""

    if not isinstance(response_text, str) or not response_text.strip():
        return None
    match = _FINAL_ANSWER_PATTERN.fullmatch(response_text)
    if match is None:
        return None
    explanation = match.group("explanation").strip()
    exact_answer = match.group("answer").strip()
    if not explanation or not exact_answer:
        return None
    confidence = float(match.group("confidence"))
    if not 0.0 <= confidence <= 100.0:  # defensive; regex already enforces this
        return None
    return ParsedFinalAnswer(
        explanation=explanation,
        exact_answer=exact_answer,
        confidence=confidence,
        cited_docids=extract_citations(response_text),
    )


def parse_judge_response(response_text: object) -> bool | None:
    """Accept exactly one explicit, standalone ``correct: yes|no`` field."""

    if not isinstance(response_text, str) or not response_text.strip():
        return None
    verdicts: list[bool] = []
    for line in response_text.splitlines():
        normalized = line.strip().replace("**", "")
        match = re.fullmatch(r"correct\s*:\s*(yes|no)", normalized, re.IGNORECASE)
        if match is not None:
            verdicts.append(match.group(1).lower() == "yes")
    return verdicts[0] if len(verdicts) == 1 else None


def render_judge_prompt(task: TrustedTask, response: str) -> str:
    if not isinstance(task, TrustedTask):
        raise TypeError("task must be a TrustedTask")
    if not isinstance(response, str) or not response:
        raise ValueError("response must be non-empty")
    return _JUDGE_PROMPT_TEMPLATE.format(
        question=task.query,
        response=response,
        correct_answer=task.answer,
    )


def _last_output(candidate: object) -> tuple[bool, str | None]:
    if not isinstance(candidate, Mapping):
        return False, None
    completed = candidate.get("status") == "completed"
    result = candidate.get("result")
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)) and result:
        last = result[-1]
        if (
            isinstance(last, Mapping)
            and last.get("type") == "output_text"
            and isinstance(last.get("output"), str)
        ):
            return completed, last["output"]
    return completed, None


def _candidate_trajectory(candidate: object) -> RetrievalTrajectory:
    if not isinstance(candidate, Mapping):
        return RetrievalTrajectory((), 0)
    raw_docids = candidate.get("retrieved_docids")
    docids: dict[str, None] = {}
    if isinstance(raw_docids, Sequence) and not isinstance(raw_docids, (str, bytes)):
        for raw_docid in raw_docids:
            if isinstance(raw_docid, str) and raw_docid.strip():
                docids.setdefault(raw_docid.strip(), None)
    tool_call_count = 0
    raw_counts = candidate.get("tool_call_counts")
    if isinstance(raw_counts, Mapping):
        raw_search_count = raw_counts.get("search")
        if (
            isinstance(raw_search_count, int)
            and not isinstance(raw_search_count, bool)
            and raw_search_count >= 0
        ):
            tool_call_count = raw_search_count
    return RetrievalTrajectory(tuple(docids), tool_call_count)


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


class BrowseCompPlusEvaluator:
    """Pure evaluator using one injected judge."""

    def __init__(self, judge: Judge) -> None:
        if not callable(judge):
            raise TypeError("judge must be callable")
        self._judge = judge

    def evaluate(
        self,
        *,
        task: TrustedTask,
        candidate: object,
        trajectory: RetrievalTrajectory | None = None,
    ) -> TaskEvaluation:
        if not isinstance(task, TrustedTask):
            raise TypeError("task must be a TrustedTask")
        if trajectory is None:
            trajectory = _candidate_trajectory(candidate)
        elif not isinstance(trajectory, RetrievalTrajectory):
            raise TypeError("trajectory must be a RetrievalTrajectory or None")

        evidence = set(task.evidence_docids)
        retrieved = set(trajectory.retrieved_docids)
        retrieval_recall = _ratio(len(retrieved & evidence), len(evidence))

        completed, response = _last_output(candidate)
        parsed = parse_final_answer(response)
        cited = set(parsed.cited_docids if parsed is not None else ())
        citation_precision = _ratio(len(cited & evidence), len(cited))
        citation_recall = _ratio(len(cited & evidence), len(evidence))

        common = {
            "retrieval_recall": retrieval_recall,
            "citation_precision": citation_precision,
            "citation_recall": citation_recall,
            "tool_call_count": trajectory.tool_call_count,
        }
        if not completed:
            return TaskEvaluation(
                outcome="measured",
                score=0,
                correctness=0,
                evaluation_signal="candidate_incomplete",
                judge=JudgeResponse(None, "not_started", None, 0),
                **common,
            )
        if parsed is None or response is None:
            return TaskEvaluation(
                outcome="measured",
                score=0,
                correctness=0,
                evaluation_signal="invalid_final_format",
                judge=JudgeResponse(None, "not_started", None, 0),
                **common,
            )

        request = JudgeRequest(prompt=render_judge_prompt(task, response))
        try:
            raw_judge_response = self._judge(request)
        except Exception:  # noqa: BLE001 - an operator router is an isolation seam
            return TaskEvaluation(
                outcome="inconclusive",
                score=None,
                correctness=None,
                evaluation_signal=None,
                judge=JudgeResponse(None, "judge_exception", None, 0),
                **common,
            )
        try:
            judge_response = _normalize_judge_response(raw_judge_response)
        except (TypeError, ValueError):
            return TaskEvaluation(
                outcome="inconclusive",
                score=None,
                correctness=None,
                evaluation_signal=None,
                judge=JudgeResponse(None, "invalid_judge_contract", None, 0),
                **common,
            )
        if judge_response.terminal_reason != "completed":
            return TaskEvaluation(
                outcome="inconclusive",
                score=None,
                correctness=None,
                evaluation_signal=None,
                judge=judge_response,
                **common,
            )
        verdict = parse_judge_response(judge_response.text)
        if verdict is None:
            return TaskEvaluation(
                outcome="inconclusive",
                score=None,
                correctness=None,
                evaluation_signal=None,
                judge=JudgeResponse(
                    judge_response.text,
                    "invalid_judge_response",
                    judge_response.http_status,
                    judge_response.retry_count,
                    judge_response.usage,
                ),
                **common,
            )
        score = int(verdict)
        return TaskEvaluation(
            outcome="measured",
            score=score,
            correctness=score,
            evaluation_signal=("judged_correct" if verdict else "judged_incorrect"),
            judge=judge_response,
            **common,
        )
