import React from "react";
import { Box, Text } from "ink";
import type { PermissionRequestData } from "../../bridge/protocol.js";
import { getColor } from "../design-system/theme.js";

type PermissionDialogProps = {
  request: PermissionRequestData;
  queueLength?: number;
};

const MAX_INPUT_PREVIEW = 200;

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

function summarizeToolInput(input: Record<string, unknown> | undefined): string {
  if (!input || Object.keys(input).length === 0) return "No input";

  try {
    return truncate(JSON.stringify(input, null, 2), MAX_INPUT_PREVIEW);
  } catch {
    return "Unable to display input";
  }
}

function riskColor(level: string | undefined): string {
  switch (level) {
    case "high":
      return getColor("error");
    case "medium":
      return getColor("warning");
    case "low":
      return getColor("success");
    default:
      return getColor("warning");
  }
}

export function PermissionDialog({
  request,
  queueLength,
}: PermissionDialogProps): React.ReactElement {
  const risk = request.risk_level ?? "medium";

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("permissionBorder")}
      paddingX={1}
      marginTop={1}
    >
      <Box>
        <Text bold color={getColor("permissionBorder")}>
          Permission Request
        </Text>
        {queueLength !== undefined && queueLength > 1 ? (
          <Text color={getColor("textDim")}> ({queueLength} pending)</Text>
        ) : null}
      </Box>

      <Box marginTop={1} flexDirection="column">
        <Box>
          <Text color={getColor("text")} bold>Tool: </Text>
          <Text color={getColor("toolMessage")}>{request.tool_name}</Text>
        </Box>

        <Box>
          <Text color={getColor("text")} bold>Risk: </Text>
          <Text color={riskColor(risk)}>{risk}</Text>
        </Box>

        {request.description ? (
          <Box>
            <Text color={getColor("text")} bold>Desc: </Text>
            <Text>{request.description}</Text>
          </Box>
        ) : null}
      </Box>

      <Box marginTop={1}>
        <Text color={getColor("textDim")}>
          {summarizeToolInput(request.tool_input)}
        </Text>
      </Box>

      <Box marginTop={1}>
        <Text color={getColor("success")} bold>y</Text>
        <Text> allow  </Text>
        <Text color={getColor("error")} bold>n</Text>
        <Text> deny  </Text>
        <Text color={getColor("primary")} bold>a</Text>
        <Text> always allow</Text>
      </Box>
    </Box>
  );
}
