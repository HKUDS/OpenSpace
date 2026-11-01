import React from "react";
import { Box, Text } from "ink";
import type {
  AppMessage,
  AppMessageRole,
} from "../state/AppStateStore.js";
import type { StructuredMessageContentBlock } from "../bridge/protocol.js";
import { getMessageText, stringifyUnknown } from "../screens/shared.js";
import {
  getColor,
  type ColorToken,
} from "./design-system/theme.js";
import { CollapsibleToolCall } from "./messages/CollapsibleToolCall.js";
import { StreamingText } from "./messages/StreamingText.js";
import { formatStructuredTextForDisplay } from "../utils/structuredDisplay.js";

export const MESSAGE_ROLE_TOKENS: Record<AppMessageRole, ColorToken> = {
  system: "systemMessage",
  user: "userMessage",
  assistant: "assistantMessage",
  tool: "toolMessage",
  status: "statusMessage",
  error: "errorMessage",
};

export const MESSAGE_ROLE_LABELS: Record<AppMessageRole, string> = {
  system: "SYS",
  user: "YOU",
  assistant: "AI",
  tool: "TOOL",
  status: "INFO",
  error: "ERR",
};

type ToolMessageShape = {
  toolName: string;
  input: string;
  result?: string;
  error?: string;
  status?: "pending" | "running" | "complete" | "error";
  progress?: string;
  collapsed?: boolean;
};

type Props = {
  message: AppMessage;
  expanded?: boolean;
};

function isToolUseBlock(
  block: StructuredMessageContentBlock,
): block is Extract<StructuredMessageContentBlock, { type: "tool_use" }> {
  return block.type === "tool_use";
}

function isFieldBlock(
  block: StructuredMessageContentBlock,
): block is Extract<StructuredMessageContentBlock, { type: "field" }> {
  return block.type === "field";
}

