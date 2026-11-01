import React from "react";
import type {
  IPCMessage,
  SessionListData,
  SessionRestoredData,
  StructuredMessageContentBlock,
  StructuredMessageData,
} from "../bridge/protocol.js";
import {
  getStructuredIOSequence,
  type StructuredIO,
} from "../bridge/structuredIO.js";
import type {
  AppMessage,
  AppMessageRole,
  RuntimeState,
  SessionContextState,
} from "../state/AppStateStore.js";

const GROUP_DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export type SessionSummary = SessionListData["sessions"][number];

export function createMessage(
  role: AppMessageRole,
  text: string,
  meta?: Record<string, unknown>,
  content?: StructuredMessageContentBlock[],
): AppMessage {
  const normalizedContent = normalizeMessageContent(
    content ?? text,
    text,
  );
  const normalizedText =
    text || flattenMessageContent(normalizedContent);

  return {
    id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
    role,
    text: normalizedText,
    content: normalizedContent,
    timestamp: Date.now(),
    meta,
  };
}

export function stringifyUnknown(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }

  if (value instanceof Error) {
    return value.message;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) {
    return text;
  }

  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

export function flattenMessageContent(
  content: StructuredMessageContentBlock[],
): string {
  return content
    .map(block => {
      if (!block || typeof block !== "object") {
        return "";
      }

      switch (block.type) {
        case "text":
          return typeof block.text === "string" ? block.text : "";
        case "status":
          return typeof block.text === "string" ? block.text : "";
        case "field":
          return `${block.label}: ${block.value}`;
        case "tool_use": {
          const parts: string[] = [];
          if (typeof block.tool_name === "string" && block.tool_name) {
            parts.push(block.tool_name);
          }
          if (typeof block.summary === "string" && block.summary) {
            parts.push(block.summary);
          }
          if (typeof block.result === "string" && block.result) {
            parts.push(block.result);
          }
          if (typeof block.error === "string" && block.error) {
            parts.push(block.error);
          }
          return parts.join("\n");
        }
        default:
          return "";
      }
    })
    .filter(Boolean)
    .join("\n")
    .trim();
}

export function formatUsd(cost: number | null | undefined): string {
  if (typeof cost !== "number" || Number.isNaN(cost)) {
    return "n/a";
  }

  return `$${cost.toFixed(4)}`;
}

export function formatTokens(value: number | undefined): string {
  if (value === undefined || Number.isNaN(value)) {
    return "n/a";
  }

  return value.toLocaleString("en-US");
}

export function formatDateTime(timestamp: number | string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return String(timestamp);
  }

  return DATE_TIME_FORMATTER.format(date);
}

export function formatGroupDate(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return "Unknown Date";
  }

  return GROUP_DATE_FORMATTER.format(date);
}

export function groupSessionsByDate(
  sessions: SessionSummary[],
): Array<{
  label: string;
  sessions: SessionSummary[];
}> {
  const groups = new Map<string, SessionSummary[]>();

  for (const session of sessions) {
    const label = formatGroupDate(session.updated_at);
    const existing = groups.get(label);
    if (existing) {
      existing.push(session);
    } else {
      groups.set(label, [session]);
    }
  }

  return [...groups.entries()].map(([label, groupedSessions]) => ({
    label,
    sessions: groupedSessions,
  }));
}

