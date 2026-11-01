"""AskUserQuestionTool.

The engine contract is deliberately interaction-centric: the tool always asks for
interactive permission, the permission UI/bridge writes ``answers`` back into
``updated_input``, and the actual tool call returns a model-facing sentence
with the user's selected answers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from openspace.grounding.core.permissions.types import PermissionAsk
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus


ASK_USER_QUESTION_TOOL_NAME = "ask_user_question"
ASK_USER_QUESTION_TOOL_ALIAS = "AskUserQuestion"
ASK_USER_QUESTION_TOOL_CHIP_WIDTH = 12
MAX_RESULT_SIZE_CHARS = 100_000

DESCRIPTION = (
    "Asks the user multiple choice questions to gather information, clarify "
    "ambiguity, understand preferences, make decisions or offer them choices."
)

ASK_USER_QUESTION_TOOL_PROMPT = """Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multiSelect: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label

Plan mode note: In plan mode, use this tool to clarify requirements or choose between approaches BEFORE finalizing your plan. Do NOT use this tool to ask "Is my plan ready?" or "Should I proceed?" - use ExitPlanMode for plan approval. IMPORTANT: Do not reference "the plan" in your questions (e.g., "Do you have feedback about the plan?", "Does the plan look good?") because the user cannot see the plan in the UI until you call ExitPlanMode. If you need plan approval, use ExitPlanMode instead.
"""

PREVIEW_FEATURE_PROMPT = {
    "markdown": """
Preview feature:
Use the optional `preview` field on options when presenting concrete artifacts that users need to visually compare:
- ASCII mockups of UI layouts or components
- Code snippets showing different implementations
- Diagram variations
- Configuration examples

Preview content is rendered as markdown in a monospace box. Multi-line text with newlines is supported. When any option has a preview, the UI switches to a side-by-side layout with a vertical option list on the left and preview on the right. Do not use previews for simple preference questions where labels and descriptions suffice. Note: previews are only supported for single-select questions (not multiSelect).
""",
    "html": """
Preview feature:
Use the optional `preview` field on options when presenting concrete artifacts that users need to visually compare:
- HTML mockups of UI layouts or components
- Formatted code snippets showing different implementations
- Visual comparisons or diagrams

