import type { StructuredIO } from "../../../bridge/structuredIO.js";
import type { PermissionRequestData } from "../../../bridge/protocol.js";
import type { PermissionQueueOps } from "../PermissionContext.js";

type InteractivePermissionParams = {
  io: StructuredIO | null;
  request: PermissionRequestData;
  queueOps: PermissionQueueOps;
};

export function handleInteractivePermission({
  io,
  request,
  queueOps,
}: InteractivePermissionParams): void {
  void io?.waitForPermissionDecision(request).catch(() => undefined);
  queueOps.push(request);
}
