import React from "react";
import { Box, Text } from "ink";
import type {
  AgentRuntimeState,
  MCPClientState,
  RuntimeState,
} from "../state/AppStateStore.js";
import type { VimMode } from "../types/textInputTypes.js";
import { formatTokens, formatUsd } from "../screens/shared.js";
import { CoordinatorStatusBar } from "./CoordinatorStatusBar.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { truncateToDisplayWidth } from "../utils/textWidth.js";

type Props = {
  runtime: RuntimeState;
  mcpClientStates?: MCPClientState[];
  agents?: AgentRuntimeState;
  vimMode?: VimMode;
};

function formatSandboxSummary(runtime: RuntimeState): string {
  const sandbox = runtime.sandbox;
  if (!sandbox) {
    return "n/a";
  }
  if (sandbox.sandboxing_enabled) {
    return sandbox.mode === "auto-allow"
      ? "on auto"
      : sandbox.mode === "regular"
        ? "on regular"
        : "on";
  }
  if (sandbox.enabled_in_settings) {
    return sandbox.status === "fail" ? "fail" : "warn";
  }
  return "off";
}

export function StatusLine({
  runtime,
  mcpClientStates = [],
  agents,
  vimMode,
}: Props): React.ReactElement {
  const { columns } = useTerminalSize();
  const connectedMcp = mcpClientStates.filter(
    client => client.status === "connected",
  ).length;
  const failingMcp = mcpClientStates.filter(
    client => client.status === "error",
  ).length;
  const tokenWarning = runtime.tokenWarning;
  const showTokenWarning =
    tokenWarning?.is_above_warning_threshold === true;
  const tokenWarningColor =
    tokenWarning?.is_above_error_threshold === true ||
    tokenWarning?.is_at_blocking_limit === true
      ? "red"
      : "yellow";
  const tokenWarningText = tokenWarning
    ? tokenWarning.is_above_auto_compact_threshold
      ? `Context compacting (${tokenWarning.percent_left}% left)`
      : `Context low (${tokenWarning.percent_left}% left)`
    : null;
  const safeColumns = Math.max(20, columns - 1);
  const primaryText = `OpenSpace | ${runtime.model ?? "model n/a"} | ${runtime.phase ?? "idle"} | Cost ${formatUsd(runtime.costUsd)}`;
  const secondaryText = [
    `Session ${runtime.sessionId ?? "n/a"}`,
    `Task ${runtime.activeTaskId ?? "n/a"}`,
    `Tokens ${formatTokens(runtime.inputTokens)} / ${formatTokens(runtime.outputTokens)}`,
    `Iterations ${runtime.totalIterations ?? 0}${runtime.maxIterations !== undefined ? ` / ${runtime.maxIterations}` : ""}`,
    `MCP ${connectedMcp}/${mcpClientStates.length}${failingMcp > 0 ? ` (${failingMcp} error)` : ""}`,
    `Sandbox ${formatSandboxSummary(runtime)}`,
    vimMode ? `Vim ${vimMode}` : null,
  ].filter(Boolean).join(" | ");

  return (
    <Box flexDirection="column" height={4} width="100%" overflowY="hidden">
      <Text bold color="cyan" wrap="truncate">
        {truncateToDisplayWidth(primaryText, safeColumns)}
      </Text>
      <Text color="gray" wrap="truncate">
        {truncateToDisplayWidth(secondaryText, safeColumns)}
      </Text>
      <Box height={1}>
        {showTokenWarning && tokenWarningText ? (
          <Text color={tokenWarningColor as never} wrap="truncate">
            {truncateToDisplayWidth(tokenWarningText, safeColumns)}
          </Text>
        ) : null}
      </Box>
      <Box height={1} width="100%" overflowX="hidden">
        {agents ? (
          <CoordinatorStatusBar
            coordinator={agents.coordinator}
            backgroundTasks={agents.backgroundTasks}
          />
        ) : null}
      </Box>
    </Box>
  );
}
