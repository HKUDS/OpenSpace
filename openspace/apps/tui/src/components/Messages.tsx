import React from "react";
import { Box, Text } from "ink";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { useRegisterKeybindingContext } from "../keybindings/KeybindingContext.js";
import type { ScrollBoxHandle } from "../ink/components/ScrollBox.js";
import type { AppMessage } from "../state/AppStateStore.js";
import { getMessageText } from "../screens/shared.js";
import { ScrollChromeContext } from "./FullscreenLayout.js";
import { ScrollKeybindingHandler } from "./ScrollKeybindingHandler.js";
import {
  MessageActionsBar,
  MessageActionsKeybindings,
  useMessageActions,
  type MessageActionsState,
  type MessageActionsNav,
} from "./messageActions.js";
import { getColor } from "./design-system/theme.js";
import type { JumpHandle } from "./VirtualMessageList.js";
import {
  buildTranscriptRows,
  estimateTranscriptRows,
  type TranscriptRow,
} from "./messages/transcriptRows.js";

const STICKY_PROMPT_CAP = 180;

type Props = {
  messages: AppMessage[];
  maxRows: number;
  scrollRef?: React.RefObject<ScrollBoxHandle | null>;
  jumpRef?: React.RefObject<JumpHandle | null>;
  trackStickyPrompt?: boolean;
  onSearchMatchesChange?: (count: number, current: number) => void;
  cursorNavRef?: React.RefObject<MessageActionsNav | null>;
  onCursorChange?: (cursor: MessageActionsState | null) => void;
  emptyLabel?: string;
  showAll?: boolean;
};

export function getMessageEstimateColumns(columns: number): number {
  return Math.max(24, columns - 2);
}

function hasVisibleMessageContent(message: AppMessage): boolean {
  if (message.meta?.hasReasoning === true) {
    return true;
  }

  if (
    message.content.some(
      block => block.type === "field" || block.type === "tool_use",
    )
  ) {
    return true;
  }

  return getMessageText(message).trim().length > 0;
}

function isRenderableMessage(message: AppMessage): boolean {
  if (message.meta?.hidden === true) {
    return false;
  }

  if (message.meta?.budget === true) {
    return false;
  }

  return hasVisibleMessageContent(message);
}

export function estimateMessagesContentHeight(
  messages: AppMessage[],
  columns: number,
  options?: {
    showAll?: boolean;
  },
): number {
  const allRenderableMessages = messages.filter(isRenderableMessage);
  const renderableMessages = allRenderableMessages;

  if (renderableMessages.length === 0) {
    return 2;
  }

  const estimateColumns = getMessageEstimateColumns(columns);
  return estimateTranscriptRows(renderableMessages, estimateColumns, {
    showAll: options?.showAll,
  });
}

function createMessageOffsets(
  rows: TranscriptRow[],
): Map<string, number> {
  const offsets = new Map<string, number>();

  rows.forEach((row, index) => {
    if (row.messageId && !offsets.has(row.messageId)) {
      offsets.set(row.messageId, index);
    }
  });

  return offsets;
}

function clampIndex(index: number, total: number): number {
  return Math.max(0, Math.min(total - 1, index));
}

function createStickyPromptText(message: AppMessage): string | null {
  if (message.role !== "user") {
    return null;
  }

  const text = getMessageText(message).trim();
  if (!text) {
    return null;
  }

  return text.length <= STICKY_PROMPT_CAP
    ? text
    : `${text.slice(0, STICKY_PROMPT_CAP - 1)}…`;
}