function getToolMessageShape(
  meta: AppMessage["meta"],
): ToolMessageShape | null {
  if (!meta || typeof meta !== "object") {
    return null;
  }

  const toolName =
    typeof meta.toolName === "string" ? meta.toolName : undefined;
  const input =
    typeof meta.input === "string" ? meta.input : undefined;

  if (!toolName || !input) {
    return null;
  }

  return {
    toolName,
    input,
    result:
      typeof meta.result === "string" ? meta.result : undefined,
    error:
      typeof meta.error === "string" ? meta.error : undefined,
    status:
      meta.status === "pending" ||
      meta.status === "running" ||
      meta.status === "complete" ||
      meta.status === "error"
        ? meta.status
        : undefined,
    progress:
      typeof meta.progress === "string" ? meta.progress : undefined,
    collapsed:
      typeof meta.collapsed === "boolean" ? meta.collapsed : true,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function summarizeNames(names: string[]): string {
  if (names.length === 0) {
    return "";
  }

  const visible = names.slice(0, 4).join(", ");
  return names.length > 4 ? `${visible}, ...` : visible;
}

function attachmentSummaryFromMeta(
  meta: AppMessage["meta"],
): string | null {
  if (!isRecord(meta)) {
    return null;
  }

  const attachmentType =
    typeof meta.attachment_type === "string"
      ? meta.attachment_type
      : undefined;
  const type = typeof meta.type === "string" ? meta.type : undefined;
  if (type !== "attachment" && !attachmentType) {
    return null;
  }

  const attachment = isRecord(meta.attachment) ? meta.attachment : {};
  const resolvedType =
    attachmentType ??
    (typeof attachment.type === "string" ? attachment.type : undefined);

  if (resolvedType === "agent_listing_delta") {
    const names = stringArray(attachment.addedTypes);
    const detail = summarizeNames(names);
    return `Context: ${names.length} agent${names.length === 1 ? "" : "s"} available${detail ? ` (${detail})` : ""}`;
  }

  if (resolvedType === "skill_listing") {
    const names = stringArray(attachment.skillNames);
    const count =
      typeof attachment.skillCount === "number"
        ? attachment.skillCount
        : names.length;
    const detail = summarizeNames(names);
    return `Context: ${count} skill${count === 1 ? "" : "s"} available${detail ? ` (${detail})` : ""}`;
  }

  if (resolvedType === "deferred_tools_delta") {
    const names = stringArray(attachment.addedNames);
    const detail = summarizeNames(names);
    return `Context: ${names.length} tool${names.length === 1 ? "" : "s"} available${detail ? ` (${detail})` : ""}`;
  }

  if (resolvedType) {
    return `Context: ${resolvedType.replaceAll("_", " ")}`;
  }

  return "Context update";
}

function activitySummaryFromMeta(
  meta: AppMessage["meta"],
  text: string,
): string | null {
  if (!isRecord(meta) || meta.activity !== true) {
    return null;
  }

  const label =
    typeof meta.activityLabel === "string" && meta.activityLabel.length > 0
      ? meta.activityLabel
      : "Activity";
  const cleaned = text.replace(/\s+/g, " ").trim();
  return cleaned ? `${label}: ${cleaned}` : label;
}

function systemReminderSummary(text: string): string | null {
  const trimmed = text.trimStart();
  if (!trimmed.startsWith("<system-reminder>")) {
    return null;
  }

  if (trimmed.includes("Available agent types")) {
    return "Context: agents available";
  }

  if (trimmed.includes("Available skills")) {
    return "Context: skills available";
  }

  if (trimmed.includes("deferred tools")) {
    return "Context: tools available";
  }

  return "Context update";
}

export function isContextAttachmentMessage(
  message: Pick<AppMessage, "meta" | "text" | "content">,
): boolean {
  return getContextAttachmentSummary(message) !== null;
}

export function getContextAttachmentSummary(
  message: Pick<AppMessage, "meta" | "text" | "content">,
): string | null {
  const text = getMessageText(message);
  return (
    activitySummaryFromMeta(message.meta, text) ??
    attachmentSummaryFromMeta(message.meta) ??
    systemReminderSummary(text) ??
    (message.meta?.hasReasoning === true && text.trim().length === 0
      ? "Thinking"
      : null)
  );
}

export function Message({
  message,
  expanded = false,
}: Props): React.ReactElement {
  const color = getColor(MESSAGE_ROLE_TOKENS[message.role]);
  const contentText = getMessageText(message);
  const attachmentSummary = getContextAttachmentSummary(message);
  const contentToolBlock = message.content.find(isToolUseBlock);
  const toolMessageShape =
    message.role === "tool"
      ? getToolMessageShape(message.meta) ??
        (contentToolBlock
          ? {
              toolName:
                typeof contentToolBlock.tool_name === "string"
                  ? contentToolBlock.tool_name
                  : "tool",
              input: stringifyUnknown(contentToolBlock.tool_input ?? {}),
              result:
                typeof contentToolBlock.result === "string"
                  ? contentToolBlock.result
                  : undefined,
              error:
                typeof contentToolBlock.error === "string"
                  ? contentToolBlock.error
                  : undefined,
              status: contentToolBlock.status,
              progress:
                typeof contentToolBlock.summary === "string"
                  ? contentToolBlock.summary
                  : undefined,
              collapsed: true,
            }
          : null)
      : null;

  if (attachmentSummary) {
    return (
      <Text color={getColor("textDim") as never}>
        {attachmentSummary}
      </Text>
    );
  }

  if (toolMessageShape) {
    return (
      <CollapsibleToolCall
        toolName={toolMessageShape.toolName}
        input={toolMessageShape.input}
        result={toolMessageShape.result}
        error={toolMessageShape.error}
        status={toolMessageShape.status}
        progress={toolMessageShape.progress}
        collapsed={expanded ? false : (toolMessageShape.collapsed ?? true)}
      />
    );
  }

  const structuredDisplayText =
    message.role === "user"
      ? null
      : formatStructuredTextForDisplay(contentText, {
          allowGenericRecord: message.role !== "assistant",
        });
  const displayText = structuredDisplayText ?? contentText;

  if (message.meta?.streaming === true) {
    return (
      <StreamingText
        text={displayText.trim().length > 0 ? displayText : "Thinking"}
        streaming={true}
        color={color}
      />
    );
  }

  const fieldBlocks = message.content.filter(isFieldBlock);

  if (fieldBlocks.length > 0 && displayText.length === 0) {
    return (
      <Box flexDirection="column">
        {fieldBlocks.map((block, index) => (
          <Text
            key={`${message.id}:field:${index}`}
            color={color as never}
          >
            {block.label}: {block.value}
          </Text>
        ))}
      </Box>
    );
  }

  return (
    <Text color={color as never}>
      {displayText.length === 0 ? " " : displayText}
    </Text>
  );
}
