import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import instances from "../ink/instances.js";
import type { AppMessage, SessionContextState } from "../state/AppStateStore.js";

const GUI_EDITOR_BASES = new Set([
  "code",
  "cursor",
  "windsurf",
  "codium",
  "subl",
  "gedit",
  "notepad",
  "notepad++",
  "open",
  "xdg-open",
]);

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function formatTimestamp(timestamp: number): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return String(timestamp);
  }

  return DATE_TIME_FORMATTER.format(date);
}

function flattenMessageContent(message: AppMessage): string {
  if (typeof message.text === "string" && message.text.length > 0) {
    return message.text;
  }

  return message.content
    .map(block => {
      if (!block || typeof block !== "object") {
        return "";
      }

      switch (block.type) {
        case "text":
        case "status":
          return typeof block.text === "string" ? block.text : "";
        case "field":
          return `${block.label}: ${block.value}`;
        case "tool_use": {
          const parts: string[] = [];
          if (typeof block.tool_name === "string" && block.tool_name) {
            parts.push(`tool: ${block.tool_name}`);
          }
          if (block.tool_input !== undefined) {
            parts.push(`input: ${stringifyUnknown(block.tool_input)}`);
          }
          if (typeof block.result === "string" && block.result) {
            parts.push(`result: ${block.result}`);
          }
          if (typeof block.error === "string" && block.error) {
            parts.push(`error: ${block.error}`);
          }
          return parts.join("\n");
        }
        default:
          return "";
      }
    })
    .filter(Boolean)
    .join("\n")
    .trim();
}

function stringifyUnknown(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }

  if (value instanceof Error) {
    return value.message;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function sessionProjectPath(sessionContext: SessionContextState | null | undefined): string | null {
  const worktree = sessionContext?.worktree;
  const metadata = sessionContext?.metadata;

  if (worktree && typeof worktree === "object") {
    for (const key of ["workspace_dir", "worktree_path", "project_path"] as const) {
      if (isNonEmptyString(worktree[key])) {
        return worktree[key].trim();
      }
    }
  }

  if (metadata && typeof metadata === "object") {
    for (const key of ["workspace_dir", "worktree_path", "project_path"] as const) {
      if (isNonEmptyString(metadata[key])) {
        return metadata[key].trim();
      }
    }
  }

  return null;
}

function sanitizeFileStem(value: string): string {
  return value
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64) || "session";
}

function timestampSlug(timestamp = new Date()): string {
  return timestamp.toISOString().replace(/[:.]/g, "-");
}

function inferEditorCommand(editorCommand?: string): string | null {
  const configured = editorCommand?.trim();
  if (configured) {
    return configured;
  }

  const visual = process.env.VISUAL?.trim();
  if (visual) {
    return visual;
  }

  const editor = process.env.EDITOR?.trim();
  if (editor) {
    return editor;
  }

  return null;
}

function isGuiEditor(command: string): boolean {
  const base = basename(command).toLowerCase();

  if (GUI_EDITOR_BASES.has(base)) {
    return true;
  }

  return [...GUI_EDITOR_BASES].some(entry => base.includes(entry));
}

export function splitCommandLine(
  commandLine: string,
): { command: string; args: string[] } | null {
  const input = commandLine.trim();
  if (input.length === 0) {
    return null;
  }

  const parts: string[] = [];
  let current = "";
  let quote: "\"" | "'" | null = null;
  let escaping = false;

  for (const char of input) {
    if (escaping) {
      current += char;
      escaping = false;
      continue;
    }

    if (char === "\\" && quote !== "'") {
      escaping = true;
      continue;
    }

    if (quote) {
      if (char === quote) {
        quote = null;
      } else {
        current += char;
      }
      continue;
    }

    if (char === "\"" || char === "'") {
      quote = char;
      continue;
    }

    if (/\s/.test(char)) {
      if (current.length > 0) {
        parts.push(current);
        current = "";
      }
      continue;
    }

    current += char;
  }

  if (escaping || quote) {
    return null;
  }

  if (current.length > 0) {
    parts.push(current);
  }

  if (parts.length === 0) {
    return null;
  }

  return {
    command: parts[0]!,
    args: parts.slice(1),
  };
}

export function renderTranscriptToPlainText({
  messages,
  sessionId,
  sessionTitle,
  sessionContext,
  selectionIndex = null,
}: {
  messages: AppMessage[];
  sessionId?: string | null;
  sessionTitle?: string | null;
  sessionContext?: SessionContextState | null;
  selectionIndex?: number | null;
}): string {
  const lines: string[] = ["OpenSpace Transcript"];

  if (isNonEmptyString(sessionTitle)) {
    lines.push(`Title: ${sessionTitle.trim()}`);
  }
  if (isNonEmptyString(sessionId)) {
    lines.push(`Session: ${sessionId.trim()}`);
  }

  const projectPath = sessionProjectPath(sessionContext);
  if (projectPath) {
    lines.push(`Project: ${projectPath}`);
  }

  lines.push(`Generated: ${formatTimestamp(Date.now())}`);
  lines.push(`Messages: ${messages.length}`);

  if (selectionIndex !== null) {
    lines.push(`Selection: #${selectionIndex + 1}`);
  }

  lines.push("");

  messages.forEach((message, index) => {
    const header = [
      `#${index + 1}`,
      message.role.toUpperCase(),
      formatTimestamp(message.timestamp),
      selectionIndex === index ? "SELECTED" : null,
    ]
      .filter(Boolean)
      .join(" | ");

    const body = flattenMessageContent(message).trim() || "(empty)";
    lines.push(header);
    lines.push(body);
    lines.push("");
  });

  return `${lines.join("\n").trimEnd()}\n`;
}

