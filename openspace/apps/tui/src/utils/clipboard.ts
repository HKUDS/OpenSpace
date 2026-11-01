import { spawnSync } from "node:child_process";
import process from "node:process";

type ClipboardCommand = {
  command: string;
  args: string[];
};

export type ClipboardResult = {
  ok: boolean;
  command?: string;
  reason?: string;
};

function candidateCommands(): ClipboardCommand[] {
  if (process.platform === "darwin") {
    return [{ command: "pbcopy", args: [] }];
  }

  if (process.platform === "win32") {
    return [{ command: "clip", args: [] }];
  }

  return [
    { command: "wl-copy", args: [] },
    { command: "xclip", args: ["-selection", "clipboard"] },
    { command: "xsel", args: ["--clipboard", "--input"] },
  ];
}

export function copyTextToClipboard(text: string): ClipboardResult {
  const payload = text.endsWith("\n") ? text : `${text}\n`;

  for (const candidate of candidateCommands()) {
    const result = spawnSync(candidate.command, candidate.args, {
      input: payload,
      encoding: "utf8",
      stdio: ["pipe", "ignore", "pipe"],
    });

    if (result.error && "code" in result.error && result.error.code === "ENOENT") {
      continue;
    }

    if (result.status === 0 && !result.error) {
      return { ok: true, command: candidate.command };
    }

    return {
      ok: false,
      command: candidate.command,
      reason:
        result.stderr?.trim() ||
        result.error?.message ||
        `Clipboard command exited with code ${result.status ?? "unknown"}.`,
    };
  }

  return {
    ok: false,
    reason: "No supported clipboard command found.",
  };
}