export function normalizeExternalMessages(
  messages: unknown[],
): AppMessage[] {
  return messages.map((message, index) => {
    if (
      typeof message === "object" &&
      message !== null &&
      "role" in message
    ) {
      const candidate = message as StructuredMessageData;
      const meta = normalizeMessageMeta(candidate, index, candidate.content);
      const normalizedContent = normalizeMessageContent(
        candidate.content ?? candidate.text,
        candidate.text,
      );
      const text =
        typeof candidate.text === "string" && candidate.text.trim().length > 0
          ? candidate.text
          : flattenMessageContent(normalizedContent);
      const timestamp = normalizeTimestamp(
        candidate.timestamp ?? meta.timestamp,
      );
      const metaUuid =
        typeof meta.uuid === "string" && meta.uuid.length > 0
          ? meta.uuid
          : undefined;

      return {
        id:
          typeof candidate.id === "string" && candidate.id
            ? candidate.id
            : metaUuid ?? `restored-${index}-${timestamp}`,
        role: normalizeRestoredRole(candidate.role, normalizedContent),
        text,
        content: normalizedContent,
        timestamp,
        meta,
      };
    }

    return createMessage("system", stringifyUnknown(message), {
      restored: true,
      index,
    });
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

function normalizeMessageMeta(
  candidate: StructuredMessageData,
  index: number,
  content: unknown,
): Record<string, unknown> {
  const underscoredMeta = isRecord(candidate._meta)
    ? candidate._meta
    : {};
  const explicitMeta = isRecord(candidate.meta) ? candidate.meta : {};
  const reasoning =
    typeof candidate.reasoning_content === "string" &&
    candidate.reasoning_content.length > 0
      ? candidate.reasoning_content
      : typeof candidate.reasoning === "string" &&
          candidate.reasoning.length > 0
        ? candidate.reasoning
        : typeof candidate.thinking === "string" &&
            candidate.thinking.length > 0
          ? candidate.thinking
          : undefined;
  const hasReasoning = Boolean(reasoning) || hasReasoningContentBlock(content);

  return {
    ...underscoredMeta,
    ...explicitMeta,
    ...(hasReasoning ? { hasReasoning: true } : {}),
    restored: true,
    index,
  };
}

function hasReasoningContentBlock(content: unknown): boolean {
  if (!Array.isArray(content)) {
    return false;
  }

  return content.some(block => {
    if (!isRecord(block)) {
      return false;
    }
    const type = typeof block.type === "string" ? block.type : "";
    return (
      type === "thinking" ||
      type === "redacted_thinking" ||
      type === "reasoning"
    );
  });
}

export function serializeAppMessages(
  messages: AppMessage[],
): StructuredMessageData[] {
  return messages.map(message => ({
    id: message.id,
    role: message.role,
    text: message.text,
    content: message.content,
    timestamp: message.timestamp,
    ...(message.meta ? { meta: message.meta } : {}),
  }));
}

export function getMessageText(message: Pick<AppMessage, "text" | "content">): string {
  if (typeof message.text === "string" && message.text.length > 0) {
    return message.text;
  }

  return flattenMessageContent(
    Array.isArray(message.content) ? message.content : [],
  );
}

export function normalizeSessionContext(
  restored: SessionRestoredData,
): SessionContextState {
  const runtime =
    restored.runtime && typeof restored.runtime === "object"
      ? restored.runtime
      : {};
  return {
    title: restored.title,
    mode: restored.mode,
    metadata:
      restored.metadata && typeof restored.metadata === "object"
        ? restored.metadata
        : {},
    runtime: normalizeRuntime(runtime),
    agent:
      restored.agent && typeof restored.agent === "object"
        ? restored.agent
        : null,
    standaloneAgentContext:
      restored.standalone_agent_context &&
      typeof restored.standalone_agent_context === "object"
        ? restored.standalone_agent_context
        : null,
    worktree:
      restored.worktree && typeof restored.worktree === "object"
        ? restored.worktree
        : null,
    fileHistorySnapshots: Array.isArray(restored.file_history_snapshots)
      ? restored.file_history_snapshots
      : [],
    contentReplacements: Array.isArray(restored.content_replacements)
      ? restored.content_replacements
      : [],
  };
}

export function useStructuredIOListener(
  io: StructuredIO | null,
  handler: (message: IPCMessage) => void,
  opts?: { replayRecent?: boolean },
): void {
  const handlerRef = React.useRef(handler);
  const lastSeenSequenceRef = React.useRef(0);

  React.useEffect(() => {
    handlerRef.current = handler;
  }, [handler]);

  React.useEffect(() => {
    if (!io) {
      return;
    }

    return io.subscribe(
      message => {
        const sequence = getStructuredIOSequence(message);

        if (
          sequence !== null &&
          sequence <= lastSeenSequenceRef.current
        ) {
          return;
        }

        if (sequence !== null) {
          lastSeenSequenceRef.current = sequence;
        }

        handlerRef.current(message);
      },
      { replayRecent: opts?.replayRecent ?? true },
    );
  }, [io, opts?.replayRecent]);
}

function normalizeRole(role: unknown): AppMessageRole {
  switch (role) {
    case "user":
    case "assistant":
    case "tool":
    case "status":
    case "error":
    case "system":
      return role;
    default:
      return "system";
  }
}

function normalizeRestoredRole(
  role: unknown,
  content: StructuredMessageContentBlock[],
): AppMessageRole {
  const normalized = normalizeRole(role);
  const hasToolBlock = content.some(block => block.type === "tool_use");
  const hasTextBlock = content.some(
    block =>
      (block.type === "text" || block.type === "status") &&
      typeof block.text === "string" &&
      block.text.trim().length > 0,
  );

  if (hasToolBlock && !hasTextBlock) {
    return "tool";
  }

  return normalized;
}

function normalizeTimestamp(timestamp: unknown): number {
  if (typeof timestamp === "number" && Number.isFinite(timestamp)) {
    return timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp;
  }

  if (typeof timestamp === "string") {
    const asNumber = Number(timestamp);
    if (Number.isFinite(asNumber)) {
      return asNumber < 10_000_000_000 ? asNumber * 1000 : asNumber;
    }
    const parsed = Date.parse(timestamp);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }

  return Date.now();
}

function normalizeMessageContent(
  value: unknown,
  fallbackText?: unknown,
): StructuredMessageContentBlock[] {
  if (Array.isArray(value)) {
    const blocks = value
      .map(normalizeMessageBlock)
      .filter(
        (
          block,
        ): block is StructuredMessageContentBlock => block !== null,
      );
    if (blocks.length > 0) {
      return blocks;
    }
  }

  const text =
    typeof value === "string"
      ? value
      : typeof fallbackText === "string"
        ? fallbackText
        : "";

  return text.length > 0 ? [{ type: "text", text }] : [];
}

function normalizeMessageBlock(
  raw: unknown,
): StructuredMessageContentBlock | null {
  if (typeof raw === "string") {
    return {
      type: "text",
      text: raw,
    };
  }

  if (!raw || typeof raw !== "object") {
    return {
      type: "text",
      text: stringifyUnknown(raw),
    };
  }

  const block = raw as Record<string, unknown>;
  const type = typeof block.type === "string" ? block.type : "text";

  switch (type) {
    case "text":
      return {
        type,
        text:
          typeof block.text === "string"
            ? block.text
            : stringifyUnknown(block.text ?? block.content ?? ""),
      };

    case "status":
      return {
        type,
        text:
          typeof block.text === "string"
            ? block.text
            : stringifyUnknown(block.message ?? block.text ?? ""),
        level:
          block.level === "warn" || block.level === "error" || block.level === "info"
            ? block.level
            : undefined,
      };

    case "field":
      return {
        type,
        label:
          typeof block.label === "string"
            ? block.label
            : "field",
        value:
          typeof block.value === "string"
            ? block.value
            : stringifyUnknown(block.value ?? ""),
      };

    case "tool_use":
      return {
        type,
        tool_name:
          typeof block.tool_name === "string"
            ? block.tool_name
            : typeof block.toolName === "string"
              ? block.toolName
              : typeof block.name === "string"
                ? block.name
              : undefined,
        tool_use_id:
          typeof block.tool_use_id === "string"
            ? block.tool_use_id
            : typeof block.toolUseId === "string"
              ? block.toolUseId
              : typeof block.id === "string"
                ? block.id
              : undefined,
        tool_input:
          block.tool_input !== undefined
            ? block.tool_input
            : block.toolInput !== undefined
              ? block.toolInput
              : block.input,
        status:
          block.status === "pending" ||
          block.status === "running" ||
          block.status === "complete" ||
          block.status === "error"
            ? block.status
            : undefined,
        summary:
          typeof block.summary === "string"
            ? block.summary
            : undefined,
        result:
          typeof block.result === "string"
            ? block.result
            : undefined,
        error:
          typeof block.error === "string"
            ? block.error
            : undefined,
      };

    case "tool_result": {
      const contentText = textFromStructuredValue(
        block.content ?? block.result ?? block.text ?? "",
      );
      const isError = block.is_error === true || block.isError === true;
      return {
        type: "tool_use",
        tool_name:
          typeof block.tool_name === "string"
            ? block.tool_name
            : typeof block.name === "string"
              ? block.name
              : "tool",
        tool_use_id:
          typeof block.tool_use_id === "string"
            ? block.tool_use_id
            : typeof block.toolUseId === "string"
              ? block.toolUseId
              : undefined,
        tool_input: {},
        status: isError ? "error" : "complete",
        ...(isError ? { error: contentText } : { result: contentText }),
      };
    }

    case "thinking":
    case "redacted_thinking":
    case "reasoning":
      return null;

    default:
      if (typeof block.text === "string") {
        return { type: "text", text: block.text };
      }
      if (typeof block.content === "string") {
        return { type: "text", text: block.content };
      }
      if (typeof block.message === "string") {
        return { type: "status", text: block.message };
      }
      return null;
  }
}

function textFromStructuredValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }

  if (Array.isArray(value)) {
    const parts = value.flatMap(item => {
      if (typeof item === "string") {
        return [item];
      }
      if (!isRecord(item)) {
        return [];
      }
      if (typeof item.text === "string") {
        return [item.text];
      }
      if (typeof item.content === "string") {
        return [item.content];
      }
      return [];
    });
    return parts.join("\n");
  }

  return "";
}

function normalizeRuntime(
  runtime: Record<string, unknown>,
): Partial<RuntimeState> {
  const next: Partial<RuntimeState> = {};

  if (typeof runtime.model === "string") {
    next.model = runtime.model;
  }
  if (typeof runtime.session_id === "string") {
    next.sessionId = runtime.session_id;
  }
  if (typeof runtime.phase === "string") {
    next.phase = runtime.phase;
  }
  if (typeof runtime.task_id === "string") {
    next.activeTaskId = runtime.task_id;
  }
  if (typeof runtime.active_task_id === "string") {
    next.activeTaskId = runtime.active_task_id;
  }
  if (runtime.sandbox && typeof runtime.sandbox === "object") {
    next.sandbox = runtime.sandbox as RuntimeState["sandbox"];
  }

  const numericKeys: Array<
    [keyof RuntimeState, unknown]
  > = [
    ["costUsd", runtime.cost_usd],
    ["inputTokens", runtime.input_tokens],
    ["outputTokens", runtime.output_tokens],
    ["maxIterations", runtime.max_iterations],
    ["totalIterations", runtime.total_iterations],
  ];

  for (const [key, value] of numericKeys) {
    if (typeof value === "number" && Number.isFinite(value)) {
      next[key] = value as never;
    }
  }

  return next;
}
