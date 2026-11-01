import type { BackgroundAgentTaskState } from "../state/AppStateStore.js";

const TERMINAL_STATUSES = new Set([
  "cancelled",
  "canceled",
  "completed",
  "failed",
  "killed",
  "stopped",
  "success",
  "error",
]);

function stringFromMetadata(
  task: BackgroundAgentTaskState,
  key: string,
): string | undefined {
  const value = task.metadata?.[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export function isTerminalBackgroundAgentStatus(status: string | undefined): boolean {
  return TERMINAL_STATUSES.has(String(status ?? "").toLowerCase());
}

export function hasForegroundBackgroundTasks(
  tasks: Record<string, BackgroundAgentTaskState>,
): boolean {
  return Object.values(tasks).some(task => {
    const status = String(task.status ?? "").toLowerCase();
    return (
      task.taskType === "local_bash" &&
      (status === "running" || status === "pending") &&
      task.background === false
    );
  });
}

export function getBackgroundTaskLabel(task: BackgroundAgentTaskState): string {
  if (task.taskType === "local_bash") {
    return (
      task.description ??
      stringFromMetadata(task, "command") ??
      (stringFromMetadata(task, "kind") === "monitor" ? "monitor" : "shell")
    );
  }
  return task.name ?? task.agentType ?? task.agentId ?? task.id;
}

export function getBackgroundTaskTail(task: BackgroundAgentTaskState): string | undefined {
  return (
    task.outputTail ??
    stringFromMetadata(task, "output_tail") ??
    stringFromMetadata(task, "outputTail")
  );
}
