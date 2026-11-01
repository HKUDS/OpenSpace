import React from "react";
import { Box } from "ink";
import { ScrollChromeContext } from "./FullscreenLayout.js";
import {
  InVirtualListContext,
  isNavigableMessage as defaultIsNavigableMessage,
  MessageActionsSelectedContext,
  type MessageActionsNav,
  type MessageActionsState,
} from "./messageActions.js";
import type { AppMessage } from "../state/AppStateStore.js";
import type { ScrollBoxHandle } from "../ink/components/ScrollBox.js";
import { getMessageText } from "../screens/shared.js";

const WINDOW_OVERSCAN_ROWS = 6;
const STICKY_PROMPT_CAP = 180;
const SCROLL_HEADROOM = 2;

export type JumpHandle = {
  jumpToIndex: (index: number) => void;
  jumpToMessageId?: (id: string) => void;
  setSearchQuery: (query: string) => void;
  nextMatch: () => void;
  prevMatch: () => void;
  setAnchor: () => void;
  warmSearchIndex: () => Promise<number>;
  disarmSearch: () => void;
};

type Props = {
  messages: AppMessage[];
  scrollRef?: React.RefObject<ScrollBoxHandle | null>;
  maxRows: number;
  itemKey: (message: AppMessage) => string;
  estimateItemHeight: (message: AppMessage, index: number) => number;
  renderItem: (message: AppMessage, index: number) => React.ReactNode;
  isItemNavigable?: (message: AppMessage) => boolean;
  cursor: MessageActionsState | null;
  setCursor: React.Dispatch<
    React.SetStateAction<MessageActionsState | null>
  >;
  cursorNavRef?: React.Ref<MessageActionsNav | null>;
  jumpRef?: React.Ref<JumpHandle | null>;
  extractSearchText?: (message: AppMessage) => string;
  onSearchMatchesChange?: (count: number, current: number) => void;
  trackStickyPrompt?: boolean;
};

function VerticalSpacer({
  rows,
}: {
  rows: number;
}): React.ReactElement | null {
  if (rows <= 0) {
    return null;
  }

  return <Box height={rows} flexShrink={0} />;
}

function findStartIndex(
  offsets: number[],
  heights: number[],
  topRow: number,
): number {
  let low = 0;
  let high = offsets.length - 1;
  let result = offsets.length;

  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const rowTop = offsets[middle] ?? 0;
    const rowBottom = rowTop + (heights[middle] ?? 0);

    if (rowBottom > topRow) {
      result = middle;
      high = middle - 1;
    } else {
      low = middle + 1;
    }
  }

  return result;
}

function findEndIndex(
  offsets: number[],
  heights: number[],
  bottomRow: number,
): number {
  let low = 0;
  let high = offsets.length - 1;
  let result = offsets.length;

  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const rowTop = offsets[middle] ?? 0;

    if (rowTop >= bottomRow) {
      result = middle;
      high = middle - 1;
    } else if (rowTop + (heights[middle] ?? 0) > bottomRow) {
      result = middle + 1;
      high = middle - 1;
    } else {
      low = middle + 1;
    }
  }

  return result;
}

function clampIndex(index: number, total: number): number {
  return Math.max(0, Math.min(total - 1, index));
}

function createStickyPromptText(
  message: AppMessage,
): string | null {
  if (message.role !== "user") {
    return null;
  }

  const text = getMessageText(message).trim();
  if (!text) {
    return null;
  }

  if (text.length <= STICKY_PROMPT_CAP) {
    return text;
  }

  return `${text.slice(0, STICKY_PROMPT_CAP - 1)}…`;
}

