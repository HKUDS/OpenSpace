import React from "react";
import { Box, Text } from "ink";
import { getColor } from "./design-system/theme.js";

type EventRecord = Record<string, unknown> | null | undefined;

type Props = {
  events: EventRecord[];
  title?: string;
  emptyLabel?: string;
  maxEvents?: number;
  selectedEventIndex?: number | null;
  actionHints?: string[];
};

function getString(record: EventRecord, keys: string[]): string | undefined {
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

function getNumber(record: EventRecord, keys: string[]): number | undefined {
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

function formatTime(timestamp: number | undefined): string {
  if (timestamp === undefined) {
    return "--:--:--";
  }

  try {
    return new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(timestamp));
  } catch {
    return "--:--:--";
  }
}

function agentLabel(record: EventRecord): string {
  return (
    getString(record, ["agent_id", "agentId", "agent", "id"]) ?? "agent"
  );
}

function eventLabel(record: EventRecord): string {
  return getString(record, ["event", "type", "name"]) ?? "event";
}

function eventTone(label: string): string {
  const normalized = label.toLowerCase();
  if (normalized.includes("error") || normalized.includes("fail")) {
    return getColor("error");
  }
  if (normalized.includes("complete") || normalized.includes("ready")) {
    return getColor("success");
  }
  if (normalized.includes("start") || normalized.includes("update")) {
    return getColor("primary");
  }
  if (normalized.includes("transcript")) {
    return getColor("accent");
  }
  return getColor("text");
}

function summarizePayload(record: EventRecord): string | undefined {
  if (!record || typeof record !== "object") {
    return undefined;
  }

  const payload = record["payload"];
  if (payload === undefined || payload === null) {
    return undefined;
  }

  if (typeof payload === "string") {
    return truncate(payload, 96);
  }

  if (Array.isArray(payload)) {
    return truncate(JSON.stringify(payload), 96);
  }

  if (typeof payload === "object") {
    const entries = Object.entries(payload as Record<string, unknown>);
    const summary = entries
      .slice(0, 3)
      .map(([key, value]) => {
        if (typeof value === "string") {
          return `${key}=${truncate(value, 24)}`;
        }
        if (typeof value === "number" || typeof value === "boolean") {
          return `${key}=${String(value)}`;
        }
        return key;
      })
      .join(", ");

    return summary.length > 0 ? summary : truncate(JSON.stringify(payload), 96);
  }

  return String(payload);
}

export function AgentEventFeed({
  events,
  title = "Recent Agent Events",
  emptyLabel = "No agent events yet",
  maxEvents = 10,
  selectedEventIndex = null,
  actionHints = [],
}: Props): React.ReactElement {
  const visibleEvents = events
    .filter((event): event is Record<string, unknown> => Boolean(event))
    .slice(-maxEvents);
  const offset = Math.max(0, events.length - visibleEvents.length);

  if (visibleEvents.length === 0) {
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
        {title} ({events.length})
      </Text>

      {actionHints.length > 0 ? (
        <Text color={getColor("textDim")}>
          {actionHints.join(" | ")}
        </Text>
      ) : null}

      {visibleEvents.map((record, index) => {
        const absoluteIndex = offset + index;
        const selected = selectedEventIndex === absoluteIndex;
        const time = formatTime(getNumber(record, ["timestamp", "updated_at"]));
        const agent = agentLabel(record);
        const label = eventLabel(record);
        const payload = summarizePayload(record);

        return (
          <Box key={`${agent}:${label}:${index}`} flexDirection="column" marginTop={1}>
            <Box>
              <Text color={selected ? getColor("primary") : getColor("textDim")}>
                {selected ? "›" : " "}
              </Text>
              <Text color={getColor("textDim")}> </Text>
              <Text color={getColor("textDim")}>{time}</Text>
              <Text color={getColor("textDim")}> </Text>
              <Text bold color={getColor("secondary")}>
                {agent}
              </Text>
              <Text color={getColor("textDim")}> · </Text>
              <Text color={selected ? getColor("primary") : eventTone(label)}>
                {truncate(label, 32)}
              </Text>
            </Box>

            {payload ? (
              <Text color={selected ? getColor("text") : getColor("textDim")}>
                {payload}
              </Text>
            ) : null}
          </Box>
        );
      })}
    </Box>
  );
}
