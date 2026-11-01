import React from "react";
import { Box, Text } from "ink";
import { getColor } from "../design-system/theme.js";

type CollapsibleToolCallProps = {
  toolName: string;
  input: string;
  result?: string;
  error?: string;
  status?: "pending" | "running" | "complete" | "error";
  progress?: string;
  collapsed: boolean;
  onToggle?: () => void;
};

const MAX_COLLAPSED_LENGTH = 140;
const MAX_EXPANDED_LENGTH = 2000;

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

export function CollapsibleToolCall({
  toolName,
  input,
  result,
  error,
  status,
  progress,
  collapsed,
}: CollapsibleToolCallProps): React.ReactElement {
  const inputDisplay = collapsed
    ? truncate(input.replace(/\s+/g, " ").trim(), MAX_COLLAPSED_LENGTH)
    : truncate(input, MAX_EXPANDED_LENGTH);
  const progressDisplay = progress
    ? truncate(progress.replace(/\s+/g, " ").trim(), MAX_COLLAPSED_LENGTH)
    : null;

  const resultDisplay = result
    ? collapsed
      ? truncate(result.replace(/\s+/g, " ").trim(), MAX_COLLAPSED_LENGTH)
      : truncate(result, MAX_EXPANDED_LENGTH)
    : null;

  return (
    <Box flexDirection="column">
      <Box>
        <Text color={getColor("toolMessage")} bold>
          {collapsed ? "▸ " : "▾ "}
          {toolName}
        </Text>
        {error ? (
          <Text color={getColor("error")}> (error)</Text>
        ) : status === "running" ? (
          <Text color={getColor("textDim")}> (running)</Text>
        ) : status === "complete" ? (
          <Text color={getColor("textDim")}> (done)</Text>
        ) : null}
      </Box>

      {collapsed ? (
        <Box flexDirection="column">
          <Text color={getColor("textDim")}> {inputDisplay}</Text>
          {progressDisplay ? (
            <Text color={getColor("textDim")}> {progressDisplay}</Text>
          ) : null}
        </Box>
      ) : (
        <Box flexDirection="column" marginLeft={2}>
          <Text color={getColor("muted")} bold>Input:</Text>
          <Text color={getColor("textDim")}>{inputDisplay}</Text>

          {progressDisplay ? (
            <>
              <Text color={getColor("muted")} bold>Progress:</Text>
              <Text color={getColor("textDim")}>{progressDisplay}</Text>
            </>
          ) : null}

          {resultDisplay ? (
            <>
              <Text color={getColor("muted")} bold>Result:</Text>
              <Text>{resultDisplay}</Text>
            </>
          ) : null}

          {error ? (
            <>
              <Text color={getColor("error")} bold>Error:</Text>
              <Text color={getColor("error")}>{error}</Text>
            </>
          ) : null}
        </Box>
      )}
    </Box>
  );
}
