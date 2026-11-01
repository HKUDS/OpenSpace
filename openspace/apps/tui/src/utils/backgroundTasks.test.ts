import assert from "node:assert/strict";
import test from "node:test";
import {
  getBackgroundTaskLabel,
  getBackgroundTaskTail,
  hasForegroundBackgroundTasks,
  isTerminalBackgroundAgentStatus,
} from "./backgroundTasks.js";

test("detects foreground local shell tasks for Ctrl+B routing", () => {
  assert.equal(
    hasForegroundBackgroundTasks({
      b12345678: {
        id: "b12345678",
        agentId: "b12345678",
        taskType: "local_bash",
        status: "running",
        startedAt: 1,
        updatedAt: 1,
        background: false,
      },
    }),
    true,
  );
  assert.equal(
    hasForegroundBackgroundTasks({
      a12345678: {
        id: "a12345678",
        agentId: "a12345678",
        taskType: "local_agent",
        status: "running",
        startedAt: 1,
        updatedAt: 1,
        background: false,
      },
    }),
    false,
  );
});

test("formats shell labels and output tails from task payload metadata", () => {
  const task = {
    id: "b12345678",
    agentId: "b12345678",
    taskType: "local_bash",
    status: "running",
    description: "Run tests",
    startedAt: 1,
    updatedAt: 1,
    background: true,
    metadata: {
      command: "npm test",
      output_tail: "last line",
    },
  };

  assert.equal(getBackgroundTaskLabel(task), "Run tests");
  assert.equal(getBackgroundTaskTail(task), "last line");
  assert.equal(isTerminalBackgroundAgentStatus("killed"), true);
  assert.equal(isTerminalBackgroundAgentStatus("running"), false);
});
