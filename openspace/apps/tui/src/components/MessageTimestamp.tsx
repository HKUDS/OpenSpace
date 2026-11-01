import React from "react";
import { Box, Text } from "ink";
import type { AppMessage } from "../state/AppStateStore.js";
import { getColor } from "./design-system/theme.js";

const TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

type Props = {
  message: AppMessage;
};

function formatTimestamp(timestamp: number): string {
  try {
    return TIME_FORMATTER.format(new Date(timestamp));
  } catch {
    return "--:--:--";
  }
}

export function MessageTimestamp({
  message,
}: Props): React.ReactElement {
  const formattedTimestamp = formatTimestamp(message.timestamp);

  return (
    <Box minWidth={formattedTimestamp.length}>
      <Text color={getColor("textDim")}>
        {formattedTimestamp}
      </Text>
    </Box>
  );
}
