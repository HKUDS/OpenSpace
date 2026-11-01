import React from "react";
import { spawnSync } from "node:child_process";
import {
  appendFileSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  Box,
  Text,
  useApp,
} from "ink";
import {
  isToolPermissionPromptForCancel,
  type AutoDreamEventData,
  type AgentEventData,
  type AgentListData,
  type AgentSpawnData,
  type AgentTaskUpdateData,
  type AgentTranscriptData,
  type BackgroundSessionUpdateData,
  type CommandResultData,
  type CompactEventData,
  type DoctorResultData,
  type ElicitationRequestData,
  type IPCMessage,
  type HookEventData,
  type LLMCompleteData,
  type MemoryExtractionCompleteData,
  type MemoryExtractionErrorData,
  type MemoryExtractionStartData,
  type MemoryLoggedEventData,
  type MemorySavedEventData,
  type MemorySelectorData,
  type MemoryTargetData,
  type McpStatusData,
  type NotificationData,
  type PermissionRequestData,
  type PromptRequestData,
  type PromptResponseData,
  type RuntimeActivityData,
  type SessionListData,
  type SessionRestoredData,
  type SettingsUpdateData,
  type StatusUpdateData,
  type TaskCompleteData,
  type TaskErrorData,
  type TaskProgressData,
  type TaskStartData,
  type TokenWarningEventData,
  type ToolCompleteData,
  type ToolErrorData,
  type ToolProgressData,
  type ToolStartData,
  type ToolPermissionAskData,
  type ToolPermissionCancelData,
  type TeamUpdateData,
  type TodoUpdateData,
} from "../bridge/protocol.js";
import { getPermissionRequestSummary } from "../bridge/permissionRequestState.js";
import { isAskUserQuestionRequest } from "../bridge/askUserQuestionState.js";
import type { StructuredIO } from "../bridge/structuredIO.js";
import {
  AgentRuntimePane,
} from "../components/AgentRuntimePane.js";
import type {
  AgentTranscriptHandle,
} from "../components/AgentTranscriptPanel.js";
import {
  BackgroundControlsPanel,
} from "../components/BackgroundControlsPanel.js";
import {
  Messages,
  estimateMessagesContentHeight,
} from "../components/Messages.js";
import type {
  MessageActionsNav,
  MessageActionsState,
} from "../components/messageActions.js";
import { FullscreenLayout } from "../components/FullscreenLayout.js";
import PromptInput from "../components/PromptInput/PromptInput.js";
import {
  SlashCommandComplete,
  type CompletionItem,
} from "../components/PromptInput/SlashCommandComplete.js";
import { getModeFromInput } from "../components/PromptInput/inputModes.js";
import { StatusLine } from "../components/StatusLine.js";
import { getTheme, setTheme } from "../components/design-system/theme.js";
import { ElicitationDialog, type ElicitationField } from "../components/mcp/ElicitationDialog.js";
import { MCPPanel } from "../components/mcp/MCPPanel.js";
import { MemoryFileSelector } from "../components/memory/MemoryFileSelector.js";
import { PermissionRequest } from "../components/permissions/PermissionRequest.js";
import { PromptDialog } from "../components/prompts/PromptDialog.js";
import { TaskPanel } from "../components/tasks/TaskPanel.js";
import { BackgroundTasksPanel } from "../components/tasks/BackgroundTasksPanel.js";
import { TodoListPanel } from "../components/tasks/TodoListPanel.js";
import { MessageSelector } from "../components/transcript/MessageSelector.js";
import { TranscriptModeFooter } from "../components/transcript/TranscriptModeFooter.js";
import { TranscriptSearchBar } from "../components/transcript/TranscriptSearchBar.js";
import {
  getCommandCompletions,
  getSlashCommandDefinition,
  formatSlashCommandDetailText,
  formatSlashCommandHelpText,
  parseSlashCommandInput,
  type SlashCommandDefinition,
} from "../commands/registry.js";
import { useCanUseTool } from "../hooks/useCanUseTool.js";
import type { PermissionResolution } from "../hooks/toolPermission/PermissionContext.js";
import { useCancelRequest } from "../hooks/useCancelRequest.js";
import { useGlobalKeybindings } from "../hooks/useGlobalKeybindings.js";
import { useSettings } from "../hooks/useSettings.js";
import { useRuntimeTasks } from "../hooks/useRuntimeTasks.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { useVimInput } from "../hooks/useVimInput.js";
import {
  useKeybinding,
  useKeybindingInput,
  useKeybindings,
} from "../keybindings/useKeybinding.js";
import { useRegisterKeybindingContext } from "../keybindings/KeybindingContext.js";
import {
  getKeybindingsPath,
  loadKeybindingsSyncWithWarnings,
} from "../keybindings/loadUserBindings.js";
import type { ScrollBoxHandle } from "../ink/components/ScrollBox.js";
import type { JumpHandle } from "../components/VirtualMessageList.js";
import {
  useAppState,
  useSetAppState,
} from "../state/AppState.js";
import { selectMcpClientStates } from "../state/selectors.js";
import { applyMcpStatusUpdate } from "../state/AppStateStore.js";
import type {
  AppMessage,
  AgentRuntimeState,
  BackgroundAgentTaskState,
  SessionContextState,
  TodoItemState,
} from "../state/AppStateStore.js";
import type { VimMode } from "../types/textInputTypes.js";
import {
  createMessage,
  getMessageText,
  normalizeSessionContext,
  normalizeExternalMessages,
  serializeAppMessages,
  stringifyUnknown,
  truncate,
  useStructuredIOListener,
} from "./shared.js";
import {
  exportTranscriptToFile,
  openPathInExternalEditor,
  prepareTranscriptEditorFile,
  renderTranscriptToPlainText,
} from "../utils/transcript.js";
import {
  isBackspaceInput,
  isDeleteInput,
} from "../utils/keyInput.js";
import { formatStructuredValueForDisplay } from "../utils/structuredDisplay.js";
import { hasForegroundBackgroundTasks } from "../utils/backgroundTasks.js";
import { copyTextToClipboard } from "../utils/clipboard.js";
import { estimateWrappedRows } from "../utils/textWidth.js";

type Props = {
  io: StructuredIO | null;
  initialMessages?: AppMessage[];
  initialSessionId?: string;
  initialCost?: number | null;
  initialSessionContext?: {
    sessionId?: string;
    cost?: number | null;
    messages?: AppMessage[];
    context?: SessionContextState | null;
  };
};

const STREAMING_FLUSH_INTERVAL_MS = 80;
const MAX_PROMPT_INPUT_ROWS = 4;

function writeLayoutDebug(payload: Record<string, unknown>): void {
  const target = process.env.OPENSPACE_TUI_LAYOUT_DEBUG;
  if (!target) {
    return;
  }

  const filePath =
    target === "1"
      ? path.join(os.tmpdir(), "openspace-tui-layout.log")
      : target;
  try {
    appendFileSync(
      filePath,
      `${JSON.stringify({
        at: new Date().toISOString(),
        ...payload,
      })}\n`,
      "utf8",
    );
  } catch {
    // Debug logging must never interfere with TUI rendering.
  }
}

type ElicitationDraft = {
  request: ElicitationRequestData;
  values: Record<string, string>;
  activeField: number;
  error: string | null;
};

type ReplViewMode = "prompt" | "transcript";

type TranscriptSearchState = {
  active: boolean;
  query: string;
  matchCount: number;
  currentMatch: number;
};

type TranscriptSelectionState = {
  active: boolean;
  selectedIndex: number;
  targetIndex: number | null;
};

type TranscriptMutationState = "idle" | "rewind" | "restore";

const BUSY_ALLOWED_CORE_SLASH_COMMANDS = new Set(["cost", "effort"]);
const REWINDABLE_MESSAGE_ROLES = new Set<AppMessage["role"]>([
  "system",
  "user",
  "assistant",
  "tool",
]);

function clampMessageIndex(index: number, total: number): number {
  if (total <= 0) {
    return 0;
  }

  return Math.max(0, Math.min(total - 1, index));
}

function applyInputChunkBeforeSubmit(
  value: string,
  cursorOffset: number,
  rawInput: string,
): string | null {
  const submitIndex = rawInput.indexOf("\r");
  if (submitIndex < 0) {
    return null;
  }

  const beforeSubmit = Array.from(rawInput.slice(0, submitIndex))
    .filter(char => char >= " " && char !== "\u007f")
    .join("");
  const boundedOffset = Math.max(0, Math.min(value.length, cursorOffset));
  return `${value.slice(0, boundedOffset)}${beforeSubmit}${value.slice(boundedOffset)}`;
}

function normalizeRestoredRuntimePhase(
  phase: string | undefined,
): string | undefined {
  switch (phase) {
    case "query_complete":
    case "query_cancelled":
    case "completed":
    case "complete":
    case "success":
    case "succeeded":
    case "cancelled":
      return "idle";
    case "query_error":
      return "error";
    default:
      return phase;
  }
}

function isStaleSessionEvent(
  currentSessionId: string | undefined,
  eventSessionId: string | undefined,
): boolean {
  return Boolean(
    currentSessionId &&
      eventSessionId &&
      currentSessionId !== eventSessionId,
  );
}

function cycleAgentPanelTab(
  current: "list" | "events" | "transcript",
): "list" | "events" | "transcript" {
  switch (current) {
    case "list":
      return "events";
    case "events":
      return "transcript";
    case "transcript":
    default:
      return "list";
  }
}

function summarizeToolInput(input: Record<string, unknown> | undefined): string {
  if (!input || Object.keys(input).length === 0) {
    return "No input";
  }

  return truncate(stringifyUnknown(input), 140);
}

function summarizeToolResult(result: unknown): string {
  const rendered = stringifyUnknown(result);
  if (typeof rendered !== "string" || rendered.trim().length === 0) {
    return "No result";
  }
  return truncate(rendered.replace(/\s+/g, " ").trim(), 160);
}

function summarizeToolComplete(data: ToolCompleteData): string {
  if (data.result !== undefined) {
    return summarizeToolResult(data.result);
  }
  const status =
    typeof data.status === "string" && data.status.trim().length > 0
      ? data.status.trim()
      : "complete";
  const size =
    typeof data.result_size_chars === "number"
      ? ` (${data.result_size_chars} chars)`
      : "";
  return `${status}${size}`;
}

function updateToolUseMessage(
  messages: AppMessage[],
  toolUseId: string,
  text: string,
  patch: Record<string, unknown>,
): { messages: AppMessage[]; updated: boolean } {
  let updated = false;
  const nextMessages = messages.map(message => {
    if (updated || message.role !== "tool") {
      return message;
    }

    const blockIndex = message.content.findIndex(block => {
      if (block.type !== "tool_use") {
        return false;
      }
      return block.tool_use_id === toolUseId;
    });
    if (blockIndex < 0) {
      return message;
    }

    const nextContent = [...message.content];
    nextContent[blockIndex] = {
      ...nextContent[blockIndex],
      ...patch,
      type: "tool_use",
      tool_use_id: toolUseId,
    };
    updated = true;
    return {
      ...message,
      text,
      content: nextContent,
    };
  });

  return {
    messages: nextMessages,
    updated,
  };
}

function upsertActivityMessage(
  messages: AppMessage[],
  key: string,
  text: string,
  options?: {
    role?: AppMessage["role"];
    label?: string;
    status?: string;
    hidden?: boolean;
  },
): AppMessage[] {
  const role = options?.role ?? "status";
  const timestamp = Date.now();
  const meta = {
    activity: true,
    activityKey: key,
    activityLabel: options?.label ?? "Activity",
    activityStatus: options?.status,
    hidden: options?.hidden === true,
  };
  const content: AppMessage["content"] = [
    {
      type: "status",
      text,
      level: role === "error" ? "error" : "info",
    },
  ];
  const index = messages.findIndex(
    message => message.meta?.activityKey === key,
  );

  if (index < 0) {
    return [
      ...messages,
      createMessage(role, text, meta, content),
    ];
  }

  const nextMessages = [...messages];
  const current = nextMessages[index]!;
  nextMessages[index] = {
    ...current,
    role,
    text,
    content,
    timestamp,
    meta: {
      ...current.meta,
      ...meta,
    },
  };
  return nextMessages;
}

function findAppendableStreamingAssistantIndex(
  messages: AppMessage[],
): number {
  let onlyHiddenAfter = true;

  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!;
    if (
      message.role === "assistant" &&
      message.meta?.streaming === true
    ) {
      return onlyHiddenAfter ? index : -1;
    }

    if (message.meta?.hidden !== true) {
      onlyHiddenAfter = false;
    }
  }

  return -1;
}

function findLastStreamingAssistantIndex(
  messages: AppMessage[],
): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!;
    if (
      message.role === "assistant" &&
      message.meta?.streaming === true
    ) {
      return index;
    }
  }

  return -1;
}

function getCompactTriggerLabel(
  trigger: CompactEventData["trigger"] | undefined,
): string | null {
  switch (trigger) {
    case "auto":
      return "auto-triggered";
    case "manual":
      return "manual";
    case "prompt_too_long":
      return "prompt too long";
    default:
      return null;
  }
}

function formatCompactProgressMessage(data: CompactEventData): string {
  const trigger = getCompactTriggerLabel(data.trigger);
  const direction = data.direction ? `, ${data.direction}` : "";
  const suffix = trigger ? ` (${trigger}${direction})` : "";
  return `Compressing conversation...${suffix}`;
}

function formatCompactCompleteMessage(data: CompactEventData): string {
  if (data.success === false) {
    return "Context compaction failed";
  }

  const pre = data.pre_compact_token_count;
  const post =
    data.true_post_compact_token_count ?? data.post_compact_token_count;
  const saved =
    data.tokens_saved ??
    (typeof pre === "number" && typeof post === "number"
      ? Math.max(0, pre - post)
      : undefined);
  const trigger = getCompactTriggerLabel(data.trigger);
  const prefix = trigger ? `Context compacted (${trigger})` : "Context compacted";
  return typeof saved === "number" && saved > 0
    ? `${prefix}: saved ${saved.toLocaleString()} tokens`
    : prefix;
}

function formatMemoryPaths(paths: string[] | undefined, fallback: string): string {
  const clean = (paths ?? []).filter(path => path.trim().length > 0);
  if (clean.length === 0) {
    return fallback;
  }
  if (clean.length === 1) {
    return clean[0]!;
  }
  return `${clean.length} files`;
}

function memorySavedVerb(data: MemorySavedEventData): string {
  if (data.verb) {
    return data.verb;
  }
  return data.source === "daily_log" ? "Improved from logs" : "Saved";
}

function formatMemorySavedMessage(data: MemorySavedEventData): string {
  const verb = memorySavedVerb(data);
  const target = formatMemoryPaths(data.memory_paths, "memory");
  if (verb === "Improved" && data.source === "daily_log") {
    return `Improved from logs: ${target}`;
  }
  return `${verb}: ${target}`;
}

function formatMemoryLoggedMessage(data: MemoryLoggedEventData): string {
  const count =
    data.entry_count ??
    (Array.isArray(data.entry_ids) ? data.entry_ids.length : undefined);
  const paths = data.log_paths ?? (data.log_path ? [data.log_path] : []);
  const target = formatMemoryPaths(paths, "daily log");
  const countText =
    typeof count === "number" && count > 0
      ? `${count} note${count === 1 ? "" : "s"}`
      : "notes";
  return `Logged ${countText} to ${target}`;
}

function formatAutoDreamOperation(
  event: string,
  data: AutoDreamEventData,
): string {
  if (event === "auto_dream_error") {
    return data.error ? `Dream failed: ${data.error}` : "Dream failed";
  }
  if (event === "auto_dream_cancelled") {
    return "Dream cancelled";
  }
  if (event === "auto_dream_complete") {
    const touched = data.files_touched?.length ?? 0;
    if (touched > 0) {
      return `Dream complete: improved ${touched} file${touched === 1 ? "" : "s"}`;
    }
    return "Dream complete: no memory changes";
  }
  const phase = data.phase ?? "starting";
  const touched = data.files_touched?.length ?? 0;
  const reviewed = data.sessions_reviewed ?? data.sessions_reviewing;
  const parts = [`Dream ${phase}`];
  if (typeof reviewed === "number") {
    parts.push(`reviewing ${reviewed} session${reviewed === 1 ? "" : "s"}`);
  }
  if (touched > 0) {
    parts.push(`${touched} file${touched === 1 ? "" : "s"} touched`);
  }
  return parts.join(" - ");
}

const TASK_EVENT_STATUS: Record<string, string> = {
  agent_task_complete: "completed",
  task_started: "running",
  task_completed: "completed",
  task_failed: "failed",
  task_stopped: "killed",
};

const AGENT_RUNTIME_EVENT_STATUS: Record<string, string> = {
  agent_start: "running",
  agent_progress: "running",
  agent_output: "running",
  agent_error: "failed",
  agent_complete: "completed",
};

const RUNTIME_ACTIVITY_EVENTS = new Set([
  "hook_message",
  "hook_non_blocking_error",
  "stop_hook_summary",
  "stop_hook_message",
  "conversation_recovery",
  "token_budget_continue",
  "token_budget_completed",
  "time_based_microcompact",
  "nested_memory_consumed",
  "dynamic_skills_consumed",
  "skill_discovery_prefetch_consumed",
  "cost_summary",
  "side_query_start",
  "side_query_complete",
  "side_query_cancelled",
  "side_query_error",
  "llm_retry",
  "memory_extraction_skipped",
  "memory_extraction_coalesced",
  "memory_extraction_trailing_start",
  "session_memory_extraction_skipped",
  "session_memory_extraction_complete",
  "session_memory_extraction_error",
  "session_memory_extraction_coalesced",
  "session_memory_extraction_trailing_start",
  "session_memory_compact",
  "session_memory_compact_skipped",
  "session_memory_compact_resumed_session",
  "session_memory_checked",
  "session_memory_updated",
  "tool_cancelled",
  "tool_deferred_intercepted",
  "tool_executing",
  "tool_hook_stopped",
  "tool_permission_denied",
  "tool_pipeline_complete",
  "tool_quality_recorded",
  "tool_validation_error",
  "task_finished_pre_persist",
  "task_session_persisted",
  "background_drain",
  "memory_background_drain_timeout",
  "background_housekeeping_drain_timeout",
  "background_housekeeping_idle",
  "background_housekeeping_cleanup_complete",
  "background_housekeeping_recurring_cleanup",
  "quality_signal_checkpoint_warning",
]);

