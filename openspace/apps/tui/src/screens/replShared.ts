import type { MCPClientState, TaskState } from "../state/AppStateStore.js";
import { stringifyUnknown, truncate } from "./shared.js";

export function summarizeToolInput(
  input: Record<string, unknown> | undefined,
): string {
  if (!input || Object.keys(input).length === 0) {
    return "No input";
  }

  return truncate(stringifyUnknown(input), 140);
}

export function summarizeToolResult(result: unknown): string {
  const rendered = stringifyUnknown(result);
  if (typeof rendered !== "string" || rendered.trim().length === 0) {
    return "No result";
  }
  return truncate(rendered.replace(/\s+/g, " ").trim(), 160);
}

export function upsertTask(
  tasks: Record<string, TaskState>,
  id: string,
  patch: Partial<TaskState>,
): Record<string, TaskState> {
  const current = tasks[id] ?? {
    id,
    status: "idle" as const,
    updatedAt: Date.now(),
  };

  return {
    ...tasks,
    [id]: {
      ...current,
      ...patch,
      id,
      updatedAt: Date.now(),
    },
  };
}

export function upsertMcpClient(
  clients: MCPClientState[],
  update: MCPClientState,
): MCPClientState[] {
  const index = clients.findIndex(
    client => client.serverName === update.serverName,
  );

  if (index === -1) {
    return [...clients, update];
  }

  const next = [...clients];
  next[index] = {
    ...next[index]!,
    ...update,
  };
  return next;
}
