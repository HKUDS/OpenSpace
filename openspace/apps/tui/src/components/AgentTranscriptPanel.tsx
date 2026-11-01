import React from "react";
import { Box, Text } from "ink";
import type { JumpHandle } from "./VirtualMessageList.js";
import type { MessageActionsNav, MessageActionsState } from "./messageActions.js";
import type { AppMessage } from "../state/AppStateStore.js";
import { estimateMessagesContentHeight, Messages } from "./Messages.js";
import ScrollBox, { type ScrollBoxHandle } from "../ink/components/ScrollBox.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { getColor } from "./design-system/theme.js";
import { TranscriptSearchBar } from "./transcript/TranscriptSearchBar.js";

export type AgentTranscriptHandle = JumpHandle & {
  enterCursor: () => void;
  navigatePrev: () => void;
  navigateNext: () => void;
  navigateTop: () => void;
  navigateBottom: () => void;
  scrollToBottom: () => void;
};

type Props = {
  messages: AppMessage[];
  agentLabel?: string;
  title?: string;
  emptyLabel?: string;
  maxRows?: number;
  selectedMessageId?: string | null;
  cursor?: number | null;
  actionHints?: string[];
  searchVisible?: boolean;
  searchQuery?: string;
  searchMatchCount?: number;
  searchCurrentMatch?: number;
  onSearchMatchesChange?: (count: number, current: number) => void;
  onCursorChange?: (messageId: string | null, index: number | null) => void;
};

function normalizeIndex(
  index: number | null | undefined,
  length: number,
): number | null {
  if (length === 0 || index === null || index === undefined || Number.isNaN(index)) {
    return null;
  }

  return Math.max(0, Math.min(length - 1, index));
}

export const AgentTranscriptPanel = React.forwardRef<
  AgentTranscriptHandle | null,
  Props
>(function AgentTranscriptPanel(
  {
    messages,
    agentLabel = "Viewed agent",
    title = "Agent Transcript",
    emptyLabel = "No transcript available",
    maxRows,
    selectedMessageId = null,
    cursor = null,
    actionHints = [],
    searchVisible = false,
    searchQuery = "",
    searchMatchCount = 0,
    searchCurrentMatch = 0,
    onSearchMatchesChange,
    onCursorChange,
  }: Props,
  ref,
): React.ReactElement {
  const terminalSize = useTerminalSize();
  const scrollRef = React.useRef<ScrollBoxHandle | null>(null);
  const jumpRef = React.useRef<JumpHandle | null>(null);
  const cursorNavRef = React.useRef<MessageActionsNav | null>(null);
  const lastSyncedSelectionRef = React.useRef<string | null>(null);

  const maxScrollableRows =
    maxRows ?? Math.max(8, Math.min(18, Math.floor(terminalSize.rows * 0.3)));
  const contentHeight = estimateMessagesContentHeight(
    messages,
    Math.max(24, terminalSize.columns - 12),
  );

  const handleCursorChange = React.useCallback(
    (cursorState: MessageActionsState | null): void => {
      const messageId = cursorState?.id ?? null;
      if (messageId === null) {
        lastSyncedSelectionRef.current = null;
        onCursorChange?.(null, null);
        return;
      }

      const index = messages.findIndex(message => message.id === messageId);
      lastSyncedSelectionRef.current = messageId;
      onCursorChange?.(messageId, index >= 0 ? index : null);
    },
    [messages, onCursorChange],
  );

  React.useEffect(() => {
    if (!jumpRef.current) {
      return;
    }

    if (selectedMessageId) {
      if (lastSyncedSelectionRef.current === selectedMessageId) {
        return;
      }
      const index = messages.findIndex(message => message.id === selectedMessageId);
      if (index >= 0) {
        lastSyncedSelectionRef.current = selectedMessageId;
        jumpRef.current.jumpToIndex(index);
      }
      return;
    }

    const normalizedCursor = normalizeIndex(cursor, messages.length);
    if (normalizedCursor !== null) {
      const messageId = messages[normalizedCursor]?.id ?? null;
      if (messageId !== null && lastSyncedSelectionRef.current !== messageId) {
        lastSyncedSelectionRef.current = messageId;
        jumpRef.current.jumpToIndex(normalizedCursor);
      }
    }
  }, [cursor, messages, selectedMessageId]);

  React.useImperativeHandle(
    ref,
    () => ({
      jumpToIndex(index: number) {
        jumpRef.current?.jumpToIndex(index);
      },
      setSearchQuery(query: string) {
        jumpRef.current?.setSearchQuery(query);
      },
      nextMatch() {
        jumpRef.current?.nextMatch();
      },
      prevMatch() {
        jumpRef.current?.prevMatch();
      },
      setAnchor() {
        jumpRef.current?.setAnchor();
      },
      warmSearchIndex() {
        return jumpRef.current?.warmSearchIndex() ?? Promise.resolve(0);
      },
      disarmSearch() {
        jumpRef.current?.disarmSearch();
      },
      enterCursor() {
        cursorNavRef.current?.enterCursor();
      },
      navigatePrev() {
        cursorNavRef.current?.navigatePrev();
      },
      navigateNext() {
        cursorNavRef.current?.navigateNext();
      },
      navigateTop() {
        cursorNavRef.current?.navigateTop();
      },
      navigateBottom() {
        cursorNavRef.current?.navigateBottom();
      },
      scrollToBottom() {
        scrollRef.current?.scrollToBottom();
      },
    }),
    [],
  );

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
        {agentLabel}
      </Text>
      {actionHints.length > 0 ? (
        <Text color={getColor("textDim")}>
          {actionHints.join(" | ")}
        </Text>
      ) : null}
      {searchVisible ? (
        <TranscriptSearchBar
          query={searchQuery}
          matchCount={searchMatchCount}
          currentMatch={searchCurrentMatch}
        />
      ) : null}
      <Box marginTop={1}>
        {/*
          Transitional: this embeds the existing primary transcript message surface
          inside a nested ScrollBox so the agent pane can reuse `Messages`,
          `VirtualMessageList`, `messageActions`, and jump/search infrastructure
          before the app moves to a single shared multi-pane scroll coordinator.
        */}
        <ScrollBox
          ref={scrollRef}
          height={maxScrollableRows}
          contentHeight={Math.max(maxScrollableRows, contentHeight)}
          borderStyle="round"
          borderColor="gray"
          paddingX={1}
        >
          <Messages
            messages={messages}
            maxRows={maxScrollableRows}
            scrollRef={scrollRef}
            jumpRef={jumpRef}
            cursorNavRef={cursorNavRef}
            onCursorChange={handleCursorChange}
            onSearchMatchesChange={onSearchMatchesChange}
            trackStickyPrompt={false}
            emptyLabel={emptyLabel}
          />
        </ScrollBox>
      </Box>
    </Box>
  );
});
