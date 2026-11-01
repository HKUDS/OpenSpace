import assert from "node:assert/strict";
import test from "node:test";
import {
  buildPendingSandboxRequest,
  buildPendingWorkerRequest,
  buildWorkerSandboxQueue,
  getAllowAlwaysLabel,
  getPermissionRequestSummary,
} from "./permissionRequestState.js";
import type { PermissionRequestData } from "./protocol.js";

test("permission request helpers classify worker network requests", () => {
  const request: PermissionRequestData = {
    tool_use_id: "perm-worker-network",
    tool_name: "web_fetch",
    tool_input: { url: "https://api.github.com/repos/openai/openai-python" },
    request_kind: "network",
    host: "api.github.com",
    origin: "worker",
    agent_id: "worker-1",
    agent_name: "reviewer",
    agent_color: "cyan",
  };

  assert.deepEqual(buildPendingSandboxRequest(request), {
    requestId: "perm-worker-network",
    host: "api.github.com",
    requestKind: "network",
  });
  assert.deepEqual(buildPendingWorkerRequest(request), {
    toolName: "web_fetch",
    toolUseId: "perm-worker-network",
    description:
      "Worker reviewer requests network access to api.github.com via web_fetch",
    workerId: "worker-1",
    workerName: "reviewer",
    workerColor: "cyan",
    host: "api.github.com",
    requestKind: "network",
  });
  assert.deepEqual(buildWorkerSandboxQueue([request]), [
    {
      requestId: "perm-worker-network",
      workerId: "worker-1",
      workerName: "reviewer",
      workerColor: "cyan",
      host: "api.github.com",
      createdAt: 0,
    },
  ]);
  assert.equal(
    getPermissionRequestSummary(request),
    "Worker reviewer requests network access to api.github.com via web_fetch",
  );
  assert.equal(getAllowAlwaysLabel(request), "allow always for api.github.com");
});

test("permission request helpers fall back to generic tool prompts", () => {
  const request: PermissionRequestData = {
    tool_use_id: "perm-tool",
    tool_name: "bash",
    tool_input: { command: "pwd" },
  };

  assert.equal(buildPendingSandboxRequest(request), null);
  assert.equal(buildPendingWorkerRequest(request), null);
  assert.equal(
    getPermissionRequestSummary(request),
    "Primary session requests permission for bash",
  );
  assert.equal(getAllowAlwaysLabel(request), "allow always for bash");
});
