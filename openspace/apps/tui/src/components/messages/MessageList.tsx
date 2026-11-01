import React from "react";
import { Box, Text } from "ink";
import type { AppMessage } from "../../state/AppStateStore.js";
import { getColor } from "../design-system/theme.js";
import { MessageRow } from "./MessageRow.js";

type MessageListProps = {
  messages: AppMessage[];
  maxVisibleRows: number;
  scrollOffset?: number;
  collapsed?: Set<string>;
  onToggleCollapse?: (messageId: string) => void;
};

const MAX_RENDER_LIMIT = 200;

function isRenderableMessage(message: AppMessage): boolean {
  if (message.meta?.hidden === true) return false;
  if (message.meta?.budget === true) return false;
  return true;
}

export function MessageList({
  messages,
  maxVisibleRows,
  scrollOffset,
}: MessageListProps): React.ReactElement {
  const renderable = React.useMemo(
    () => messages.filter(isRenderableMessage).slice(-MAX_RENDER_LIMIT),
    [messages],
  );

  const offset = scrollOffset ?? 0;
  const totalMessages = renderable.length;
  const startIndex = Math.max(0, totalMessages - maxVisibleRows - offset);
  const endIndex = Math.min(totalMessages, startIndex + maxVisibleRows);
  const visibleMessages = renderable.slice(startIndex, endIndex);

  const hasOlderMessages = startIndex > 0;
  const hasNewerMessages = endIndex < totalMessages;

  if (visibleMessages.length === 0) {
    return (
      <Text color={getColor("textDim")}>
        No messages yet. Type a prompt and press Enter.
      </Text>
    );
  }

  return (
    <Box flexDirection="column">
      {hasOlderMessages ? (
        <Text color={getColor("textDim")}>
          ↑ {startIndex} older message{startIndex === 1 ? "" : "s"}
        </Text>
      ) : null}

      {visibleMessages.map(message => (
        <MessageRow key={message.id} message={message} />
      ))}

      {hasNewerMessages ? (
        <Text color={getColor("textDim")}>
          ↓ {totalMessages - endIndex} newer message{totalMessages - endIndex === 1 ? "" : "s"}
        </Text>
      ) : null}
    </Box>
  );
}
