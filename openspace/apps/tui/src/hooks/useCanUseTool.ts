import React from "react";
import type { StructuredIO } from "../bridge/structuredIO.js";
import type {
  PermissionRequestData,
  ToolPermissionCancelData,
} from "../bridge/protocol.js";
import {
  type PermissionDecision,
  type PermissionResolution,
  usePermissionQueue,
} from "./toolPermission/PermissionContext.js";
import { handleInteractivePermission } from "./toolPermission/handlers/interactiveHandler.js";
import { logPermissionDecision } from "./toolPermission/permissionLogging.js";

export type UseCanUseToolResult = {
  permissionQueue: PermissionRequestData[];
  activePermission: PermissionRequestData | null;
  enqueuePermissionRequest: (request: PermissionRequestData) => void;
  resolvePermissionRequest: (
    resolution: PermissionResolution,
  ) => PermissionRequestData | null;
  cancelPermissionRequest: (
    cancel: ToolPermissionCancelData,
  ) => PermissionRequestData | null;
  clearPermissionQueue: () => void;
};

function getDecisionForLog(
  resolution: PermissionResolution,
): PermissionDecision {
  if (typeof resolution === "string") {
    return resolution;
  }

  if ("decision" in resolution) {
    return resolution.decision;
  }

  switch (resolution.option_id) {
    case "allow_always":
      return "allow_always";
    case "allow_once":
    case "provide_input":
      return "allow";
    case "deny":
    default:
      return "deny";
  }
}

export function useCanUseTool(io: StructuredIO | null): UseCanUseToolResult {
  const {
    permissionQueue,
    activePermission,
    queueOps,
    resolveCurrent,
    cancelByToolUseId,
  } = usePermissionQueue(io);

  const enqueuePermissionRequest = React.useCallback(
    (request: PermissionRequestData) => {
      handleInteractivePermission({
        io,
        request,
        queueOps,
      });
    },
    [io, queueOps],
  );

  const resolvePermissionRequest = React.useCallback(
    (resolution: PermissionResolution) => {
      const current = resolveCurrent(resolution);
      if (!current) {
        return null;
      }

      logPermissionDecision(current, {
        decision: getDecisionForLog(resolution),
        source: "user",
      });
      return current;
    },
    [resolveCurrent],
  );

  const cancelPermissionRequest = React.useCallback(
    (cancel: ToolPermissionCancelData) =>
      cancelByToolUseId(cancel.tool_use_id),
    [cancelByToolUseId],
  );

  const clearPermissionQueue = React.useCallback(() => {
    queueOps.clear();
  }, [queueOps]);

  return {
    permissionQueue,
    activePermission,
    enqueuePermissionRequest,
    resolvePermissionRequest,
    cancelPermissionRequest,
    clearPermissionQueue,
  };
}
