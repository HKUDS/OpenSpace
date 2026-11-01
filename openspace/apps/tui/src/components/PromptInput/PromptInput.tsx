import React from "react";
import { Box, Text } from "ink";
import { useRegisterOverlay } from "../../context/overlayContext.js";
import { useSetPromptOverlay } from "../../context/promptOverlayContext.js";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import type { InputMode } from "../../state/AppStateStore.js";
import type { SandboxStatusData } from "../../bridge/protocol.js";
import type { VimMode } from "../../types/textInputTypes.js";
import type { CompletionItem } from "./SlashCommandComplete.js";
import { SlashCommandComplete } from "./SlashCommandComplete.js";
import PromptInputFooter from "./PromptInputFooter.js";
import { PromptInputModeIndicator } from "./PromptInputModeIndicator.js";
import { estimateWrappedRows } from "../../utils/textWidth.js";

type Props = {
  value: string;
  disabled: boolean;
  disabledReason?: string;
  busy?: boolean;
  inputMode: InputMode;
  vimMode?: VimMode;
  cursorOffset?: number;
  placeholder?: string;
  suggestions?: CompletionItem[];
  selectedSuggestion?: number;
  showSuggestions?: boolean;
  renderSuggestionsInline?: boolean;
  publishSuggestionsOverlay?: boolean;
  maxInputRows?: number;
  sandbox?: SandboxStatusData;
};

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function renderWithCursor(
  value: string,
  cursorOffset: number,
  disabled: boolean,
  maxVisibleChars?: number,
): React.ReactElement {
  const visibleWindow = createVisibleInputWindow(
    value,
    cursorOffset,
    maxVisibleChars,
  );
  const boundedOffset = clamp(
    visibleWindow.cursorOffset,
    0,
    visibleWindow.value.length,
  );
  const before = visibleWindow.value.slice(0, boundedOffset);
  const cursor = visibleWindow.value[boundedOffset] ?? " ";
  const after =
    boundedOffset < visibleWindow.value.length
      ? visibleWindow.value.slice(boundedOffset + 1)
      : "";

  return (
    <Text>
      <Text>{before}</Text>
      <Text inverse={!disabled}>{cursor}</Text>
      <Text>{after}</Text>
    </Text>
  );
}

function createVisibleInputWindow(
  value: string,
  cursorOffset: number,
  maxVisibleChars: number | undefined,
): {
  value: string;
  cursorOffset: number;
} {
  if (!maxVisibleChars || value.length <= maxVisibleChars) {
    return {
      value,
      cursorOffset,
    };
  }

  const boundedOffset = clamp(cursorOffset, 0, value.length);
  const windowSize = Math.max(10, maxVisibleChars - 6);
  const start = clamp(
    boundedOffset - Math.floor(windowSize * 0.75),
    0,
    Math.max(0, value.length - windowSize),
  );
  const end = Math.min(value.length, start + windowSize);
  const prefix = start > 0 ? "..." : "";
  const suffix = end < value.length ? "..." : "";
  return {
    value: `${prefix}${value.slice(start, end)}${suffix}`,
    cursorOffset: prefix.length + boundedOffset - start,
  };
}

export default function PromptInput({
  value,
  disabled,
  disabledReason,
  busy = false,
  inputMode,
  vimMode,
  cursorOffset = value.length,
  placeholder,
  suggestions = [],
  selectedSuggestion = 0,
  showSuggestions = false,
  renderSuggestionsInline = true,
  publishSuggestionsOverlay = true,
  maxInputRows = 4,
  sandbox,
}: Props): React.ReactElement {
  const terminalSize = useTerminalSize();
  const color =
    inputMode === "command"
      ? "cyan"
      : vimMode === "NORMAL"
        ? "yellow"
        : "green";
  const shouldRenderOverlay =
    publishSuggestionsOverlay &&
    showSuggestions &&
    !renderSuggestionsInline &&
    suggestions.length > 0;
  const emptyPlaceholder =
    placeholder ??
    (disabled
      ? disabledReason ?? "Input is temporarily unavailable"
      : "Type a prompt and press Enter");
  const inputColumns = Math.max(10, terminalSize.columns - 8);
  const inputRows = value
    ? Math.max(
        1,
        Math.min(maxInputRows, estimateWrappedRows(value, inputColumns)),
      )
    : 1;
  const maxVisibleInputChars = value
    ? Math.max(20, inputColumns * maxInputRows - 6)
    : undefined;

  useRegisterOverlay("autocomplete", shouldRenderOverlay);
  useSetPromptOverlay(
    shouldRenderOverlay
      ? {
          suggestions,
          selectedSuggestion,
          maxColumnWidth: Math.max(24, Math.floor(terminalSize.columns * 0.7)),
        }
      : null,
  );

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={disabled ? "gray" : (color as never)}
      borderLeft={false}
      borderRight={false}
      borderBottom={false}
      paddingX={1}
      marginTop={1}
      width="100%"
      flexShrink={0}
    >
      <Box alignItems="flex-start" width="100%">
        <PromptInputModeIndicator
          inputMode={inputMode}
          disabled={disabled}
          vimMode={vimMode}
        />
        <Box
          flexDirection="column"
          flexGrow={1}
          flexShrink={1}
          height={inputRows}
          overflowY="hidden"
        >
          {value ? (
            renderWithCursor(
              value,
              cursorOffset,
              disabled,
              maxVisibleInputChars,
            )
          ) : (
            <Text color="gray" wrap="truncate">{emptyPlaceholder}</Text>
          )}
        </Box>
      </Box>

      {showSuggestions && renderSuggestionsInline ? (
        <Box marginTop={1}>
          <SlashCommandComplete
            items={suggestions}
            selectedIndex={selectedSuggestion}
            visible={showSuggestions}
            bordered={false}
          />
        </Box>
      ) : null}

      <PromptInputFooter
        disabled={disabled}
        disabledReason={disabledReason}
        busy={busy}
        inputMode={inputMode}
        vimMode={vimMode}
        suggestionsVisible={showSuggestions}
        sandbox={sandbox}
      />
    </Box>
  );
}
