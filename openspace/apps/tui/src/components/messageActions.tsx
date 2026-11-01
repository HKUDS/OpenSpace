import React from "react";
import { Box, Text } from "ink";
import { useKeybindings } from "../keybindings/useKeybinding.js";
import { useShortcutDisplay } from "../keybindings/useShortcutDisplay.js";
import type { AppMessage } from "../state/AppStateStore.js";
import { getMessageText } from "../screens/shared.js";
import { getColor } from "./design-system/theme.js";

export type NavigableMessage = AppMessage;

export type MessageActionsState = {
  id: string;
  expanded: boolean;
};

export type MessageActionsNav = {
  enterCursor: () => void;
  navigatePrev: () => void;
  navigateNext: () => void;
  navigateTop: () => void;
  navigateBottom: () => void;
  getSelected: () => NavigableMessage | null;
};

export const MessageActionsSelectedContext =
  React.createContext(false);
export const InVirtualListContext = React.createContext(false);

export function isNavigableMessage(
  message: NavigableMessage,
): boolean {
  if (message.meta?.hidden === true || message.meta?.budget === true) {
    return false;
  }

  return getMessageText(message).trim().length > 0;
}

export function useSelectedMessageBg(): string | undefined {
  return React.useContext(MessageActionsSelectedContext)
    ? getColor("bgHighlight")
    : undefined;
}

export function useMessageActions(
  cursor: MessageActionsState | null,
  setCursor: React.Dispatch<
    React.SetStateAction<MessageActionsState | null>
  >,
  navRef: React.RefObject<MessageActionsNav | null>,
): {
  enter: () => void;
  handlers: Record<string, () => void>;
} {
  const cursorRef = React.useRef(cursor);
  cursorRef.current = cursor;

  const handlers = React.useMemo(
    () => ({
      "messageActions:enter": () => {
        const current = cursorRef.current;
        if (!current) {
          navRef.current?.enterCursor();
          return;
        }

        setCursor(prev =>
          prev
            ? {
                ...prev,
                expanded: !prev.expanded,
              }
            : prev,
        );
      },
      "messageActions:escape": () => {
        setCursor(null);
      },
      "messageActions:prev": () => {
        navRef.current?.navigatePrev();
      },
      "messageActions:next": () => {
        navRef.current?.navigateNext();
      },
      "messageActions:top": () => {
        navRef.current?.navigateTop();
      },
      "messageActions:bottom": () => {
        navRef.current?.navigateBottom();
      },
    }),
    [navRef, setCursor],
  );

  const enter = React.useCallback(() => {
    if (cursorRef.current) {
      setCursor(prev =>
        prev
          ? {
              ...prev,
              expanded: !prev.expanded,
            }
          : prev,
      );
      return;
    }

    navRef.current?.enterCursor();
  }, [navRef, setCursor]);

  return {
    enter,
    handlers,
  };
}

export function MessageActionsKeybindings({
  handlers,
  isActive,
}: {
  handlers: Record<string, () => void>;
  isActive: boolean;
}): null {
  useKeybindings(handlers, {
    context: "MessageActions",
    isActive,
  });

  return null;
}

export function MessageActionsBar({
  cursor,
}: {
  cursor: MessageActionsState;
}): React.ReactElement {
  const enterShortcut = useShortcutDisplay(
    "messageActions:enter",
    "MessageActions",
    "Enter",
  );
  const escapeShortcut = useShortcutDisplay(
    "messageActions:escape",
    "MessageActions",
    "Esc",
  );
  const prevShortcut = useShortcutDisplay(
    "messageActions:prev",
    "MessageActions",
    "Up",
  );
  const nextShortcut = useShortcutDisplay(
    "messageActions:next",
    "MessageActions",
    "Down",
  );

  return (
    <Box marginTop={1} paddingX={1}>
      <Text color={getColor("textDim")}>
        Selected {cursor.id} · {enterShortcut} expand/collapse · {prevShortcut}/{nextShortcut} move · {escapeShortcut} clear
      </Text>
    </Box>
  );
}
