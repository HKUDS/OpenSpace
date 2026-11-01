import React from "react";
import { Box, Text } from "ink";
import type { PromptRequestData } from "../../bridge/protocol.js";
import { useRegisterOverlay } from "../../context/overlayContext.js";

type Draft = {
  request: PromptRequestData;
  value: string;
  error: string | null;
};

type Props = {
  draft: Draft;
};

export function PromptDialog({ draft }: Props): React.ReactElement {
  useRegisterOverlay("prompt-dialog");

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="cyan"
      paddingX={1}
      marginTop={1}
    >
      <Text bold color="cyan">
        {draft.request.title || "Input Required"}
      </Text>
      {draft.request.description ? (
        <Text>{draft.request.description}</Text>
      ) : null}
      {draft.request.placeholder ? (
        <Text color="gray">Placeholder: {draft.request.placeholder}</Text>
      ) : null}

      <Box marginTop={1} flexDirection="column">
        <Text color="gray">Value</Text>
        <Text>{draft.value || "<empty>"}</Text>
      </Box>

      {draft.error ? (
        <Box marginTop={1}>
          <Text color="red">{draft.error}</Text>
        </Box>
      ) : null}

      <Box marginTop={1}>
        <Text color="gray">
          Type to edit | Enter submit | Esc cancel | Backspace delete
        </Text>
      </Box>
    </Box>
  );
}
