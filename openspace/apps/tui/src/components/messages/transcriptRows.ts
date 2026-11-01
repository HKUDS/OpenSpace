import type { StructuredMessageContentBlock } from "../../bridge/protocol.js";
import { getMessageText, stringifyUnknown } from "../../screens/shared.js";
import type {
  AppMessage,
  AppMessageRole,
} from "../../state/AppStateStore.js";
import {
  charDisplayWidth,
  stringDisplayWidth,
  stripAnsi,
  truncateToDisplayWidth,
} from "../../utils/textWidth.js";
import { formatStructuredTextForDisplay } from "../../utils/structuredDisplay.js";
import {
  getContextAttachmentSummary,
  MESSAGE_ROLE_LABELS,
  MESSAGE_ROLE_TOKENS,
} from "../Message.js";
import type { ColorToken } from "../design-system/theme.js";

export type TranscriptRow = {
  key: string;
  text: string;
  colorToken: ColorToken;
  bold?: boolean;
  dim?: boolean;
  messageId?: string;
  messageIndex?: number;
  isHeader?: boolean;
};

type BuildOptions = {
  showAll?: boolean;
  expandedMessageIds?: ReadonlySet<string>;
};

type ToolShape = {
  toolName: string;
  input?: unknown;
  result?: string;
  error?: string;
  status?: "pending" | "running" | "complete" | "error";
  progress?: string;
};

const TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
});

function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

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

function formatTimestamp(timestamp: number): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return "";
  }

  return TIME_FORMATTER.format(date);
}

export function wrapToDisplayRows(
  value: string,
  columns: number,
): string[] {
  const safeColumns = Math.max(1, columns);
  const rows: string[] = [];
  const normalized = stripAnsi(value).replace(/\r\n?/g, "\n");

  for (const logicalLine of normalized.split("\n")) {
    if (logicalLine.length === 0) {
      rows.push("");
      continue;
    }

    let current = "";
    let width = 0;

    for (const char of Array.from(logicalLine)) {
      const charWidth = charDisplayWidth(char);
      if (width + charWidth > safeColumns && current.length > 0) {
        rows.push(current);
        current = "";
        width = 0;

        if (/\s/u.test(char)) {
          continue;
        }
      }

      current += char;
      width += charWidth;
    }

    rows.push(current);
  }

  return rows.length > 0 ? rows : [""];
}

function pushRow(
  rows: TranscriptRow[],
  message: AppMessage,
  messageIndex: number,
  rowIndex: number,
  text: string,
  options?: Partial<TranscriptRow>,
): number {
  rows.push({
    key: `${message.id}:${rowIndex}`,
    text,
    colorToken: MESSAGE_ROLE_TOKENS[message.role],
    messageId: message.id,
    messageIndex,
    ...options,
  });
  return rowIndex + 1;
}

function pushWrappedRows(
  rows: TranscriptRow[],
  message: AppMessage,
  messageIndex: number,
  rowIndex: number,
  text: string,
  columns: number,
  options?: Partial<TranscriptRow> & { indent?: string },
): number {
  const indent = options?.indent ?? "";
  const wrapped = wrapToDisplayRows(
    text.length > 0 ? text : " ",
    Math.max(1, columns - stringDisplayWidth(indent)),
  );
  let nextRowIndex = rowIndex;

  for (const line of wrapped) {
    nextRowIndex = pushRow(
      rows,
      message,
      messageIndex,
      nextRowIndex,
      `${indent}${line}`,
      options,
    );
  }

  return nextRowIndex;
}

function summarizeToolInput(input: unknown): string | null {
  if (typeof input === "string") {
    return input.trim() || null;
  }

  if (!isRecord(input)) {
    return null;
  }

  for (const key of [
    "command",
    "cmd",
    "pattern",
    "query",
    "path",
    "file_path",
    "url",
  ]) {
    const value = input[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return `${key}: ${value.trim()}`;
    }
  }

  const keys = Object.keys(input).filter(key => input[key] !== undefined);
  return keys.length > 0 ? `input: ${keys.slice(0, 4).join(", ")}` : null;
}

