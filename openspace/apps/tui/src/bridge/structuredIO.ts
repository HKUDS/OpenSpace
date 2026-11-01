import { createInterface } from "node:readline";
import { ndjsonParse, ndjsonSafeStringify } from "./ndjson.js";
import { isKnownEventType, makeProtocolWarningMessage } from "./protocol.js";
import type {
  EventType,
  IPCMessage,
  PermissionRequestData,
  PermissionResponseData,
  ToolPermissionResponseData,
} from "./protocol.js";

const MAX_RESOLVED_TOOL_USE_IDS = 1000;
const RECENT_MESSAGE_LIMIT = 200;
const RECEIVE_YIELD_EVERY_MESSAGES = 32;
const RECEIVE_YIELD_EVERY_MS = 8;

export const STRUCTURED_IO_SEQUENCE = Symbol("structured_io_sequence");

export type StructuredIOListener = (message: IPCMessage) => void;

type SequencedIPCMessage = IPCMessage & {
  [STRUCTURED_IO_SEQUENCE]?: number;
};

type PermissionDecisionResponse =
  | PermissionResponseData
  | ToolPermissionResponseData;

export function getStructuredIOSequence(
  message: IPCMessage,
): number | null {
  return (
    (message as SequencedIPCMessage)[STRUCTURED_IO_SEQUENCE] ??
    null
  );
}

export interface StructuredIOOptions {
  stdin?: NodeJS.ReadableStream;
  stdout?: NodeJS.WritableStream;
}

/**
 * Bidirectional NDJSON IPC channel between the TS TUI (this process)
 * and the Python Core (parent process).
 *
 * - Reads NDJSON from stdin (events sent by Python Core)
 * - Writes NDJSON to stdout (events sent to Python Core)
 * - Manages pending permission requests with Future-like resolution
 * - Deduplicates tool_use_ids
 */
export class StructuredIO {
  private readonly input: NodeJS.ReadableStream;
  private readonly output: NodeJS.WritableStream;
  private readonly listeners = new Set<StructuredIOListener>();
  private readonly recentMessages: IPCMessage[] = [];
  private sequence = 0;

  /**
   * Map of tool_use_id → { resolve, reject } for outstanding
   * permission_request events awaiting a permission_response from the TUI.
   * Used on the TUI side to track which permission prompts are still pending.
   */
  readonly pendingPermissions = new Map<
    string,
    {
      data: PermissionRequestData;
      resolve: (response: PermissionDecisionResponse) => void;
      reject: (error: Error) => void;
    }
  >();

  /**
   * Set of tool_use_ids with an active prompt already delivered to the UI.
   * Cleared on resolve/reject so edited-input retry flows can ask again with
   * the same tool_use_id after Core rechecks permissions.
   */
  readonly seenToolUseIds = new Set<string>();

  private closed = false;

  constructor(opts?: StructuredIOOptions) {
    this.input = opts?.stdin ?? process.stdin;
    this.output = opts?.stdout ?? process.stdout;
  }

  subscribe(
    listener: StructuredIOListener,
    opts?: { replayRecent?: boolean },
  ): () => void {
    this.listeners.add(listener);

    if (opts?.replayRecent) {
      for (const message of this.recentMessages) {
        listener(message);
      }
    }

    return () => {
      this.listeners.delete(listener);
    };
  }

  // ── Writing ───────────────────────────────────────────────────

  send<T extends EventType>(message: IPCMessage<T>): void {
    if (this.closed) return;
    const stamped = { ...message, timestamp: message.timestamp ?? Date.now() };
    const line = ndjsonSafeStringify(stamped) + "\n";
    this.output.write(line);
  }

  // ── Reading ───────────────────────────────────────────────────