export type TranscriptExportResult = {
  path: string;
  bytes: number;
  scope: "full" | "selection";
};

export async function exportTranscriptToFile({
  messages,
  sessionId,
  sessionTitle,
  sessionContext,
  selectionIndex = null,
  outputPath,
  cwd = process.cwd(),
}: {
  messages: AppMessage[];
  sessionId?: string | null;
  sessionTitle?: string | null;
  sessionContext?: SessionContextState | null;
  selectionIndex?: number | null;
  outputPath?: string | null;
  cwd?: string;
}): Promise<TranscriptExportResult> {
  const body = renderTranscriptToPlainText({
    messages,
    sessionId,
    sessionTitle,
    sessionContext,
    selectionIndex,
  });
  const baseDir = sessionProjectPath(sessionContext) ?? cwd;
  const scope = selectionIndex === null ? "full" : "selection";
  const defaultFileName =
    scope === "selection"
      ? `openspace-message-${String((selectionIndex ?? 0) + 1).padStart(3, "0")}-${sanitizeFileStem(sessionId ?? sessionTitle ?? "session")}.txt`
      : `openspace-transcript-${sanitizeFileStem(sessionId ?? sessionTitle ?? "session")}-${timestampSlug()}.txt`;
  const resolvedPath = outputPath
    ? (isAbsolute(outputPath) ? outputPath : resolve(baseDir, outputPath))
    : resolve(baseDir, defaultFileName);

  await mkdir(dirname(resolvedPath), { recursive: true });
  await writeFile(resolvedPath, body, "utf8");

  return {
    path: resolvedPath,
    bytes: Buffer.byteLength(body, "utf8"),
    scope,
  };
}

export async function prepareTranscriptEditorFile({
  messages,
  sessionId,
  sessionTitle,
  sessionContext,
  selectionIndex = null,
}: {
  messages: AppMessage[];
  sessionId?: string | null;
  sessionTitle?: string | null;
  sessionContext?: SessionContextState | null;
  selectionIndex?: number | null;
}): Promise<string> {
  const tempDir = await mkdtemp(join(tmpdir(), "openspace-transcript-"));
  const fileName =
    selectionIndex === null
      ? `transcript-${sanitizeFileStem(sessionId ?? sessionTitle ?? "session")}.txt`
      : `message-${String((selectionIndex ?? 0) + 1).padStart(3, "0")}.txt`;
  const outputPath = join(tempDir, fileName);

  await exportTranscriptToFile({
    messages,
    sessionId,
    sessionTitle,
    sessionContext,
    selectionIndex,
    outputPath,
  });

  return outputPath;
}

export function openPathInExternalEditor(
  filePath: string,
  editorCommand?: string,
): {
  ok: boolean;
  commandLine?: string;
  reason?: string;
} {
  const resolvedCommand = inferEditorCommand(editorCommand);
  if (!resolvedCommand) {
    return {
      ok: false,
      reason: "Set $EDITOR, $VISUAL, or /settings externalEditor to open files automatically.",
    };
  }

  const parsed = splitCommandLine(resolvedCommand);
  if (!parsed) {
    return {
      ok: false,
      commandLine: resolvedCommand,
      reason: `Could not parse external editor command: ${resolvedCommand}`,
    };
  }

  if (isGuiEditor(parsed.command)) {
    try {
      const child = spawn(parsed.command, [...parsed.args, filePath], {
        detached: true,
        stdio: "ignore",
      });
      child.unref();
      return {
        ok: true,
        commandLine: resolvedCommand,
      };
    } catch (error) {
      return {
        ok: false,
        commandLine: resolvedCommand,
        reason: error instanceof Error ? error.message : String(error),
      };
    }
  }

  const stdin = process.stdin;
  const canToggleRaw = typeof stdin.setRawMode === "function";
  const wasRaw = canToggleRaw && "isRaw" in stdin ? Boolean(stdin.isRaw) : false;

  try {
    if (canToggleRaw) {
      stdin.setRawMode(false);
    }
    stdin.pause();

    const result = spawnSync(parsed.command, [...parsed.args, filePath], {
      stdio: "inherit",
    });

    if (result.error) {
      return {
        ok: false,
        commandLine: resolvedCommand,
        reason: result.error.message,
      };
    }

    if ((result.status ?? 0) !== 0) {
      return {
        ok: false,
        commandLine: resolvedCommand,
        reason: `Editor exited with status ${result.status ?? "unknown"}.`,
      };
    }

    return {
      ok: true,
      commandLine: resolvedCommand,
    };
  } finally {
    stdin.resume();
    if (canToggleRaw) {
      stdin.setRawMode(wasRaw);
    }
    instances.get(process.stdout)?.forceRedraw();
    instances.get(process.stderr)?.forceRedraw();
  }
}
