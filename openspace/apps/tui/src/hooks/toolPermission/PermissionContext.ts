import React from "react";
import type { StructuredIO } from "../../bridge/structuredIO.js";
import type {
  PermissionRequestData,
  PermissionResponseData,
  ToolPermissionResponseData,
} from "../../bridge/protocol.js";
import {
  buildPendingSandboxRequest,
  buildPendingWorkerRequest,
  buildWorkerSandboxQueue,
} from "../../bridge/permissionRequestState.js";
import { useSetAppState } from "../../state/AppState.js";

export type ToolUseConfirm = PermissionRequestData;
export type PermissionDecision = PermissionResponseData["decision"];
export type PermissionResolution =
  | PermissionDecision
  | PermissionResponseData
  | ToolPermissionResponseData;

export type PermissionQueueOps = {
  push: (item: ToolUseConfirm) => void;
  remove: (toolUseID: string) => void;
  clear: () => void;
};

type PermissionQueueState = {
  permissionQueue: ToolUseConfirm[];
  activePermission: ToolUseConfirm | null;
  queueOps: PermissionQueueOps;
  resolveCurrent: (resolution: PermissionResolution) => ToolUseConfirm | null;
  cancelByToolUseId: (toolUseID: string) => ToolUseConfirm | null;
};

function isToolPermissionResponseChannel(request: ToolUseConfirm): boolean {
  return request.response_channel === "tool_permission_response";
}

function firstAllowAlwaysOption(
  request: ToolUseConfirm,
): { suggestion_index?: number | null } | null {
  return (
    request.options?.find(option => option.option_id === "allow_always") ??
    null
  );
}

function normalizeResolution(
  request: ToolUseConfirm,
  resolution: PermissionResolution,
): PermissionResponseData | ToolPermissionResponseData {
  if (typeof resolution !== "string") {
    return resolution;
  }

  if (!isToolPermissionResponseChannel(request)) {
    return {
      tool_use_id: request.tool_use_id,
      decision: resolution,
      pattern:
        resolution === "allow_always"
          ? request.allow_always_pattern
          : undefined,
    };
  }

  if (resolution === "allow") {
    return {
      tool_use_id: request.tool_use_id,
      option_id: "allow_once",
    };
  }

  if (resolution === "allow_always") {
    const option = firstAllowAlwaysOption(request);
    return {
      tool_use_id: request.tool_use_id,
      option_id: "allow_always",
      suggestion_index: option?.suggestion_index ?? null,
    };
  }

  return {
    tool_use_id: request.tool_use_id,
    option_id: "deny",
  };
}

function isToolPermissionResponse(
  response: PermissionResponseData | ToolPermissionResponseData,
): response is ToolPermissionResponseData {
  return (
    "option_id" in response ||
    "updated_input" in response ||
    "edited_input" in response ||
    "suggestion_index" in response ||
    "selected_suggestion" in response
  );
}

export function createResolveOnce<T>(resolve: (value: T) => void): {
  resolve: (value: T) => void;
  claim: () => boolean;
  isResolved: () => boolean;
} {
  let claimed = false;
  let delivered = false;

  return {
    resolve(value: T) {
      if (delivered) {
        return;
      }

      delivered = true;
      claimed = true;
      resolve(value);
    },
    claim() {
      if (claimed) {
        return false;
      }

      claimed = true;
      return true;
    },
    isResolved() {
      return claimed;
    },
  };
}

export function usePermissionQueue(
  io: StructuredIO | null,
): PermissionQueueState {
  const [permissionQueue, setPermissionQueue] = React.useState<
    ToolUseConfirm[]
  >([]);
  const setAppState = useSetAppState();

  const activePermission = permissionQueue[0] ?? null;

  React.useEffect(() => {
    const workerQueue = buildWorkerSandboxQueue(permissionQueue);
    setAppState(prev => ({
      ...prev,
      toolPermissionContext: {
        ...prev.toolPermissionContext,
        pendingRequest: activePermission,
      },
      pendingSandboxRequest: buildPendingSandboxRequest(activePermission),
      pendingWorkerRequest: buildPendingWorkerRequest(activePermission),
      workerSandboxPermissions: {
        ...prev.workerSandboxPermissions,
        queue: workerQueue,
        selectedIndex:
          workerQueue.length === 0
            ? 0
            : Math.min(
                prev.workerSandboxPermissions.selectedIndex,
                Math.max(0, workerQueue.length - 1),
              ),
      },
    }));
  }, [activePermission, permissionQueue, setAppState]);

  const queueOps = React.useMemo<PermissionQueueOps>(
    () => ({
      push(item) {
        setPermissionQueue(queue => {
          if (queue.some(existing => existing.tool_use_id === item.tool_use_id)) {
            return queue;
          }
          return [...queue, item];
        });
      },
      remove(toolUseID) {
        setPermissionQueue(queue =>
          queue.filter(item => item.tool_use_id !== toolUseID),
        );
      },
      clear() {
        setPermissionQueue([]);
      },
    }),
    [],
  );

  const resolveCurrent = React.useCallback(
    (resolution: PermissionResolution): ToolUseConfirm | null => {
      const current = permissionQueue[0] ?? null;

      if (!current) {
        return null;
      }

      const response = normalizeResolution(current, resolution);
      if (isToolPermissionResponse(response)) {
        io?.resolveToolPermission(response);
      } else {
        io?.resolvePermission(response);
      }
      queueOps.remove(current.tool_use_id);
      return current;
    },
    [io, permissionQueue, queueOps],
  );

  const cancelByToolUseId = React.useCallback(
    (toolUseID: string): ToolUseConfirm | null => {
      const current =
        permissionQueue.find(item => item.tool_use_id === toolUseID) ?? null;
      if (current) {
        queueOps.remove(toolUseID);
      }
      return current;
    },
    [permissionQueue, queueOps],
  );

  return {
    permissionQueue,
    activePermission,
    queueOps,
    resolveCurrent,
    cancelByToolUseId,
  };
}
