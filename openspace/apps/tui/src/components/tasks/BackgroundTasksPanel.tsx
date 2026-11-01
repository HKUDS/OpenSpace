import React from "react";
import { Box, Text } from "ink";
import type { BackgroundAgentTaskState } from "../../state/AppStateStore.js";
import {
  getBackgroundTaskLabel,
  getBackgroundTaskTail,
  isTerminalBackgroundAgentStatus,
} from "../../utils/backgroundTasks.js";
import { getColor } from "../design-system/theme.js";

const TERMINAL_FADE_MS = 5_000;

type Props = {
  tasks: Record<string, BackgroundAgentTaskState>;
  title?: string;
  maxTasks?: number;
};

export { isTerminalBackgroundAgentStatus };

function formatDuration(ms: number): string {
  if (ms < 1_000) return `${Math.max(0, Math.round(ms))}ms`;
  if (ms < 60_000) return `${Math.floor(ms / 1_000)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.floor((ms % 60_000) / 1_000)}s`;
}

function taskTone(status: string): { label: string; color: string; mark: string } {
  const normalized = status.toLowerCase();
  if (normalized === "running") {
    return { label: "running", color: getColor("spinner"), mark: "*" };
  }
  if (normalized === "pending" || normalized === "starting") {
    return { label: normalized, color: getColor("warning"), mark: "." };
  }
  if (normalized === "completed" || normalized === "success") {
    return { label: normalized, color: getColor("success"), mark: "ok" };
  }
  if (normalized === "failed" || normalized === "error") {
    return { label: normalized, color: getColor("error"), mark: "!" };
  }
  if (normalized === "killed" || normalized === "stopped") {
    return { label: normalized, color: getColor("muted"), mark: "x" };
  }
  return { label: normalized || "unknown", color: getColor("textDim"), mark: "-" };
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 3))}...`;
}

export function BackgroundTasksPanel({
  tasks,
  title = "Background Tasks",
  maxTasks = 8,
}: Props): React.ReactElement {
  const [now, setNow] = React.useState(() => Date.now());

  React.useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);

  const visibleTasks = Object.values(tasks)
    .filter(task => {
      if (
        task.background === false &&
        !isTerminalBackgroundAgentStatus(task.status)
      ) {
        return false;
      }
      if (!isTerminalBackgroundAgentStatus(task.status)) {
        return true;
      }
      const completedAt = task.completedAt ?? task.updatedAt;
      return now - completedAt <= TERMINAL_FADE_MS;
    })
    .sort((a, b) => {
      const aDone = isTerminalBackgroundAgentStatus(a.status);
      const bDone = isTerminalBackgroundAgentStatus(b.status);
      if (aDone !== bDone) return aDone ? 1 : -1;
      return b.updatedAt - a.updatedAt;
    })
    .slice(0, maxTasks);

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("border")}
      paddingX={1}
    >
      <Text bold color={getColor("primary")}>
        {title}
        {visibleTasks.length > 0 ? ` (${visibleTasks.length})` : ""}
      </Text>

      {visibleTasks.length === 0 ? (
        <Text color={getColor("textDim")}>No background agent tasks</Text>
      ) : (
        visibleTasks.map(task => {
          const tone = taskTone(task.status);
          const elapsedMs =
            (task.completedAt ?? now) - Math.min(task.startedAt, task.updatedAt);
          const operation =
            task.currentOperation ?? task.description ?? task.outputFile ?? "";
          const outputTail = getBackgroundTaskTail(task);
          const team = task.teamName ? ` @${task.teamName}` : "";

          return (
            <Box key={task.id} flexDirection="column" marginTop={1}>
              <Box>
                <Text color={tone.color as never}>{tone.mark} </Text>
                <Text bold>{truncate(getBackgroundTaskLabel(task), 28)}</Text>
                <Text color={getColor("textDim")}>{team} </Text>
                <Text color={tone.color as never}>[{tone.label}] </Text>
                <Text color={getColor("textDim")}>{formatDuration(elapsedMs)}</Text>
              </Box>
              {operation ? (
                <Text color={getColor("textDim")}>
                  {"  "}
                  {truncate(operation.replace(/\s+/g, " "), 96)}
                </Text>
              ) : null}
              {outputTail ? (
                <Text color={getColor("textDim")}>
                  {"  "}
                  {truncate(outputTail.replace(/\s+/g, " ").trim(), 96)}
                </Text>
              ) : null}
            </Box>
          );
        })
      )}
    </Box>
  );
}