export function VirtualMessageList({
  messages,
  scrollRef,
  maxRows,
  itemKey,
  estimateItemHeight,
  renderItem,
  isItemNavigable = defaultIsNavigableMessage,
  cursor,
  setCursor,
  cursorNavRef,
  jumpRef,
  extractSearchText,
  onSearchMatchesChange,
  trackStickyPrompt = false,
}: Props): React.ReactElement {
  const scrollChrome = React.useContext(ScrollChromeContext);
  const [, forceWindowRefresh] = React.useReducer(
    (value: number) => value + 1,
    0,
  );
  const unsubscribeRef = React.useRef<(() => void) | null>(null);
  const subscribedHandleRef =
    React.useRef<ScrollBoxHandle | null>(null);
  const searchQueryRef = React.useRef("");
  const searchAnchorRef = React.useRef<number | null>(null);
  const searchMatchesRef = React.useRef<number[]>([]);
  const currentSearchMatchRef = React.useRef<number>(-1);

  React.useLayoutEffect(() => {
    const handle = scrollRef?.current ?? null;
    if (handle === subscribedHandleRef.current) {
      return;
    }

    unsubscribeRef.current?.();
    subscribedHandleRef.current = handle;
    unsubscribeRef.current =
      handle?.subscribe(() => {
        forceWindowRefresh();
      }) ?? null;

    return () => {
      unsubscribeRef.current?.();
      unsubscribeRef.current = null;
      subscribedHandleRef.current = null;
    };
  });

  const rowHeights = React.useMemo(
    () =>
      messages.map((message, index) =>
        estimateItemHeight(message, index),
      ),
    [estimateItemHeight, messages],
  );
  const rowOffsets = React.useMemo(() => {
    const offsets: number[] = [];
    let total = 0;

    for (const height of rowHeights) {
      offsets.push(total);
      total += height;
    }

    return offsets;
  }, [rowHeights]);
  const totalRows = rowHeights.reduce(
    (sum, height) => sum + height,
    0,
  );
  const scrollHandle = scrollRef?.current ?? null;
  const viewportRows =
    scrollHandle?.getViewportHeight() ??
    Math.max(1, maxRows);
  const maxScrollTop = Math.max(0, totalRows - viewportRows);
  const shouldStickToBottom = scrollHandle?.isSticky() ?? true;
  const scrollTop = Math.max(
    0,
    Math.min(
      maxScrollTop,
      shouldStickToBottom
        ? maxScrollTop
        : (scrollHandle?.getScrollTop() ?? maxScrollTop) +
            (scrollHandle?.getPendingDelta() ?? 0),
    ),
  );
  const windowTop = scrollTop;
  const windowBottom = scrollTop + viewportRows + WINDOW_OVERSCAN_ROWS;
  const startIndex = findStartIndex(rowOffsets, rowHeights, windowTop);
  const endIndex = Math.max(
    startIndex,
    findEndIndex(rowOffsets, rowHeights, windowBottom),
  );
  const visibleRows = rowHeights
    .slice(startIndex, endIndex)
    .reduce((sum, height) => sum + height, 0);
  const topSpacerRows =
    startIndex < rowOffsets.length ? (rowOffsets[startIndex] ?? 0) : totalRows;
  const bottomSpacerRows = Math.max(
    0,
    totalRows - topSpacerRows - visibleRows,
  );
  const visibleMessages = messages.slice(startIndex, endIndex);
  const loweredSearchTexts = React.useMemo(
    () =>
      messages.map(message =>
        (extractSearchText?.(message) ?? getMessageText(message)).toLowerCase(),
      ),
    [extractSearchText, messages],
  );

  const scrollToIndex = React.useCallback(
    (index: number) => {
      const handle = scrollRef?.current;
      if (!handle || messages.length === 0) {
        return;
      }

      const boundedIndex = clampIndex(index, messages.length);
      const targetTop =
        Math.max(0, (rowOffsets[boundedIndex] ?? 0) - SCROLL_HEADROOM);
      handle.scrollTo(targetTop);
    },
    [messages.length, rowOffsets, scrollRef],
  );

  const selectIndex = React.useCallback(
    (index: number, preserveExpanded = false) => {
      if (messages.length === 0) {
        setCursor(null);
        return;
      }

      const boundedIndex = clampIndex(index, messages.length);
      const message = messages[boundedIndex];
      if (!message) {
        setCursor(null);
        return;
      }

      setCursor(prev => ({
        id: message.id,
        expanded: preserveExpanded && prev?.id === message.id ? prev.expanded : false,
      }));
    },
    [messages, setCursor],
  );

  React.useEffect(() => {
    if (!cursor) {
      return;
    }

    if (!messages.some(message => message.id === cursor.id)) {
      setCursor(null);
    }
  }, [cursor, messages, setCursor]);

  React.useEffect(() => {
    if (!scrollChrome) {
      return;
    }

    if (!trackStickyPrompt) {
      scrollChrome.setStickyPrompt(null);
      return;
    }

    if (scrollTop <= 0) {
      scrollChrome.setStickyPrompt(null);
      return;
    }

    for (let index = Math.max(0, startIndex - 1); index >= 0; index -= 1) {
      const message = messages[index];
      if (!message) {
        continue;
      }

      const promptText = createStickyPromptText(message);
      if (!promptText) {
        continue;
      }

      scrollChrome.setStickyPrompt({
        text: promptText,
        scrollTo: () => {
          scrollToIndex(index);
          selectIndex(index, true);
        },
      });
      return;
    }

    scrollChrome.setStickyPrompt(null);
  }, [
    messages,
    scrollChrome,
    scrollToIndex,
    scrollTop,
    selectIndex,
    startIndex,
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
        searchAnchorRef.current =
          scrollRef?.current?.getScrollTop() ?? null;
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
      scrollRef,
      scrollToIndex,
      selectIndex,
    ],
  );

  React.useImperativeHandle(
    cursorNavRef,
    () => ({
      enterCursor() {
        const candidateIndexes =
          scrollTop > 0
            ? [
                ...Array.from(
                  { length: Math.max(0, endIndex - startIndex) },
                  (_, offset) => startIndex + offset,
                ),
                ...Array.from(
                  { length: startIndex },
                  (_, offset) => startIndex - offset - 1,
                ),
              ]
            : Array.from(
                { length: messages.length },
                (_, offset) => messages.length - offset - 1,
              );

        for (const index of candidateIndexes) {
          if (isItemNavigable(messages[index]!)) {
            selectIndex(index, true);
            scrollToIndex(index);
            return;
          }
        }
      },
      navigatePrev() {
        const currentIndex = cursor
          ? messages.findIndex(message => message.id === cursor.id)
          : messages.length;

        for (let index = currentIndex - 1; index >= 0; index -= 1) {
          if (!isItemNavigable(messages[index]!)) {
            continue;
          }

          selectIndex(index);
          scrollToIndex(index);
          return;
        }
      },
      navigateNext() {
        const currentIndex = cursor
          ? messages.findIndex(message => message.id === cursor.id)
          : -1;

        for (let index = currentIndex + 1; index < messages.length; index += 1) {
          if (!isItemNavigable(messages[index]!)) {
            continue;
          }

          selectIndex(index);
          scrollToIndex(index);
          return;
        }
      },
      navigateTop() {
        for (let index = 0; index < messages.length; index += 1) {
          if (!isItemNavigable(messages[index]!)) {
            continue;
          }

          selectIndex(index);
          scrollToIndex(index);
          return;
        }
      },
      navigateBottom() {
        for (let index = messages.length - 1; index >= 0; index -= 1) {
          if (!isItemNavigable(messages[index]!)) {
            continue;
          }

          selectIndex(index);
          scrollToIndex(index);
          return;
        }
      },
      getSelected() {
        return cursor
          ? messages.find(message => message.id === cursor.id) ?? null
          : null;
      },
    }),
    [
      cursor,
      cursorNavRef,
      endIndex,
      isItemNavigable,
      messages,
      scrollTop,
      scrollToIndex,
      selectIndex,
      startIndex,
    ],
  );

  return (
    <InVirtualListContext.Provider value={true}>
      <Box flexDirection="column">
        <VerticalSpacer rows={topSpacerRows} />
        {visibleMessages.map((message, index) => {
          const absoluteIndex = startIndex + index;
          const isSelected = cursor?.id === message.id;
          return (
            <MessageActionsSelectedContext.Provider
              key={itemKey(message)}
              value={Boolean(isSelected)}
            >
              {renderItem(message, absoluteIndex)}
            </MessageActionsSelectedContext.Provider>
          );
        })}
        <VerticalSpacer rows={bottomSpacerRows} />
      </Box>
    </InVirtualListContext.Provider>
  );
}