function getMetaToolShape(message: AppMessage): ToolShape | null {
  const meta = message.meta;
  if (!isRecord(meta)) {
    return null;
  }

  const toolName =
    typeof meta.toolName === "string" && meta.toolName.length > 0
      ? meta.toolName
      : undefined;
  if (!toolName) {
    return null;
  }

  return {
    toolName,
    input: meta.input,
    result:
      typeof meta.result === "string" ? meta.result : undefined,
    error: typeof meta.error === "string" ? meta.error : undefined,
    status:
      meta.status === "pending" ||
      meta.status === "running" ||
      meta.status === "complete" ||
      meta.status === "error"
        ? meta.status
        : undefined,
    progress:
      typeof meta.progress === "string" ? meta.progress : undefined,
  };
}

function getToolShape(message: AppMessage): ToolShape | null {
  const metaShape = getMetaToolShape(message);
  if (metaShape) {
    return metaShape;
  }

  const block = message.content.find(isToolUseBlock);
  if (!block) {
    return null;
  }

  return {
    toolName:
      typeof block.tool_name === "string" && block.tool_name.length > 0
        ? block.tool_name
        : "tool",
    input: block.tool_input,
    result: typeof block.result === "string" ? block.result : undefined,
    error: typeof block.error === "string" ? block.error : undefined,
    status: block.status,
    progress:
      typeof block.summary === "string" ? block.summary : undefined,
  };
}

function statusSuffix(status: ToolShape["status"]): string {
  if (!status) {
    return "";
  }

  switch (status) {
    case "pending":
      return " pending";
    case "running":
      return " running";
    case "complete":
      return " completed";
    case "error":
      return " failed";
  }
}

function getRoleColorToken(role: AppMessageRole): ColorToken {
  return MESSAGE_ROLE_TOKENS[role];
}

function pushMessageHeader(
  rows: TranscriptRow[],
  message: AppMessage,
  messageIndex: number,
  rowIndex: number,
): number {
  const time = formatTimestamp(message.timestamp);
  const label = MESSAGE_ROLE_LABELS[message.role];
  const prefix = time.length > 0 ? `${time} ` : "";
  return pushRow(
    rows,
    message,
    messageIndex,
    rowIndex,
    `${prefix}[${label}]`,
    {
      bold: true,
      isHeader: true,
      colorToken: getRoleColorToken(message.role),
    },
  );
}

function pushToolMessageRows(
  rows: TranscriptRow[],
  message: AppMessage,
  messageIndex: number,
  rowIndex: number,
  columns: number,
  toolShape: ToolShape,
  expanded: boolean,
): number {
  let nextRowIndex = pushMessageHeader(rows, message, messageIndex, rowIndex);
  const inputSummary = summarizeToolInput(toolShape.input);
  const headline = `Tool: ${toolShape.toolName}${statusSuffix(toolShape.status)}${
    inputSummary ? ` - ${inputSummary}` : ""
  }`;
  nextRowIndex = pushWrappedRows(
    rows,
    message,
    messageIndex,
    nextRowIndex,
    headline,
    columns,
    { indent: "  ", colorToken: MESSAGE_ROLE_TOKENS.tool },
  );

  if (toolShape.progress) {
    nextRowIndex = pushWrappedRows(
      rows,
      message,
      messageIndex,
      nextRowIndex,
      toolShape.progress,
      columns,
      { indent: "  ", colorToken: "textDim", dim: true },
    );
  }

  if (expanded && toolShape.input !== undefined) {
    nextRowIndex = pushWrappedRows(
      rows,
      message,
      messageIndex,
      nextRowIndex,
      `Input: ${stringifyUnknown(toolShape.input)}`,
      columns,
      { indent: "  ", colorToken: "textDim", dim: true },
    );
  }

  const detail = toolShape.error ?? toolShape.result;
  if (detail && (expanded || toolShape.error)) {
    nextRowIndex = pushWrappedRows(
      rows,
      message,
      messageIndex,
      nextRowIndex,
      detail,
      columns,
      {
        indent: "  ",
        colorToken: toolShape.error ? "errorMessage" : "textDim",
        dim: !toolShape.error,
      },
    );
  }

  if (!expanded && detail && !toolShape.error) {
    nextRowIndex = pushWrappedRows(
      rows,
      message,
      messageIndex,
      nextRowIndex,
      truncateToDisplayWidth(detail.replace(/\s+/g, " "), columns - 4),
      columns,
      { indent: "  ", colorToken: "textDim", dim: true },
    );
  }

  return nextRowIndex;
}

