import assert from "node:assert/strict";
import test from "node:test";
import { estimateMessagesContentHeight } from "../components/Messages.js";
import {
  buildTranscriptRows,
  wrapToDisplayRows,
} from "../components/messages/transcriptRows.js";
import {
  createMessage,
  normalizeExternalMessages,
} from "../screens/shared.js";

test("restored SDK tool-use blocks render as tool messages without raw JSON", () => {
  const [message] = normalizeExternalMessages([
    {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "toolu_123",
          name: "Bash",
          input: { command: "pwd" },
        },
      ],
    },
  ]);

  assert.equal(message?.role, "tool");
  assert.equal(message?.content[0]?.type, "tool_use");
  assert.equal(message?.content[0]?.tool_name, "Bash");
  assert.equal(message?.content[0]?.tool_use_id, "toolu_123");
  assert.equal(message?.text.includes('"type"'), false);
});

test("restored reasoning-only blocks are not shown as raw JSON", () => {
  const [message] = normalizeExternalMessages([
    {
      role: "assistant",
      content: [
        {
          type: "thinking",
          thinking: "private reasoning",
        },
      ],
    },
  ]);

  assert.equal(message?.text, "");
  assert.equal(message?.meta?.hasReasoning, true);
});

test("message height estimation keeps full scrollback instead of recent cap", () => {
  const messages = Array.from({ length: 450 }, (_, index) =>
    createMessage("assistant", `message ${index}`),
  );

  const height = estimateMessagesContentHeight(messages, 100);

  assert.ok(height > 1_300);
});

test("long assistant replies remain available as transcript rows", () => {
  const text = Array.from({ length: 24 }, (_, index) => `line ${index}`).join("\n");
  const rows = buildTranscriptRows([createMessage("assistant", text)], 80);

  assert.ok(rows.some(row => row.text.includes("line 0")));
  assert.ok(rows.some(row => row.text.includes("line 23")));
  assert.ok(rows.length > 24);
});

test("soft-wrapped transcript rows do not start with carried whitespace", () => {
  assert.deepEqual(wrapToDisplayRows("hello world", 5), [
    "hello",
    "world",
  ]);
});

test("collapsed tool rows summarize input without dumping raw JSON", () => {
  const [message] = normalizeExternalMessages([
    {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "toolu_123",
          name: "Bash",
          input: { command: "pwd", timeout_ms: 1000 },
        },
      ],
    },
  ]);
  assert.ok(message);

  const rendered = buildTranscriptRows([message], 100)
    .map(row => row.text)
    .join("\n");

  assert.match(rendered, /Tool: Bash/);
  assert.match(rendered, /command: pwd/);
  assert.equal(rendered.includes('"type"'), false);
  assert.equal(rendered.includes('"timeout_ms"'), false);
});

test("system JSON evolution suggestions render as readable transcript text", () => {
  const message = createMessage(
    "system",
    JSON.stringify({
      evolution_suggestions: [
        {
          type: "fix",
          target_skills: ["skill-a"],
          rationale: "The skill failed on repeated usage.",
          proposed_change: "Clarify command arguments.",
        },
      ],
    }),
  );

  const rendered = buildTranscriptRows([message], 100)
    .map(row => row.text)
    .join("\n");

  assert.match(rendered, /Evolution suggestions:/);
  assert.match(rendered, /FIX target=skill-a/);
  assert.equal(rendered.includes('"evolution_suggestions"'), false);
});

test("assistant OpenSpace task summaries render without raw JSON", () => {
  const message = createMessage(
    "assistant",
    JSON.stringify({
      task_completed: true,
      execution_note:
        "The agent replied to the greeting without tool usage.",
      tool_issues: [],
      skill_judgments: [],
      skill_phase_failed_skill_ids: [],
      evolution_suggestions: [],
    }),
  );

  const rendered = buildTranscriptRows([message], 100)
    .map(row => row.text)
    .join("\n");

  assert.match(rendered, /Task complete/);
  assert.match(rendered, /The agent replied to the greeting/);
  assert.equal(rendered.includes('"task_completed"'), false);
});

test("assistant ordinary JSON remains raw when it is not OpenSpace structured output", () => {
  const message = createMessage(
    "assistant",
    JSON.stringify({ language: "json", answer: { ok: true } }, null, 2),
  );

  const rendered = buildTranscriptRows([message], 100)
    .map(row => row.text)
    .join("\n");

  assert.match(rendered, /"language": "json"/);
  assert.match(rendered, /"ok": true/);
});

test("system JSONL logs render as concise log rows", () => {
  const message = createMessage(
    "system",
    [
      JSON.stringify({
        timestamp: "2026-06-01T12:00:00Z",
        level: "info",
        message: "Loaded runtime log",
      }),
      JSON.stringify({
        level: "warning",
        error: "Quality signal checkpoint warning",
      }),
    ].join("\n"),
  );

  const rendered = buildTranscriptRows([message], 100)
    .map(row => row.text)
    .join("\n");

  assert.match(rendered, /Logs:/);
  assert.match(rendered, /INFO Loaded runtime log/);
  assert.match(rendered, /WARNING Quality signal checkpoint warning/);
  assert.equal(rendered.includes('"message"'), false);
});
