import type { PermissionDecision, ToolUseConfirm } from "./PermissionContext.js";

export type PermissionDecisionArgs = {
  decision: PermissionDecision;
  source: "user" | "system";
};

export function logPermissionDecision(
  request: ToolUseConfirm,
  args: PermissionDecisionArgs,
): void {
  if (
    process.env.NODE_ENV === "test" ||
    process.env.OPENSPACE_DEBUG_PERMISSIONS !== "1"
  ) {
    return;
  }

  const detail =
    args.decision === "allow_always"
      ? "persistent allow"
      : args.decision;

  process.stderr.write(
    `[permission] ${request.tool_name} -> ${detail} (${args.source})\n`,
  );
}
