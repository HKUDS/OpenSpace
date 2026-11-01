import React from "react";
import { Box, Text } from "ink";
import { getColor } from "./design-system/theme.js";

type AgentRecord = Record<string, unknown> | null | undefined;

type Props = {
  agents: AgentRecord[];
  title?: string;
  emptyLabel?: string;
  selectedAgentId?: string | null;
  maxAgents?: number;
};

type StatusTone = {
  icon: string;
  color: string;
  label: string;
};

function getString(record: AgentRecord, keys: string[]): string | undefined {
  if (!record || typeof record !== "object") {
    return undefined;
  }

  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }

  return undefined;
}

function getNumber(record: AgentRecord, keys: string[]): number | undefined {
  if (!record || typeof record !== "object") {
    return undefined;
  }

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

function statusTone(record: AgentRecord): StatusTone {
  const rawStatus = (getString(record, [
    "status",
    "state",
    "phase",
    "health",
  ]) ?? "unknown").toLowerCase();

  switch (rawStatus) {
    case "running":
    case "active":
    case "connected":
    case "ready":
      return { icon: "●", color: getColor("success"), label: rawStatus };
    case "waiting":
    case "pending":
    case "paused":
      return { icon: "◐", color: getColor("warning"), label: rawStatus };
    case "error":
    case "failed":
      return { icon: "✗", color: getColor("error"), label: rawStatus };
    case "idle":
    case "stopped":
    case "disconnected":
      return { icon: "○", color: getColor("muted"), label: rawStatus };
    default:
      return { icon: "•", color: getColor("textDim"), label: rawStatus };
  }
}

function buildSummary(record: AgentRecord): string {
  const parts: string[] = [];

  const type = getString(record, ["type", "agent_type"]);
  const mode = getString(record, ["mode"]);
  const model = getString(record, ["model"]);
  const sessionId = getString(record, ["session_id", "sessionId"]);
  const taskId = getString(record, ["task_id", "taskId"]);
  const updatedAt = formatRelativeTime(
    getNumber(record, ["updated_at", "updatedAt"]),
  );

  if (type) parts.push(type);
  if (mode) parts.push(mode);
  if (model) parts.push(model);
  if (sessionId) parts.push(`session ${truncate(sessionId, 18)}`);
  if (taskId) parts.push(`task ${truncate(taskId, 18)}`);
  if (updatedAt) parts.push(updatedAt);

  const summary = getString(record, ["summary", "preview", "description"]);
  if (summary) {
    parts.push(truncate(summary, 72));
  }

  return parts.join(" · ");
}

function agentLabel(record: AgentRecord): string {
  return (
    getString(record, [
      "name",
      "title",
      "label",
      "agent_name",
      "agent_id",
      "id",
    ]) ?? "Unnamed agent"
  );
}

function agentId(record: AgentRecord): string | undefined {
  return getString(record, ["agent_id", "agentId", "id"]);
}

function agentError(record: AgentRecord): string | undefined {
  return getString(record, ["error", "message", "last_error"]);
}

export function AgentListPanel({
  agents,
  title = "Agents",
  emptyLabel = "No agents reported yet",
  selectedAgentId,
  maxAgents = 8,
}: Props): React.ReactElement {
  const visibleAgents = agents
    .filter((agent): agent is Record<string, unknown> => Boolean(agent))
    .slice(-maxAgents);
  const hiddenCount = Math.max(0, agents.length - visibleAgents.length);

  if (visibleAgents.length === 0) {
    return (
      <Box borderStyle="round" borderColor={getColor("border")} paddingX={1}>
        <Text color={getColor("textDim")}>{emptyLabel}</Text>
      </Box>
    );
  }

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("border")}
      paddingX={1}
    >
      <Text bold color={getColor("primary")}>
        {title} ({agents.length})
      </Text>

      {hiddenCount > 0 ? (
        <Text color={getColor("textDim")}>
          Showing the latest {visibleAgents.length} agent{visibleAgents.length === 1 ? "" : "s"}.
        </Text>
      ) : null}

      {visibleAgents.map(record => {
        const id = agentId(record) ?? agentLabel(record);
        const selected = selectedAgentId !== null && selectedAgentId === id;
        const tone = statusTone(record);
        const summary = buildSummary(record);
        const error = agentError(record);

        return (
          <Box key={id} flexDirection="column" marginTop={1}>
            <Box>
              {selected ? (
                <Text color={tone.color} bold>
                  ›{" "}
                </Text>
              ) : null}
              <Text color={tone.color}>
                {tone.icon}{" "}
              </Text>
              <Text bold>{agentLabel(record)}</Text>
              <Text color={getColor("textDim")}>
                {" "}— {tone.label}
              </Text>
            </Box>

            {summary ? (
              <Text color={getColor("textDim")}>  {summary}</Text>
            ) : null}

            {error ? (
              <Text color={getColor("error")}>  Error: {truncate(error, 96)}</Text>
            ) : null}
          </Box>
        );
      })}
    </Box>
  );
}
