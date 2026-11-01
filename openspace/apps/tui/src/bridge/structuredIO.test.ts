import assert from "node:assert/strict";
import test from "node:test";
import { PassThrough } from "node:stream";
import { ndjsonParse } from "./ndjson.js";
import { StructuredIO } from "./structuredIO.js";
import type { IPCMessage, PermissionRequestData } from "./protocol.js";

test("send stamps a timestamp on outgoing messages", async () => {
  const output = new PassThrough();
  const input = new PassThrough();
  const io = new StructuredIO({ stdin: input, stdout: output });

  io.send({ type: "query", data: { text: "hello" } });
  const line = output.read()?.toString("utf8") ?? "";

  const parsed = ndjsonParse<{ type: string; timestamp: number; data: { text: string } }>(
    line,
  );
  assert.ok(parsed);
  assert.equal(parsed.type, "query");
  assert.equal(parsed.data.text, "hello");
  assert.equal(typeof parsed.timestamp, "number");

  output.destroy();
  input.destroy();
});

test("receive deduplicates permission requests by tool_use_id", async () => {
  const output = new PassThrough();
  const input = new PassThrough();
  const io = new StructuredIO({ stdin: input, stdout: output });
  const received: Array<{ type: string }> = [];

  const reader = (async () => {
    for await (const message of io.receive()) {
      received.push(message);
    }
  })();

  const line = JSON.stringify({
    type: "permission_request",
    data: {
      tool_use_id: "dup-1",
      tool_name: "bash",
      tool_input: { command: "ls" },
    },
    timestamp: Date.now(),
  });

  input.write(`${line}\n`);
  input.write(`${line}\n`);
  input.end();

  await reader;

  assert.equal(received.length, 1);
  assert.equal(io.seenToolUseIds.size, 1);
  assert.ok(io.seenToolUseIds.has("dup-1"));

  output.destroy();
  input.destroy();
});

test("resolved tool permission id can be asked again for edited-input retries", async () => {
  const output = new PassThrough();
  const input = new PassThrough();
  const io = new StructuredIO({ stdin: input, stdout: output });
  const iterator = io.receive();
  const requestData: PermissionRequestData = {
    tool_use_id: "retry-1",
    tool_name: "bash",
    tool_input: { command: "echo first" },
    response_channel: "tool_permission_response",
    options: [{ option_id: "provide_input", label: "Edit input" }],
  };
  const request = {
    type: "tool_permission_ask",
    data: requestData,
    timestamp: Date.now(),
  } satisfies IPCMessage;

  input.write(`${JSON.stringify(request)}\n`);
  const first = await iterator.next();
  assert.equal(first.done, false);
  assert.equal(first.value.type, "tool_permission_ask");

  const pending = io.waitForPermissionDecision(requestData);
  io.resolveToolPermission({
    tool_use_id: "retry-1",
    option_id: "provide_input",
    edited_input: { command: "echo second" },
  });
  await pending;

  input.write(`${JSON.stringify({
    ...request,
    data: {
      ...request.data,
      tool_input: { command: "echo second" },
    },
  })}\n`);
  input.end();

  const second = await iterator.next();
  assert.equal(second.done, false);
  assert.equal(second.value.type, "tool_permission_ask");
  assert.equal((second.value.data as { tool_input: { command: string } }).tool_input.command, "echo second");

  await iterator.return?.();
  output.destroy();
  input.destroy();
});

test("resolved tool permission id can be asked again without pending promise", async () => {
  const output = new PassThrough();
  const input = new PassThrough();
  const io = new StructuredIO({ stdin: input, stdout: output });
  const iterator = io.receive();
  const request = {
    type: "tool_permission_ask",
    data: {
      tool_use_id: "retry-no-pending",
      tool_name: "bash",
      tool_input: { command: "echo first" },
      response_channel: "tool_permission_response",
      options: [{ option_id: "provide_input", label: "Edit input" }],
    },
    timestamp: Date.now(),
  } satisfies IPCMessage;

  input.write(`${JSON.stringify(request)}\n`);
  const first = await iterator.next();
  assert.equal(first.done, false);
  assert.equal(first.value.type, "tool_permission_ask");
  assert.ok(io.seenToolUseIds.has("retry-no-pending"));

  io.resolveToolPermission({
    tool_use_id: "retry-no-pending",
    option_id: "provide_input",
    edited_input: { command: "echo second" },
  });
  assert.equal(io.seenToolUseIds.has("retry-no-pending"), false);

  input.write(`${JSON.stringify({
    ...request,
    data: {
      ...request.data,
      tool_input: { command: "echo second" },
    },
  })}\n`);
  input.end();

  const second = await iterator.next();
  assert.equal(second.done, false);
  assert.equal(second.value.type, "tool_permission_ask");
  assert.equal((second.value.data as { tool_input: { command: string } }).tool_input.command, "echo second");

  await iterator.return?.();
  output.destroy();
  input.destroy();
});

test("resolvePermission clears pending state and writes a response", async () => {
  const output = new PassThrough();
  const input = new PassThrough();
  const io = new StructuredIO({ stdin: input, stdout: output });

  const pending = io.waitForPermissionDecision({
    tool_use_id: "perm-1",
    tool_name: "bash",
    tool_input: { command: "pwd" },
  });

  let line = "";
  output.on("data", (chunk) => {
    line += chunk.toString("utf8");
  });

  io.resolvePermission({
    tool_use_id: "perm-1",
    decision: "allow",
  });

  const result = await pending;
  assert.ok("decision" in result);
  assert.equal(result.decision, "allow");
  assert.equal(io.pendingPermissions.size, 0);

  const parsed = ndjsonParse<{ type: string; data: { tool_use_id: string } }>(
    output.read()?.toString("utf8") ?? line,
  );
  assert.ok(parsed);
  assert.equal(parsed.type, "permission_response");
  assert.equal(parsed.data.tool_use_id, "perm-1");

  output.destroy();
  input.destroy();
});

test("resolveToolPermission clears pending state and writes a response", async () => {
  const output = new PassThrough();
  const input = new PassThrough();
  const io = new StructuredIO({ stdin: input, stdout: output });

  const pending = io.waitForPermissionDecision({
    tool_use_id: "tool-perm-1",
    tool_name: "bash",
    tool_input: { command: "git status" },
    response_channel: "tool_permission_response",
    options: [{ option_id: "allow_once", label: "Allow" }],
  });

  let line = "";
  output.on("data", (chunk) => {
    line += chunk.toString("utf8");
  });

  io.resolveToolPermission({
    tool_use_id: "tool-perm-1",
    option_id: "allow_once",
  });

  const result = await pending;
  assert.equal(result.tool_use_id, "tool-perm-1");
  assert.equal(io.pendingPermissions.size, 0);

  const parsed = ndjsonParse<{ type: string; data: { tool_use_id: string } }>(
    output.read()?.toString("utf8") ?? line,
  );
  assert.ok(parsed);
  assert.equal(parsed.type, "tool_permission_response");
  assert.equal(parsed.data.tool_use_id, "tool-perm-1");

  output.destroy();
  input.destroy();
});

test("close rejects outstanding permission promises", async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const io = new StructuredIO({ stdin: input, stdout: output });

  const pending = io.waitForPermissionDecision({
    tool_use_id: "perm-close",
    tool_name: "bash",
    tool_input: { command: "sleep 10" },
  });

  io.close();

  await assert.rejects(pending, /StructuredIO closed/);
  input.destroy();
  output.destroy();
});
