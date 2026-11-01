import React from "react";
import { Box, Text } from "ink";
import type { InputMode } from "../../state/AppStateStore.js";
import type { VimMode } from "../../types/textInputTypes.js";

type Props = {
  inputMode: InputMode;
  disabled: boolean;
  vimMode?: VimMode;
};

export function PromptInputModeIndicator({
  inputMode,
  disabled,
  vimMode,
}: Props): React.ReactElement {
  const prefix =
    vimMode === "NORMAL"
        ? "N"
        : ">";
  const color =
    inputMode === "command"
      ? "cyan"
      : vimMode === "NORMAL"
        ? "yellow"
        : "green";

  return (
    <Box marginRight={1}>
      <Text color={disabled ? "gray" : (color as never)} dimColor={disabled}>
        {prefix}
      </Text>
    </Box>
  );
}
