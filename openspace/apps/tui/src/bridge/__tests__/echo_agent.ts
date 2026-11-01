/**
 * Minimal TS "agent" that reads NDJSON from stdin, echoes every message
 * back with type prefixed by "echo_", and also sends a startup greeting.
 *
 * Used by the Python integration test to verify the IPC round-trip.
 */

import { createInterface } from "node:readline";
import { ndjsonParse, ndjsonSafeStringify } from "../ndjson.js";

function write(obj: unknown): void {
  process.stdout.write(ndjsonSafeStringify(obj) + "\n");
}

write({
  type: "status_update",
  data: { model: "test-model", session_id: "test-session" },
  timestamp: Date.now(),
});

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });

for await (const line of rl) {
  const msg = ndjsonParse<{
    type: string;
    data: unknown;
    timestamp?: number;
  }>(line);
  if (!msg) continue;

  if (msg.type === "permission_request") {
    write({
      type: "permission_response",
      data: {
        tool_use_id: (msg.data as Record<string, unknown>).tool_use_id,
        decision: "allow",
      },
      timestamp: Date.now(),
    });
    continue;
  }

  write({
    type: `echo_${msg.type}`,
    data: msg.data,
    timestamp: Date.now(),
  });
}
