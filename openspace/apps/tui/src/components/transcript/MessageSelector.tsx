import React from "react";
import { Box, Text } from "ink";
import type { AppMessage } from "../../state/AppStateStore.js";
import {
  formatDateTime,
  getMessageText,
  truncate,
} from "../../screens/shared.js";
import { useShortcutDisplay } from "../../keybindings/useShortcutDisplay.js";

type Props = {
  messages: AppMessage[];
  selectedIndex: number;
  targetIndex: number | null;
};

const WINDOW_SIZE = 9;

function roleColor(role: AppMessage["role"]): string {
  switch (role) {
    case "user":
      return "green";
    case "assistant":
      return "cyan";
    case "tool":
      return "magenta";
    case "error":
      return "red";
    case "status":
      return "yellow";
    case "system":
    default:
      return "gray";
  }
}

export function MessageSelector({
  messages,
  selectedIndex,
  targetIndex,
}: Props): React.ReactElement {
  const confirmShortcut = useShortcutDisplay(
    "transcript:confirmSelection",
    "Transcript",
    "Enter",
  );
  const rewindShortcut = useShortcutDisplay(
    "transcript:rewind",
    "Transcript",
    "ctrl+r",
  );
  const clearShortcut = useShortcutDisplay(
    "transcript:clearSelection",
    "Transcript",
    "Backspace",
  );

  const clampedIndex = Math.max(
    0,
    Math.min(messages.length - 1, selectedIndex),
  );
  const startIndex = Math.max(
    0,
    clampedIndex - Math.floor(WINDOW_SIZE / 2),
  );
  const endIndex = Math.min(messages.length, startIndex + WINDOW_SIZE);
  const visibleMessages = messages.slice(startIndex, endIndex);

  return (
    <Box flexDirection="column">
      <Text bold color="cyan">
        Message Selector
      </Text>
      <Text color="gray">
        Pick a rewind target from the current transcript.
      </Text>
      <Box
        marginTop={1}
        borderStyle="round"
        borderColor="cyan"
        paddingX={1}
        flexDirection="column"
      >
        {visibleMessages.map((message, offset) => {
          const index = startIndex + offset;
          const isSelected = index === clampedIndex;
          const isTarget = index === targetIndex;

          return (
            <Box key={message.id} flexDirection="column" marginBottom={1}>
              <Text color={isSelected ? "cyan" : "gray"}>
                {isSelected ? ">" : " "} #{index + 1}
                {isTarget ? " [target]" : ""} [{formatDateTime(message.timestamp)}]
              </Text>
              <Text color={roleColor(message.role)}>
                {message.role}: {truncate(getMessageText(message), 120) || "(empty)"}
              </Text>
            </Box>
          );
        })}
      </Box>
      <Text color="gray">
        {confirmShortcut} mark target | {rewindShortcut} apply | {clearShortcut} clear
      </Text>
    </Box>
  );
}