Preview content must be a self-contained HTML fragment (no <html>/<body> wrapper, no <script> or <style> tags - use inline style attributes instead). Do not use previews for simple preference questions where labels and descriptions suffice. Note: previews are only supported for single-select questions (not multiSelect).
""",
}


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str
    preview: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "QuestionOption":
        preview = raw.get("preview")
        return cls(
            label=str(raw.get("label", "")),
            description=str(raw.get("description", "")),
            preview=str(preview) if preview is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "label": self.label,
            "description": self.description,
        }
        if self.preview is not None:
            data["preview"] = self.preview
        return data


@dataclass(frozen=True)
class Question:
    question: str
    header: str
    options: tuple[QuestionOption, ...]
    multiSelect: bool = False

    @property
    def multi_select(self) -> bool:
        return self.multiSelect

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Question":
        options_raw = raw.get("options") or ()
        options = tuple(
            QuestionOption.from_mapping(option)
            for option in options_raw
            if isinstance(option, Mapping)
        )
        return cls(
            question=str(raw.get("question", "")),
            header=str(raw.get("header", "")),
            options=options,
            multiSelect=bool(raw.get("multiSelect", raw.get("multi_select", False))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "header": self.header,
            "options": [option.to_dict() for option in self.options],
            "multiSelect": self.multiSelect,
        }


@dataclass(frozen=True)
class AnswerAnnotation:
    preview: str | None = None
    notes: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AnswerAnnotation":
        preview = raw.get("preview")
        notes = raw.get("notes")
        return cls(
            preview=str(preview) if preview is not None else None,
            notes=str(notes) if notes is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.preview is not None:
            data["preview"] = self.preview
        if self.notes is not None:
            data["notes"] = self.notes
        return data


def get_question_preview_format() -> str | None:
    """Return the preview format exposed in the prompt.

    CLI clients use markdown by default. ``OPENSPACE_ASK_USER_QUESTION_PREVIEW_FORMAT``
    may override the format; ``none``/``off`` omit preview guidance for SDK-style
    clients.
    """

    raw = (
        os.environ.get("OPENSPACE_ASK_USER_QUESTION_PREVIEW_FORMAT")
        or ""
    ).strip().lower()
    if raw in {"markdown", "html"}:
        return raw
    if raw in {"none", "off", "disabled", "undefined"}:
        return None
    return "markdown"


def validate_html_preview(preview: str | None) -> str | None:
    """Lightweight HTML fragment validation."""

    if preview is None:
        return None
    if re.search(r"<\s*(html|body|!doctype)\b", preview, flags=re.IGNORECASE):
        return (
            "preview must be an HTML fragment, not a full document "
            "(no <html>, <body>, or <!DOCTYPE>)"
        )
    if re.search(r"<\s*(script|style)\b", preview, flags=re.IGNORECASE):
        return (
            "preview must not contain <script> or <style> tags. Use inline "
            "styles via the style attribute if needed."
        )
    if not re.search(r"<[a-z][^>]*>", preview, flags=re.IGNORECASE):
        return (
            'preview must contain HTML (previewFormat is set to "html"). '
            "Wrap content in a tag like <div> or <pre>."
        )
    return None


def normalize_questions(raw_questions: Sequence[Any]) -> list[Question]:
    return [
        Question.from_mapping(item)
        for item in raw_questions
        if isinstance(item, Mapping)
    ]


def normalize_annotations(raw: Mapping[str, Any] | None) -> dict[str, dict[str, Any]] | None:
    if not isinstance(raw, Mapping):
        return None
    annotations: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, Mapping):
            annotation = AnswerAnnotation.from_mapping(value).to_dict()
            if annotation:
                annotations[str(key)] = annotation
    return annotations or None


def normalize_answers(raw: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def validate_questions_payload(input_data: Mapping[str, Any]) -> str | None:
    raw_questions = input_data.get("questions")
    if not isinstance(raw_questions, list):
        return "questions must be an array"
    if len(raw_questions) < 1 or len(raw_questions) > 4:
        return "questions must contain 1-4 questions"

    questions = normalize_questions(raw_questions)
    if len(questions) != len(raw_questions):
        return "each question must be an object"

    question_texts: list[str] = []
    for question in questions:
        question_texts.append(question.question)
        if not question.options or len(question.options) < 2 or len(question.options) > 4:
            return "each question must have 2-4 options"
        labels = [option.label for option in question.options]
        if len(labels) != len(set(labels)):
            return (
                "Question texts must be unique, option labels must be unique "
                "within each question"
            )

    if len(question_texts) != len(set(question_texts)):
        return (
            "Question texts must be unique, option labels must be unique "
            "within each question"
        )

    if get_question_preview_format() == "html":
        for question in questions:
            for option in question.options:
                err = validate_html_preview(option.preview)
                if err is not None:
                    return (
                        f'Option "{option.label}" in question '
                        f'"{question.question}": {err}'
                    )

    return None


def is_interaction_complete(input_data: Mapping[str, Any]) -> bool:
    raw_questions = input_data.get("questions")
    answers = input_data.get("answers")
    if not isinstance(raw_questions, list) or not isinstance(answers, Mapping):
        return False
    questions = normalize_questions(raw_questions)
    if len(questions) != len(raw_questions):
        return False
    for question in questions:
        answer = answers.get(question.question)
        if not isinstance(answer, str) or not answer.strip():
            return False
    return True


def format_answers_for_model(
    answers: Mapping[str, Any],
    annotations: Mapping[str, Any] | None = None,
) -> str:
    annotation_map = annotations if isinstance(annotations, Mapping) else {}
    answer_parts: list[str] = []
    for question_text, answer in answers.items():
        parts = [f'"{question_text}"="{answer}"']
        annotation = annotation_map.get(question_text)
        if isinstance(annotation, Mapping):
            preview = annotation.get("preview")
            notes = annotation.get("notes")
            if preview:
                parts.append(f"selected preview:\n{preview}")
            if notes:
                parts.append(f"user notes: {notes}")
        answer_parts.append(" ".join(parts))
    answers_text = ", ".join(answer_parts)
    return (
        f"User has answered your questions: {answers_text}. You can now "
        "continue with the user's answers in mind."
    )


def make_input_schema() -> dict[str, Any]:
    option_schema = {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": (
                    "The display text for this option that the user will see "
                    "and select. Should be concise (1-5 words) and clearly "
                    "describe the choice."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Explanation of what this option means or what will happen "
                    "if chosen. Useful for providing context about trade-offs "
                    "or implications."
                ),
            },
            "preview": {
                "type": "string",
                "description": (
                    "Optional preview content rendered when this option is "
                    "focused. Use for mockups, code snippets, or visual "
                    "comparisons that help users compare options."
                ),
            },
        },
        "required": ["label", "description"],
    }
    question_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The complete question to ask the user. Should be clear, "
                    "specific, and end with a question mark."
                ),
            },
            "header": {
                "type": "string",
                "description": (
                    "Very short label displayed as a chip/tag "
                    f"(max {ASK_USER_QUESTION_TOOL_CHIP_WIDTH} chars)."
                ),
            },
            "options": {
                "type": "array",
                "items": option_schema,
                "minItems": 2,
                "maxItems": 4,
                "description": (
                    "The available choices for this question. Must have 2-4 "
                    "options. There should be no 'Other' option, that will be "
                    "provided automatically."
                ),
            },
            "multiSelect": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set to true to allow the user to select multiple options "
                    "instead of just one."
                ),
            },
        },
        "required": ["question", "header", "options"],
    }
    annotation_schema = {
        "type": "object",
        "properties": {
            "preview": {
                "type": "string",
                "description": (
                    "The preview content of the selected option, if the "
                    "question used previews."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "Free-text notes the user added to their selection."
                ),
            },
        },
    }
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": question_schema,
                "minItems": 1,
                "maxItems": 4,
                "description": "Questions to ask the user (1-4 questions)",
            },
            "answers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "User answers collected by the permission component"
                ),
            },
            "annotations": {
                "type": "object",
                "additionalProperties": annotation_schema,
                "description": (
                    "Optional per-question annotations from the user, keyed "
                    "by question text."
                ),
            },
            "metadata": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Optional identifier for the source of this "
                            "question."
                        ),
                    }
                },
                "description": (
                    "Optional metadata for tracking and analytics purposes. "
                    "Not displayed to user."
                ),
            },
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


def make_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "questions": make_input_schema()["properties"]["questions"],
            "answers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "The answers provided by the user (question text -> "
                    "answer string; multi-select answers are comma-separated)"
                ),
            },
            "annotations": make_input_schema()["properties"]["annotations"],
        },
        "required": ["questions", "answers"],
    }


class AskUserQuestionTool(BaseTool):
    _name = ASK_USER_QUESTION_TOOL_NAME
    _description = DESCRIPTION
    backend_type = BackendType.META
    aliases = [ASK_USER_QUESTION_TOOL_ALIAS]
    should_defer = True
    search_hint = "prompt the user with a multiple-choice question"
    max_result_size_chars = MAX_RESULT_SIZE_CHARS
    _is_read_only = True
    _is_concurrency_safe = True
    requires_user_interaction = True

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=DESCRIPTION,
                parameters=make_input_schema(),
                return_schema=make_output_schema(),
                backend_type=self.backend_type,
            )
        )

    def get_prompt(self, context: Any = None) -> str:
        preview_format = get_question_preview_format()
        if preview_format is None:
            return ASK_USER_QUESTION_TOOL_PROMPT
        return ASK_USER_QUESTION_TOOL_PROMPT + PREVIEW_FEATURE_PROMPT[preview_format]

    def user_facing_name(self) -> str:
        return ""

    def to_auto_classifier_input(self, input_data: Mapping[str, Any]) -> str:
        raw_questions = input_data.get("questions") if isinstance(input_data, Mapping) else []
        if not isinstance(raw_questions, list):
            return ""
        return " | ".join(
            str(question.get("question"))
            for question in raw_questions
            if isinstance(question, Mapping) and question.get("question") is not None
        )

    def is_user_interaction_complete(self, input_data: Mapping[str, Any]) -> bool:
        return is_interaction_complete(input_data)

    async def validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> str | None:
        return validate_questions_payload(input)

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> PermissionAsk:
        return PermissionAsk(message="Answer questions?", updated_input=dict(input))

    async def _arun(
        self,
        questions: list[dict[str, Any]],
        answers: dict[str, str] | None = None,
        annotations: dict[str, dict[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        normalized_questions = [question.to_dict() for question in normalize_questions(questions)]
        normalized_answers = normalize_answers(answers)
        normalized_annotations = normalize_annotations(annotations)

        result_data: dict[str, Any] = {
            "questions": normalized_questions,
            "answers": normalized_answers,
        }
        if normalized_annotations is not None:
            result_data["annotations"] = normalized_annotations

        result_metadata: dict[str, Any] = {
            "tool": self.name,
            "questions": normalized_questions,
            "answers": normalized_answers,
        }
        if normalized_annotations is not None:
            result_metadata["annotations"] = normalized_annotations
        if metadata is not None:
            result_metadata["input_metadata"] = dict(metadata)
        result_metadata["data"] = result_data

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=format_answers_for_model(
                normalized_answers,
                normalized_annotations,
            ),
            metadata=result_metadata,
        )


__all__ = [
    "ASK_USER_QUESTION_TOOL_CHIP_WIDTH",
    "ASK_USER_QUESTION_TOOL_ALIAS",
    "ASK_USER_QUESTION_TOOL_NAME",
    "ASK_USER_QUESTION_TOOL_PROMPT",
    "AskUserQuestionTool",
    "AnswerAnnotation",
    "DESCRIPTION",
    "MAX_RESULT_SIZE_CHARS",
    "PREVIEW_FEATURE_PROMPT",
    "Question",
    "QuestionOption",
    "format_answers_for_model",
    "get_question_preview_format",
    "is_interaction_complete",
    "make_input_schema",
    "make_output_schema",
    "normalize_answers",
    "normalize_annotations",
    "normalize_questions",
    "validate_html_preview",
    "validate_questions_payload",
]
