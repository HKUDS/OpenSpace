import React from "react";
import { Box, Text } from "ink";
import type { AppMessage } from "../../state/AppStateStore.js";
import { getMessageText } from "../../screens/shared.js";
import {
  isContextAttachmentMessage,
  Message,
  MESSAGE_ROLE_LABELS,
  MESSAGE_ROLE_TOKENS,
} from "../Message.js";
import { MessageTimestamp } from "../MessageTimestamp.js";
import { getColor } from "../design-system/theme.js";
import { useSelectedMessageBg } from "../messageActions.js";
import { estimateWrappedRows } from "../../utils/textWidth.js";

type MessageRowProps = {
  message: AppMessage;
  isUserContinuation?: boolean;
  expanded?: boolean;
};

function estimateWrappedTextRows(
  text: string,
  width: number,
): number {
  const safeWidth = Math.max(24, width);
  return estimateWrappedRows(text, safeWidth);
}

function hasToolUseBlock(message: AppMessage): boolean {
  return message.content.some(block => block.type === "tool_use");
}

export function estimateMessageRowHeight(
  message: AppMessage,
  columns: number,
): number {
  if (isContextAttachmentMessage(message)) {
    return 2;
  }

  if (message.role === "tool" && hasToolUseBlock(message)) {
    const hasProgress = message.content.some(
      block =>
        block.type === "tool_use" &&
        typeof block.summary === "string" &&
        block.summary.length > 0,
    );
    return hasProgress ? 5 : 4;
  }

  const contentWidth = Math.max(24, columns - 12);
  const contentRows = estimateWrappedTextRows(
    getMessageText(message),
    contentWidth,
  );
  return 1 + contentRows + 1;
}

export function MessageRow({
  message,
  isUserContinuation = false,
  expanded = false,
}: MessageRowProps): React.ReactElement {
  const isContextAttachment = isContextAttachmentMessage(message);
  const color = getColor(MESSAGE_ROLE_TOKENS[message.role]);
  const label = MESSAGE_ROLE_LABELS[message.role];
  const showHeader =
    !isContextAttachment &&
    !(isUserContinuation && message.role === "user");
  const selectedBg = useSelectedMessageBg();
  const isSelected = selectedBg !== undefined;

  if (isContextAttachment) {
    return (
      <Box
        marginBottom={1}
        paddingX={isSelected ? 1 : 0}
      >
        {isSelected ? (
          <Text color={getColor("textDim") as never} bold>
            ›{" "}
          </Text>
        ) : null}
        <Message message={message} expanded={expanded} />
      </Box>
    );
  }

  return (
    <Box
      flexDirection="column"
      marginBottom={1}
      paddingX={isSelected ? 1 : 0}
    >
      {showHeader ? (
        <Box>
          {isSelected ? (
            <Text color={color as never} bold>
              ›{" "}
            </Text>
          ) : null}
          <MessageTimestamp message={message} />
          <Text color={getColor("textDim")}> </Text>
          <Text color={color as never} bold>
            [{label}]
          </Text>
        </Box>
      ) : isSelected ? (
        <Text color={color as never} bold>
          ›
        </Text>
      ) : null}
      <Message message={message} expanded={expanded} />
    </Box>
  );
}