  /**
   * Async generator that yields parsed IPC messages from stdin.
   * Terminates when stdin closes.
   */
  async *receive(): AsyncGenerator<IPCMessage, void, undefined> {
    const rl = createInterface({ input: this.input, crlfDelay: Infinity });
    let messagesSinceYield = 0;
    let lastYieldAt = Date.now();
    const yieldToRendererIfNeeded = async (): Promise<void> => {
      messagesSinceYield += 1;
      if (
        messagesSinceYield < RECEIVE_YIELD_EVERY_MESSAGES &&
        Date.now() - lastYieldAt < RECEIVE_YIELD_EVERY_MS
      ) {
        return;
      }

      messagesSinceYield = 0;
      lastYieldAt = Date.now();
      await new Promise<void>(resolve => setImmediate(resolve));
    };

    for await (const line of rl) {
      const msg = ndjsonParse<IPCMessage>(line);
      if (!msg || typeof msg.type !== "string" || msg.type.length === 0) {
        const warning = makeProtocolWarningMessage(
          "<missing>",
          "Malformed protocol event",
        );
        this.emit(warning);
        yield warning;
        await yieldToRendererIfNeeded();
        continue;
      }

      if (!isKnownEventType(msg.type)) {
        const warning = makeProtocolWarningMessage(
          msg.type,
          "Unknown protocol event type",
        );
        this.emit(warning);
        yield warning;
        await yieldToRendererIfNeeded();
        continue;
      }

      if (msg.type === "permission_request" || msg.type === "tool_permission_ask") {
        const data = msg.data as PermissionRequestData;
        if (this.seenToolUseIds.has(data.tool_use_id)) continue;
        this.trackToolUseId(data.tool_use_id);
      }

      if (msg.type === "permission_response") {
        const data = msg.data as PermissionResponseData;
        this.resolvePermission(data);
      }

      if (msg.type === "cancel") {
        this.rejectAllPending("Cancelled by core");
      }

      this.emit(msg);
      yield msg;
      await yieldToRendererIfNeeded();
    }

    this.close();
  }

  // ── Permission management ─────────────────────────────────────

  /**
   * Register an incoming permission_request and return a Promise that
   * resolves when the TUI user makes a decision. The REPL screen should
   * display the prompt and call `resolvePermission()` with the answer.
   */
  waitForPermissionDecision(
    data: PermissionRequestData,
  ): Promise<PermissionDecisionResponse> {
    return new Promise<PermissionDecisionResponse>((resolve, reject) => {
      this.pendingPermissions.set(data.tool_use_id, {
        data,
        resolve,
        reject,
      });
    });
  }

  /**
   * Resolve a pending permission prompt and send the response back to
   * the Python Core.
   */
  resolvePermission(response: PermissionResponseData): void {
    this.finalizePermission(response);
    this.send({ type: "permission_response", data: response });
  }

  /**
   * Send a native tool-permission response for Core tool_permission_ask events.
   */
  resolveToolPermission(response: ToolPermissionResponseData): void {
    this.finalizePermission(response);
    this.send({ type: "tool_permission_response", data: response });
  }

  /**
   * Reject all pending permission requests (e.g. on cancel/shutdown).
   */
  rejectAllPending(reason: string): void {
    for (const [id, pending] of this.pendingPermissions) {
      pending.reject(new Error(reason));
      this.pendingPermissions.delete(id);
      this.seenToolUseIds.delete(id);
    }
  }

  // ── Lifecycle ─────────────────────────────────────────────────

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.listeners.clear();
    this.rejectAllPending("StructuredIO closed");
  }

  get isClosed(): boolean {
    return this.closed;
  }

  // ── Internal helpers ──────────────────────────────────────────

  private trackToolUseId(id: string): void {
    this.seenToolUseIds.add(id);
    if (this.seenToolUseIds.size > MAX_RESOLVED_TOOL_USE_IDS) {
      const first = this.seenToolUseIds.values().next().value;
      if (first !== undefined) {
        this.seenToolUseIds.delete(first);
      }
    }
  }

  private finalizePermission(response: PermissionDecisionResponse): void {
    this.seenToolUseIds.delete(response.tool_use_id);

    const pending = this.pendingPermissions.get(response.tool_use_id);
    if (!pending) return;

    this.pendingPermissions.delete(response.tool_use_id);
    pending.resolve(response);
  }

  private emit(message: IPCMessage): void {
    const sequenced = message as SequencedIPCMessage;

    if (sequenced[STRUCTURED_IO_SEQUENCE] === undefined) {
      Object.defineProperty(sequenced, STRUCTURED_IO_SEQUENCE, {
        value: ++this.sequence,
        enumerable: false,
        writable: false,
      });
    }

    this.recentMessages.push(message);
    if (this.recentMessages.length > RECENT_MESSAGE_LIMIT) {
      this.recentMessages.shift();
    }

    for (const listener of this.listeners) {
      listener(message);
    }
  }
}
