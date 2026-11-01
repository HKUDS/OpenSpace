import React from "react";
import { Box, Text } from "ink";
import { getColor } from "./design-system/theme.js";

type BackgroundSessionLike = Record<string, unknown> | null | undefined;

type BackgroundSessionFields = {
  sessionId?: string;
  session_id?: string;
  taskId?: string;
  task_id?: string;
  status?: string;
  title?: string;
  updatedAt?: number;
  updated_at?: number | string;
};

type Props = {
  session: BackgroundSessionLike;
  title?: string;
  emptyLabel?: string;
};

function getString(
  record: BackgroundSessionLike,
  keys: string[],
): string | undefined {
  if (!record || typeof record !== "object") {
    return undefined;
  }

  for (const key of keys) {
    const value = (record as BackgroundSessionFields)[key as keyof BackgroundSessionFields];
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }

  return undefined;
}

function getNumber(
  record: BackgroundSessionLike,
  keys: string[],
): number | undefined {
  if (!record || typeof record !== "object") {
    return undefined;
  }

  for (const key of keys) {
    const value = (record as BackgroundSessionFields)[key as keyof BackgroundSessionFields];
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

function truncate(text: string, max: number): string {
  if (text.length <= max) {
    return text;
  }

  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

function formatRelativeTime(timestamp: number | undefined): string {
  if (timestamp === undefined) {
    return "";
  }

  const diffSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (diffSeconds < 60) return `${diffSeconds}s ago`;
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)}m ago`;
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h ago`;
  return `${Math.floor(diffSeconds / 86400)}d ago`;
}

function statusTone(status: string | undefined): {
  icon: string;
  color: string;
  label: string;
} {
  const normalized = (status ?? "unknown").toLowerCase();

  switch (normalized) {
    case "running":
    case "active":
    case "open":
    case "focused":
      return { icon: "●", color: getColor("success"), label: normalized };
    case "queued":
    case "waiting":
    case "pending":
      return { icon: "◐", color: getColor("warning"), label: normalized };
    case "stopped":
    case "idle":
    case "closed":
      return { icon: "○", color: getColor("muted"), label: normalized };
    case "error":
    case "failed":
      return { icon: "✗", color: getColor("error"), label: normalized };
    default:
      return { icon: "•", color: getColor("textDim"), label: normalized };
  }
}

export function BackgroundSessionSummary({
  session,
  title = "Background Session",
  emptyLabel = "No background session active",
}: Props): React.ReactElement {
  if (!session) {
    return (
      <Box borderStyle="round" borderColor={getColor("border")} paddingX={1}>
        <Text color={getColor("textDim")}>{emptyLabel}</Text>
      </Box>
    );
  }

  const sessionId = getString(session, ["sessionId", "session_id"]);
  const taskId = getString(session, ["taskId", "task_id"]);
  const status = statusTone(getString(session, ["status"]));
  const sessionTitle = getString(session, ["title"]);
  const updatedAt = formatRelativeTime(
    getNumber(session, ["updatedAt", "updated_at"]),
  );

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("border")}
      paddingX={1}
    >
      <Text bold color={getColor("primary")}>
        {title}
      </Text>

      <Box marginTop={1}>
        <Text color={status.color}>
          {status.icon}{" "}
        </Text>
        <Text bold>{sessionTitle ?? sessionId ?? "Background work"}</Text>
        <Text color={getColor("textDim")}>
          {" "}— {status.label}
        </Text>
      </Box>

      {sessionId ? (
        <Text color={getColor("textDim")}>Session: {truncate(sessionId, 40)}</Text>
      ) : null}

      {taskId ? (
        <Text color={getColor("textDim")}>Task: {truncate(taskId, 40)}</Text>
      ) : null}

      {updatedAt ? (
        <Text color={getColor("textDim")}>Updated {updatedAt}</Text>
      ) : null}
    </Box>
  );
}
