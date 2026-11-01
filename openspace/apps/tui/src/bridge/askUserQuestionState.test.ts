import assert from "node:assert/strict";
import test from "node:test";
import {
  buildAskUserQuestionAllowResponse,
  buildAskUserQuestionDenyResponse,
  getAskUserQuestionQuestions,
  isAskUserQuestionRequest,
} from "./askUserQuestionState.js";
import type { PermissionRequestData } from "./protocol.js";

function makeRequest(
  overrides: Partial<PermissionRequestData> = {},
): PermissionRequestData {
  return {
    tool_use_id: "ask-1",
    tool_name: "ask_user_question",
    tool_input: {
      questions: [
        {
          question: "Pick a branch",
          options: [
            { label: "A", preview: "<p>A preview</p>" },
            { label: "B" },
          ],
        },
      ],
      annotations: {
        "Pick a branch": {
          existing: "kept",
        },
        "Earlier question": "legacy note",
      },
    },
    response_channel: "tool_permission_response",
    options: [{ option_id: "allow_once", label: "Allow once" }],
    ...overrides,
  };
}

test("detects AskUserQuestion requests from interaction and tool aliases", () => {
  assert.equal(
    isAskUserQuestionRequest(
      makeRequest({ tool_name: "Bash", interaction: "ask_user_question" }),
    ),
    true,
  );
  assert.equal(
    isAskUserQuestionRequest(makeRequest({ tool_name: "AskUserQuestion" })),
    true,
  );
  assert.equal(
    isAskUserQuestionRequest(makeRequest({ tool_name: "Bash" })),
    false,
  );
});

test("coerces valid questions and caps the native panel payload at four", () => {
  const questions = getAskUserQuestionQuestions(
    makeRequest({
      questions: [
        {
          question: "One",
          options: [{ label: "A" }, { label: "B" }],
        },
        {
          question: "",
          options: [{ label: "A" }, { label: "B" }],
        },
        {
          question: "Two",
          options: [{ label: "C" }, { label: "D" }],
          multiSelect: true,
        },
        {
          question: "Three",
          options: [{ label: "E" }, { label: "F" }],
        },
        {
          question: "Four",
          options: [{ label: "G" }, { label: "H" }],
        },
        {
          question: "Five",
          options: [{ label: "I" }, { label: "J" }],
        },
      ],
    }),
  );

  assert.deepEqual(
    questions.map(question => question.question),
    ["One", "Two", "Three", "Four"],
  );
  assert.equal(questions[1]?.multiSelect, true);
});

test("allow response merges answers, previews, notes, and existing annotations", () => {
  const request = makeRequest();
  const response = buildAskUserQuestionAllowResponse(
    request,
    { "Pick a branch": "A" },
    { "Pick a branch": "take this path" },
  );

  assert.equal(response.tool_use_id, "ask-1");
  assert.equal(response.option_id, "allow_once");
  assert.deepEqual(response.updated_input?.answers, {
    "Pick a branch": "A",
  });
  assert.deepEqual(response.updated_input?.annotations, {
    "Pick a branch": {
      existing: "kept",
      preview: "<p>A preview</p>",
      notes: "take this path",
    },
    "Earlier question": "legacy note",
  });
});

test("deny response is fail-closed and does not include updated input", () => {
  const response = buildAskUserQuestionDenyResponse(makeRequest(), "cancelled");

  assert.deepEqual(response, {
    tool_use_id: "ask-1",
    option_id: "deny",
    message: "cancelled",
  });
});
