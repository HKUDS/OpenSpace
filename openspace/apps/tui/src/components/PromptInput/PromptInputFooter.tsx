import React from "react";
import { Box, Text } from "ink";
import { useShortcutDisplay } from "../../keybindings/useShortcutDisplay.js";
import type { InputMode } from "../../state/AppStateStore.js";
import type { SandboxStatusData } from "../../bridge/protocol.js";
import type { VimMode } from "../../types/textInputTypes.js";
import { sandboxHint } from "../../utils/sandboxPromptFooter.js";

type Props = {
  disabled: boolean;
  disabledReason?: string;
  busy?: boolean;
  inputMode: InputMode;
  vimMode?: VimMode;
  suggestionsVisible: boolean;
  sandbox?: SandboxStatusData;
};

export default function PromptInputFooter({
  disabled,
  disabledReason,
  busy = false,
  inputMode,
  vimMode,
  suggestionsVisible,
  sandbox,
}: Props): React.ReactElement {
  const submitShortcut = useShortcutDisplay("chat:submit", "Chat", "Enter");
  const newlineShortcut = useShortcutDisplay("chat:newline", "Chat", "shift+Enter");
  const cancelShortcut = useShortcutDisplay("app:interrupt", "Global", "ctrl+c");
  const autocompleteAcceptShortcut = useShortcutDisplay(
    "autocomplete:accept",
    "Autocomplete",
    "Tab",
  );
  const autocompletePrevShortcut = useShortcutDisplay(
    "autocomplete:previous",
    "Autocomplete",
    "Up",
  );
  const autocompleteNextShortcut = useShortcutDisplay(
    "autocomplete:next",
    "Autocomplete",
    "Down",
  );
  const dismissShortcut = useShortcutDisplay(
    "chat:cancel",
    "Chat",
    "Esc",
  );

  let hint = disabled
    ? disabledReason ?? "Input is temporarily unavailable"
    : busy
      ? `Task running | ${cancelShortcut} cancel`
      : `${submitShortcut} send | ${newlineShortcut} newline | ${cancelShortcut} cancel`;

  if (!disabled && busy && inputMode === "command") {
    hint = suggestionsVisible
      ? `${autocompleteAcceptShortcut} complete | ${autocompletePrevShortcut}/${autocompleteNextShortcut} select | ${cancelShortcut} cancel`
      : `Task running | ${autocompleteAcceptShortcut} complete | ${cancelShortcut} cancel`;
  } else if (!disabled && inputMode === "command") {
    hint = suggestionsVisible
      ? `${autocompleteAcceptShortcut} complete | ${autocompletePrevShortcut}/${autocompleteNextShortcut} select | ${submitShortcut} run`
      : `${submitShortcut} run | ${autocompleteAcceptShortcut} complete | ${dismissShortcut} clear`;
  }
  const sandboxStatus = sandboxHint(sandbox);

  const vimStatus = vimMode ? `vim ${vimMode}` : null;

  return (
    <Box marginTop={1} width="100%" height={2} flexDirection="column">
      <Text color="gray" wrap="truncate">{hint}</Text>
      {sandboxStatus || vimStatus ? (
        <Text wrap="truncate">
          {sandboxStatus ? (
            <Text color={sandboxStatus.color}>{sandboxStatus.text}</Text>
          ) : null}
          {sandboxStatus && vimStatus ? <Text color="gray"> | </Text> : null}
          {vimStatus ? <Text color="gray">{vimStatus}</Text> : null}
        </Text>
      ) : (
        <Text> </Text>
      )}
    </Box>
  );
}
