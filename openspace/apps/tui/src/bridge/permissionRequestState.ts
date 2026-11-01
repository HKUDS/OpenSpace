import type { PermissionRequestData } from "./protocol.js";

export type WorkerPermissionQueueItem = {
  requestId: string;
  workerId: string;
  workerName: string;
  workerColor?: string;
  host: string;
  createdAt: number;
};

export type PendingSandboxPermissionState = {
  requestId: string;
  host: string;
  requestKind: "network" | "sandbox";
} | null;

export type PendingWorkerPermissionState = {
  toolName: string;
  toolUseId: string;
  description: string;
  workerId: string;
  workerName: string;
  workerColor?: string;
  host?: string;
  requestKind: "tool" | "network" | "sandbox";
} | null;

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function isWorkerPermissionRequest(
  request: PermissionRequestData | null | undefined,
): request is PermissionRequestData {
  if (!request) {
    return false;
  }
  if (request.origin === "worker") {
    return true;
  }
  const agentId = nonEmptyString(request.agent_id);
  return agentId !== null && agentId !== "primary";
}

export function isSandboxPermissionRequest(
  request: PermissionRequestData | null | undefined,
): request is PermissionRequestData & {
  request_kind: "network" | "sandbox";
} {
  return request?.request_kind === "network" || request?.request_kind === "sandbox";
}

export function getPermissionRequestSummary(
  request: PermissionRequestData,
): string {
  if (request.description?.trim()) {
    return request.description.trim();
  }

  const workerName = request.agent_name ?? request.agent_id;
  const prefix =
    isWorkerPermissionRequest(request) && workerName
      ? `Worker ${workerName} `
      : "";

  if (request.request_kind === "network" && request.host) {
    return `${prefix}requests network access to ${request.host} via ${request.tool_name}`;
  }
  if (request.request_kind === "sandbox" && request.host) {
    return `${prefix}requests sandbox access to ${request.host} via ${request.tool_name}`;
  }
  if (request.request_kind === "sandbox") {
    return `${prefix}requests sandbox access via ${request.tool_name}`;
  }
  return prefix
    ? `${prefix}requests permission for ${request.tool_name}`
    : `Primary session requests permission for ${request.tool_name}`;
}

export function getAllowAlwaysLabel(
  request: PermissionRequestData,
): string {
  if (request.host) {
    return `allow always for ${request.host}`;
  }
  return `allow always for ${request.tool_name}`;
}

export function buildPendingSandboxRequest(
  request: PermissionRequestData | null | undefined,
): PendingSandboxPermissionState {
  if (!isSandboxPermissionRequest(request)) {
    return null;
  }
  return {
    requestId: request.tool_use_id,
    host: request.host ?? request.tool_name,
    requestKind: request.request_kind,
  };
}

export function buildPendingWorkerRequest(
  request: PermissionRequestData | null | undefined,
): PendingWorkerPermissionState {
  if (!isWorkerPermissionRequest(request)) {
    return null;
  }

  const workerId = nonEmptyString(request.agent_id) ?? "worker";
  const workerName = nonEmptyString(request.agent_name) ?? workerId;
  return {
    toolName: request.tool_name,
    toolUseId: request.tool_use_id,
    description: getPermissionRequestSummary(request),
    workerId,
    workerName,
    workerColor: nonEmptyString(request.agent_color) ?? undefined,
    host: nonEmptyString(request.host) ?? undefined,
    requestKind: request.request_kind ?? "tool",
  };
}

export function buildWorkerSandboxQueue(
  requests: PermissionRequestData[],
): WorkerPermissionQueueItem[] {
  return requests
    .filter(
      request =>
        isWorkerPermissionRequest(request) &&
        (request.host !== undefined || request.request_kind === "sandbox"),
    )
    .map((request, index) => {
      const workerId = nonEmptyString(request.agent_id) ?? "worker";
      const workerName = nonEmptyString(request.agent_name) ?? workerId;
      return {
        requestId: request.tool_use_id,
        workerId,
        workerName,
        workerColor: nonEmptyString(request.agent_color) ?? undefined,
        host: nonEmptyString(request.host) ?? request.tool_name,
        createdAt: index,
      };
    });
}
