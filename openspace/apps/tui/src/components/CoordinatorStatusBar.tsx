import React from "react";
import { Text } from "ink";
import type {
  BackgroundAgentTaskState,
  CoordinatorRuntimeState,
} from "../state/AppStateStore.js";
import { getColor } from "./design-system/theme.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { truncateToDisplayWidth } from "../utils/textWidth.js";

type Props = {
  coordinator: CoordinatorRuntimeState;
  backgroundTasks: Record<string, BackgroundAgentTaskState>;
};

const QUIET_TERMINAL_COORDINATOR_STATUSES = new Set([
  "completed",
  "complete",
  "success",
  "cancelled",
  "canceled",
  "stopped",
]);

function countRunningTeamTasks(
  tasks: Record<string, BackgroundAgentTaskState>,
  teamName: string | undefined,
): number {
  return Object.values(tasks).filter(task => {
    if (teamName && task.teamName !== teamName) {
      return false;
    }
    return ["running", "pending", "starting"].includes(task.status.toLowerCase());
  }).length;
}

export function CoordinatorStatusBar({
  coordinator,
  backgroundTasks,
}: Props): React.ReactElement | null {
  const { columns } = useTerminalSize();
  const derivedRunning = countRunningTeamTasks(
    backgroundTasks,
    coordinator.teamName,
  );
  const normalizedStatus = coordinator.status?.toLowerCase();
  const terminalStatus =
    normalizedStatus !== undefined &&
    QUIET_TERMINAL_COORDINATOR_STATUSES.has(normalizedStatus);
  const effectiveDerivedRunning = terminalStatus ? 0 : derivedRunning;
  const runningWorkers = Math.max(
    terminalStatus ? 0 : coordinator.runningWorkers,
    effectiveDerivedRunning,
  );
  const totalWorkers = Math.max(
    coordinator.totalWorkers,
    Object.values(backgroundTasks).filter(task =>
      coordinator.teamName ? task.teamName === coordinator.teamName : Boolean(task.teamName),
    ).length,
    runningWorkers,
  );

  if (!coordinator.teamName && runningWorkers === 0 && totalWorkers === 0) {
    return null;
  }

  if (runningWorkers === 0 && terminalStatus) {
    return null;
  }

  const teamLabel = coordinator.teamName ?? "default";
  const status = coordinator.status ? ` ${coordinator.status}` : "";
  const workerSummary =
    runningWorkers > 0
      ? `${runningWorkers}/${totalWorkers} workers running`
      : `${totalWorkers} workers tracked`;

  return (
    <Text color={getColor("accent")} wrap="truncate">
      {truncateToDisplayWidth(
        `Coordinator: ${teamLabel}${status} | ${workerSummary}`,
        Math.max(20, columns - 1),
      )}
    </Text>
  );
}
