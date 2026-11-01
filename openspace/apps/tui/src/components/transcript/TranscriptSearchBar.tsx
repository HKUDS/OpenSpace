import React from "react";
import { Box, Text } from "ink";
import { useShortcutDisplay } from "../../keybindings/useShortcutDisplay.js";

type Props = {
  query: string;
  matchCount: number;
  currentMatch: number;
};

export function TranscriptSearchBar({
  query,
  matchCount,
  currentMatch,
}: Props): React.ReactElement {
  const nextShortcut = useShortcutDisplay(
    "transcript:searchNext",
    "Transcript",
    "n",
  );
  const prevShortcut = useShortcutDisplay(
    "transcript:searchPrev",
    "Transcript",
    "p",
  );

  const status =
    matchCount > 0
      ? `${currentMatch}/${matchCount} matches`
      : query.trim().length > 0
        ? "No matches"
        : "Type to search";

  return (
    <Box
      marginTop={1}
      borderStyle="round"
      borderColor="cyan"
      paddingX={1}
      flexDirection="column"
    >
      <Text bold color="cyan">
        Transcript Search
      </Text>
      <Text color={query ? "white" : "gray"}>
        {query || "Type a search query"}
      </Text>
      <Text color="gray">
        {status}
      </Text>
      <Text color="gray">
        Enter confirm | Esc close | {nextShortcut}/{prevShortcut} move
      </Text>
    </Box>
  );
}
