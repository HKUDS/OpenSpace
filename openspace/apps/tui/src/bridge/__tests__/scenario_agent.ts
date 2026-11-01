import type { IPCMessage, PermissionRequestData } from "../protocol.js";
import { StructuredIO } from "../structuredIO.js";

const io = new StructuredIO();
const receivedTypes: string[] = [];
const permissionCounts = new Map<string, number>();

function send(message: IPCMessage | { type: string; data: unknown; timestamp?: number }): void {
  process.stdout.write(JSON.stringify({
    ...message,
    timestamp: message.timestamp ?? Date.now(),
  }) + "\n");
}

function emitReport(type: string): void {
  send({
    type,
    data: {
      received_types: [...receivedTypes],
      permission_counts: Object.fromEntries(permissionCounts.entries()),
    },
  });
}

process.stdout.write("not-json\n");
process.stdout.write("\n");
send({
  type: "query",
  data: {
    text: "List the workspace status and explain any blockers.",
    attachments: ["/tmp/spec.md", "/tmp/screenshot.png"],
  },
});

for await (const message of io.receive()) {
  receivedTypes.push(message.type);

  if (message.type === "permission_request") {
    const data = message.data as PermissionRequestData;
    permissionCounts.set(data.tool_use_id, (permissionCounts.get(data.tool_use_id) ?? 0) + 1);
    void io.waitForPermissionDecision(data).catch(() => undefined);

    if (!data.tool_use_id.startsWith("hang-")) {
      setTimeout(() => {
        io.resolvePermission({
          tool_use_id: data.tool_use_id,
          decision: "allow",
        });
      }, 25);
    }

    continue;
  }

  if (message.type === "notification") {
    const data = message.data as Record<string, unknown>;
    if (data.title === "report-permissions") {
      emitReport("permission_report");
    }
    continue;
  }

  if (message.type === "task_complete") {
    emitReport("scenario_complete");
    continue;
  }

  if (message.type === "cancel") {
    send({
      type: "cancel_ack",
      data: {
        pending_count: io.pendingPermissions.size,
        received_types: [...receivedTypes],
      },
    });
    io.rejectAllPending("Cancelled by core");
    break;
  }
}

io.close();
