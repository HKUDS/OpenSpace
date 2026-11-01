import type {
  AskUserQuestionData,
  AskUserQuestionOptionData,
  PermissionRequestData,
  ToolPermissionResponseData,
} from "./protocol.js";

export type AskUserQuestionAnswers = Record<string, string>;
export type AskUserQuestionNotes = Record<string, string>;

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function coerceOption(value: unknown): AskUserQuestionOptionData | null {
  const record = asRecord(value);
  const label = nonEmptyString(record?.label);
  if (!record || !label) {
    return null;
  }
  return {
    label,
    description: nonEmptyString(record.description) ?? undefined,
    preview: nonEmptyString(record.preview) ?? undefined,
  };
}

function coerceQuestion(value: unknown): AskUserQuestionData | null {
  const record = asRecord(value);
  const question = nonEmptyString(record?.question);
  const rawOptions = Array.isArray(record?.options) ? record.options : [];
  if (!record || !question || rawOptions.length === 0) {
    return null;
  }

  const options = rawOptions
    .map(coerceOption)
    .filter((option): option is AskUserQuestionOptionData => option !== null);
  if (options.length === 0) {
    return null;
  }

  return {
    header: nonEmptyString(record.header) ?? undefined,
    question,
    options,
    multiSelect: record.multiSelect === true,
  };
}

export function getAskUserQuestionQuestions(
  request: PermissionRequestData,
): AskUserQuestionData[] {
  const source = Array.isArray(request.questions)
    ? request.questions
    : Array.isArray(request.tool_input.questions)
      ? request.tool_input.questions
      : [];
  return source
    .map(coerceQuestion)
    .filter((question): question is AskUserQuestionData => question !== null)
    .slice(0, 4);
}

export function isAskUserQuestionRequest(
  request: PermissionRequestData,
): boolean {
  if (request.interaction === "ask_user_question") {
    return true;
  }
  const toolName = request.tool_name.toLowerCase();
  return (
    toolName === "askuserquestion" ||
    toolName === "ask_user_question" ||
    toolName === "ask-user-question"
  );
}

function cloneExistingAnnotations(
  input: Record<string, unknown>,
): Record<string, unknown> {
  const annotations = asRecord(input.annotations);
  if (!annotations) {
    return {};
  }

  return Object.fromEntries(
    Object.entries(annotations).map(([key, value]) => {
      const record = asRecord(value);
      return [key, record ? { ...record } : value];
    }),
  );
}

export function buildAskUserQuestionUpdatedInput(
  request: PermissionRequestData,
  answers: AskUserQuestionAnswers,
  notes: AskUserQuestionNotes = {},
): Record<string, unknown> {
  const questions = getAskUserQuestionQuestions(request);
  const annotations = cloneExistingAnnotations(request.tool_input);

  for (const question of questions) {
    const answer = answers[question.question];
    const selectedOption = answer
      ? question.options.find(option => option.label === answer)
      : undefined;
    const note = notes[question.question]?.trim();
    const currentValue = annotations[question.question];
    const current = asRecord(currentValue);
    const next: Record<string, unknown> =
      currentValue === undefined
        ? {}
        : current
          ? { ...current }
          : { value: currentValue };

    if (selectedOption?.preview) {
      next.preview = selectedOption.preview;
    }
    if (note) {
      next.notes = note;
    }

    if (Object.keys(next).length > 0) {
      annotations[question.question] = next;
    }
  }

  const nextInput: Record<string, unknown> = {
    ...request.tool_input,
    answers,
  };

  if (Object.keys(annotations).length > 0) {
    nextInput.annotations = annotations;
  }

  return nextInput;
}

export function buildAskUserQuestionAllowResponse(
  request: PermissionRequestData,
  answers: AskUserQuestionAnswers,
  notes: AskUserQuestionNotes = {},
): ToolPermissionResponseData {
  return {
    tool_use_id: request.tool_use_id,
    option_id: "allow_once",
    updated_input: buildAskUserQuestionUpdatedInput(request, answers, notes),
  };
}

export function buildAskUserQuestionDenyResponse(
  request: PermissionRequestData,
  message = "Question prompt cancelled by user.",
): ToolPermissionResponseData {
  return {
    tool_use_id: request.tool_use_id,
    option_id: "deny",
    message,
  };
}
