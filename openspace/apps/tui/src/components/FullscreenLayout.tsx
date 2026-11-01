import React from "react";
import { Box, Text } from "ink";
import { QueuedMessageProvider } from "../context/QueuedMessageContext.js";
import { useRecordFpsFrame } from "../context/fpsMetrics.js";
import { ModalContext } from "../context/modalContext.js";
import {
  PromptOverlayProvider,
  usePromptOverlay,
  usePromptOverlayDialog,
} from "../context/promptOverlayContext.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import ScrollBox, {
  type ScrollBoxHandle,
} from "../ink/components/ScrollBox.js";
import { SlashCommandComplete } from "./PromptInput/SlashCommandComplete.js";
import { truncateToDisplayWidth } from "../utils/textWidth.js";

export type StickyPrompt = {
  sourceId?: string;
  text: string;
  scrollTo?: () => void;
};

export const ScrollChromeContext = React.createContext<{
  setStickyPrompt: (prompt: StickyPrompt | null) => void;
} | null>(null);

type Props = {
  messages: React.ReactNode;
  afterMessages?: React.ReactNode;
  bottom: React.ReactNode;
  overlay?: React.ReactNode;
  bottomFloat?: React.ReactNode;
  modal?: React.ReactNode;
  modalScrollRef?: React.RefObject<ScrollBoxHandle | null>;
  scrollRef?: React.RefObject<ScrollBoxHandle | null>;
  contentHeight?: number;
  scrollRows?: number;
};

function estimatePromptOverlayRows(
  suggestionsCount: number,
): number {
  if (suggestionsCount === 0) {
    return 0;
  }

  return Math.min(10, suggestionsCount + 2);
}

function truncateStickyPrompt(text: string, columns: number): string {
  const safeWidth = Math.max(12, columns - 6);
  return truncateToDisplayWidth(text, safeWidth);
}

function FullscreenLayoutBody({
  messages,
  afterMessages,
  bottom,
  overlay,
  bottomFloat,
  modal,
  modalScrollRef,
  scrollRef,
  contentHeight,
  scrollRows: scrollRowsProp,
}: Props): React.ReactElement {
  const size = useTerminalSize();
  const promptOverlay = usePromptOverlay();
  const promptOverlayDialog = usePromptOverlayDialog();
  const recordFrame = useRecordFpsFrame();
  const lastCommitRef = React.useRef<number | null>(null);
  const internalModalScrollRef = React.useRef<ScrollBoxHandle | null>(null);
  const resolvedModalScrollRef = modalScrollRef ?? internalModalScrollRef;
  const [stickyPrompt, setStickyPrompt] = React.useState<StickyPrompt | null>(
    null,
  );
  const updateStickyPrompt = React.useCallback(
    (nextPrompt: StickyPrompt | null) => {
      setStickyPrompt(current => {
        if (current === null && nextPrompt === null) {
          return current;
        }

        if (
          current !== null &&
          nextPrompt !== null &&
          current.sourceId === nextPrompt.sourceId &&
          current.text === nextPrompt.text
        ) {
          return current;
        }

        return nextPrompt;
      });
    },
    [],
  );
  const scrollChromeValue = React.useMemo(
    () => ({ setStickyPrompt: updateStickyPrompt }),
    [updateStickyPrompt],
  );

  React.useLayoutEffect(() => {
    const now = performance.now();
    if (lastCommitRef.current !== null) {
      recordFrame?.(now - lastCommitRef.current);
    }
    lastCommitRef.current = now;
  });

  const promptOverlayRows = promptOverlayDialog
    ? Math.max(6, Math.floor(size.rows * 0.25))
    : estimatePromptOverlayRows(promptOverlay?.suggestions.length ?? 0);
  const modalRows = modal ? Math.max(8, Math.floor(size.rows * 0.35)) : 0;
  const scrollRows = scrollRowsProp ?? Math.max(
    6,
    size.rows - 8 - promptOverlayRows - modalRows,
  );
  const maxOverlayRows = Math.max(4, size.rows - 12);
  const suggestionCount = promptOverlay?.suggestions.length ?? 0;
  const maxOverlayItems = Math.max(
    1,
    Math.min(
      suggestionCount,
      suggestionCount > maxOverlayRows
        ? Math.max(1, maxOverlayRows - 1)
        : maxOverlayRows,
    ),
  );
  const stickyPromptHeader =
    stickyPrompt && !overlay ? (
      <Box
        flexShrink={0}
        marginTop={1}
        borderStyle="round"
        borderColor="gray"
        borderLeft={false}
        borderRight={false}
        paddingX={1}
        width="100%"
      >
        <Text color="gray">
          ↟ {truncateStickyPrompt(stickyPrompt.text, size.columns)}
        </Text>
      </Box>
    ) : null;

  const surfaceContent = (
    <ScrollChromeContext.Provider value={scrollChromeValue}>
      <Box flexDirection="column">
        {messages}
        {afterMessages ? <Box flexDirection="column">{afterMessages}</Box> : null}
        {overlay ? (
          <Box marginTop={1} width="100%" overflowX="hidden">
            <QueuedMessageProvider isFirst={true}>
              {overlay}
            </QueuedMessageProvider>
          </Box>
        ) : null}
        {bottomFloat ? <Box marginTop={1}>{bottomFloat}</Box> : null}
      </Box>
    </ScrollChromeContext.Provider>
  );

  return (
    <Box flexDirection="column" flexGrow={1} width="100%">
      {stickyPromptHeader}
      <ScrollBox
        ref={scrollRef}
        flexGrow={1}
        height={scrollRows}
        contentHeight={contentHeight}
        stickyScroll={true}
        overflowY="hidden"
      >
        {surfaceContent}
      </ScrollBox>

      {modal ? (
        <Box marginTop={1} flexDirection="column">
          <Text color="gray">▔</Text>
          <ModalContext.Provider
            value={{
              rows: Math.max(3, modalRows - 2),
              columns: Math.max(24, size.columns - 4),
              scrollRef: resolvedModalScrollRef,
            }}
          >
            {/*
              Transitional: OpenSpace paints this in an absolute modal slot over
              the fullscreen scroll region. Upstream Ink in OpenSpace still lacks
              the same slotting primitives, so this keeps the modal anchored in a
              dedicated section while preserving the modal context contract.
            */}
            <ScrollBox
              ref={resolvedModalScrollRef}
              height={modalRows}
              borderStyle="round"
              borderColor="gray"
              paddingX={1}
            >
              {modal}
            </ScrollBox>
          </ModalContext.Provider>
        </Box>
      ) : null}

      <Box flexDirection="column" flexShrink={0} width="100%" overflowY="hidden">
        {promptOverlayDialog ? (
          <Box width="100%">
            {promptOverlayDialog}
          </Box>
        ) : promptOverlay?.suggestions.length ? (
          <Box paddingX={2} width="100%">
            <SlashCommandComplete
              items={promptOverlay.suggestions}
              selectedIndex={promptOverlay.selectedSuggestion}
              visible={promptOverlay.suggestions.length > 0}
              maxVisibleItems={maxOverlayItems}
              maxColumnWidth={promptOverlay.maxColumnWidth}
              bordered={false}
            />
          </Box>
        ) : null}
        {bottom}
      </Box>
    </Box>
  );
}

export function FullscreenLayout(props: Props): React.ReactElement {
  return (
    <PromptOverlayProvider>
      <FullscreenLayoutBody {...props} />
    </PromptOverlayProvider>
  );
}