export function Messages({
  messages,
  maxRows,
  scrollRef,
  jumpRef,
  trackStickyPrompt = true,
  onSearchMatchesChange,
  cursorNavRef,
  onCursorChange,
  emptyLabel = "No messages yet. Type a prompt and press Enter.",
  showAll = false,
}: Props): React.ReactElement {
  const scrollChrome = React.useContext(ScrollChromeContext);
  const setStickyPrompt = scrollChrome?.setStickyPrompt;
  const terminalSize = useTerminalSize();
  const allRenderableMessages = React.useMemo(
    () => messages.filter(isRenderableMessage),
    [messages],
  );
  const renderableMessages = React.useMemo(
    () => allRenderableMessages,
    [allRenderableMessages],
  );
  const internalCursorNavRef = React.useRef<MessageActionsNav | null>(null);
  const resolvedCursorNavRef: React.RefObject<MessageActionsNav | null> =
    cursorNavRef ?? internalCursorNavRef;
  const [messageCursor, setMessageCursor] =
    React.useState<MessageActionsState | null>(null);
  const { handlers } = useMessageActions(
    messageCursor,
    setMessageCursor,
    resolvedCursorNavRef,
  );
  const [, forceScrollRefresh] = React.useReducer(
    (value: number) => value + 1,
    0,
  );
  const unsubscribeRef = React.useRef<(() => void) | null>(null);
  const subscribedHandleRef =
    React.useRef<ScrollBoxHandle | null>(null);
  const searchQueryRef = React.useRef("");
  const searchMatchesRef = React.useRef<number[]>([]);
  const currentSearchMatchRef = React.useRef(-1);

  React.useEffect(() => {
    onCursorChange?.(messageCursor);
  }, [messageCursor, onCursorChange]);

  React.useLayoutEffect(() => {
    const handle = scrollRef?.current ?? null;
    if (handle === subscribedHandleRef.current) {
      return;
    }

    unsubscribeRef.current?.();
    subscribedHandleRef.current = handle;
    unsubscribeRef.current =
      handle?.subscribe(() => {
        forceScrollRefresh();
      }) ?? null;

    return () => {
      unsubscribeRef.current?.();
      unsubscribeRef.current = null;
      subscribedHandleRef.current = null;
    };
  });

  useRegisterKeybindingContext(
    "MessageActions",
    messageCursor !== null,
  );

  const estimateColumns = getMessageEstimateColumns(terminalSize.columns);
  const expandedMessageIds = React.useMemo(() => {
    const expanded = new Set<string>();
    if (messageCursor?.expanded === true) {
      expanded.add(messageCursor.id);
    }
    return expanded;
  }, [messageCursor?.expanded, messageCursor?.id]);
  const rows = React.useMemo(
    () =>
      buildTranscriptRows(renderableMessages, estimateColumns, {
        showAll,
        expandedMessageIds,
      }),
    [estimateColumns, expandedMessageIds, renderableMessages, showAll],
  );
  const messageOffsets = React.useMemo(
    () => createMessageOffsets(rows),
    [rows],
  );
  const loweredSearchTexts = React.useMemo(
    () =>
      renderableMessages.map(message =>
        `${message.role} ${getMessageText(message)}`.toLowerCase(),
      ),
    [renderableMessages],
  );
  const scrollHandle = scrollRef?.current ?? null;
  const isStickyToBottom = scrollHandle?.isSticky() ?? true;
  const viewportRows = Math.max(1, maxRows);
  const maxScrollTop = Math.max(0, rows.length - viewportRows);
  const scrollTop = Math.max(
    0,
    Math.min(
      maxScrollTop,
      isStickyToBottom
        ? maxScrollTop
        : scrollHandle?.getScrollTop() ?? 0,
    ),
  );
  const visibleRows = rows.slice(scrollTop, scrollTop + viewportRows);
  const firstVisibleMessageIndex =
    visibleRows.find(
      (row): row is TranscriptRow & { messageIndex: number } =>
        typeof row.messageIndex === "number",
    )?.messageIndex ?? null;
  const leadingBlankRows =
    rows.length < viewportRows ? viewportRows - rows.length : 0;
  const emptyBlankRows =
    renderableMessages.length === 0 ? Math.max(0, viewportRows - 1) : 0;

  const scrollToIndex = React.useCallback(
    (index: number) => {
      const handle = scrollRef?.current;
      if (!handle || renderableMessages.length === 0) {
        return;
      }

      const boundedIndex = clampIndex(index, renderableMessages.length);
      const message = renderableMessages[boundedIndex];
      if (!message) {
        return;
      }

      handle.scrollTo(Math.max(0, (messageOffsets.get(message.id) ?? 0) - 1));
    },
    [messageOffsets, renderableMessages, scrollRef],
  );

  const selectIndex = React.useCallback(
    (index: number, preserveExpanded = false) => {
      if (renderableMessages.length === 0) {
        setMessageCursor(null);
        return;
      }

      const boundedIndex = clampIndex(index, renderableMessages.length);
      const message = renderableMessages[boundedIndex];
      if (!message) {
        setMessageCursor(null);
        return;
      }

      setMessageCursor(prev => ({
        id: message.id,
        expanded:
          preserveExpanded && prev?.id === message.id ? prev.expanded : false,
      }));
    },
    [renderableMessages],
  );

  React.useEffect(() => {
    if (!messageCursor) {
      return;
    }

    if (!renderableMessages.some(message => message.id === messageCursor.id)) {
      setMessageCursor(null);
    }
  }, [messageCursor, renderableMessages]);

  React.useEffect(() => {
    if (!setStickyPrompt) {
      return;
    }

    if (
      !trackStickyPrompt ||
      isStickyToBottom ||
      scrollTop <= 0 ||
      firstVisibleMessageIndex === null
    ) {
      setStickyPrompt(null);
      return;
    }

    for (
      let index = Math.min(
        firstVisibleMessageIndex,
        renderableMessages.length - 1,
      );
      index >= 0;
      index -= 1
    ) {
      const message = renderableMessages[index];
      if (!message) {
        continue;
      }

      const promptText = createStickyPromptText(message);
      if (!promptText) {
        continue;
      }

      setStickyPrompt({
        sourceId: message.id,
        text: promptText,
        scrollTo: () => {
          scrollToIndex(index);
          selectIndex(index, true);
        },
      });
      return;
    }

    setStickyPrompt(null);
  }, [
    firstVisibleMessageIndex,
    renderableMessages,
    isStickyToBottom,
    setStickyPrompt,
    scrollToIndex,
    scrollTop,
    selectIndex,
    trackStickyPrompt,
  ]);

  const recomputeSearchMatches = React.useCallback(
    (query: string) => {
      const normalized = query.trim().toLowerCase();
      searchQueryRef.current = normalized;

      if (!normalized) {
        searchMatchesRef.current = [];
        currentSearchMatchRef.current = -1;
        onSearchMatchesChange?.(0, 0);
        return;
      }

      const matches: number[] = [];
      loweredSearchTexts.forEach((text, index) => {
        if (text.includes(normalized)) {
          matches.push(index);
        }
      });

      searchMatchesRef.current = matches;
      currentSearchMatchRef.current = matches.length > 0 ? 0 : -1;
      onSearchMatchesChange?.(
        matches.length,
        matches.length > 0 ? 1 : 0,
      );

      if (matches.length > 0) {
        const matchIndex = matches[0]!;
        scrollToIndex(matchIndex);
        selectIndex(matchIndex, true);
      }
    },
    [loweredSearchTexts, onSearchMatchesChange, scrollToIndex, selectIndex],
  );

  React.useImperativeHandle(
    jumpRef,
    () => ({
      jumpToIndex(index: number) {
        scrollToIndex(index);
        selectIndex(index, true);
      },
      jumpToMessageId(id: string) {
        const index = renderableMessages.findIndex(message => message.id === id);
        if (index < 0) {
          return;
        }
        scrollToIndex(index);
        selectIndex(index, true);
      },
      setSearchQuery(query: string) {
        recomputeSearchMatches(query);
      },
      nextMatch() {
        const matches = searchMatchesRef.current;
        if (matches.length === 0) {
          return;
        }

        currentSearchMatchRef.current =
          (currentSearchMatchRef.current + 1) % matches.length;
        const matchIndex =
          matches[currentSearchMatchRef.current] ?? matches[0]!;
        onSearchMatchesChange?.(
          matches.length,
          currentSearchMatchRef.current + 1,
        );
        scrollToIndex(matchIndex);
        selectIndex(matchIndex, true);
      },
      prevMatch() {
        const matches = searchMatchesRef.current;
        if (matches.length === 0) {
          return;
        }

        currentSearchMatchRef.current =
          (currentSearchMatchRef.current - 1 + matches.length) %
          matches.length;
        const matchIndex =
          matches[currentSearchMatchRef.current] ??
          matches[matches.length - 1]!;
        onSearchMatchesChange?.(
          matches.length,
          currentSearchMatchRef.current + 1,
        );
        scrollToIndex(matchIndex);
        selectIndex(matchIndex, true);
      },
      setAnchor() {
      },
      async warmSearchIndex() {
        const start = performance.now();
        loweredSearchTexts.length;
        return Math.round(performance.now() - start);
      },
      disarmSearch() {
        currentSearchMatchRef.current = -1;
        if (searchQueryRef.current.length > 0) {
          onSearchMatchesChange?.(
            searchMatchesRef.current.length,
            0,
          );
        }
      },
    }),
    [
      jumpRef,
      loweredSearchTexts.length,
      onSearchMatchesChange,
      recomputeSearchMatches,
      renderableMessages,
      scrollToIndex,
      selectIndex,
    ],
  );

  React.useImperativeHandle(
    resolvedCursorNavRef,
    () => ({
      enterCursor() {
        const visibleMessageIndexes = new Set(
          visibleRows
            .map(row => row.messageIndex)
            .filter((index): index is number => typeof index === "number"),
        );
        const candidateIndexes =
          visibleMessageIndexes.size > 0
            ? [
                ...Array.from(visibleMessageIndexes).sort((a, b) => b - a),
                ...Array.from(
                  { length: renderableMessages.length },
                  (_, offset) => renderableMessages.length - offset - 1,
                ),
              ]
            : Array.from(
                { length: renderableMessages.length },
                (_, offset) => renderableMessages.length - offset - 1,
              );

        for (const index of candidateIndexes) {
          if (!renderableMessages[index]) {
            continue;
          }

          selectIndex(index, true);
          scrollToIndex(index);
          return;
        }
      },
      navigatePrev() {
        const currentIndex = messageCursor
          ? renderableMessages.findIndex(message => message.id === messageCursor.id)
          : renderableMessages.length;

        for (let index = currentIndex - 1; index >= 0; index -= 1) {
          if (!renderableMessages[index]) {
            continue;
          }

          selectIndex(index);
          scrollToIndex(index);
          return;
        }
      },
      navigateNext() {
        const currentIndex = messageCursor
          ? renderableMessages.findIndex(message => message.id === messageCursor.id)
          : -1;

        for (let index = currentIndex + 1; index < renderableMessages.length; index += 1) {
          if (!renderableMessages[index]) {
            continue;
          }

          selectIndex(index);
          scrollToIndex(index);
          return;
        }
      },
      navigateTop() {
        if (renderableMessages.length === 0) {
          return;
        }

        selectIndex(0);
        scrollToIndex(0);
      },
      navigateBottom() {
        if (renderableMessages.length === 0) {
          return;
        }

        const lastIndex = renderableMessages.length - 1;
        selectIndex(lastIndex);
        scrollToIndex(lastIndex);
      },
      getSelected() {
        return messageCursor
          ? renderableMessages.find(message => message.id === messageCursor.id) ?? null
          : null;
      },
    }),
    [
      messageCursor,
      renderableMessages,
      resolvedCursorNavRef,
      scrollToIndex,
      selectIndex,
      visibleRows,
    ],
  );

  return (
    <Box flexDirection="column">
      <ScrollKeybindingHandler
        scrollRef={scrollRef}
        isActive={true}
      />
      <MessageActionsKeybindings
        handlers={handlers}
        isActive={messageCursor !== null}
      />
      <Box flexDirection="column">
        {renderableMessages.length === 0 ? (
          <>
            {Array.from({ length: emptyBlankRows }, (_, index) => (
              <Text key={`empty-blank:${index}`}> </Text>
            ))}
            <Text color={getColor("textDim")}>
              {emptyLabel}
            </Text>
          </>
        ) : (
          <>
            {Array.from({ length: leadingBlankRows }, (_, index) => (
              <Text key={`blank:${index}`}> </Text>
            ))}
            {visibleRows.map(row => {
              const selected = row.messageId === messageCursor?.id;
              return (
                <Text
                  key={row.key}
                  color={getColor(row.colorToken) as never}
                  bold={row.bold}
                  dimColor={row.dim}
                  backgroundColor={selected ? (getColor("bgHighlight") as never) : undefined}
                >
                  {row.text.length > 0 ? row.text : " "}
                </Text>
              );
            })}
          </>
        )}
      </Box>
      {messageCursor ? (
        <MessageActionsBar cursor={messageCursor} />
      ) : null}
    </Box>
  );
}
