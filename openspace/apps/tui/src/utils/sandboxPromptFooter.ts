import type { SandboxStatusData } from "../bridge/protocol.js";

export type SandboxPromptHint = {
  text: string;
  color: "gray" | "green" | "yellow" | "red";
};

export function sandboxHint(sandbox?: SandboxStatusData): SandboxPromptHint | null {
  if (!sandbox) {
    return null;
  }
  const recentViolations =
    sandbox.recent_violation_count ?? sandbox.recent_violations?.length ?? 0;
  if (recentViolations > 0) {
    return {
      text: `Sandbox blocked ${recentViolations} operation${
        recentViolations === 1 ? "" : "s"
      } | /sandbox status for details | /sandbox to configure`,
      color: "yellow",
    };
  }
  if (sandbox.enabled_in_settings && !sandbox.sandboxing_enabled) {
    const reason = sandbox.unavailable_reason
      ? `: ${sandbox.unavailable_reason}`
      : "";
    return {
      text: `Sandbox unavailable${reason} | /sandbox doctor`,
      color: sandbox.status === "fail" ? "red" : "yellow",
    };
  }
  if (sandbox.sandboxing_enabled) {
    const mode =
      sandbox.mode === "auto-allow"
        ? "auto-allow"
        : sandbox.mode === "regular"
          ? "regular"
          : "on";
    return {
      text: `Sandbox: ${mode}`,
      color: "green",
    };
  }
  return {
    text: "Sandbox: off",
    color: "gray",
  };
}
