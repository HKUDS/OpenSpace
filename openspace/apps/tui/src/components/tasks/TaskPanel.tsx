import React from "react";
import { Box, Text } from "ink";
import type { TaskState } from "../../state/AppStateStore.js";
import { getColor } from "../design-system/theme.js";

type TaskPanelProps = {
  tasks: Record<string, TaskState>;
  expanded?: boolean;
};

function taskStatusIcon(status: TaskState["status"]): string {
  switch (status) {
    case "running":
      return "●";
    case "success":
      return "✓";
    case "error":
      return "✗";
    case "cancelled":
      return "⊘";
    case "incomplete":
      return "◐";
    case "idle":
    default:
      return "○";
  }
}

function taskStatusColor(status: TaskState["status"]): string {
  switch (status) {
    case "running":
      return getColor("spinner");
    case "success":
      return getColor("success");
    case "error":
      return getColor("error");
    case "cancelled":
      return getColor("textDim");
    case "incomplete":
      return getColor("warning");
    case "idle":
    default:
      return getColor("muted");
  }
}

function formatDuration(ms: number | undefined): string {
  if (ms === undefined) return "";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

function pillLabel(tasks: TaskState[]): string {
  const running = tasks.filter(t => t.status === "running");
  if (running.length === 0) return "";
  if (running.length === 1) {
    const task = running[0]!;
    const label = task.title ?? task.id;
    return task.iterations !== undefined
      ? `${label} (${task.iterations}/${task.maxIterations ?? "?"})`
      : label;
  }
  return `${running.length} tasks`;
}

export function TaskPill({
  tasks,
}: {
  tasks: Record<string, TaskState>;
}): React.ReactElement | null {
  const entries = Object.values(tasks);
  const label = pillLabel(entries);
  if (!label) return null;

  return (
    <Text color={getColor("accent")}>
      [{label}]
    </Text>
  );
}

export function TaskPanel({
  tasks,
  expanded,
}: TaskPanelProps): React.ReactElement {
  const entries = Object.values(tasks).sort(
    (a, b) => b.updatedAt - a.updatedAt,
  );

  if (entries.length === 0) {
    return (
      <Box borderStyle="round" borderColor={getColor("border")} paddingX={1}>
        <Text color={getColor("textDim")}>No tasks</Text>
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
        Tasks ({entries.length})
      </Text>

      {entries.map(task => (
        <Box key={task.id} flexDirection="column" marginTop={1}>
          <Box>
            <Text color={taskStatusColor(task.status)}>
              {taskStatusIcon(task.status)}{" "}
            </Text>
            <Text bold>{task.title ?? task.id}</Text>
            <Text color={getColor("textDim")}>
              {" "}— {task.status}
              {task.executionTime !== undefined
                ? ` (${formatDuration(task.executionTime)})`
                : ""}
            </Text>
          </Box>

          {expanded && task.phase ? (
            <Text color={getColor("textDim")}>  Phase: {task.phase}</Text>
          ) : null}

          {expanded && task.iterations !== undefined ? (
            <Text color={getColor("textDim")}>
              {"  "}Iterations: {task.iterations}
              {task.maxIterations !== undefined ? `/${task.maxIterations}` : ""}
              {task.toolCalls !== undefined ? ` │ Tool calls: ${task.toolCalls}` : ""}
            </Text>
          ) : null}

          {expanded && task.error ? (
            <Text color={getColor("error")}>  Error: {task.error}</Text>
          ) : null}
        </Box>
      ))}
    </Box>
  );
}
