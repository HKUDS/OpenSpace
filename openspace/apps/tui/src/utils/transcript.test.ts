import assert from "node:assert/strict";
import test from "node:test";
import type { AppMessage, SessionContextState } from "../state/AppStateStore.js";
import {
  renderTranscriptToPlainText,
  splitCommandLine,
} from "./transcript.js";

function createMessage(
  id: string,
  role: AppMessage["role"],
  text: string,
): AppMessage {
  return {
    id,
    role,
    text,
    content: [{ type: "text", text }],
    timestamp: 1_713_000_000_000,
  };
}

test("splitCommandLine keeps quoted editor arguments together", () => {
  assert.deepEqual(splitCommandLine("code --wait \"notes file.txt\""), {
    command: "code",
    args: ["--wait", "notes file.txt"],
  });
});

test("splitCommandLine rejects unbalanced quotes", () => {
  assert.equal(splitCommandLine("code --wait \"notes.txt"), null);
});

test("renderTranscriptToPlainText includes metadata and selection markers", () => {
  const messages = [
    createMessage("m1", "user", "Summarize the latest diff."),
    createMessage("m2", "assistant", "Summary goes here."),
  ];
  const sessionContext: SessionContextState = {
    metadata: {},
    runtime: {},
    worktree: {
      project_path: "/tmp/project",
    },
    fileHistorySnapshots: [],
    contentReplacements: [],
  };

  const text = renderTranscriptToPlainText({
    messages,
    sessionId: "sess-123",
    sessionTitle: "Review flow",
    sessionContext,
    selectionIndex: 1,
  });

  assert.match(text, /OpenSpace Transcript/);
  assert.match(text, /Title: Review flow/);
  assert.match(text, /Session: sess-123/);
  assert.match(text, /Project: \/tmp\/project/);
  assert.match(text, /Selection: #2/);
  assert.match(text, /#2 \| ASSISTANT \| .* \| SELECTED/);
  assert.match(text, /Summary goes here\./);
});
