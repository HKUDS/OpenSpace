import React from "react";
import { Box, Text } from "ink";
import type { AppMessage } from "../state/AppStateStore.js";
import { getMessageText } from "../screens/shared.js";
import {
  MESSAGE_ROLE_LABELS,
  MESSAGE_ROLE_TOKENS,
} from "./Message.js";
import { MessageTimestamp } from "./MessageTimestamp.js";
import { getColor } from "./design-system/theme.js";

type Props = {
  messages: AppMessage[];
  agentLabel?: string;
  title?: string;
  emptyLabel?: string;
  maxMessages?: number;
};

function isRenderableMessage(message: AppMessage): boolean {
  if (message.meta?.hidden === true) {
    return false;
  }

  if (message.meta?.budget === true) {
    return false;
  }

  return true;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) {
    return text;
  }

  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

function summarizeMessage(message: AppMessage): string {
  const text = getMessageText(message).replace(/\s+/g, " ").trim();
  return truncate(text || " ", 100);
}

export function AgentTranscriptPreview({
  messages,
  agentLabel = "Viewed agent",
  title = "Agent Transcript",
  emptyLabel = "No transcript available",
  maxMessages = 5,
}: Props): React.ReactElement {
  const visibleMessages = messages.filter(isRenderableMessage).slice(-maxMessages);

  if (visibleMessages.length === 0) {
    return (
      <Box borderStyle="round" borderColor={getColor("border")} paddingX={1}>
        <Text color={getColor("textDim")}>{emptyLabel}</Text>
      </Box>
    );
  }

  const latestMessage = visibleMessages[visibleMessages.length - 1]!;

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("border")}
      paddingX={1}
    >
      <Text bold color={getColor("primary")}>
        {title} ({messages.length})
      </Text>
      <Text color={getColor("textDim")}>
        {agentLabel} · {visibleMessages.length} message{visibleMessages.length === 1 ? "" : "s"}
      </Text>
      <Box>
        <MessageTimestamp message={latestMessage} />
        <Text color={getColor("textDim")}> </Text>
        <Text color={getColor(MESSAGE_ROLE_TOKENS[latestMessage.role])} bold>
          [{MESSAGE_ROLE_LABELS[latestMessage.role]}]
        </Text>
        <Text color={getColor("textDim")}> </Text>
        <Text color={getColor("text")}>{summarizeMessage(latestMessage)}</Text>
      </Box>

      {visibleMessages.slice(0, -1).map(message => (
        <Box key={message.id} marginTop={1}>
          <Text color={getColor("textDim")}>
            {MESSAGE_ROLE_LABELS[message.role]}:
          </Text>
          <Text color={getColor("textDim")}> </Text>
          <Text color={getColor(MESSAGE_ROLE_TOKENS[message.role])}>
            {summarizeMessage(message)}
          </Text>
        </Box>
      ))}
    </Box>
  );
}