const QUIET_RUNTIME_ACTIVITY_EVENTS = new Set([
  "memory_background_drain_timeout",
  "background_housekeeping_drain_timeout",
]);

const BACKGROUND_TERMINAL_STATUSES = new Set([
  "cancelled",
  "canceled",
  "completed",
  "failed",
  "killed",
  "stopped",
  "success",
  "error",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function getStringValue(
  record: Record<string, unknown>,
  keys: string[],
): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      return String(value);
    }
  }
  return undefined;
}

function getNumberValue(
  record: Record<string, unknown>,
  keys: string[],
): number | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return undefined;
}

function eventTimestamp(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return fallback;
}

function formatEventName(event: string): string {
  return event
    .split("_")
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function extractRuntimeMessage(payload: Record<string, unknown>): string | null {
  const direct = getStringValue(payload, [
    "current_operation",
    "summary",
    "message",
    "description",
    "content",
    "error",
    "reason",
    "status",
  ]);
  if (direct) {
    return direct;
  }

  const nestedMessage = payload.message;
  if (isRecord(nestedMessage)) {
    return (
      getStringValue(nestedMessage, ["content", "text", "message", "summary"]) ??
      formatStructuredValueForDisplay(nestedMessage) ??
      truncate(stringifyUnknown(nestedMessage), 160)
    );
  }

  return null;
}

function runtimeActivityKey(
  event: string,
  payload: Record<string, unknown>,
): string {
  const stableId =
    getStringValue(payload, ["task_id", "taskId", "agent_id", "agentId"]) ??
    getStringValue(payload, ["hook_name", "hookName", "hook_event"]) ??
    getStringValue(payload, ["tool_use_id", "toolUseId"]) ??
    getStringValue(payload, ["event_id", "eventId"]) ??
    "active";
  return `runtime:${event}:${stableId}`;
}

function runtimeActivityStatus(
  event: string,
  payload: Record<string, unknown>,
): string {
  return (
    getStringValue(payload, ["status", "outcome"]) ??
    AGENT_RUNTIME_EVENT_STATUS[event] ??
    (event.endsWith("_complete") || event.endsWith("_completed")
      ? "completed"
      : event.endsWith("_error") || event.endsWith("_failed")
        ? "failed"
        : event.endsWith("_denied")
          ? "denied"
          : event.endsWith("_start") || event.endsWith("_started")
            ? "running"
            : "updated")
  );
}

function runtimeActivityRole(status: string): AppMessage["role"] {
  return status === "failed" || status === "error" || status === "denied"
    ? "error"
    : "status";
}

function shouldDisplayAgentRuntimeActivity(
  event: string,
  status: string,
): boolean {
  return (
    event === "agent_error" ||
    status === "failed" ||
    status === "error" ||
    status === "denied"
  );
}

function shouldDisplayRuntimeActivity(
  event: string,
  status: string,
): boolean {
  if (
    status === "failed" ||
    status === "error" ||
    status === "denied"
  ) {
    return true;
  }

  if (QUIET_RUNTIME_ACTIVITY_EVENTS.has(event)) {
    return false;
  }

  return (
    event.includes("error") ||
    event.includes("failed") ||
    event.includes("warning") ||
    event.includes("timeout") ||
    event.includes("denied") ||
    event === "llm_retry" ||
    event === "conversation_recovery"
  );
}

function formatHookActivity(
  event: "hook_start" | "hook_complete",
  data: HookEventData,
): string {
  const payload = asRecord(data);
  const hookName =
    getStringValue(payload, ["hook_name", "hookName", "hook_event"]) ?? "hook";
  const count = getNumberValue(payload, ["hook_count", "hookCount"]);
  if (event === "hook_start") {
    return count && count > 1
      ? `Hook ${hookName} running (${count})`
      : `Hook ${hookName} running`;
  }
  return `Hook ${hookName} completed`;
}

function formatAgentRuntimeActivity(
  event: string,
  data: RuntimeActivityData,
): string {
  const payload = asRecord(data);
  const name =
    getStringValue(payload, ["name", "agent_name", "agentName", "agent_id"]) ??
    "Agent";
  const message = extractRuntimeMessage(payload);
  switch (event) {
    case "agent_start":
      return message ? `${name} started: ${message}` : `${name} started`;
    case "agent_progress":
      return message ? `${name}: ${message}` : `${name} running`;
    case "agent_output":
      return message ? `${name} output: ${message}` : `${name} output`;
    case "agent_error":
      return message ? `${name} failed: ${message}` : `${name} failed`;
    case "agent_complete":
      return message ? `${name} completed: ${message}` : `${name} completed`;
    default:
      return message ? `${name}: ${message}` : `${name} updated`;
  }
}

function formatRuntimeActivity(
  event: string,
  data: RuntimeActivityData,
): string {
  const payload = asRecord(data);
  const message = extractRuntimeMessage(payload);
  const label = formatEventName(event);

  if (event === "llm_retry") {
    return message ? `LLM retry: ${message}` : "LLM retrying";
  }
  if (event === "cost_summary") {
    return "Cost summary updated";
  }
  if (event === "conversation_recovery") {
    return message ? `Conversation recovery: ${message}` : "Conversation recovery";
  }
  if (event === "token_budget_continue") {
    const pct = getNumberValue(payload, ["pct"]);
    return pct !== undefined
      ? `Token budget continuation: ${pct}%`
      : "Token budget continuation";
  }
  if (event === "token_budget_completed") {
    return "Token budget completed";
  }

  return message ? `${label}: ${message}` : label;
}

function normalizeTodoItems(raw: unknown): TodoItemState[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.flatMap(item => {
    const record = asRecord(item);
    const content = getStringValue(record, ["content"]);
    const status = getStringValue(record, ["status"]);
    if (!content || !status) {
      return [];
    }
    return [
      {
        content,
        status,
        activeForm: getStringValue(record, ["activeForm", "active_form"]),
      },
    ];
  });
}

function upsertAgentRecord(
  list: Array<Record<string, unknown>>,
  payload: Record<string, unknown>,
  timestamp: number,
): Array<Record<string, unknown>> {
  const agentId = getStringValue(payload, ["agent_id", "agentId", "id"]);
  if (!agentId) {
    return list;
  }
  const existing = list.find(agent => agent.agent_id === agentId || agent.id === agentId);
  const patch: Record<string, unknown> = {
    agent_id: agentId,
    id: agentId,
    updatedAt: timestamp,
  };
  for (const [target, keys] of Object.entries({
    name: ["name", "agent_name", "agent_type"],
    agent_type: ["agent_type", "type"],
    status: ["status"],
    task_id: ["task_id", "taskId"],
    team_name: ["team_name", "teamName"],
    model: ["model"],
    description: ["description"],
    summary: ["summary", "current_operation", "currentOperation", "description"],
  })) {
    const value = getStringValue(payload, keys);
    if (value !== undefined) {
      patch[target] = value;
    }
  }

  if (!existing) {
    return [...list, patch];
  }
  return list.map(agent =>
    agent === existing
      ? {
          ...agent,
          ...patch,
        }
      : agent,
  );
}

function backgroundTaskFromPayload(
  current: BackgroundAgentTaskState | undefined,
  payload: Record<string, unknown>,
  timestamp: number,
): BackgroundAgentTaskState | null {
  const progress = asRecord(payload.progress);
  const taskId =
    getStringValue(payload, ["task_id", "taskId", "id"]) ??
    getStringValue(payload, ["agent_id", "agentId"]);
  const agentId =
    getStringValue(payload, ["agent_id", "agentId"]) ??
    taskId;
  if (!taskId || !agentId) {
    return null;
  }

  const incomingStatus =
    getStringValue(payload, ["status", "state"]) ??
    current?.status ??
    "running";
  const status =
    current !== undefined &&
    BACKGROUND_TERMINAL_STATUSES.has(current.status.toLowerCase()) &&
    !BACKGROUND_TERMINAL_STATUSES.has(incomingStatus.toLowerCase())
      ? current.status
      : incomingStatus;
  const startedAt =
    getNumberValue(payload, ["start_time", "startedAt", "created_at"]) ??
    current?.startedAt ??
    timestamp;
  const endTime = getNumberValue(payload, ["end_time", "completedAt"]);
  const completedAt = BACKGROUND_TERMINAL_STATUSES.has(status.toLowerCase())
    ? endTime ?? current?.completedAt ?? timestamp
    : undefined;
  const currentOperation =
    getStringValue(payload, ["current_operation", "currentOperation", "activity"]) ??
    getStringValue(progress, ["current_operation", "summary"]) ??
    getStringValue(payload, ["summary", "description"]);

  return {
    ...(current ?? {
      id: taskId,
      agentId,
      startedAt,
      updatedAt: timestamp,
      status,
    }),
    id: taskId,
    agentId,
    name: getStringValue(payload, ["name", "agent_name"]) ?? current?.name,
    agentType:
      getStringValue(payload, ["agent_type", "type"]) ?? current?.agentType,
    taskType:
      getStringValue(payload, ["task_type", "taskType"]) ?? current?.taskType,
    teamName:
      getStringValue(payload, ["team_name", "teamName"]) ?? current?.teamName,
    status,
    description:
      getStringValue(payload, ["description"]) ?? current?.description,
    currentOperation,
    startedAt,
    updatedAt: timestamp,
    completedAt,
    outputFile:
      getStringValue(payload, ["output_file", "outputFile"]) ?? current?.outputFile,
    outputTail:
      getStringValue(payload, ["output_tail", "outputTail"]) ?? current?.outputTail,
    parentTaskId:
      getStringValue(payload, ["parent_task_id", "parentTaskId"]) ??
      current?.parentTaskId,
    model: getStringValue(payload, ["model"]) ?? current?.model,
    background:
      typeof payload.background === "boolean"
        ? payload.background
        : typeof payload.is_backgrounded === "boolean"
          ? payload.is_backgrounded
          : current?.background,
    metadata: {
      ...(current?.metadata ?? {}),
      ...payload,
    },
  };
}

function appendAgentEvent(
  agents: AgentRuntimeState,
  event: string,
  agentId: string,
  payload: Record<string, unknown>,
  timestamp: number,
  sessionId?: string,
): AgentRuntimeState {
  const nextEvents = [
    ...agents.events.slice(-99),
    {
      id: `agent-event-${timestamp}-${Math.random().toString(36).slice(2, 8)}`,
      agentId,
      event,
      timestamp,
      payload,
    },
  ];
  return {
    ...agents,
    sessionId: sessionId ?? agents.sessionId,
    viewedAgentId: agents.viewedAgentId ?? agentId,
    events: nextEvents,
    selectedEventIndex: Math.max(0, nextEvents.length - 1),
  };
}

function applyAgentPayloadEvent(
  agents: AgentRuntimeState,
  event: string,
  payload: Record<string, unknown>,
  timestamp: number,
  sessionId?: string,
): AgentRuntimeState {
  const normalizedStatus =
    TASK_EVENT_STATUS[event] ?? AGENT_RUNTIME_EVENT_STATUS[event];
  const eventName = normalizedStatus ? "agent_task_update" : event;
  const eventPayload = normalizedStatus
    ? {
        ...payload,
        status: getStringValue(payload, ["status"]) ?? normalizedStatus,
        raw_event: event,
      }
    : payload;
  const agentId =
    getStringValue(eventPayload, ["agent_id", "agentId"]) ??
    getStringValue(eventPayload, ["task_id", "taskId", "id"]) ??
    "primary";
  let next = appendAgentEvent(
    agents,
    eventName,
    agentId,
    eventPayload,
    timestamp,
    sessionId,
  );

  const payloadTeamName = getStringValue(eventPayload, ["team_name", "teamName"]);
  const payloadStatus = getStringValue(eventPayload, ["status", "state"]);
  const updateCoordinator = (
    patch: Partial<AgentRuntimeState["coordinator"]>,
  ): void => {
    next = {
      ...next,
      coordinator: {
        ...next.coordinator,
        ...patch,
        updatedAt: timestamp,
      },
    };
  };

  if (eventName === "agent_spawn" || eventName === "agent_task_update") {
    const task = backgroundTaskFromPayload(
      next.backgroundTasks[
        getStringValue(eventPayload, ["task_id", "taskId", "id"]) ?? agentId
      ],
      eventPayload,
      timestamp,
    );
    next = {
      ...next,
      list: upsertAgentRecord(next.list, eventPayload, timestamp),
      backgroundTasks:
        task === null
          ? next.backgroundTasks
          : {
              ...next.backgroundTasks,
              [task.id]: task,
            },
    };
    if (payloadTeamName || payloadStatus) {
      const workers = Object.values(next.backgroundTasks).filter(taskState => {
        const taskTeamName = taskState.teamName;
        return (
          !payloadTeamName ||
          taskTeamName === undefined ||
          taskTeamName === payloadTeamName
        );
      });
      const runningWorkers = workers.filter(
        taskState =>
          !BACKGROUND_TERMINAL_STATUSES.has(taskState.status.toLowerCase()),
      ).length;
      updateCoordinator({
        teamName: payloadTeamName ?? next.coordinator.teamName,
        status: payloadStatus ?? next.coordinator.status,
        runningWorkers,
        totalWorkers: Math.max(next.coordinator.totalWorkers, workers.length),
        message:
          getStringValue(eventPayload, [
            "summary",
            "current_operation",
            "currentOperation",
          ]) ?? next.coordinator.message,
      });
    }
  } else if (eventName === "team_update") {
    const nextCoordinatorStatus =
      getStringValue(eventPayload, ["status", "action"]) ??
      next.coordinator.status;
    const reportedRunningWorkers = getNumberValue(eventPayload, [
      "running_workers",
      "runningWorkers",
    ]);
    next = {
      ...next,
      coordinator: {
        teamName: payloadTeamName ?? next.coordinator.teamName,
        status: nextCoordinatorStatus,
        runningWorkers:
          reportedRunningWorkers ??
          (nextCoordinatorStatus !== undefined &&
          BACKGROUND_TERMINAL_STATUSES.has(nextCoordinatorStatus.toLowerCase())
            ? 0
            : next.coordinator.runningWorkers),
        totalWorkers:
          getNumberValue(eventPayload, ["total_workers", "totalWorkers"]) ??
          next.coordinator.totalWorkers,
        updatedAt: timestamp,
        message:
          getStringValue(eventPayload, ["message"]) ?? next.coordinator.message,
      },
    };
  } else if (eventName === "todo_update") {
    const todos = normalizeTodoItems(
      eventPayload.storedTodos ?? eventPayload.todos ?? eventPayload.newTodos,
    );
    next = {
      ...next,
      todos: {
        key: getStringValue(eventPayload, ["todo_key", "todoKey"]) ?? next.todos.key,
        agentId:
          getStringValue(eventPayload, ["agent_id", "agentId"]) ??
          next.todos.agentId,
        todos,
        oldTodos: normalizeTodoItems(eventPayload.oldTodos),
        updatedAt: timestamp,
        verificationNudgeNeeded:
          typeof eventPayload.verificationNudgeNeeded === "boolean"
            ? eventPayload.verificationNudgeNeeded
            : next.todos.verificationNudgeNeeded,
      },
    };
  }

  return next;
}

function applyAgentEventData(
  agents: AgentRuntimeState,
  data: AgentEventData,
  fallbackTimestamp: number,
): AgentRuntimeState {
  const payload = asRecord(data.payload);
  const event =
    data.event ??
    data.event_type ??
    data.kind ??
    getStringValue(payload, ["event", "event_type", "kind"]) ??
    "update";
  const agentId =
    data.agent_id ??
    getStringValue(payload, ["agent_id", "agentId"]) ??
    "primary";
  const timestamp = eventTimestamp(data.timestamp, fallbackTimestamp);
  return applyAgentPayloadEvent(
    agents,
    event,
    {
      ...payload,
      agent_id: getStringValue(payload, ["agent_id", "agentId"]) ?? agentId,
    },
    timestamp,
    data.session_id,
  );
}

function formatPermissionResolution(resolution: PermissionResolution): string {
  if (typeof resolution === "string") {
    return resolution;
  }
  if ("decision" in resolution) {
    return resolution.decision;
  }

  switch (resolution.option_id) {
    case "allow_always":
      return "allow_always";
    case "allow_once":
      return "allow";
    case "provide_input":
      return "allow_with_input";
    case "deny":
    default:
      return "deny";
  }
}

function isCopyableMessage(message: AppMessage): boolean {
  return message.meta?.hidden !== true && message.meta?.budget !== true;
}

function isRewindableMessage(message: AppMessage): boolean {
  return isCopyableMessage(message) && REWINDABLE_MESSAGE_ROLES.has(message.role);
}

function extractSchemaFields(
  schema: Record<string, unknown> | undefined,
): ElicitationField[] {
  const properties =
    schema &&
    typeof schema === "object" &&
    "properties" in schema &&
    typeof schema.properties === "object" &&
    schema.properties !== null
      ? (schema.properties as Record<string, unknown>)
      : {};

  const required = Array.isArray(schema?.required)
    ? new Set(
        schema.required.filter(
          (field): field is string => typeof field === "string",
        ),
      )
    : new Set<string>();

  return Object.entries(properties).map(([key, raw]) => {
    const property =
      typeof raw === "object" && raw !== null
        ? (raw as Record<string, unknown>)
        : {};
    const type =
      typeof property.type === "string" ? property.type : "string";
    const defaultValue =
      property.default === undefined
        ? undefined
        : String(property.default);

    return {
      key,
      type,
      title:
        typeof property.title === "string"
          ? property.title
          : key,
      required: required.has(key),
      description:
        typeof property.description === "string"
          ? property.description
          : undefined,
      defaultValue,
    };
  });
}

function parseElicitationValues(
  draft: ElicitationDraft,
  fields: ElicitationField[],
): {
  ok: boolean;
  values: Record<string, unknown>;
  error: string | null;
} {
  const parsed: Record<string, unknown> = {};

  for (const field of fields) {
    const rawValue = (draft.values[field.key] ?? "").trim();

    if (!rawValue) {
      if (field.required) {
        return {
          ok: false,
          values: {},
          error: `Missing required field: ${field.key}`,
        };
      }
      continue;
    }

    switch (field.type) {
      case "number": {
        const numberValue = Number(rawValue);
        if (Number.isNaN(numberValue)) {
          return {
            ok: false,
            values: {},
            error: `Field ${field.key} must be a number`,
          };
        }
        parsed[field.key] = numberValue;
        break;
      }

      case "integer": {
        const integerValue = Number.parseInt(rawValue, 10);
        if (Number.isNaN(integerValue)) {
          return {
            ok: false,
            values: {},
            error: `Field ${field.key} must be an integer`,
          };
        }
        parsed[field.key] = integerValue;
        break;
      }

      case "boolean": {
        if (rawValue !== "true" && rawValue !== "false") {
          return {
            ok: false,
            values: {},
            error: `Field ${field.key} must be true or false`,
          };
        }
        parsed[field.key] = rawValue === "true";
        break;
      }

      case "array":
      case "object": {
        try {
          parsed[field.key] = JSON.parse(rawValue);
        } catch {
          return {
            ok: false,
            values: {},
            error: `Field ${field.key} must be valid JSON`,
          };
        }
        break;
      }

      default:
        parsed[field.key] = rawValue;
        break;
    }
  }

  return {
    ok: true,
    values: parsed,
    error: null,
  };
}

function editTextInExternalEditor(
  initialValue: string,
): {
  ok: boolean;
  value?: string;
  error?: string;
  filePath?: string;
} {
  const editor = process.env.VISUAL || process.env.EDITOR;
  const dir = mkdtempSync(path.join(os.tmpdir(), "openspace-input-"));
  const filePath = path.join(dir, "prompt.md");
  writeFileSync(filePath, initialValue, "utf8");

  if (!editor) {
    return {
      ok: false,
      error: "Set $EDITOR or $VISUAL to use the external editor.",
      filePath,
    };
  }

  const result = spawnSync(editor, [filePath], {
    stdio: "inherit",
    shell: true,
  });
  if (result.error) {
    return {
      ok: false,
      error: result.error.message,
      filePath,
    };
  }
  if (result.status !== 0) {
    return {
      ok: false,
      error: `Editor exited with status ${String(result.status)}`,
      filePath,
    };
  }

  const nextValue = readFileSync(filePath, "utf8");
  rmSync(dir, { recursive: true, force: true });
  return {
    ok: true,
    value: nextValue,
  };
}

export function REPL({
  io,
  initialMessages,
  initialSessionId,
  initialCost,
  initialSessionContext,
}: Props): React.ReactElement {
  const { exit } = useApp();
  const setAppState = useSetAppState();
  const size = useTerminalSize();

  const input = useAppState(state => state.input);
  const messages = useAppState(state => state.messages);
  const isQuerying = useAppState(state => state.isQuerying);
  const notifications = useAppState(state => state.notifications);
  const runtime = useAppState(state => state.runtime);
  const sessionContext = useAppState(state => state.sessionContext);
  const mainLoopModel = useAppState(state => state.mainLoopModel);
  const mcpClientStates = useAppState(selectMcpClientStates);
  const mcpState = useAppState(state => state.mcp);
  const tasks = useAppState(state => state.tasks);
  const agentsRuntime = useAppState(state => state.agents);
  const backgroundSession = useAppState(state => state.backgroundSession);
  const commandHistory = useAppState(state => state.commandHistory);
  const expandedView = useAppState(state => state.expandedView);
  const footerSelection = useAppState(state => state.footerSelection);
  const promptQueue = useAppState(state => state.promptDialog.queue);
  const inputRef = React.useRef(input);
  const initRef = React.useRef(false);
  const settingsApi = useSettings();

  const [inputCursorOffset, setInputCursorOffsetState] = React.useState(0);
  const inputCursorOffsetRef = React.useRef(inputCursorOffset);
  const setInputCursorOffset = React.useCallback((offset: number): void => {
    inputCursorOffsetRef.current = offset;
    setInputCursorOffsetState(offset);
  }, []);
  const [vimMode, setVimMode] = React.useState<VimMode>("INSERT");
  const [compactProgress, setCompactProgress] =
    React.useState<CompactEventData | null>(null);
  const [dreamProgress, setDreamProgress] =
    React.useState<AutoDreamEventData | null>(null);
  const [memorySelector, setMemorySelector] =
    React.useState<MemorySelectorData | null>(null);
  const [elicitationQueue, setElicitationQueue] = React.useState<
    ElicitationDraft[]
  >([]);
  const [selectedSlashSuggestion, setSelectedSlashSuggestion] = React.useState(0);
  const [dismissedSlashInput, setDismissedSlashInput] = React.useState<
    string | null
  >(null);
  const [viewMode, setViewMode] = React.useState<ReplViewMode>("prompt");
  const [showAllInTranscript, setShowAllInTranscript] = React.useState(false);
  const [transcriptCursorId, setTranscriptCursorId] =
    React.useState<string | null>(null);
  const [transcriptSearch, setTranscriptSearch] =
    React.useState<TranscriptSearchState>({
      active: false,
      query: "",
      matchCount: 0,
      currentMatch: 0,
    });
  const [agentTranscriptSearch, setAgentTranscriptSearch] =
    React.useState<TranscriptSearchState>({
      active: false,
      query: "",
      matchCount: 0,
      currentMatch: 0,
    });
  const [transcriptSelection, setTranscriptSelection] =
    React.useState<TranscriptSelectionState>({
      active: false,
      selectedIndex: 0,
      targetIndex: null,
    });
  const [transcriptMutation, setTranscriptMutation] =
    React.useState<TranscriptMutationState>("idle");
  const [transcriptRestoreBuffer, setTranscriptRestoreBuffer] =
    React.useState<AppMessage[] | null>(null);
  const scrollRef = React.useRef<ScrollBoxHandle | null>(null);
  const modalScrollRef = React.useRef<ScrollBoxHandle | null>(null);
  const jumpRef = React.useRef<JumpHandle | null>(null);
  const transcriptCursorNavRef =
    React.useRef<MessageActionsNav | null>(null);
  const promptInputModeRef = React.useRef<
    ((nextMode: VimMode) => void) | null
  >(null);
  const agentTranscriptRef =
    React.useRef<AgentTranscriptHandle | null>(null);
  const pendingAssistantTokensRef = React.useRef<string[]>([]);
  const streamingFlushTimerRef = React.useRef<NodeJS.Timeout | null>(null);
  const runSequenceRef = React.useRef(0);
  const activeRunActivityKeyRef = React.useRef("query:0");

  const {
    permissionQueue,
    activePermission,
    enqueuePermissionRequest,
    resolvePermissionRequest,
    cancelPermissionRequest,
  } = useCanUseTool(io);
  const {
    applyStatusUpdate,
    markTaskStart,
    markTaskProgress,
    markTaskComplete,
    markTaskError,
  } = useRuntimeTasks();

  const activeElicitation = elicitationQueue[0] ?? null;
  const activePrompt = promptQueue[0] ?? null;
  const isTranscriptMode = viewMode === "transcript";
  const copyableMessages = React.useMemo(
    () => messages.filter(isCopyableMessage),
    [messages],
  );
  const rewindableMessages = React.useMemo(
    () => messages.filter(isRewindableMessage),
    [messages],
  );
  const selectedTranscriptMessage =
    transcriptSelection.targetIndex !== null
      ? rewindableMessages[transcriptSelection.targetIndex] ?? null
      : null;
  const selectedTranscriptCursorMessage =
    transcriptCursorId !== null
      ? messages.find(message => message.id === transcriptCursorId) ?? null
      : null;
  const selectedTranscriptCursorIndex =
    selectedTranscriptCursorMessage !== null
      ? copyableMessages.findIndex(
          message => message.id === selectedTranscriptCursorMessage.id,
        )
      : -1;
  const elicitationFields = activeElicitation
    ? extractSchemaFields(activeElicitation.request.schema)
    : [];

  const slashSuggestions = React.useMemo(() => {
    if (!input.startsWith("/")) {
      return [];
    }

    const commandPortion = input.slice(1);
    if (commandPortion.includes(" ")) {
      return [];
    }

    return getCommandCompletions(commandPortion.trim());
  }, [input]);
  const slashCommandPortion = React.useMemo(() => {
    if (!input.startsWith("/")) {
      return "";
    }

    const commandPortion = input.slice(1);
    return commandPortion.includes(" ")
      ? ""
      : commandPortion.trim().toLowerCase();
  }, [input]);

  const showSlashSuggestions =
    slashSuggestions.length > 0 &&
    (slashCommandPortion
      ? getSlashCommandDefinition(slashCommandPortion) === null
      : true) &&
    dismissedSlashInput !== input;
  const slashCompletionItems = React.useMemo<CompletionItem[]>(
    () =>
      slashSuggestions.map(item => ({
        name: item.name,
        summary: item.summary,
        category: item.category,
      })),
    [slashSuggestions],
  );

  React.useLayoutEffect(() => {
    inputRef.current = input;
  }, [input]);

  const setRuntimeViewMode = React.useCallback(
    (nextMode: "prompt" | "transcript" | "agent"): void => {
      setAppState(prev => ({
        ...prev,
        runtime: {
          ...prev.runtime,
          viewMode: nextMode,
        },
      }));
    },
    [setAppState],
  );

  React.useEffect(() => {
    setSelectedSlashSuggestion(0);
  }, [slashCommandPortion]);

  React.useEffect(() => {
    setSelectedSlashSuggestion(current =>
      slashSuggestions.length === 0
        ? 0
        : Math.min(current, slashSuggestions.length - 1),
    );
  }, [slashSuggestions.length]);

  React.useEffect(() => {
    if (!input.startsWith("/")) {
      setDismissedSlashInput(null);
      return;
    }

    if (dismissedSlashInput !== null && dismissedSlashInput !== input) {
      setDismissedSlashInput(null);
    }
  }, [dismissedSlashInput, input]);

  const statusLineRows = 4;
  const promptInputRows = input
    ? Math.max(
        1,
        Math.min(
          MAX_PROMPT_INPUT_ROWS,
          estimateWrappedRows(input, Math.max(10, size.columns - 8)),
        ),
      )
    : 1;
  const promptRows = promptInputRows + 5;
  const selectorRows = memorySelector
    ? Math.max(8, Math.min(16, memorySelector.targets.length * 2 + 5))
    : 0;
  const transcriptRows = isTranscriptMode ? 3 : 0;
  const modalRows =
    activeElicitation ||
    activePrompt ||
    (isTranscriptMode && transcriptSelection.active)
      ? Math.max(8, Math.floor(size.rows * 0.35)) + 2
      : 0;
  const maxSlashSuggestionRows = Math.max(
    0,
    size.rows -
      statusLineRows -
      promptRows -
      selectorRows -
      modalRows -
      transcriptRows -
      4,
  );
  const slashSuggestionItemRows =
    showSlashSuggestions && maxSlashSuggestionRows > 0
      ? Math.min(
          slashSuggestions.length,
          slashSuggestions.length > maxSlashSuggestionRows
            ? Math.max(1, maxSlashSuggestionRows - 1)
            : maxSlashSuggestionRows,
        )
      : 0;
  const slashSuggestionRows =
    slashSuggestionItemRows +
    (showSlashSuggestions && slashSuggestionItemRows < slashSuggestions.length
      ? 1
      : 0);
  const bottomRows = activePermission !== null
    ? 0
    : selectorRows +
      (isTranscriptMode ? transcriptRows : promptRows);
  const messageRows = Math.max(
    4,
    size.rows -
      statusLineRows -
      modalRows -
      bottomRows,
  );

  useRegisterKeybindingContext(
    "Confirmation",
    activePermission !== null || activeElicitation !== null,
  );
  useRegisterKeybindingContext(
    "Prompt",
    activePrompt !== null,
  );
  useRegisterKeybindingContext(
    "Autocomplete",
    showSlashSuggestions &&
      activePermission === null &&
      activeElicitation === null &&
      activePrompt === null,
  );
  useRegisterKeybindingContext(
    "Chat",
    activePermission === null &&
      activeElicitation === null &&
      activePrompt === null &&
      !isTranscriptMode,
  );
  useRegisterKeybindingContext("Transcript", isTranscriptMode);

  React.useEffect(() => {
    if (initRef.current) {
      return;
    }

    initRef.current = true;
    inputRef.current = "";
    inputCursorOffsetRef.current = 0;
    setAppState(prev => ({
      ...prev,
      input: "",
      inputMode: "insert",
      isQuerying: false,
      messages:
        initialSessionContext?.messages ??
        initialMessages ??
        prev.messages,
      sessionContext:
        initialSessionContext?.context ??
        prev.sessionContext,
      runtime: {
        ...prev.runtime,
        screen: "repl",
        viewMode: prev.runtime.viewMode ?? "prompt",
        ...(initialSessionContext?.context?.runtime ?? {}),
	        sessionId:
	          initialSessionContext?.sessionId ??
	          initialSessionId ??
	          prev.runtime.sessionId,
	        costUsd:
	          initialSessionContext
	            ? (initialSessionContext.cost ?? undefined)
	            : (initialCost ?? prev.runtime.costUsd),
	        phase: normalizeRestoredRuntimePhase(
	          initialSessionContext
	            ? initialSessionContext.context?.runtime?.phase
	            : prev.runtime.phase,
	        ),
	      },
    }));
    setInputCursorOffset(0);
  }, [
    initialCost,
    initialMessages,
    initialSessionId,
    initialSessionContext,
    setAppState,
  ]);

  const appendMessage = React.useCallback(
    (
      role: AppMessage["role"],
      text: string,
      meta?: Record<string, unknown>,
      content?: AppMessage["content"],
    ): void => {
      setAppState(prev => ({
        ...prev,
        messages: [
          ...prev.messages,
          createMessage(role, text, meta, content),
        ],
      }));
    },
    [setAppState],
  );

  const upsertActivity = React.useCallback(
    (
      key: string,
      text: string,
      options?: {
        role?: AppMessage["role"];
        label?: string;
        status?: string;
        hidden?: boolean;
      },
    ): void => {
      setAppState(prev => ({
        ...prev,
        messages: upsertActivityMessage(
          prev.messages,
          key,
          text,
          options,
        ),
      }));
    },
    [setAppState],
  );

  React.useEffect(() => {
    const current = notifications.current;
    if (!current || !("text" in current)) {
      return;
    }

    const isError =
      current.priority === "immediate" || current.color === "red";
    upsertActivity(`notification:${current.key}`, current.text, {
      role: isError ? "error" : "status",
      label: "Notice",
      status: current.priority,
    });
  }, [notifications.current, upsertActivity]);

  const appendAssistantText = React.useCallback(
    (text: string): void => {
      if (!text) {
        return;
      }

      setAppState(prev => {
        const streamingIndex = findAppendableStreamingAssistantIndex(
          prev.messages,
        );
        const last =
          streamingIndex >= 0 ? prev.messages[streamingIndex] : undefined;

        if (
          last?.role === "assistant" &&
          last.meta?.streaming === true
        ) {
          const nextText = `${getMessageText(last)}${text}`;
          const nextMessages = [...prev.messages];
          nextMessages[streamingIndex] = {
            ...last,
            text: nextText,
            content: [
              {
                type: "text",
                text: nextText,
              },
            ],
          };
          return {
            ...prev,
            messages: nextMessages,
          };
        }

        return {
          ...prev,
          messages: [
            ...prev.messages,
            createMessage("assistant", text, {
              streaming: true,
            }, [{ type: "text", text }]),
          ],
        };
      });
    },
    [setAppState],
  );

  const flushPendingAssistantTokens = React.useCallback((): void => {
    const text = pendingAssistantTokensRef.current.join("");
    pendingAssistantTokensRef.current = [];
    streamingFlushTimerRef.current = null;
    appendAssistantText(text);
  }, [appendAssistantText]);

  const scheduleAssistantTokenFlush = React.useCallback((): void => {
    if (streamingFlushTimerRef.current !== null) {
      return;
    }

    streamingFlushTimerRef.current = setTimeout(() => {
      flushPendingAssistantTokens();
    }, STREAMING_FLUSH_INTERVAL_MS);
  }, [flushPendingAssistantTokens]);

  const enqueueAssistantToken = React.useCallback(
    (token: string): void => {
      if (!token) {
        return;
      }

      pendingAssistantTokensRef.current.push(token);
      scheduleAssistantTokenFlush();
    },
    [scheduleAssistantTokenFlush],
  );

  React.useEffect(() => {
    return () => {
      if (streamingFlushTimerRef.current !== null) {
        clearTimeout(streamingFlushTimerRef.current);
        streamingFlushTimerRef.current = null;
      }
    };
  }, []);

  const setInputValue = React.useCallback(
    (next: string): void => {
      inputRef.current = next;
      setAppState(prev => ({
        ...prev,
        input: next,
        inputMode: getModeFromInput(next),
      }));
    },
    [setAppState],
  );

  const rememberCommandHistory = React.useCallback(
    (value: string): void => {
      setAppState(prev => {
        if (prev.commandHistory.entries.at(-1) === value) {
          return {
            ...prev,
            commandHistory: {
              ...prev.commandHistory,
              selectedIndex: null,
              draftInput: "",
            },
          };
        }

        const entries = [...prev.commandHistory.entries, value];
        return {
          ...prev,
          commandHistory: {
            entries: entries.length > 100 ? entries.slice(entries.length - 100) : entries,
            selectedIndex: null,
            draftInput: "",
          },
        };
      });
    },
    [setAppState],
  );

  const copyTranscriptToClipboard = React.useCallback(
    (scope: string | undefined): void => {
      const normalizedScope = (scope ?? "last").toLowerCase();
      if (!["last", "all"].includes(normalizedScope)) {
        appendMessage("error", "Usage: /copy [last|all]");
        return;
      }

      const copyableMessages = messages.filter(isCopyableMessage);
      const selectedMessages =
        normalizedScope === "all"
          ? copyableMessages
          : copyableMessages.slice(-1);

      if (selectedMessages.length === 0) {
        appendMessage("error", "No transcript text to copy.");
        return;
      }

      const text = renderTranscriptToPlainText({
        messages: selectedMessages,
        sessionId: runtime.sessionId ?? null,
        sessionTitle: sessionContext?.title ?? null,
        sessionContext,
        selectionIndex: normalizedScope === "last" ? 0 : null,
      });
      const result = copyTextToClipboard(text);

      if (result.ok) {
        appendMessage(
          "status",
          `Copied ${normalizedScope === "all" ? "transcript" : "last message"} to clipboard.`,
        );
        return;
      }

      appendMessage(
        "error",
        `Copy failed${result.command ? ` (${result.command})` : ""}: ${result.reason ?? "unknown error"}`,
      );
    },
    [appendMessage, messages, runtime.sessionId, sessionContext],
  );

  const executeLocalSlashCommand = React.useCallback(
    (command: string, args: string[]): boolean => {
      switch (command) {
        case "help": {
          const target = args[0];
          if (target) {
            const definition = getSlashCommandDefinition(target);
            appendMessage(
              definition ? "system" : "error",
              definition
                ? formatSlashCommandDetailText(definition)
                : `Unknown command: /${target}`,
            );
          } else {
            appendMessage("system", formatSlashCommandHelpText());
          }
          return true;
        }

        case "clear":
          setAppState(prev => ({
            ...prev,
            messages: [],
          }));
          return true;

        case "history": {
          const count = Math.max(
            1,
            Number.parseInt(args[0] ?? "10", 10) || 10,
          );
          const entries = commandHistory.entries.slice(-count);
          if (entries.length === 0) {
            appendMessage("system", "No prompt history yet.");
            return true;
          }
          appendMessage(
            "system",
            entries
              .map((entry, index) => `${index + 1}. ${entry}`)
              .join("\n"),
          );
          return true;
        }

        case "copy":
          copyTranscriptToClipboard(args[0]);
          return true;

        case "status":
          appendMessage(
            "system",
            [
              `Model: ${runtime.model ?? "n/a"}`,
              `Main loop model: ${mainLoopModel ?? "n/a"}`,
              `Session: ${runtime.sessionId ?? "n/a"}`,
              `Cost: ${runtime.costUsd !== undefined ? `$${runtime.costUsd.toFixed(4)}` : "n/a"}`,
              `Tokens: ${runtime.inputTokens ?? 0} / ${runtime.outputTokens ?? 0}`,
              `Phase: ${runtime.phase ?? "idle"}`,
            ].join("\n"),
          );
          return true;

        case "agent": {
          if (!io) {
            appendMessage("error", "TUI bridge is not available.");
            return true;
          }

          if (args.length === 0) {
            appendMessage("error", "Usage: /agent [agent-id] <message>");
            return true;
          }

          const defaultAgentId = agentsRuntime.viewedAgentId ?? "primary";
          const hasExplicitAgent = args.length > 1;
          const agentId = hasExplicitAgent ? args[0]! : defaultAgentId;
          const text = hasExplicitAgent ? args.slice(1).join(" ") : args.join(" ");

          if (!text.trim()) {
            appendMessage("error", "Usage: /agent [agent-id] <message>");
            return true;
          }

          io.send({
            type: "agent_input",
            data: {
              agent_id: agentId,
              text: text.trim(),
            },
          });
          setAppState(prev => ({
            ...prev,
            footerSelection: "agents",
            runtime: {
              ...prev.runtime,
              viewMode: "agent",
            },
            agents: {
              ...prev.agents,
              viewedAgentId: agentId,
              selectedPanelTab: "transcript",
            },
          }));
          appendMessage("status", `Sent input to agent ${agentId}`);
          return true;
        }

        case "background": {
          if (!io) {
            appendMessage("error", "TUI bridge is not available.");
            return true;
          }

          const action = (args[0] ?? "focus").toLowerCase();
          if (!["start", "stop", "pause", "resume", "focus"].includes(action)) {
            appendMessage("error", "Usage: /background [start|stop|pause|resume|focus] [value]");
            return true;
          }

          const value = args.slice(1).join(" ").trim();
          const payload: {
            action: "start" | "stop" | "pause" | "resume" | "focus";
            title?: string;
            agent_id?: string;
          } = {
            action: action as "start" | "stop" | "pause" | "resume" | "focus",
          };
          if (action === "start" && value) {
            payload.title = value;
          }
          if (action === "focus" && value) {
            payload.agent_id = value;
          }

          io.send({
            type: "background_control",
            data: payload,
          });
          if (action === "focus") {
            if (payload.agent_id) {
              setAppState(prev => ({
                ...prev,
                footerSelection: "agents",
                runtime: {
                  ...prev.runtime,
                  viewMode: "agent",
                },
                agents: {
                  ...prev.agents,
                  viewedAgentId: payload.agent_id ?? prev.agents.viewedAgentId,
                  selectedPanelTab: "transcript",
                },
              }));
            } else {
              setAppState(prev => ({
                ...prev,
                footerSelection:
                  prev.footerSelection === "background" ? null : "background",
                runtime: {
                  ...prev.runtime,
                  viewMode:
                    prev.footerSelection === "background" ? "prompt" : "prompt",
                },
              }));
            }
          }
          appendMessage(
            "status",
            action === "start" && payload.title
              ? `Background control sent: ${action} (${payload.title})`
              : action === "focus" && payload.agent_id
                ? `Background control sent: ${action} (${payload.agent_id})`
                : `Background control sent: ${action}`,
          );
          return true;
        }

        case "agents":
          setAppState(prev => ({
            ...prev,
            footerSelection:
              prev.footerSelection === "agents" ? null : "agents",
            runtime: {
              ...prev.runtime,
              viewMode:
                prev.footerSelection === "agents" ? "prompt" : "agent",
            },
          }));
          return true;

        case "tasks":
          setAppState(prev => ({
            ...prev,
            expandedView:
              prev.expandedView === "tasks" ? "none" : "tasks",
            footerSelection:
              prev.footerSelection === "tasks" ? null : "tasks",
          }));
          return true;

        case "mcp": {
          const action = (args[0] ?? "").toLowerCase();
          if (action === "reconnect") {
            const serverName = args[1];
            if (!serverName) {
              appendMessage("error", "Usage: /mcp reconnect <server>");
              return true;
            }
            io?.send({
              type: "mcp_reconnect",
              data: { server_name: serverName },
            });
            appendMessage("status", `Requested MCP reconnect for ${serverName}`);
            return true;
          }

          setAppState(prev => ({
            ...prev,
            footerSelection: prev.footerSelection === "mcp" ? null : "mcp",
          }));
          return true;
        }

        case "theme": {
          const name = args[0];
          if (!name) {
            appendMessage("system", `Current theme: ${getTheme().name}`);
            return true;
          }

          setTheme(name);
          settingsApi.setSetting("theme", name);
          appendMessage("status", `Theme set to ${name}`);
          return true;
        }

        case "keybindings": {
          const result = loadKeybindingsSyncWithWarnings();
          const warnings =
            result.warnings.length > 0
              ? `Warnings: ${result.warnings.map(item => item.message).join(" | ")}`
              : "Warnings: none";
          appendMessage(
            "system",
            [
              `Keybindings file: ${getKeybindingsPath()}`,
              `Loaded bindings: ${result.bindings.length}`,
              warnings,
            ].join("\n"),
          );
          return true;
        }

        case "vim": {
          const mode = (args[0] ?? "").toLowerCase();
          if (!mode) {
            appendMessage("system", `Current Vim mode: ${vimMode}`);
            return true;
          }

          if (mode === "toggle") {
            promptInputModeRef.current?.(
              vimMode === "INSERT" ? "NORMAL" : "INSERT",
            );
            appendMessage(
              "status",
              `Vim mode set to ${vimMode === "INSERT" ? "NORMAL" : "INSERT"}`,
            );
            return true;
          }

          if (mode === "insert" || mode === "normal") {
            const nextMode = mode === "insert" ? "INSERT" : "NORMAL";
            promptInputModeRef.current?.(nextMode);
            appendMessage("status", `Vim mode set to ${nextMode}`);
            return true;
          }

          appendMessage("error", "Usage: /vim [insert|normal|toggle]");
          return true;
        }

        case "exit":
          exit();
          return true;

        default:
          return false;
      }
    },
    [
      agentsRuntime.viewedAgentId,
      appendMessage,
      commandHistory.entries,
      copyTranscriptToClipboard,
      exit,
      io,
      mainLoopModel,
      runtime,
      setAppState,
      settingsApi,
      vimMode,
    ],
  );

  const submitPrompt = React.useCallback(
    (rawValue: string): void => {
      const trimmed = rawValue.trim();
      if (!trimmed || !io) {
        return;
      }

      if (trimmed.startsWith("/")) {
        const parsed = parseSlashCommandInput(trimmed);
        if (!parsed) {
          return;
        }

        rememberCommandHistory(trimmed);

        const command = parsed.command;
        const args = parsed.args;
        const isLocalCommand = parsed.definition?.handler === "local";

        if (
          isQuerying &&
          !isLocalCommand &&
          !BUSY_ALLOWED_CORE_SLASH_COMMANDS.has(command)
        ) {
          appendMessage("status", "Task running; wait for it to finish first.");
          return;
        }

        if (
          isLocalCommand &&
          executeLocalSlashCommand(command, args)
        ) {
          setInputValue("");
          setInputCursorOffset(0);
          if (command !== "vim") {
            promptInputModeRef.current?.("INSERT");
          }
          return;
        }

        io.send({
          type: "slash_command",
          data: {
            command,
            args,
          },
        });

        inputRef.current = "";
        setAppState(prev => ({
          ...prev,
          input: "",
          inputMode: "insert",
          messages: [
            ...prev.messages,
            createMessage("user", trimmed),
            createMessage("status", `Sent /${command}`),
          ],
        }));
        setInputCursorOffset(0);
        promptInputModeRef.current?.("INSERT");
        return;
      }

      if (isQuerying) {
        appendMessage("status", "Task running; wait for it to finish first.");
        return;
      }

      rememberCommandHistory(trimmed);
      runSequenceRef.current += 1;
      activeRunActivityKeyRef.current = `query:${runSequenceRef.current}`;

      io.send({
        type: "query",
        data: { text: trimmed },
      });

      inputRef.current = "";
      setAppState(prev => ({
        ...prev,
        input: "",
        inputMode: "insert",
        isQuerying: true,
        expandedView: "none",
        footerSelection: null,
        runtime: {
          ...prev.runtime,
          viewMode: "prompt",
        },
        messages: [
          ...prev.messages,
          createMessage("user", trimmed),
        ],
      }));
      setInputCursorOffset(0);
      promptInputModeRef.current?.("INSERT");
    },
    [
      executeLocalSlashCommand,
      appendMessage,
      io,
      isQuerying,
      rememberCommandHistory,
      setAppState,
      setInputValue,
    ],
  );

  const restoreHistoryEntry = React.useCallback(
    (index: number | null) => {
      if (index === null) {
        setInputValue(commandHistory.draftInput);
        setInputCursorOffset(commandHistory.draftInput.length);
        return;
      }

      const entry = commandHistory.entries[index];
      if (entry === undefined) {
        return;
      }

      setInputValue(entry);
      setInputCursorOffset(entry.length);
    },
    [commandHistory.draftInput, commandHistory.entries, setInputValue],
  );

  const historyUp = React.useCallback(() => {
    if (commandHistory.entries.length === 0) {
      return;
    }

    if (commandHistory.selectedIndex === null) {
      const next = commandHistory.entries.length - 1;
      setAppState(prev => ({
        ...prev,
        commandHistory: {
          ...prev.commandHistory,
          draftInput: input,
          selectedIndex: next,
        },
      }));
      restoreHistoryEntry(next);
      return;
    }

    const next = Math.max(0, commandHistory.selectedIndex - 1);
    setAppState(prev => ({
      ...prev,
      commandHistory: {
        ...prev.commandHistory,
        selectedIndex: next,
      },
    }));
    restoreHistoryEntry(next);
  }, [
    commandHistory.entries.length,
    commandHistory.selectedIndex,
    input,
    restoreHistoryEntry,
    setAppState,
  ]);

  const historyDown = React.useCallback(() => {
    if (commandHistory.selectedIndex === null) {
      return;
    }

    if (commandHistory.selectedIndex >= commandHistory.entries.length - 1) {
      setAppState(prev => ({
        ...prev,
        commandHistory: {
          ...prev.commandHistory,
          selectedIndex: null,
        },
      }));
      restoreHistoryEntry(null);
      return;
    }

    const next = commandHistory.selectedIndex + 1;
    setAppState(prev => ({
      ...prev,
      commandHistory: {
        ...prev.commandHistory,
        selectedIndex: next,
      },
    }));
    restoreHistoryEntry(next);
  }, [
    commandHistory.entries.length,
    commandHistory.selectedIndex,
    restoreHistoryEntry,
    setAppState,
  ]);

  const resetHistoryNavigation = React.useCallback(() => {
    if (commandHistory.selectedIndex !== null) {
      setAppState(prev => ({
        ...prev,
        commandHistory: {
          ...prev.commandHistory,
          selectedIndex: null,
        },
      }));
    }
  }, [commandHistory.selectedIndex, setAppState]);

  const applySlashSuggestion = React.useCallback(
    (suggestion: SlashCommandDefinition | null): void => {
      if (!suggestion) {
        return;
      }

      const nextValue = `/${suggestion.name} `;
      setInputValue(nextValue);
      setInputCursorOffset(nextValue.length);
    },
    [setInputValue],
  );

  const resolvePermission = React.useCallback(
    (resolution: PermissionResolution): void => {
      const resolved = resolvePermissionRequest(resolution);
      if (!resolved) {
        return;
      }

      const decision = formatPermissionResolution(resolution);
      appendMessage(
        "status",
        `Permission ${decision}: ${getPermissionRequestSummary(resolved)}`,
      );
    },
    [appendMessage, resolvePermissionRequest],
  );

  const submitElicitation = React.useCallback(
    (sendEmpty = false): void => {
      const current = elicitationQueue[0];
      if (!current || !io) {
        return;
      }

      if (sendEmpty) {
        io.send({
          type: "elicitation_response",
          data: {
            elicitation_id: current.request.elicitation_id,
            values: {},
          },
        });
        appendMessage(
          "status",
          `Responded to MCP elicitation ${current.request.elicitation_id} with an empty payload`,
        );
        setElicitationQueue(queue => queue.slice(1));
        return;
      }

      const parsed = parseElicitationValues(current, elicitationFields);
      if (!parsed.ok) {
        setElicitationQueue(queue =>
          queue.map((entry, index) =>
            index === 0
              ? { ...entry, error: parsed.error }
              : entry,
          ),
        );
        return;
      }

      io.send({
        type: "elicitation_response",
        data: {
          elicitation_id: current.request.elicitation_id,
          values: parsed.values,
        },
      });

      appendMessage(
        "status",
        `Submitted MCP elicitation ${current.request.elicitation_id}`,
      );
      setElicitationQueue(queue => queue.slice(1));
    },
    [appendMessage, elicitationFields, elicitationQueue, io],
  );

  const moveElicitationField = React.useCallback(
    (delta: number): void => {
      setElicitationQueue(queue =>
        queue.map((entry, index) =>
          index === 0
            ? {
                ...entry,
                activeField:
                  elicitationFields.length === 0
                    ? 0
                    : (entry.activeField + delta + elicitationFields.length) %
                      elicitationFields.length,
                error: null,
              }
            : entry,
        ),
      );
    },
    [elicitationFields.length],
  );

  const submitPromptRequest = React.useCallback(
    (decision: PromptResponseData["decision"]): void => {
      const current = promptQueue[0];
      if (!current || !io) {
        return;
      }

      const trimmed = current.value.trim();
      if (decision === "submit" && !current.request.multiline && trimmed.length === 0) {
        setAppState(prev => ({
          ...prev,
          promptDialog: {
            queue: prev.promptDialog.queue.map((entry, index) =>
              index === 0
                ? {
                    ...entry,
                    error: "A value is required",
                  }
                : entry,
            ),
          },
        }));
        return;
      }

      io.send({
        type: "prompt_response",
        data: {
          prompt_id: current.request.prompt_id,
          decision,
          ...(decision === "submit" ? { value: current.value } : {}),
        },
      });

      appendMessage(
        "status",
        decision === "submit"
          ? `Submitted prompt response for ${current.request.prompt_id}`
          : `Cancelled prompt ${current.request.prompt_id}`,
      );
      setAppState(prev => ({
        ...prev,
        promptDialog: {
          queue: prev.promptDialog.queue.slice(1),
        },
      }));
    },
    [appendMessage, io, promptQueue, setAppState],
  );

  const updatePromptRequestValue = React.useCallback(
    (updater: (value: string) => string): void => {
      setAppState(prev => ({
        ...prev,
        promptDialog: {
          queue: prev.promptDialog.queue.map((entry, index) =>
            index === 0
              ? {
                  ...entry,
                  value: updater(entry.value),
                  error: null,
                }
              : entry,
          ),
        },
      }));
    },
    [setAppState],
  );

  const promptInput = useVimInput({
    value: input,
    onChange: setInputValue,
    onSubmit: submitPrompt,
    onHistoryUp: historyUp,
    onHistoryDown: historyDown,
    onHistoryReset: resetHistoryNavigation,
    focus:
      activePermission === null &&
      activeElicitation === null &&
      activePrompt === null &&
      !isTranscriptMode,
    multiline: true,
    columns: Math.max(10, size.columns - 4),
    cursorOffset: inputCursorOffset,
    onChangeCursorOffset: setInputCursorOffset,
    onModeChange: setVimMode,
    handleSubmitKeys: false,
    handleHistoryKeys: false,
    handleClearKey: false,
    handleNewlineKeys: false,
  });
  promptInputModeRef.current = promptInput.setMode;

  const handleTranscriptSearchMatchesChange = React.useCallback(
    (count: number, current: number): void => {
      setTranscriptSearch(prev => ({
        ...prev,
        matchCount: count,
        currentMatch: current,
      }));
    },
    [],
  );

  const updateTranscriptSearchQuery = React.useCallback(
    (nextQuery: string): void => {
      setTranscriptSearch(prev => ({
        ...prev,
        query: nextQuery,
      }));
      jumpRef.current?.setSearchQuery(nextQuery);
    },
    [],
  );

  const closeTranscriptSearch = React.useCallback((): void => {
    setTranscriptSearch(prev => ({
      ...prev,
      active: false,
    }));
  }, []);

  const clearTranscriptSearch = React.useCallback((): void => {
    setTranscriptSearch({
      active: false,
      query: "",
      matchCount: 0,
      currentMatch: 0,
    });
    jumpRef.current?.setSearchQuery("");
    jumpRef.current?.disarmSearch();
  }, []);

  const enterTranscriptSearch = React.useCallback((): void => {
    setTranscriptSearch(prev => ({
      ...prev,
      active: true,
    }));
    jumpRef.current?.setAnchor();
    if (transcriptSearch.query.trim().length > 0) {
      jumpRef.current?.setSearchQuery(transcriptSearch.query);
    }
  }, [transcriptSearch.query]);

  const handleAgentTranscriptSearchMatchesChange = React.useCallback(
    (count: number, current: number): void => {
      setAgentTranscriptSearch(prev => ({
        ...prev,
        matchCount: count,
        currentMatch: current,
      }));
    },
    [],
  );

  const updateAgentTranscriptSearchQuery = React.useCallback(
    (nextQuery: string): void => {
      setAgentTranscriptSearch(prev => ({
        ...prev,
        query: nextQuery,
      }));
      agentTranscriptRef.current?.setSearchQuery(nextQuery);
    },
    [],
  );

  const closeAgentTranscriptSearch = React.useCallback((): void => {
    setAgentTranscriptSearch(prev => ({
      ...prev,
      active: false,
    }));
  }, []);

  const clearAgentTranscriptSearch = React.useCallback((): void => {
    setAgentTranscriptSearch({
      active: false,
      query: "",
      matchCount: 0,
      currentMatch: 0,
    });
    agentTranscriptRef.current?.setSearchQuery("");
    agentTranscriptRef.current?.disarmSearch();
  }, []);

  const enterAgentTranscriptSearch = React.useCallback((): void => {
    setAgentTranscriptSearch(prev => ({
      ...prev,
      active: true,
    }));
    agentTranscriptRef.current?.setAnchor();
    if (agentTranscriptSearch.query.trim().length > 0) {
      agentTranscriptRef.current?.setSearchQuery(agentTranscriptSearch.query);
    }
  }, [agentTranscriptSearch.query]);

  const handleTranscriptCursorChange = React.useCallback(
    (cursor: MessageActionsState | null): void => {
      if (!cursor) {
        setTranscriptCursorId(null);
        return;
      }

      setTranscriptCursorId(cursor.id);
    },
    [],
  );

  React.useEffect(() => {
    if (
      transcriptCursorId !== null &&
      !messages.some(message => message.id === transcriptCursorId)
    ) {
      setTranscriptCursorId(null);
    }
  }, [messages, transcriptCursorId]);

  const openTranscriptSelector = React.useCallback((): void => {
    if (rewindableMessages.length === 0) {
      appendMessage("error", "No rewindable transcript messages are available.");
      return;
    }

    setTranscriptSelection(prev => ({
      ...prev,
      active: true,
      selectedIndex: clampMessageIndex(
        prev.targetIndex ?? rewindableMessages.length - 1,
        rewindableMessages.length,
      ),
    }));
  }, [appendMessage, rewindableMessages.length]);

  const moveTranscriptSelector = React.useCallback(
    (delta: number): void => {
      if (rewindableMessages.length === 0) {
        return;
      }

      setTranscriptSelection(prev => ({
        ...prev,
        selectedIndex: clampMessageIndex(
          prev.selectedIndex + delta,
          rewindableMessages.length,
        ),
      }));
    },
    [rewindableMessages.length],
  );

  const confirmTranscriptSelection = React.useCallback((): void => {
    if (rewindableMessages.length === 0) {
      return;
    }

    const targetIndex = clampMessageIndex(
      transcriptSelection.selectedIndex,
      rewindableMessages.length,
    );
    const targetMessage = rewindableMessages[targetIndex];
    setTranscriptSelection(prev => ({
      ...prev,
      active: false,
      targetIndex,
    }));
    if (targetMessage) {
      jumpRef.current?.jumpToMessageId?.(targetMessage.id);
    }
  }, [rewindableMessages, transcriptSelection.selectedIndex]);

  const clearTranscriptSelection = React.useCallback((): void => {
    setTranscriptSelection(prev => ({
      ...prev,
      active: false,
      targetIndex: null,
    }));
  }, []);

  React.useEffect(() => {
    setTranscriptSelection(prev => {
      if (rewindableMessages.length === 0) {
        return prev.targetIndex === null && prev.selectedIndex === 0
          ? prev
          : {
              ...prev,
              selectedIndex: 0,
              targetIndex: null,
            };
      }

      const selectedIndex = clampMessageIndex(
        prev.selectedIndex,
        rewindableMessages.length,
      );
      const targetIndex =
        prev.targetIndex === null
          ? null
          : clampMessageIndex(prev.targetIndex, rewindableMessages.length);

      if (
        selectedIndex === prev.selectedIndex &&
        targetIndex === prev.targetIndex
      ) {
        return prev;
      }

      return {
        ...prev,
        selectedIndex,
        targetIndex,
      };
    });
  }, [rewindableMessages.length]);

  const requestTranscriptMutation = React.useCallback(
    (
      nextMessages: AppMessage[],
      mutation: Exclude<TranscriptMutationState, "idle">,
    ): void => {
      if (!io) {
        appendMessage("error", "TUI bridge is not available.");
        return;
      }

      if (!runtime.sessionId) {
        appendMessage("error", "No active session to rewind.");
        return;
      }

      if (isQuerying) {
        appendMessage("error", "Wait for the active query to finish before rewinding.");
        return;
      }

      setTranscriptMutation(mutation);
      io.send({
        type: "resume_session",
        data: {
          action: "rewind",
          session_id: runtime.sessionId,
          messages: serializeAppMessages(nextMessages),
        },
      });
    },
    [appendMessage, io, isQuerying, runtime.sessionId],
  );

  const rewindTranscript = React.useCallback((): void => {
    const targetIndex = transcriptSelection.targetIndex;
    if (targetIndex === null) {
      appendMessage(
        "error",
        "Choose a message in transcript mode before rewinding.",
      );
      return;
    }

    const nextMessages = rewindableMessages.slice(0, targetIndex + 1);
    if (nextMessages.length === 0) {
      return;
    }

    setTranscriptRestoreBuffer(prev => prev ?? rewindableMessages);
    setTranscriptSelection(prev => ({
      ...prev,
      active: false,
    }));
    requestTranscriptMutation(nextMessages, "rewind");
  }, [
    appendMessage,
    requestTranscriptMutation,
    rewindableMessages,
    transcriptSelection.targetIndex,
  ]);

  const restoreTranscript = React.useCallback((): void => {
    if (!transcriptRestoreBuffer || transcriptRestoreBuffer.length === 0) {
      appendMessage("error", "No rewind snapshot is available to restore.");
      return;
    }

    requestTranscriptMutation(transcriptRestoreBuffer, "restore");
  }, [appendMessage, requestTranscriptMutation, transcriptRestoreBuffer]);

  const jumpTranscriptSearch = React.useCallback(
    (direction: "next" | "prev"): void => {
      if (transcriptSearch.query.trim().length === 0) {
        enterTranscriptSearch();
        return;
      }

      if (direction === "next") {
        jumpRef.current?.nextMatch();
      } else {
        jumpRef.current?.prevMatch();
      }
    },
    [enterTranscriptSearch, transcriptSearch.query],
  );

  const toggleRuntimePanel = React.useCallback(
    (panel: "agents" | "background"): void => {
      if (panel !== "agents") {
        closeAgentTranscriptSearch();
      }
      setAppState(prev => {
        const nextSelection =
          prev.footerSelection === panel ? null : panel;
        return {
          ...prev,
          footerSelection: nextSelection,
          runtime: {
            ...prev.runtime,
            viewMode:
              nextSelection === "agents"
                ? "agent"
                : viewMode === "transcript"
                  ? "transcript"
                  : "prompt",
          },
        };
      });
    },
    [closeAgentTranscriptSearch, setAppState, viewMode],
  );

  const focusAgent = React.useCallback(
    (agentId: string | null): void => {
      if (!agentId) {
        return;
      }

      closeAgentTranscriptSearch();
      setAppState(prev => ({
        ...prev,
        footerSelection: "agents",
        runtime: {
          ...prev.runtime,
          viewMode: "agent",
        },
        agents: {
          ...prev.agents,
          viewedAgentId: agentId,
          selectedPanelTab: "transcript",
        },
      }));
    },
    [closeAgentTranscriptSearch, setAppState],
  );

  const focusRelativeAgent = React.useCallback(
    (delta: number): void => {
      const availableAgents = agentsRuntime.list;
      if (availableAgents.length === 0) {
        return;
      }

      const currentIndex = availableAgents.findIndex(
        candidate => candidate.agent_id === agentsRuntime.viewedAgentId,
      );
      const baseIndex = currentIndex >= 0 ? currentIndex : 0;
      const nextIndex =
        (baseIndex + delta + availableAgents.length) % availableAgents.length;
      const nextAgentId =
        typeof availableAgents[nextIndex]?.agent_id === "string"
          ? availableAgents[nextIndex].agent_id
          : null;
      focusAgent(nextAgentId);
    },
    [agentsRuntime.list, agentsRuntime.viewedAgentId, focusAgent],
  );

  const cycleViewedAgentPanelTab = React.useCallback((): void => {
    setAppState(prev => {
      const nextTab = cycleAgentPanelTab(prev.agents.selectedPanelTab);
      if (nextTab !== "transcript") {
        closeAgentTranscriptSearch();
      }
      return {
        ...prev,
        footerSelection: "agents",
        runtime: {
          ...prev.runtime,
          viewMode: "agent",
        },
        agents: {
          ...prev.agents,
          selectedPanelTab: nextTab,
        },
      };
    });
  }, [closeAgentTranscriptSearch, setAppState]);

  const moveAgentTranscriptCursor = React.useCallback(
    (delta: number): void => {
      if (delta < 0) {
        agentTranscriptRef.current?.navigatePrev();
      } else {
        agentTranscriptRef.current?.navigateNext();
      }
    },
    [],
  );

  const moveAgentEventSelection = React.useCallback(
    (delta: number): void => {
      const totalEvents = agentsRuntime.events.length;
      if (totalEvents === 0) {
        return;
      }

      setAppState(prev => ({
        ...prev,
        agents: {
          ...prev.agents,
          selectedEventIndex: clampMessageIndex(
            prev.agents.selectedEventIndex + delta,
            totalEvents,
          ),
          selectedPanelTab: "events",
        },
      }));
    },
    [agentsRuntime.events.length, setAppState],
  );

  const jumpAgentTranscriptSearch = React.useCallback(
    (direction: "next" | "prev"): void => {
      if (agentTranscriptSearch.query.trim().length === 0) {
        enterAgentTranscriptSearch();
        return;
      }

      if (direction === "next") {
        agentTranscriptRef.current?.nextMatch();
      } else {
        agentTranscriptRef.current?.prevMatch();
      }
    },
    [agentTranscriptSearch.query, enterAgentTranscriptSearch],
  );

  const sendInputToViewedAgent = React.useCallback((): void => {
    const targetAgentId = agentsRuntime.viewedAgentId ?? "primary";
    const trimmed = input.trim();

    if (!io) {
      appendMessage("error", "TUI bridge is not available.");
      return;
    }

    if (trimmed.length > 0 && !trimmed.startsWith("/")) {
      io.send({
        type: "agent_input",
        data: {
          agent_id: targetAgentId,
          text: trimmed,
        },
      });
      appendMessage("status", `Sent input to agent ${targetAgentId}`);
      setInputValue("");
      setInputCursorOffset(0);
      promptInput.setMode("INSERT");
      setAppState(prev => ({
        ...prev,
        commandHistory: {
          ...prev.commandHistory,
          selectedIndex: null,
          draftInput: "",
        },
      }));
      return;
    }

    const nextValue = `/agent ${targetAgentId} `;
    setInputValue(nextValue);
    setInputCursorOffset(nextValue.length);
    promptInput.setMode("INSERT");
  }, [
    agentsRuntime.viewedAgentId,
    appendMessage,
    input,
    io,
    promptInput,
    setAppState,
    setInputValue,
  ]);

  const handleEnterTranscript = React.useCallback((): void => {
    setViewMode("transcript");
    setRuntimeViewMode("transcript");
    jumpRef.current?.setAnchor();
  }, [setRuntimeViewMode]);

  const handleExitTranscript = React.useCallback((): void => {
    setTranscriptSelection(prev => ({
      ...prev,
      active: false,
    }));
    setTranscriptCursorId(null);
    closeTranscriptSearch();
    setViewMode("prompt");
    setRuntimeViewMode(
      agentsRuntime.list.length > 0 && footerSelection === "agents"
        ? "agent"
        : "prompt",
    );
  }, [
    agentsRuntime.list.length,
    closeTranscriptSearch,
    footerSelection,
    setRuntimeViewMode,
  ]);

  const toggleTranscriptMode = React.useCallback((): void => {
    if (viewMode === "transcript") {
      handleExitTranscript();
      return;
    }
    handleEnterTranscript();
  }, [handleEnterTranscript, handleExitTranscript, viewMode]);

  const toggleShowAllTranscript = React.useCallback((): void => {
    setShowAllInTranscript(current => !current);
  }, []);

  const exportCurrentTranscript = React.useCallback((): void => {
    const selectedMessage =
      transcriptCursorId !== null
        ? messages.find(message => message.id === transcriptCursorId)
        : undefined;
    const exportedMessages =
      selectedMessage !== undefined
        ? [selectedMessage]
        : messages.filter(isCopyableMessage);
    const exportLabel =
      selectedMessage !== undefined ? "selected message" : "transcript";
    const configuredPath = settingsApi.getSetting<string | undefined>(
      "transcriptExportPath",
      undefined,
    );

    void exportTranscriptToFile({
      messages: exportedMessages,
      sessionId: runtime.sessionId ?? null,
      sessionTitle: sessionContext?.title ?? null,
      sessionContext,
      selectionIndex: selectedMessage !== undefined ? 0 : null,
      outputPath:
        typeof configuredPath === "string" && configuredPath.trim().length > 0
          ? configuredPath
          : undefined,
    })
      .then(result => {
        appendMessage(
          "status",
          `Exported ${exportLabel} to ${result.path}`,
        );
      })
      .catch(error => {
        appendMessage(
          "error",
          `Failed to export transcript: ${error instanceof Error ? error.message : String(error)}`,
        );
      });
  }, [
    appendMessage,
    messages,
    runtime.sessionId,
    sessionContext,
    settingsApi,
    transcriptCursorId,
  ]);

  const openTranscriptInExternalEditor = React.useCallback((): void => {
    const selectedMessage =
      transcriptCursorId !== null
        ? messages.find(message => message.id === transcriptCursorId)
        : undefined;
    const exportedMessages =
      selectedMessage !== undefined
        ? [selectedMessage]
        : messages.filter(isCopyableMessage);
    const configuredEditor = settingsApi.getSetting<string | undefined>(
      "externalEditor",
      undefined,
    );
    const label =
      selectedMessage !== undefined ? "selected message" : "transcript";

    void prepareTranscriptEditorFile({
      messages: exportedMessages,
      sessionId: runtime.sessionId ?? null,
      sessionTitle: sessionContext?.title ?? null,
      sessionContext,
      selectionIndex: selectedMessage !== undefined ? 0 : null,
    })
      .then(filePath => {
        const result = openPathInExternalEditor(filePath, configuredEditor);
        if (!result.ok) {
          appendMessage(
            "error",
            `${result.reason ?? "External editor failed."} Exported ${label} to ${filePath}`,
          );
          return;
        }
        appendMessage(
          "status",
          `Opened ${label} in external editor: ${filePath}`,
        );
      })
      .catch(error => {
        appendMessage(
          "error",
          `Failed to open transcript editor: ${error instanceof Error ? error.message : String(error)}`,
        );
      });
  }, [
    appendMessage,
    messages,
    runtime.sessionId,
    sessionContext,
    settingsApi,
    transcriptCursorId,
  ]);

  const targetTranscriptCursor = React.useCallback((): void => {
    if (transcriptCursorId === null) {
      appendMessage("error", "Move the transcript cursor onto a message first.");
      return;
    }

    const targetIndex = rewindableMessages.findIndex(
      message => message.id === transcriptCursorId,
    );
    if (targetIndex < 0) {
      appendMessage(
        "error",
        "Choose a user, assistant, tool, or system message before rewinding.",
      );
      return;
    }
    const targetMessage = rewindableMessages[targetIndex]!;

    setTranscriptSelection(prev => ({
      ...prev,
      active: false,
      targetIndex,
    }));
    jumpRef.current?.jumpToMessageId?.(targetMessage.id);
  }, [appendMessage, rewindableMessages, transcriptCursorId]);

  const clearPrompt = React.useCallback((): void => {
    setInputValue("");
    setInputCursorOffset(0);
    promptInput.setMode("INSERT");
    setAppState(prev => ({
      ...prev,
      commandHistory: {
        ...prev.commandHistory,
        selectedIndex: null,
        draftInput: "",
      },
    }));
  }, [promptInput, setAppState, setInputValue]);

  const requestCancellation = React.useCallback((): void => {
    if (!isQuerying) {
      clearPrompt();
      return;
    }

    io?.send({
      type: "cancel",
      data: { reason: "tui_keybinding_cancel" },
    });
    appendMessage("status", "Cancellation requested");
  }, [appendMessage, clearPrompt, io, isQuerying]);

  const cancelMemorySelector = React.useCallback((): void => {
    setMemorySelector(null);
    appendMessage("status", "Cancelled memory editing");
  }, [appendMessage]);

  const selectMemoryTarget = React.useCallback(
    (target: MemoryTargetData): void => {
      setMemorySelector(null);
      if (!io) {
        appendMessage("error", "TUI bridge is not available.");
        return;
      }
      io.send({
        type: "slash_command",
        data: {
          command: "memory",
          args: ["edit", target.path],
        },
      });
      appendMessage(
        "status",
        target.is_folder
          ? `Opening memory folder: ${target.display_path ?? target.path}`
          : `Opening memory file: ${target.display_path ?? target.path}`,
      );
    },
    [appendMessage, io],
  );

  const insertNewlineAtCursor = React.useCallback((): void => {
    const currentInput = inputRef.current;
    const currentOffset = inputCursorOffsetRef.current;
    const nextValue = `${currentInput.slice(0, currentOffset)}\n${currentInput.slice(currentOffset)}`;
    setInputValue(nextValue);
    setInputCursorOffset(currentOffset + 1);
    resetHistoryNavigation();
  }, [
    resetHistoryNavigation,
    setInputValue,
    setInputCursorOffset,
  ]);

  const toggleInputMode = React.useCallback((): void => {
    promptInput.setMode(vimMode === "INSERT" ? "NORMAL" : "INSERT");
  }, [promptInput, vimMode]);

  const dismissAutocomplete = React.useCallback((): void => {
    setDismissedSlashInput(input);
    setSelectedSlashSuggestion(0);
  }, [input]);

  const handleInterrupt = React.useCallback((): void => {
    if (isTranscriptMode) {
      handleExitTranscript();
      return;
    }
    if (isQuerying) {
      requestCancellation();
      return;
    }

    exit();
  }, [exit, handleExitTranscript, isQuerying, isTranscriptMode, requestCancellation]);

  const isConfirmationActive =
    activePermission !== null ||
    activeElicitation !== null ||
    memorySelector !== null;
  const isPromptActive = activePrompt !== null;
  const isBlockingOverlayActive = isConfirmationActive || isPromptActive;

  useGlobalKeybindings({
    onInterrupt: handleInterrupt,
    onExit: exit,
    canInterrupt: !isBlockingOverlayActive,
    canExit: !isQuerying && !isBlockingOverlayActive,
  });

  useKeybinding("app:toggleTranscript", toggleTranscriptMode, {
    context: "Global",
    isActive: !isBlockingOverlayActive,
  });
  useKeybinding("app:toggleTranscript", toggleTranscriptMode, {
    context: "Transcript",
    isActive: !isBlockingOverlayActive && isTranscriptMode,
  });
  useKeybinding("app:toggleBackgroundPanel", () => {
    if (hasForegroundBackgroundTasks(agentsRuntime.backgroundTasks)) {
      io?.send({
        type: "background_control",
        data: {
          action: "background_all_foreground_tasks",
        },
      });
      return;
    }
    toggleRuntimePanel("background");
  }, {
    context: "Global",
    isActive: !isBlockingOverlayActive && !isTranscriptMode,
  });
  useKeybinding("app:toggleAgentsPanel", () => {
    toggleRuntimePanel("agents");
  }, {
    context: "Global",
    isActive: !isBlockingOverlayActive && !isTranscriptMode,
  });
  useKeybinding("history:search", () => {
    enterAgentTranscriptSearch();
  }, {
    context: "Global",
    isActive:
      !isBlockingOverlayActive &&
      !isTranscriptMode &&
      footerSelection === "agents" &&
      agentsRuntime.selectedPanelTab === "transcript",
  });
  useKeybinding("agent:focusNext", () => {
    if (footerSelection === "agents") {
      if (agentsRuntime.selectedPanelTab === "events") {
        moveAgentEventSelection(1);
        return;
      }
      if (agentsRuntime.selectedPanelTab === "transcript") {
        if (agentTranscriptSearch.active) {
          jumpAgentTranscriptSearch("next");
          return;
        }
        moveAgentTranscriptCursor(1);
        return;
      }
    }
    focusRelativeAgent(1);
  }, {
    context: "Global",
    isActive: !isBlockingOverlayActive,
  });
  useKeybinding("agent:focusPrev", () => {
    if (footerSelection === "agents") {
      if (agentsRuntime.selectedPanelTab === "events") {
        moveAgentEventSelection(-1);
        return;
      }
      if (agentsRuntime.selectedPanelTab === "transcript") {
        if (agentTranscriptSearch.active) {
          jumpAgentTranscriptSearch("prev");
          return;
        }
        moveAgentTranscriptCursor(-1);
        return;
      }
    }
    focusRelativeAgent(-1);
  }, {
    context: "Global",
    isActive: !isBlockingOverlayActive,
  });
  useKeybinding("agent:openViewed", () => {
    if (footerSelection !== "agents") {
      focusAgent(
        agentsRuntime.viewedAgentId ??
          (typeof agentsRuntime.list[0]?.agent_id === "string"
            ? agentsRuntime.list[0].agent_id
            : null),
      );
      return;
    }
    if (agentsRuntime.selectedPanelTab === "transcript") {
      agentTranscriptRef.current?.enterCursor();
      return;
    }
    cycleViewedAgentPanelTab();
  }, {
    context: "Global",
    isActive: !isBlockingOverlayActive,
  });
  useKeybinding("agent:sendInput", () => {
    sendInputToViewedAgent();
  }, {
    context: "Global",
    isActive: !isBlockingOverlayActive,
  });

  useKeybindings(
    {
      "history:search": () => {
        enterTranscriptSearch();
      },
      "transcript:searchNext": () => {
        jumpTranscriptSearch("next");
      },
      "transcript:searchPrev": () => {
        jumpTranscriptSearch("prev");
      },
      "transcript:toggleShowAll": () => {
        toggleShowAllTranscript();
      },
      "transcript:export": () => {
        exportCurrentTranscript();
      },
      "transcript:externalEditor": () => {
        openTranscriptInExternalEditor();
      },
      "transcript:targetCursor": () => {
        targetTranscriptCursor();
      },
      "transcript:openSelector": () => {
        openTranscriptSelector();
      },
      "transcript:confirmSelection": () => {
        if (!transcriptSelection.active) {
          transcriptCursorNavRef.current?.enterCursor();
          return;
        }
        confirmTranscriptSelection();
      },
      "transcript:clearSelection": () => {
        clearTranscriptSelection();
      },
      "transcript:rewind": () => {
        rewindTranscript();
      },
      "transcript:restore": () => {
        restoreTranscript();
      },
      "transcript:selectorUp": () => {
        if (!transcriptSelection.active) {
          return false;
        }
        moveTranscriptSelector(-1);
      },
      "transcript:selectorDown": () => {
        if (!transcriptSelection.active) {
          return false;
        }
        moveTranscriptSelector(1);
      },
    },
    {
      context: "Transcript",
      isActive: !isBlockingOverlayActive && isTranscriptMode,
    },
  );

  useKeybindings(
    {
      "transcript:toggleShowAll": () => {
        toggleShowAllTranscript();
      },
      "transcript:export": () => {
        exportCurrentTranscript();
      },
      "transcript:externalEditor": () => {
        openTranscriptInExternalEditor();
      },
      "transcript:targetCursor": () => {
        targetTranscriptCursor();
      },
    },
    {
      context: "MessageActions",
      isActive: !isBlockingOverlayActive && isTranscriptMode,
    },
  );

  useCancelRequest({
    isActive: !isBlockingOverlayActive,
    onCancel: requestCancellation,
  });

  useKeybindings(
    {
      "chat:submit": () => submitPrompt(inputRef.current),
      "chat:newline": insertNewlineAtCursor,
      "chat:cycleMode": toggleInputMode,
      "chat:externalEditor": () => {
        const result = editTextInExternalEditor(input);
        if (!result.ok) {
          appendMessage(
            "error",
            result.filePath
              ? `${result.error ?? "External editor failed."} Temp file: ${result.filePath}`
              : (result.error ?? "External editor failed."),
          );
          return;
        }
        setInputValue(result.value ?? "");
        setInputCursorOffset((result.value ?? "").length);
        resetHistoryNavigation();
      },
      "history:previous": historyUp,
      "history:next": historyDown,
    },
    {
      context: "Chat",
      isActive: !isBlockingOverlayActive && !isTranscriptMode,
    },
  );

  useKeybindings(
    {
      "autocomplete:accept": () => {
        applySlashSuggestion(
          slashSuggestions[selectedSlashSuggestion] ?? null,
        );
        resetHistoryNavigation();
      },
      "autocomplete:previous": () => {
        setSelectedSlashSuggestion(current =>
          (current - 1 + slashSuggestions.length) % slashSuggestions.length,
        );
      },
      "autocomplete:next": () => {
        setSelectedSlashSuggestion(current =>
          (current + 1) % slashSuggestions.length,
        );
      },
      "autocomplete:dismiss": dismissAutocomplete,
    },
    {
      context: "Autocomplete",
      isActive:
        showSlashSuggestions &&
        !isBlockingOverlayActive &&
        !isTranscriptMode,
    },
  );

  useKeybindings(
    {
      "confirm:yes": () => {
        if (activePermission) {
          return false;
        }
        submitElicitation(false);
      },
      "confirm:no": () => {
        if (activePermission) {
          return false;
        }
        submitElicitation(true);
      },
      "confirm:next": () => {
        if (activePermission) {
          return false;
        }
        if (activeElicitation) {
          moveElicitationField(1);
        }
      },
      "confirm:previous": () => {
        if (activePermission) {
          return false;
        }
        if (activeElicitation) {
          moveElicitationField(-1);
        }
      },
      "confirm:nextField": () => {
        if (activePermission) {
          return false;
        }
        if (activeElicitation) {
          moveElicitationField(1);
        }
      },
      "confirm:previousField": () => {
        if (activePermission) {
          return false;
        }
        if (activeElicitation) {
          moveElicitationField(-1);
        }
      },
      "permission:allowAlways": () => {
        if (activePermission) {
          return false;
        }
      },
    },
    {
      context: "Confirmation",
      isActive: isConfirmationActive && memorySelector === null,
    },
  );

  useKeybindingInput(
    (value, key) => {
      if (memorySelector !== null) {
        return;
      }

      if (activePermission) {
        return false;
      }

      if (activePrompt) {
        if (key.escape) {
          submitPromptRequest("cancel");
          return;
        }

        if (key.return) {
          submitPromptRequest("submit");
          return;
        }

        if (isBackspaceInput(value, key) || isDeleteInput(value, key)) {
          updatePromptRequestValue(current => current.slice(0, -1));
          return;
        }

        if (value.length === 1 && !key.ctrl && !key.meta) {
          updatePromptRequestValue(current => current + value);
        }
        return;
      }

      if (activeElicitation) {
        const activeField = elicitationFields[activeElicitation.activeField];

        if (!activeField) {
          return;
        }

        if (isBackspaceInput(value, key) || isDeleteInput(value, key)) {
          setElicitationQueue(queue =>
            queue.map((entry, index) =>
              index === 0
                ? {
                    ...entry,
                    error: null,
                    values: {
                      ...entry.values,
                      [activeField.key]: (
                        entry.values[activeField.key] ?? ""
                      ).slice(0, -1),
                    },
                  }
                : entry,
            ),
          );
          return;
        }

        if (activeField.type === "boolean" && value === " ") {
          setElicitationQueue(queue =>
            queue.map((entry, index) =>
              index === 0
                ? {
                    ...entry,
                    error: null,
                    values: {
                      ...entry.values,
                      [activeField.key]:
                        entry.values[activeField.key] === "true"
                          ? "false"
                          : "true",
                    },
                  }
                : entry,
            ),
          );
          return;
        }

        if (value.length === 1 && !key.ctrl && !key.meta) {
          setElicitationQueue(queue =>
            queue.map((entry, index) =>
              index === 0
                ? {
                    ...entry,
                    error: null,
                    values: {
                      ...entry.values,
                      [activeField.key]:
                        (entry.values[activeField.key] ?? "") + value,
                    },
                  }
                : entry,
            ),
          );
        }
        return;
      }

      if (isTranscriptMode) {
        if (transcriptSearch.active) {
          if (key.escape || key.return) {
            closeTranscriptSearch();
            return;
          }

          if (isBackspaceInput(value, key) || isDeleteInput(value, key)) {
            updateTranscriptSearchQuery(
              transcriptSearch.query.slice(0, -1),
            );
            return;
          }

          if (value.length === 1 && !key.ctrl && !key.meta) {
            updateTranscriptSearchQuery(transcriptSearch.query + value);
          }
        }
        return;
      }

      if (
        footerSelection === "agents" &&
        agentsRuntime.selectedPanelTab === "transcript" &&
        agentTranscriptSearch.active
      ) {
        if (key.escape || key.return) {
          closeAgentTranscriptSearch();
          return;
        }

        if (isBackspaceInput(value, key) || isDeleteInput(value, key)) {
          updateAgentTranscriptSearchQuery(
            agentTranscriptSearch.query.slice(0, -1),
          );
          return;
        }

        if (value.length === 1 && !key.ctrl && !key.meta) {
          updateAgentTranscriptSearchQuery(
            agentTranscriptSearch.query + value,
          );
        }
        return;
      }

      if (vimMode === "INSERT") {
        const chunkSubmitValue = applyInputChunkBeforeSubmit(
          inputRef.current,
          inputCursorOffsetRef.current,
          value,
        );
        if (chunkSubmitValue !== null) {
          submitPrompt(chunkSubmitValue);
          return;
        }
      }

      promptInput.onInput(value, key);
    },
    {
      context: isPromptActive
        ? "Prompt"
        : isConfirmationActive
          ? "Confirmation"
          : isTranscriptMode
            ? "Transcript"
            : "Chat",
      isActive:
        memorySelector === null &&
        (activePermission === null ||
          !isAskUserQuestionRequest(activePermission)),
    },
  );

  const handleEvent = React.useCallback(
    (message: IPCMessage): void => {
      switch (message.type) {
        case "status_update":
          applyStatusUpdate(message.data as StatusUpdateData);
          return;

        case "command_result": {
          const data = message.data as CommandResultData;
          if (data.command === "rewind") {
            setTranscriptMutation("idle");
          }

          if (data.next_input !== undefined) {
            inputRef.current = data.next_input;
          }

          setAppState(prev => {
            const nextState = {
              ...prev,
              input:
                data.next_input !== undefined
                  ? data.next_input
                  : prev.input,
              inputMode:
                data.next_input !== undefined
                  ? getModeFromInput(data.next_input)
                  : prev.inputMode,
              messages: data.clear_messages ? [] : prev.messages,
            };
            return nextState;
          });

          if (data.next_input !== undefined) {
            setInputCursorOffset(data.next_input.length);
          }

          if (data.display !== "skip" && data.message) {
            appendMessage(
              data.display === "user" ? "user" : "system",
              data.message,
            );
          }

          if (data.next_input && data.submit_next_input) {
            submitPrompt(data.next_input);
          }
          return;
        }

        case "notification": {
          const data = message.data as NotificationData;
          if (transcriptMutation !== "idle" && data.title === "Rewind") {
            setTranscriptMutation("idle");
          }
          upsertActivity(
            `notification:${data.title}:${data.message}`,
            `${data.title}: ${data.message}`,
            {
              role: data.level === "error" ? "error" : "status",
              label: "Notice",
              status: data.level,
            },
          );
          return;
        }

        case "settings_update": {
          const data = message.data as SettingsUpdateData;
          setAppState(prev => {
            const nextSettings = { ...prev.settings };
            if (data.value === null) {
              delete nextSettings[data.key];
            } else {
              nextSettings[data.key] = data.value;
            }
            return {
              ...prev,
              settings: nextSettings,
            };
          });
          return;
        }

        case "llm_start":
          setAppState(prev => ({
            ...prev,
            isQuerying: true,
            messages: upsertActivityMessage(
              prev.messages,
              activeRunActivityKeyRef.current,
              "Thinking",
              {
                label: "Run",
                status: "running",
                hidden: true,
              },
            ),
          }));
          return;

        case "llm_token": {
          const token =
            (message.data as { token?: string }).token ?? "";
          enqueueAssistantToken(token);
          return;
        }

        case "llm_complete": {
          const data = message.data as LLMCompleteData;
          if (streamingFlushTimerRef.current !== null) {
            clearTimeout(streamingFlushTimerRef.current);
          }
          flushPendingAssistantTokens();
          setAppState(prev => {
            const streamingIndex = findLastStreamingAssistantIndex(
              prev.messages,
            );
            const messages =
              streamingIndex >= 0
                ? prev.messages.map((candidate, index) =>
                    index === streamingIndex
                      ? {
                          ...candidate,
                          meta: {
                            ...candidate.meta,
                            streaming: false,
                            hasReasoning:
                              getMessageText(candidate).trim().length === 0
                                ? true
                                : candidate.meta?.hasReasoning,
                          },
                        }
                      : candidate,
                  )
                : prev.messages;
            return {
              ...prev,
              isQuerying: false,
              runtime: {
                ...prev.runtime,
                inputTokens: data.input_tokens ?? prev.runtime.inputTokens,
                outputTokens:
                  data.output_tokens ?? prev.runtime.outputTokens,
              },
              messages: upsertActivityMessage(
                messages,
                activeRunActivityKeyRef.current,
                "Response complete",
                {
                  label: "Run",
                  status: data.stop_reason || "complete",
                  hidden: true,
                },
              ),
            };
          });
          return;
        }

        case "tool_start": {
          const data = message.data as ToolStartData;
          appendMessage(
            "tool",
            `${data.tool_name} started: ${summarizeToolInput(data.tool_input)}`,
            undefined,
            [
              {
                type: "tool_use",
                tool_name: data.tool_name,
                tool_use_id: data.tool_use_id,
                tool_input: data.tool_input,
                status: "running",
              },
            ],
          );
          return;
        }

        case "tool_progress": {
          const data = message.data as ToolProgressData;
          const progress = data.progress || "Tool progress";
          setAppState(prev => {
            const updated = updateToolUseMessage(
              prev.messages,
              data.tool_use_id,
              progress,
              {
                status: "running",
                summary: progress,
              },
            );
            if (updated.updated) {
              return {
                ...prev,
                messages: updated.messages,
              };
            }

            return {
              ...prev,
              messages: [
                ...prev.messages,
                createMessage("status", progress),
              ],
            };
          });
          return;
        }

        case "tool_complete": {
          const data = message.data as ToolCompleteData;
          const result = summarizeToolComplete(data);
          setAppState(prev => {
            const updated = updateToolUseMessage(
              prev.messages,
              data.tool_use_id,
              `Tool completed: ${result}`,
              {
                status: "complete",
                summary: undefined,
                result,
              },
            );
            if (updated.updated) {
              return {
                ...prev,
                messages: updated.messages,
              };
            }

            return {
              ...prev,
              messages: [
                ...prev.messages,
                createMessage(
                  "tool",
                  `Tool completed: ${result}`,
                  undefined,
                  [
                    {
                      type: "tool_use",
                      tool_use_id: data.tool_use_id,
                      tool_input: {},
                      status: "complete",
                      result,
                    },
                  ],
                ),
              ],
            };
          });
          return;
        }

        case "tool_error": {
          const data = message.data as ToolErrorData;
          const error = data.error || "Tool failed";
          setAppState(prev => {
            const updated = updateToolUseMessage(
              prev.messages,
              data.tool_use_id,
              error,
              {
                status: "error",
                summary: undefined,
                error,
              },
            );
            if (updated.updated) {
              return {
                ...prev,
                messages: updated.messages,
              };
            }

            return {
              ...prev,
              messages: [
                ...prev.messages,
                createMessage(
                  "tool",
                  error,
                  undefined,
                  [
                    {
                      type: "tool_use",
                      tool_use_id: data.tool_use_id,
                      tool_input: {},
                      status: "error",
                      error,
                    },
                  ],
                ),
              ],
            };
          });
          return;
        }

        case "permission_request":
          enqueuePermissionRequest(message.data as PermissionRequestData);
          return;

        case "tool_permission_ask": {
          const data = message.data as ToolPermissionAskData;
          enqueuePermissionRequest({
            ...data,
            response_channel: "tool_permission_response",
          });
          return;
        }

        case "prompt_request": {
          const data = message.data as PromptRequestData;
          setAppState(prev => ({
            ...prev,
            promptDialog: {
              queue: [
                ...prev.promptDialog.queue,
                {
                  request: data,
                  value: data.default_value ?? "",
                  error: null,
                },
              ],
            },
          }));
          appendMessage(
            "status",
            `Prompt requested: ${data.title ?? data.prompt_id}`,
          );
          return;
        }

        case "tool_permission_cancel": {
          const data = message.data as ToolPermissionCancelData;
          cancelPermissionRequest(data);
          setAppState(prev => {
            const queue = prev.promptDialog.queue.filter(
              entry =>
                !isToolPermissionPromptForCancel(entry.request.prompt_id, data),
            );
            if (queue.length === prev.promptDialog.queue.length) {
              return prev;
            }
            return {
              ...prev,
              promptDialog: {
                queue,
              },
            };
          });
          return;
        }

        case "elicitation_request": {
          const data = message.data as ElicitationRequestData;
          const initialValues = Object.fromEntries(
            extractSchemaFields(data.schema).map(field => [
              field.key,
              field.defaultValue ?? (field.type === "boolean" ? "false" : ""),
            ]),
          );
          setElicitationQueue(queue => [
            ...queue,
            {
              request: data,
              values: initialValues,
              activeField: 0,
              error: null,
            },
          ]);
          return;
        }

        case "iteration_start": {
          const data = message.data as {
            turn?: number;
            iteration?: number;
          };
          const iteration = data.iteration ?? data.turn;
          upsertActivity(
            `iteration:${String(iteration ?? "active")}`,
            `Iteration ${String(iteration ?? "?")} started`,
            {
              label: "Run",
              status: "running",
              hidden: true,
            },
          );
          return;
        }

        case "compact_start": {
          const data = message.data as CompactEventData;
          setCompactProgress(data);
          upsertActivity("compact", formatCompactProgressMessage(data), {
            label: "Context",
            status: "running",
          });
          setAppState(prev => ({
            ...prev,
            runtime: {
              ...prev.runtime,
              phase: "compact",
              tokenWarning: undefined,
            },
          }));
          return;
        }

        case "compact_complete": {
          const data = message.data as CompactEventData;
          setCompactProgress(null);
          const compactText = formatCompactCompleteMessage(data);
          const compactRole: AppMessage["role"] =
            data.success === false ? "error" : "status";
          const compactOptions = {
            role: compactRole,
            label: "Context",
            status: data.success === false ? "failed" : "completed",
          };
          const compactMessages = data.messages;
          const nextPhase =
            data.success === false ? "compact_failed" : "compacted";
          if (Array.isArray(compactMessages)) {
            clearTranscriptSearch();
            clearTranscriptSelection();
            setAppState(prev => ({
              ...prev,
              messages: upsertActivityMessage(
                normalizeExternalMessages(compactMessages),
                "compact",
                compactText,
                compactOptions,
              ),
              runtime: {
                ...prev.runtime,
                phase: nextPhase,
                tokenWarning: undefined,
              },
            }));
          } else {
            setAppState(prev => ({
              ...prev,
              messages: upsertActivityMessage(
                prev.messages,
                "compact",
                compactText,
                compactOptions,
              ),
              runtime: {
                ...prev.runtime,
                phase: nextPhase,
                tokenWarning: undefined,
              },
            }));
          }
          return;
        }

        case "token_warning": {
          const data = message.data as TokenWarningEventData;
          setAppState(prev => ({
            ...prev,
            runtime: {
              ...prev.runtime,
              tokenWarning: data,
            },
          }));
          return;
        }

        case "memory_selector": {
          setMemorySelector(message.data as MemorySelectorData);
          return;
        }

        case "memory_saved": {
          const data = message.data as MemorySavedEventData;
          const text = formatMemorySavedMessage(data);
          upsertActivity(`memory-saved:${text}`, text, {
            label: "Memory",
            status: "completed",
          });
          return;
        }

        case "memory_logged": {
          const data = message.data as MemoryLoggedEventData;
          const text = formatMemoryLoggedMessage(data);
          const timestamp = message.timestamp ?? Date.now();
          upsertActivity(`memory-logged:${timestamp}`, text, {
            label: "Memory",
            status: "completed",
          });
          setAppState(prev => ({
            ...prev,
            agents: applyAgentPayloadEvent(
              prev.agents,
              "agent_task_update",
              {
                task_id: `memory-log-${timestamp}`,
                agent_id: "memory",
                name: "Daily Log Append",
                agent_type: "memory",
                task_type: "daily_log",
                status: "completed",
                description: "Daily memory log append",
                current_operation: text,
                start_time: timestamp,
                end_time: timestamp,
                background: true,
                ...asRecord(data),
              },
              timestamp,
            ),
          }));
          return;
        }

        case "memory_extraction_start": {
          const data = message.data as MemoryExtractionStartData;
          const timestamp = message.timestamp ?? Date.now();
          const taskId = data.task_id ?? "memory-extract";
          const operation =
            data.memory_mode === "daily_log"
              ? "Scanning conversation for daily-log entries"
              : "Scanning conversation for persistent memories";
          upsertActivity(`memory-extract:${taskId}`, operation, {
            label: "Memory",
            status: "running",
            hidden: true,
          });
          setAppState(prev => ({
            ...prev,
            agents: applyAgentPayloadEvent(
              prev.agents,
              "agent_task_update",
              {
                task_id: taskId,
                agent_id: "memory",
                name: "Memory Extract",
                agent_type: "memory",
                task_type: "memory_extract",
                status: "running",
                description: "Background memory extraction",
                current_operation: operation,
                start_time: timestamp,
                background: true,
                ...asRecord(data),
              },
              timestamp,
            ),
          }));
          return;
        }

        case "memory_extraction_complete": {
          const data = message.data as MemoryExtractionCompleteData;
          const timestamp = message.timestamp ?? Date.now();
          const taskId = data.task_id ?? "memory-extract";
          const saved = data.memories_saved ?? 0;
          const logs = data.logs_written ?? 0;
          const text =
            saved > 0
              ? `Memory extract saved ${saved} memor${saved === 1 ? "y" : "ies"}`
              : logs > 0
                ? `Memory extract logged ${logs} file${logs === 1 ? "" : "s"}`
                : "Memory extract completed";
          upsertActivity(`memory-extract:${taskId}`, text, {
            label: "Memory",
            status: "completed",
            hidden: true,
          });
          setAppState(prev => ({
            ...prev,
            agents: applyAgentPayloadEvent(
              prev.agents,
              "agent_task_update",
              {
                task_id: taskId,
                agent_id: "memory",
                name: "Memory Extract",
                agent_type: "memory",
                task_type: "memory_extract",
                status: "completed",
                description: "Background memory extraction",
                current_operation: text,
                start_time:
                  typeof data.duration_ms === "number"
                    ? timestamp - data.duration_ms
                    : timestamp,
                end_time: timestamp,
                background: true,
                ...asRecord(data),
              },
              timestamp,
            ),
          }));
          return;
        }

        case "memory_extraction_error": {
          const data = message.data as MemoryExtractionErrorData;
          const timestamp = message.timestamp ?? Date.now();
          const taskId = data.task_id ?? "memory-extract";
          const text = `Memory extract failed: ${data.error}`;
          upsertActivity(`memory-extract:${taskId}`, text, {
            role: "error",
            label: "Memory",
            status: "failed",
          });
          setAppState(prev => ({
            ...prev,
            agents: applyAgentPayloadEvent(
              prev.agents,
              "agent_task_update",
              {
                task_id: taskId,
                agent_id: "memory",
                name: "Memory Extract",
                agent_type: "memory",
                task_type: "memory_extract",
                status: "failed",
                description: "Background memory extraction",
                current_operation: text,
                start_time:
                  typeof data.duration_ms === "number"
                    ? timestamp - data.duration_ms
                    : timestamp,
                end_time: timestamp,
                background: true,
                ...asRecord(data),
              },
              timestamp,
            ),
          }));
          return;
        }

        case "auto_dream_start":
        case "auto_dream_progress":
        case "auto_dream_complete":
        case "auto_dream_error":
        case "auto_dream_cancelled": {
          const data = message.data as AutoDreamEventData;
          const timestamp = message.timestamp ?? Date.now();
          const taskId = data.task_id ?? "memory-dream";
          const status =
            message.type === "auto_dream_complete"
              ? "completed"
              : message.type === "auto_dream_error"
                ? "failed"
                : message.type === "auto_dream_cancelled"
                  ? "killed"
                  : "running";
          const operation = formatAutoDreamOperation(message.type, data);
          if (status === "running") {
            setDreamProgress(previous => ({
              ...(previous ?? {}),
              ...data,
            }));
            upsertActivity(`auto-dream:${taskId}`, operation, {
              label: "Dream",
              status,
            });
          } else {
            setDreamProgress(null);
          }
          setAppState(prev => ({
            ...prev,
            agents: applyAgentPayloadEvent(
              prev.agents,
              "agent_task_update",
              {
                task_id: taskId,
                agent_id: "memory",
                name: "Memory Dream",
                agent_type: "memory",
                task_type: "dream",
                status,
                description: "Memory consolidation",
                current_operation: operation,
                ...(message.type === "auto_dream_start"
                  ? { start_time: timestamp }
                  : {}),
                end_time: status === "running" ? undefined : timestamp,
                background: true,
                progress: {
                  phase: data.phase,
                  files_touched: data.files_touched,
                  turn: data.turn,
                },
                ...asRecord(data),
              },
              timestamp,
            ),
          }));
          if (status !== "running") {
            upsertActivity(`auto-dream:${taskId}`, operation, {
              role: status === "failed" ? "error" : "status",
              label: "Dream",
              status,
            });
          }
          return;
        }

        case "task_start": {
          const data = message.data as TaskStartData;
          markTaskStart(data);
          upsertActivity(
            `task:${data.task_id ?? "active"}`,
            data.title
              ? `Task started: ${data.title}`
              : `Task ${data.task_id} started`,
            {
              label: "Task",
              status: "running",
              hidden: true,
            },
          );
          return;
        }

        case "task_progress": {
          const data = message.data as TaskProgressData;
          markTaskProgress(data);
          upsertActivity(
            `task:${data.task_id ?? "active"}`,
            data.progress
              ? `Task progress: ${data.progress}`
              : `Task ${data.task_id} in progress`,
            {
              label: "Task",
              status: "running",
              hidden: true,
            },
          );
          return;
        }

        case "task_complete": {
          const data = message.data as TaskCompleteData;
          markTaskComplete(data);
          setAppState(prev => ({
            ...prev,
            isQuerying: false,
            runtime: {
              ...prev.runtime,
              phase: data.status ?? "completed",
            },
          }));
          upsertActivity(
            `task:${data.task_id ?? "active"}`,
            data.result
              ? `Task completed: ${data.result}`
              : `Task ${data.task_id ?? "active"} completed`,
            {
              label: "Task",
              status: "completed",
              hidden: true,
            },
          );
          return;
        }

        case "task_error": {
          const data = message.data as TaskErrorData;
          markTaskError(data);
          setAppState(prev => ({
            ...prev,
            isQuerying: false,
            runtime: {
              ...prev.runtime,
              phase: "error",
            },
          }));
          upsertActivity(
            `task:${data.task_id ?? "active"}`,
            `Task ${data.task_id ?? "active"} failed: ${data.error}`,
            {
              role: "error",
              label: "Task",
              status: "failed",
            },
          );
          return;
        }

        case "mcp_status": {
          const data = message.data as McpStatusData;
          setAppState(prev =>
            applyMcpStatusUpdate(prev, {
              serverName: data.server_name,
              status: data.status,
              error: data.error,
              updatedAt: Date.now(),
              tools: Array.isArray(data.tools)
                ? data.tools.map(tool => ({
                    name: tool.name,
                    description: tool.description,
                    serverName: tool.server_name,
                  }))
                : undefined,
              commands: Array.isArray(data.commands) ? data.commands : undefined,
              resources:
                data.resources && typeof data.resources === "object"
                  ? data.resources
                  : undefined,
            }),
          );
          upsertActivity(
            `mcp:${data.server_name}`,
            `MCP ${data.server_name}: ${data.message ?? data.status}`,
            {
              role: data.status === "error" ? "error" : "status",
              label: "MCP",
              status: data.status,
            },
          );
          return;
        }

        case "skill_selected": {
          const data = message.data as {
            skill_name?: string;
            skill_ids?: string[];
          };
          const selected =
            data.skill_name ??
            (Array.isArray(data.skill_ids)
              ? data.skill_ids.join(", ")
              : "unknown");
          upsertActivity(`skill:${selected}`, `Skill selected: ${selected}`, {
            label: "Context",
            status: "selected",
          });
          return;
        }

        case "agent_list": {
          const data = message.data as AgentListData;
          if (isStaleSessionEvent(runtime.sessionId, data.session_id)) {
            return;
          }
          if (
            typeof data.session_id === "string" &&
            data.session_id !== agentsRuntime.sessionId
          ) {
            clearAgentTranscriptSearch();
          }
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
              return prev;
            }

            const nextSessionId =
              typeof data.session_id === "string"
                ? (data.session_id ?? prev.agents.sessionId)
                : prev.agents.sessionId;
            const isFreshSession =
              typeof nextSessionId === "string" &&
              nextSessionId.length > 0 &&
              nextSessionId !== prev.agents.sessionId;
            const nextAgents = Array.isArray(data.agents) ? data.agents : [];
            const viewedAgentStillExists =
              prev.agents.viewedAgentId !== null &&
              nextAgents.some(
                candidate => candidate.agent_id === prev.agents.viewedAgentId,
              );
            const defaultViewedAgentId =
              typeof nextAgents[0]?.agent_id === "string"
                ? nextAgents[0].agent_id
                : null;

            return {
              ...prev,
              agents: {
                ...prev.agents,
                sessionId: nextSessionId,
                selectedPanelTab: isFreshSession ? "list" : prev.agents.selectedPanelTab,
                selectedEventIndex: isFreshSession ? 0 : prev.agents.selectedEventIndex,
                transcriptCursor: isFreshSession ? null : prev.agents.transcriptCursor,
                list: nextAgents,
                events: isFreshSession ? [] : prev.agents.events,
                transcripts: isFreshSession ? {} : prev.agents.transcripts,
                backgroundTasks: isFreshSession
                  ? {}
                  : prev.agents.backgroundTasks,
                coordinator: isFreshSession
                  ? {
                      runningWorkers: 0,
                      totalWorkers: 0,
                    }
                  : prev.agents.coordinator,
                todos: isFreshSession
                  ? {
                      todos: [],
                      oldTodos: [],
                    }
                  : prev.agents.todos,
                viewedAgentId:
                  isFreshSession
                    ? defaultViewedAgentId
                    : viewedAgentStillExists
                      ? prev.agents.viewedAgentId
                      : defaultViewedAgentId,
              },
            };
          });
          return;
        }

        case "agent_event": {
          const data = message.data as AgentEventData;
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
              return prev;
            }

            return {
              ...prev,
              agents: applyAgentEventData(
                prev.agents,
                data,
                message.timestamp ?? Date.now(),
              ),
            };
          });
          return;
        }

        case "agent_start":
        case "agent_progress":
        case "agent_output":
        case "agent_error":
        case "agent_complete": {
          const data = message.data as RuntimeActivityData;
          const payload = asRecord(data);
          const eventSessionId = getStringValue(payload, ["session_id", "sessionId"]);
          const status = runtimeActivityStatus(message.type, payload);
          const timestamp = eventTimestamp(
            data.timestamp,
            message.timestamp ?? Date.now(),
          );
          upsertActivity(
            runtimeActivityKey(message.type, payload),
            formatAgentRuntimeActivity(message.type, data),
            {
              role: runtimeActivityRole(status),
              label: "Agent",
              status,
              hidden: !shouldDisplayAgentRuntimeActivity(message.type, status),
            },
          );
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, eventSessionId)) {
              return prev;
            }

            return {
              ...prev,
              agents: applyAgentPayloadEvent(
                prev.agents,
                message.type,
                payload,
                timestamp,
                eventSessionId,
              ),
            };
          });
          return;
        }

        case "agent_spawn": {
          const data = message.data as AgentSpawnData;
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
              return prev;
            }

            return {
              ...prev,
              agents: applyAgentPayloadEvent(
                prev.agents,
                "agent_spawn",
                asRecord(data),
                message.timestamp ?? Date.now(),
                data.session_id,
              ),
            };
          });
          return;
        }

        case "agent_task_update":
        case "agent_task_complete":
        case "task_started":
        case "task_completed":
        case "task_failed":
        case "task_stopped": {
          const data = message.data as AgentTaskUpdateData;
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
              return prev;
            }

            return {
              ...prev,
              agents: applyAgentPayloadEvent(
                prev.agents,
                message.type,
                asRecord(data),
                message.timestamp ?? Date.now(),
                data.session_id,
              ),
            };
          });
          return;
        }

        case "team_update": {
          const data = message.data as TeamUpdateData;
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
              return prev;
            }

            return {
              ...prev,
              agents: applyAgentPayloadEvent(
                prev.agents,
                "team_update",
                asRecord(data),
                message.timestamp ?? Date.now(),
                data.session_id,
              ),
            };
          });
          return;
        }

        case "todo_update": {
          const data = message.data as TodoUpdateData;
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
              return prev;
            }

            return {
              ...prev,
              agents: applyAgentPayloadEvent(
                prev.agents,
                "todo_update",
                asRecord(data),
                message.timestamp ?? Date.now(),
                data.session_id,
              ),
            };
          });
          return;
        }

        case "agent_transcript": {
          const data = message.data as AgentTranscriptData;
          const transcriptAgentId = data.agent_id ?? "primary";
          setAppState(prev => {
            if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
              return prev;
            }

            return {
              ...prev,
              agents: {
                ...prev.agents,
                sessionId:
                  typeof data.session_id === "string"
                    ? data.session_id
                    : prev.agents.sessionId,
                viewedAgentId: prev.agents.viewedAgentId ?? transcriptAgentId,
                transcripts: {
                  ...prev.agents.transcripts,
                  [transcriptAgentId]: normalizeExternalMessages(
                    Array.isArray(data.messages) ? data.messages : [],
                  ),
                },
                transcriptCursor: Array.isArray(data.messages)
                  ? Math.max(0, data.messages.length - 1)
                  : prev.agents.transcriptCursor,
              },
            };
          });
          return;
        }

        case "hook_start":
        case "hook_complete": {
          const data = message.data as HookEventData;
          const payload = asRecord(data);
          const status = message.type === "hook_start" ? "running" : "completed";
          const hookId =
            getStringValue(payload, ["hook_name", "hookName", "hook_event"]) ??
            "active";
          upsertActivity(
            `runtime:hook:${hookId}`,
            formatHookActivity(message.type, data),
            {
              label: "Hook",
              status,
              hidden: true,
            },
          );
          return;
        }

        case "background_session_update": {
          const data = message.data as BackgroundSessionUpdateData;
          setAppState(prev => ({
            ...prev,
            backgroundSession: {
              sessionId: data.session_id,
              taskId: data.task_id,
              status: data.status,
              title: data.title,
              activeAgentId: data.active_agent_id,
              agentCount: data.agent_count,
              eventCount: data.event_count,
              transcriptCount: data.transcript_count,
              metadata: data.metadata,
              updatedAt: Date.now(),
            },
          }));
          return;
        }

        case "session_list": {
          const data = message.data as SessionListData;
          appendMessage(
            "status",
            `Received ${data.sessions.length} resumable session(s)`,
          );
          for (const session of data.sessions.slice(0, 8)) {
            appendMessage(
              "system",
              `${session.session_id} | ${session.project_path || "unknown"} | ${truncate(session.preview || "No preview", 100)}`,
            );
          }
          return;
        }

        case "session_restored": {
          const data = message.data as SessionRestoredData;
          const sessionContext = normalizeSessionContext(data);
          clearTranscriptSearch();
          clearTranscriptSelection();
          clearAgentTranscriptSearch();
          if (transcriptMutation === "restore") {
            setTranscriptRestoreBuffer(null);
          }
          if (transcriptMutation !== "idle") {
            setTranscriptMutation("idle");
          }
          inputRef.current = "";
          setAppState(prev => ({
            ...prev,
            input: "",
            inputMode: "insert",
            isQuerying: false,
            sessionContext,
	      runtime: {
	        ...prev.runtime,
	        ...sessionContext.runtime,
	        sessionId: data.session_id,
	        costUsd: data.cost ?? undefined,
	        phase: normalizeRestoredRuntimePhase(
	          sessionContext.runtime?.phase,
	        ),
              viewMode:
                prev.footerSelection === "agents"
                  ? "agent"
                  : "prompt",
            },
            messages: [
              ...normalizeExternalMessages(
                Array.isArray(data.messages) ? data.messages : [],
              ),
              createMessage("status", `Restored session ${data.session_id}`),
            ],
          }));
          setInputCursorOffset(0);
          promptInputModeRef.current?.("INSERT");
          return;
        }

        case "doctor_result": {
          const data = message.data as DoctorResultData;
          if (data.run_done || (data.run_done === undefined && data.done)) {
            appendMessage(
              "status",
              data.summary ?? "Doctor run completed",
            );
            return;
          }

          for (const check of data.checks) {
            appendMessage(
              check.status === "fail" ? "error" : "system",
              `[${check.status}] ${check.name}: ${check.message}${check.details ? ` (${check.details})` : ""}`,
            );
          }
          return;
        }

        case "cancel":
          setAppState(prev => ({
            ...prev,
            isQuerying: false,
          }));
          upsertActivity(
            activeRunActivityKeyRef.current,
            "Query cancelled",
            {
              label: "Run",
              status: "cancelled",
            },
          );
          return;

        default:
          if (RUNTIME_ACTIVITY_EVENTS.has(message.type)) {
            const data = message.data as RuntimeActivityData;
            const payload = asRecord(data);
            const status = runtimeActivityStatus(message.type, payload);
            upsertActivity(
              runtimeActivityKey(message.type, payload),
              formatRuntimeActivity(message.type, data),
              {
                role: runtimeActivityRole(status),
                label: "Runtime",
                status,
                hidden: !shouldDisplayRuntimeActivity(message.type, status),
              },
            );
            return;
          }
          return;
      }
    },
    [
      agentsRuntime.sessionId,
      appendMessage,
      applyStatusUpdate,
      clearAgentTranscriptSearch,
      clearTranscriptSearch,
      clearTranscriptSelection,
      enqueuePermissionRequest,
      enqueueAssistantToken,
      cancelPermissionRequest,
      flushPendingAssistantTokens,
      markTaskProgress,
      markTaskStart,
      markTaskComplete,
      markTaskError,
      runtime.sessionId,
      setAppState,
      submitPrompt,
      transcriptMutation,
      upsertActivity,
    ],
  );

  useStructuredIOListener(io, handleEvent, {
    replayRecent: true,
  });

  const viewedAgentId = agentsRuntime.viewedAgentId;
  const viewedAgentTranscript =
    viewedAgentId !== null
      ? agentsRuntime.transcripts[viewedAgentId] ?? []
      : [];
  const selectedAgentTranscriptMessageId =
    agentsRuntime.transcriptCursor !== null
      ? viewedAgentTranscript[agentsRuntime.transcriptCursor]?.id ?? null
      : null;
  const hasRuntimeData =
    agentsRuntime.list.length > 0 ||
    agentsRuntime.events.length > 0 ||
    viewedAgentTranscript.length > 0 ||
    backgroundSession !== null;
  const showAgentsPanel = footerSelection === "agents";
  const showBackgroundPanel = footerSelection === "background";
  const runtimeActionHints = [
    "`ctrl+]` close",
    "`ctrl+b` background",
  ];
  const agentTranscriptActionHints = [
    ...runtimeActionHints,
    "`ctrl+r` search",
  ];
  const backgroundControlHints = [
    { key: "/background start", label: "start" },
    { key: "/background pause", label: "pause" },
    { key: "/background resume", label: "resume" },
    { key: "/background stop", label: "stop" },
    { key: "/background focus", label: "focus" },
  ];
  const handleAgentTranscriptCursorChange = React.useCallback(
    (messageId: string | null, index: number | null): void => {
      setAppState(prev => ({
        ...prev,
        agents: {
          ...prev.agents,
          transcriptCursor: index,
        },
      }));
    },
    [setAppState],
  );

  const afterMessagesContent = (
    <>
      {expandedView === "tasks" || footerSelection === "tasks" ? (
        <Box marginTop={1} flexDirection="column">
          <TodoListPanel state={agentsRuntime.todos} />
          <Box marginTop={agentsRuntime.todos.todos.length > 0 ? 1 : 0}>
            <TaskPanel tasks={tasks} expanded={true} />
          </Box>
        </Box>
      ) : null}

      {expandedView === "teammates" ? (
        <Box marginTop={1}>
          <BackgroundTasksPanel tasks={agentsRuntime.backgroundTasks} />
        </Box>
      ) : null}

      {footerSelection === "mcp" ? (
        <Box marginTop={1}>
          <MCPPanel
            clients={mcpClientStates}
            tools={mcpState?.tools ?? []}
            commands={mcpState?.commands ?? []}
            resources={mcpState?.resources ?? {}}
          />
        </Box>
      ) : null}

      {showAgentsPanel ? (
        <Box marginTop={1} flexDirection="column">
          <BackgroundTasksPanel tasks={agentsRuntime.backgroundTasks} />
          <Box marginTop={1}>
          <AgentRuntimePane
            title="Agent Runtime"
            selectedTab={agentsRuntime.selectedPanelTab}
            agents={agentsRuntime.list}
            events={agentsRuntime.events}
            transcriptMessages={viewedAgentTranscript}
            backgroundSession={
              backgroundSession?.status === "idle"
                ? undefined
                : backgroundSession ?? undefined
            }
            selectedAgentId={viewedAgentId}
            selectedEventIndex={agentsRuntime.selectedEventIndex}
            transcriptCursor={agentsRuntime.transcriptCursor}
            selectedMessageId={selectedAgentTranscriptMessageId}
            agentLabel={viewedAgentId ?? "primary"}
            actionHints={agentTranscriptActionHints}
            maxTranscriptMessages={12}
            transcriptPanelRef={agentTranscriptRef}
            transcriptSearchVisible={agentTranscriptSearch.active}
            transcriptSearchQuery={agentTranscriptSearch.query}
            transcriptSearchMatchCount={agentTranscriptSearch.matchCount}
            transcriptSearchCurrentMatch={agentTranscriptSearch.currentMatch}
            onTranscriptSearchMatchesChange={
              handleAgentTranscriptSearchMatchesChange
            }
            onTranscriptCursorChange={handleAgentTranscriptCursorChange}
          />
          </Box>
        </Box>
      ) : null}

      {showBackgroundPanel ? (
        <Box marginTop={1} flexDirection="column">
          <BackgroundTasksPanel tasks={agentsRuntime.backgroundTasks} />
          <Box marginTop={1}>
          <BackgroundControlsPanel
            session={backgroundSession}
            selectedTab={agentsRuntime.selectedPanelTab}
            actionHints={[
              hasRuntimeData
                ? "Background runtime is connected"
                : "Background runtime is idle",
            ]}
            controls={backgroundControlHints}
          />
          </Box>
        </Box>
      ) : null}

      {showSlashSuggestions && slashSuggestionRows > 0 && !isTranscriptMode ? (
        <Box
          marginTop={1}
          paddingX={2}
          width="100%"
          height={slashSuggestionRows}
          overflowY="hidden"
        >
          <SlashCommandComplete
            items={slashCompletionItems}
            selectedIndex={selectedSlashSuggestion}
            visible={showSlashSuggestions}
            maxVisibleItems={slashSuggestionItemRows}
            maxColumnWidth={Math.max(12, Math.floor(size.columns * 0.4))}
            bordered={false}
          />
        </Box>
      ) : null}
    </>
  );

  const showAllMessages = isTranscriptMode && showAllInTranscript;
  const messageSurfaceHeight = React.useMemo(
    () =>
      estimateMessagesContentHeight(messages, size.columns, {
        showAll: showAllMessages,
      }),
    [messages, showAllMessages, size.columns],
  );
  const permissionSurfaceRows = activePermission
    ? Math.max(
        6,
        Math.min(
          18,
          size.rows - statusLineRows - bottomRows - 1,
        ),
      )
    : 0;
  const auxiliarySurfaceHeight =
    (expandedView === "tasks" || footerSelection === "tasks"
      ? Math.max(
          3,
          Object.keys(tasks).length * 3 +
            Math.max(0, agentsRuntime.todos.todos.length * 2),
        )
      : 0) +
    (expandedView === "teammates"
      ? Math.max(4, Object.keys(agentsRuntime.backgroundTasks).length * 3)
      : 0) +
    (footerSelection === "mcp"
      ? Math.max(
          3,
          mcpClientStates.length * 2 +
            (mcpState?.tools.length ?? 0) +
            Object.keys(mcpState?.resources ?? {}).length * 2,
        )
      : 0) +
    (showAgentsPanel ? 26 : 0) +
    (showBackgroundPanel ? 18 : 0) +
    permissionSurfaceRows +
    (memorySelector
      ? Math.max(8, Math.min(16, memorySelector.targets.length * 2 + 5))
      : 0) +
    (showSlashSuggestions && !isTranscriptMode ? slashSuggestionRows + 1 : 0) +
    (activePrompt ? 7 : 0);
  const layoutContentHeight = Math.max(
    messageRows,
    messageSurfaceHeight + auxiliarySurfaceHeight,
  );
  const visibleAuxiliaryRows =
    auxiliarySurfaceHeight > 0
      ? Math.min(auxiliarySurfaceHeight, Math.max(0, messageRows - 1))
      : 0;
  const messageViewportRows = Math.max(
    1,
    messageRows - visibleAuxiliaryRows,
  );

  React.useEffect(() => {
    writeLayoutDebug({
      rows: size.rows,
      columns: size.columns,
      statusLineRows,
      modalRows,
      bottomRows,
      promptRows,
      selectorRows,
      transcriptRows,
      slashSuggestionRows,
      slashSuggestionItemRows,
      messageRows,
      messageViewportRows,
      messageSurfaceHeight,
      auxiliarySurfaceHeight,
      permissionSurfaceRows,
      layoutContentHeight,
      messages: messages.length,
      inputLength: input.length,
      showSlashSuggestions,
      footerSelection,
      expandedView,
      isTranscriptMode,
      activePermission: activePermission !== null,
      activeElicitation: activeElicitation !== null,
      activePrompt: activePrompt !== null,
      memorySelector: memorySelector !== null,
      scrollTop: scrollRef.current?.getScrollTop() ?? null,
      scrollHeight: scrollRef.current?.getScrollHeight() ?? null,
      viewportHeight: scrollRef.current?.getViewportHeight() ?? null,
    });
  }, [
    activeElicitation,
    activePermission,
    activePrompt,
    auxiliarySurfaceHeight,
    bottomRows,
    expandedView,
    footerSelection,
    input.length,
    isTranscriptMode,
    layoutContentHeight,
    memorySelector,
    messageRows,
    messageViewportRows,
    messageSurfaceHeight,
    messages.length,
    modalRows,
    permissionSurfaceRows,
    promptRows,
    selectorRows,
    showSlashSuggestions,
    size.columns,
    size.rows,
    slashSuggestionItemRows,
    slashSuggestionRows,
    statusLineRows,
    transcriptRows,
  ]);

  const inputDisabledReason =
    activePermission !== null
      ? "Awaiting your permission decision"
      : activeElicitation !== null
        ? "Complete the active MCP prompt"
        : activePrompt !== null
          ? "Complete the active prompt"
          : compactProgress !== null
            ? "Compacting conversation context"
            : undefined;

  const bottomContent = activePermission !== null ? (
    <Box height={0} width="100%" />
  ) : (
    <Box
      flexDirection="column"
      height={bottomRows}
      overflowY="hidden"
      width="100%"
    >
      {memorySelector ? (
        <MemoryFileSelector
          data={memorySelector}
          onSelect={selectMemoryTarget}
          onCancel={cancelMemorySelector}
        />
      ) : null}

      {isTranscriptMode ? (
	        <TranscriptModeFooter
	          messageCount={copyableMessages.length}
          showAllInTranscript={showAllInTranscript}
          searchQuery={transcriptSearch.query}
          searchMatchCount={transcriptSearch.matchCount}
          searchCurrentMatch={transcriptSearch.currentMatch}
	          cursorMessageLabel={
	            selectedTranscriptCursorMessage
	              ? `#${Math.max(1, selectedTranscriptCursorIndex + 1)} ${truncate(
	                  getMessageText(selectedTranscriptCursorMessage).replace(/\s+/g, " ").trim() ||
	                    selectedTranscriptCursorMessage.role,
	                  72,
                )}`
              : null
          }
	          selectedMessageLabel={
	            selectedTranscriptMessage
	              ? `#${(transcriptSelection.targetIndex ?? 0) + 1} ${truncate(
                  getMessageText(selectedTranscriptMessage).replace(/\s+/g, " ").trim() ||
                    selectedTranscriptMessage.role,
                  72,
                )}`
              : null
          }
          restoreAvailable={transcriptRestoreBuffer !== null}
        />
      ) : (
        <PromptInput
          value={input}
          disabled={
            compactProgress !== null ||
            activePermission !== null ||
            activeElicitation !== null ||
            activePrompt !== null
          }
          disabledReason={inputDisabledReason}
          busy={isQuerying}
          inputMode={getModeFromInput(input)}
          vimMode={vimMode}
          cursorOffset={promptInput.offset}
          suggestions={slashCompletionItems}
          selectedSuggestion={selectedSlashSuggestion}
          showSuggestions={showSlashSuggestions}
          renderSuggestionsInline={false}
          publishSuggestionsOverlay={false}
          maxInputRows={MAX_PROMPT_INPUT_ROWS}
          sandbox={runtime.sandbox}
        />
      )}
    </Box>
  );
  const messagesContent = React.useMemo(
    () => (
      <Box
        flexDirection="column"
        paddingX={1}
        width="100%"
      >
        <Messages
          messages={messages}
          maxRows={messageViewportRows}
          scrollRef={scrollRef}
          jumpRef={jumpRef}
          onSearchMatchesChange={handleTranscriptSearchMatchesChange}
          cursorNavRef={transcriptCursorNavRef}
          onCursorChange={handleTranscriptCursorChange}
          showAll={showAllMessages}
        />
      </Box>
    ),
    [
      handleTranscriptCursorChange,
      handleTranscriptSearchMatchesChange,
      messageRows,
      messageViewportRows,
      messages,
      showAllMessages,
    ],
  );

  return (
    <Box flexDirection="column" height={size.rows}>
      <StatusLine
        runtime={runtime}
        mcpClientStates={mcpClientStates}
        agents={agentsRuntime}
        vimMode={vimMode}
      />
      <FullscreenLayout
        scrollRef={scrollRef}
        modalScrollRef={modalScrollRef}
        messages={messagesContent}
        afterMessages={afterMessagesContent}
        overlay={
          activePermission ? (
            <PermissionRequest
              request={activePermission}
              queueLength={permissionQueue.length}
              onResolve={resolvePermission}
            />
          ) : undefined
        }
        modal={
          activeElicitation ? (
            <ElicitationDialog
              draft={activeElicitation}
              fields={elicitationFields}
            />
          ) : activePrompt ? (
            <PromptDialog draft={activePrompt} />
	          ) : isTranscriptMode && transcriptSelection.active ? (
	            <MessageSelector
	              messages={rewindableMessages}
	              selectedIndex={transcriptSelection.selectedIndex}
	              targetIndex={transcriptSelection.targetIndex}
	            />
          ) : undefined
        }
        bottomFloat={
          isTranscriptMode && transcriptSearch.active ? (
            <TranscriptSearchBar
              query={transcriptSearch.query}
              matchCount={transcriptSearch.matchCount}
              currentMatch={transcriptSearch.currentMatch}
            />
          ) : undefined
        }
        bottom={bottomContent}
        scrollRows={messageRows}
        contentHeight={layoutContentHeight}
      />
    </Box>
  );
}