function pushFieldRows(
  rows: TranscriptRow[],
  message: AppMessage,
  messageIndex: number,
  rowIndex: number,
  columns: number,
): number {
  let nextRowIndex = rowIndex;
  for (const block of message.content.filter(isFieldBlock)) {
    nextRowIndex = pushWrappedRows(
      rows,
      message,
      messageIndex,
      nextRowIndex,
      `${block.label}: ${block.value}`,
      columns,
      { indent: "  " },
    );
  }

  return nextRowIndex;
}

export function buildTranscriptRows(
  messages: AppMessage[],
  columns: number,
  options: BuildOptions = {},
): TranscriptRow[] {
  const safeColumns = Math.max(24, columns);
  const rows: TranscriptRow[] = [];

  messages.forEach((message, messageIndex) => {
    let rowIndex = 0;
    const contextSummary = getContextAttachmentSummary(message);
    const expanded =
      options.showAll === true ||
      options.expandedMessageIds?.has(message.id) === true;

    if (contextSummary) {
      rowIndex = pushWrappedRows(
        rows,
        message,
        messageIndex,
        rowIndex,
        contextSummary,
        safeColumns,
        { colorToken: "textDim", dim: true },
      );
      rowIndex = pushRow(rows, message, messageIndex, rowIndex, "", {
        colorToken: "textDim",
        dim: true,
      });
      return;
    }

    const toolShape =
      message.role === "tool" || message.content.some(isToolUseBlock)
        ? getToolShape(message)
        : null;
    if (toolShape) {
      rowIndex = pushToolMessageRows(
        rows,
        message,
        messageIndex,
        rowIndex,
        safeColumns,
        toolShape,
        expanded,
      );
      rowIndex = pushRow(rows, message, messageIndex, rowIndex, "", {
        colorToken: "textDim",
        dim: true,
      });
      return;
    }

    rowIndex = pushMessageHeader(rows, message, messageIndex, rowIndex);

    const rawText = getMessageText(message);
    const text =
      message.role === "user"
        ? rawText
        : formatStructuredTextForDisplay(rawText, {
            allowGenericRecord: message.role !== "assistant",
          }) ?? rawText;
    if (text.trim().length > 0) {
      rowIndex = pushWrappedRows(
        rows,
        message,
        messageIndex,
        rowIndex,
        text,
        safeColumns,
        { indent: "  " },
      );
    } else if (message.content.some(isFieldBlock)) {
      rowIndex = pushFieldRows(
        rows,
        message,
        messageIndex,
        rowIndex,
        safeColumns,
      );
    } else {
      rowIndex = pushRow(rows, message, messageIndex, rowIndex, "  ", {
        colorToken: "textDim",
        dim: true,
      });
    }

    rowIndex = pushRow(rows, message, messageIndex, rowIndex, "", {
      colorToken: "textDim",
      dim: true,
    });
  });

  return rows;
}

export function estimateTranscriptRows(
  messages: AppMessage[],
  columns: number,
  options?: BuildOptions,
): number {
  return Math.max(1, buildTranscriptRows(messages, columns, options).length);
}
