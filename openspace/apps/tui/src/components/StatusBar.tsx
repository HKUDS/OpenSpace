import React from "react";
import { Box, Text } from "ink";
import { getColor } from "./design-system/theme.js";
import type { RuntimeState, TaskState } from "../state/AppStateStore.js";

type StatusBarProps = {
  runtime: RuntimeState;
  isQuerying: boolean;
  tasks?: Record<string, TaskState>;
};

function formatUsd(cost: number | undefined): string {
  if (cost === undefined || Number.isNaN(cost)) return "—";
  return `$${cost.toFixed(4)}`;
}

function formatTokens(value: number | undefined): string {
  if (value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US");
}

function formatActiveTaskPill(tasks: Record<string, TaskState>): string | null {
  const running = Object.values(tasks).filter(t => t.status === "running");
  if (running.length === 0) return null;
  if (running.length === 1) return running[0]!.title ?? running[0]!.id;
  return `${running.length} tasks`;
}

export function StatusBar({
  runtime,
  isQuerying,
  tasks,
}: StatusBarProps): React.ReactElement {
  const taskPill = tasks ? formatActiveTaskPill(tasks) : null;
  const phaseColor = isQuerying ? getColor("warning") : getColor("muted");

  return (
    <Box flexDirection="column">
      <Box>
        <Text color={getColor("primary")} bold>
          Model:{" "}
        </Text>
        <Text>{runtime.model ?? "n/a"}</Text>
        <Text color={getColor("muted")}> │ </Text>
        <Text color={getColor("primary")} bold>
          Session:{" "}
        </Text>
        <Text>{runtime.sessionId ? runtime.sessionId.slice(0, 12) : "n/a"}</Text>
        <Text color={getColor("muted")}> │ </Text>
        <Text color={getColor("primary")} bold>
          Cost:{" "}
        </Text>
        <Text>{formatUsd(runtime.costUsd)}</Text>
      </Box>
      <Box>
        <Text color={getColor("primary")} bold>
          Tokens:{" "}
        </Text>
        <Text>
          {formatTokens(runtime.inputTokens)} in / {formatTokens(runtime.outputTokens)} out
        </Text>
        <Text color={getColor("muted")}> │ </Text>
        <Text color={phaseColor} bold>
          {isQuerying ? "● " : "○ "}
        </Text>
        <Text color={phaseColor}>{runtime.phase ?? "idle"}</Text>
        {runtime.totalIterations !== undefined && runtime.maxIterations !== undefined ? (
          <Text color={getColor("muted")}>
            {" "}({runtime.totalIterations}/{runtime.maxIterations})
          </Text>
        ) : null}
        {taskPill ? (
          <>
            <Text color={getColor("muted")}> │ </Text>
            <Text color={getColor("accent")}>{taskPill}</Text>
          </>
        ) : null}
      </Box>
    </Box>
  );
}
