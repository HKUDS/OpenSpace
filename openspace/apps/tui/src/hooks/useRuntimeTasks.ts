import React from "react";
import type {
  TaskProgressData,
  TaskStartData,
  StatusUpdateData,
  TaskCompleteData,
  TaskErrorData,
} from "../bridge/protocol.js";
import { useSetAppState } from "../state/AppState.js";
import type { TaskState } from "../state/AppStateStore.js";

function upsertTask(
  tasks: Record<string, TaskState>,
  id: string,
  patch: Partial<TaskState>,
): Record<string, TaskState> {
  const current = tasks[id] ?? {
    id,
    status: "idle",
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

function isTerminalRuntimePhase(phase: string | undefined): boolean {
  return (
    phase === "query_complete" ||
    phase === "query_cancelled" ||
    phase === "query_error" ||
    phase === "completed" ||
    phase === "complete" ||
    phase === "success" ||
    phase === "succeeded" ||
    phase === "error" ||
    phase === "cancelled"
  );
}

function taskStatusForRuntimePhase(
  phase: string | undefined,
  currentStatus: TaskState["status"] | undefined,
): TaskState["status"] {
  switch (phase) {
    case "execution_start":
      return "running";
    case "query_complete":
    case "completed":
    case "complete":
    case "success":
    case "succeeded":
      return "success";
    case "query_cancelled":
    case "cancelled":
      return "cancelled";
    case "query_error":
    case "error":
      return "error";
    default:
      return currentStatus ?? "running";
  }
}

function displayPhaseForRuntimePhase(
  phase: string | undefined,
  currentPhase: string | undefined,
): string | undefined {
  switch (phase) {
    case "query_complete":
    case "query_cancelled":
    case "completed":
    case "complete":
    case "success":
    case "succeeded":
    case "cancelled":
      return "idle";
    case "query_error":
      return "error";
    default:
      return phase ?? currentPhase;
  }
}

function keepIdleForLateRuntimePhase(
  phase: string | undefined,
  currentPhase: string | undefined,
  isQuerying: boolean,
): boolean {
  if (isQuerying || currentPhase !== "idle" || !phase) {
    return false;
  }
  if (isTerminalRuntimePhase(phase)) {
    return false;
  }

  const normalized = phase.toLowerCase();
  return ![
    "execution_start",
    "running",
    "running execution",
    "compact_start",
  ].includes(normalized);
}

function isStaleSessionEvent(
  currentSessionId: string | undefined,
  eventSessionId: string | undefined,
): boolean {
  return Boolean(
    currentSessionId &&
      eventSessionId &&
      currentSessionId !== eventSessionId,
  );
}

export function useRuntimeTasks(): {
  applyStatusUpdate: (data: StatusUpdateData) => void;
  markTaskStart: (data: TaskStartData) => void;
  markTaskProgress: (data: TaskProgressData) => void;
  markTaskComplete: (data: TaskCompleteData) => void;
  markTaskError: (data: TaskErrorData) => void;
} {
  const setAppState = useSetAppState();

  const applyStatusUpdate = React.useCallback(
    (data: StatusUpdateData) => {
      setAppState(prev => {
        if (isStaleSessionEvent(prev.runtime.sessionId, data.session_id)) {
          return prev;
        }

        const taskId = data.task_id ?? prev.runtime.activeTaskId;
        const nextTasks =
          taskId !== undefined
            ? upsertTask(prev.tasks, taskId, {
                status: taskStatusForRuntimePhase(
                  data.phase,
                  prev.tasks[taskId]?.status,
                ),
                phase: data.phase ?? prev.tasks[taskId]?.phase,
                maxIterations:
                  data.max_iterations ?? prev.tasks[taskId]?.maxIterations,
                iterations:
                  data.total_iterations ?? prev.tasks[taskId]?.iterations,
              })
            : prev.tasks;
        const nextPhase = keepIdleForLateRuntimePhase(
          data.phase,
          prev.runtime.phase,
          prev.isQuerying,
        )
          ? prev.runtime.phase
          : displayPhaseForRuntimePhase(data.phase, prev.runtime.phase);

        return {
          ...prev,
          isQuerying: isTerminalRuntimePhase(data.phase)
            ? false
            : prev.isQuerying,
          tasks: nextTasks,
          mainLoopModel: data.model ?? prev.mainLoopModel,
          runtime: {
            ...prev.runtime,
            model: data.model ?? prev.runtime.model,
            sessionId: data.session_id ?? prev.runtime.sessionId,
            costUsd: data.cost_usd ?? prev.runtime.costUsd,
            inputTokens: data.input_tokens ?? prev.runtime.inputTokens,
            outputTokens: data.output_tokens ?? prev.runtime.outputTokens,
            phase: nextPhase,
            activeTaskId: taskId ?? prev.runtime.activeTaskId,
            maxIterations:
              data.max_iterations ?? prev.runtime.maxIterations,
            totalIterations:
              data.total_iterations ?? prev.runtime.totalIterations,
            sandbox: data.sandbox ?? prev.runtime.sandbox,
          },
        };
      });
    },
    [setAppState],
  );

  const markTaskComplete = React.useCallback(
    (data: TaskCompleteData) => {
      const taskId = data.task_id ?? "active";
      setAppState(prev => ({
        ...prev,
        isQuerying: false,
        tasks: upsertTask(prev.tasks, taskId, {
          status: data.status === "incomplete" ? "incomplete" : "success",
          iterations: data.iterations,
          toolCalls: data.tool_calls,
          executionTime: data.execution_time,
          title: data.result,
        }),
      }));
    },
    [setAppState],
  );

  const markTaskError = React.useCallback(
    (data: TaskErrorData) => {
      const taskId = data.task_id ?? "active";
      setAppState(prev => ({
        ...prev,
        isQuerying: false,
        tasks: upsertTask(prev.tasks, taskId, {
          status: "error",
          error: data.error,
          executionTime: data.execution_time,
        }),
      }));
    },
    [setAppState],
  );

  const markTaskStart = React.useCallback(
    (data: TaskStartData) => {
      setAppState(prev => ({
        ...prev,
        tasks: upsertTask(prev.tasks, data.task_id, {
          status: "running",
          title: data.title,
          phase: data.status,
        }),
        runtime: {
          ...prev.runtime,
          activeTaskId: data.task_id,
          phase: data.status ?? prev.runtime.phase,
        },
      }));
    },
    [setAppState],
  );

  const markTaskProgress = React.useCallback(
    (data: TaskProgressData) => {
      const taskId = data.task_id;
      setAppState(prev => ({
        ...prev,
        tasks: upsertTask(prev.tasks, taskId, {
          status: "running",
          title: data.title ?? prev.tasks[taskId]?.title,
          phase: data.progress ?? data.status ?? prev.tasks[taskId]?.phase,
        }),
        runtime: {
          ...prev.runtime,
          activeTaskId: taskId,
          phase: data.progress ?? data.status ?? prev.runtime.phase,
        },
      }));
    },
    [setAppState],
  );

  return {
    applyStatusUpdate,
    markTaskStart,
    markTaskProgress,
    markTaskComplete,
    markTaskError,
  };
}
