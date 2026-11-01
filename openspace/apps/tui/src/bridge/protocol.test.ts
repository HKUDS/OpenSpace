import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  CORE_TO_TUI_MSG_TYPES,
  TUI_TO_CORE_MSG_TYPES,
  isKnownEventType,
  isToolPermissionPromptForCancel,
  makeProtocolWarningMessage,
} from "./protocol.js";
import type {
  AutoDreamEventData,
  CompactEventData,
  EventDataMap,
  IPCMessage,
  MemoryExtractionCompleteData,
  MemoryExtractionErrorData,
  MemoryExtractionStartData,
  MemoryLoggedEventData,
  MemorySavedEventData,
  MemorySelectorData,
  TokenWarningEventData,
  ToolPermissionAskData,
  ToolPermissionCancelData,
  ToolPermissionResponseData,
} from "./protocol.js";

type IsEqual<Left, Right> =
  (<T>() => T extends Left ? 1 : 2) extends
  (<T>() => T extends Right ? 1 : 2)
    ? true
    : false;

type Assert<T extends true> = T;

const _toolPermissionPayloadAssertions: [
  Assert<IsEqual<EventDataMap["tool_permission_ask"], ToolPermissionAskData>>,
  Assert<IsEqual<IPCMessage<"tool_permission_ask">["data"], ToolPermissionAskData>>,
  Assert<IsEqual<EventDataMap["tool_permission_response"], ToolPermissionResponseData>>,
  Assert<IsEqual<IPCMessage<"tool_permission_response">["data"], ToolPermissionResponseData>>,
  Assert<IsEqual<EventDataMap["tool_permission_cancel"], ToolPermissionCancelData>>,
  Assert<IsEqual<IPCMessage<"tool_permission_cancel">["data"], ToolPermissionCancelData>>,
  Assert<IsEqual<EventDataMap["compact_start"], CompactEventData>>,
  Assert<IsEqual<EventDataMap["compact_complete"], CompactEventData>>,
  Assert<IsEqual<EventDataMap["token_warning"], TokenWarningEventData>>,
  Assert<IsEqual<EventDataMap["memory_selector"], MemorySelectorData>>,
  Assert<IsEqual<EventDataMap["memory_saved"], MemorySavedEventData>>,
  Assert<IsEqual<EventDataMap["memory_logged"], MemoryLoggedEventData>>,
  Assert<IsEqual<EventDataMap["memory_extraction_start"], MemoryExtractionStartData>>,
  Assert<IsEqual<EventDataMap["memory_extraction_complete"], MemoryExtractionCompleteData>>,
  Assert<IsEqual<EventDataMap["memory_extraction_error"], MemoryExtractionErrorData>>,
  Assert<IsEqual<EventDataMap["auto_dream_start"], AutoDreamEventData>>,
  Assert<IsEqual<EventDataMap["auto_dream_progress"], AutoDreamEventData>>,
  Assert<IsEqual<EventDataMap["auto_dream_complete"], AutoDreamEventData>>,
  Assert<IsEqual<EventDataMap["auto_dream_error"], AutoDreamEventData>>,
  Assert<IsEqual<EventDataMap["auto_dream_cancelled"], AutoDreamEventData>>,
] = [
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
];

type EventManifest = {
  tui_to_core: string[];
  core_to_tui: string[];
  payload_types: Record<string, string>;
  payload_schemas: Record<string, unknown>;
};

function readEventManifest(): EventManifest {
  const url = new URL(
    "../../../../openspace/protocol/schema/events.json",
    import.meta.url,
  );
  return JSON.parse(readFileSync(url, "utf8")) as EventManifest;
}

test("event type runtime lists match the shared manifest", () => {
  const manifest = readEventManifest();
  assert.deepEqual([...TUI_TO_CORE_MSG_TYPES], manifest.tui_to_core);
  assert.deepEqual([...CORE_TO_TUI_MSG_TYPES], manifest.core_to_tui);
  assert.equal(isKnownEventType("tool_permission_ask"), true);
  assert.equal(isKnownEventType("bash_tool_command_executed"), true);
  assert.equal(isKnownEventType("background_housekeeping_idle"), true);
  assert.equal(
    isKnownEventType("background_housekeeping_cleanup_complete"),
    true,
  );
  assert.equal(isKnownEventType("not_a_protocol_event"), false);
  assert.ok(manifest.payload_schemas.tool_permission_response);
  assert.ok(manifest.payload_schemas.resume_session);
});

test("unknown event strategy produces a unified warning event", () => {
  const warning = makeProtocolWarningMessage(
    "not_a_protocol_event",
    "Unknown protocol event type",
  );
  assert.equal(warning.type, "notification");
  assert.equal(warning.data.level, "warn");
  assert.equal(warning.data.title, "Protocol warning");
  assert.equal(warning.data.event_type, "not_a_protocol_event");
});

test("tool permission cancel prompt matching is idempotent", () => {
  const cancel = {
    tool_use_id: "abc-123",
    reason: "timed out",
  };
  const queue = [
    "tool-permission-abc-123-choice",
    "tool-permission-abc-123-edit",
    "tool-permission-other-choice",
    "normal-prompt",
  ];

  const dismissed = queue.filter(
    promptId => !isToolPermissionPromptForCancel(promptId, cancel),
  );
  const dismissedAgain = dismissed.filter(
    promptId => !isToolPermissionPromptForCancel(promptId, cancel),
  );

  assert.deepEqual(dismissed, [
    "tool-permission-other-choice",
    "normal-prompt",
  ]);
  assert.deepEqual(dismissedAgain, dismissed);
});

test("tool permission cancel ignores empty ids", () => {
  assert.equal(
    isToolPermissionPromptForCancel(
      "tool-permission-abc-123-choice",
      { tool_use_id: "  " },
    ),
    false,
  );
});

test("tool permission cancel does not match prefix-colliding ids", () => {
  assert.equal(
    isToolPermissionPromptForCancel(
      "tool-permission-abc-123-choice",
      { tool_use_id: "abc" },
    ),
    false,
  );
  assert.equal(
    isToolPermissionPromptForCancel(
      "tool-permission-abc-123-edit",
      { tool_use_id: "abc-123" },
    ),
    true,
  );
});
