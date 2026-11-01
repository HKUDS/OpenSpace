import React from "react";
import { useKeybindings } from "../keybindings/useKeybinding.js";
import type { ScrollBoxHandle } from "../ink/components/ScrollBox.js";

type Props = {
  scrollRef?: React.RefObject<ScrollBoxHandle | null>;
  isActive: boolean;
  onScroll?: (sticky: boolean, handle: ScrollBoxHandle) => void;
};

function withHandle(
  scrollRef: React.RefObject<ScrollBoxHandle | null> | undefined,
  run: (handle: ScrollBoxHandle) => void,
): void {
  const handle = scrollRef?.current;
  if (!handle) {
    return;
  }

  run(handle);
}

export function ScrollKeybindingHandler({
  scrollRef,
  isActive,
  onScroll,
}: Props): null {
  const scrollBy = React.useCallback(
    (delta: number) => {
      withHandle(scrollRef, handle => {
        handle.scrollBy(delta);
        onScroll?.(handle.isSticky(), handle);
      });
    },
    [onScroll, scrollRef],
  );

  const pageSize = React.useCallback(
    (handle: ScrollBoxHandle): number =>
      Math.max(1, handle.getViewportHeight() - 2),
    [],
  );

  const handlers = React.useMemo(
    () => ({
      "scroll:pageUp": () => {
        withHandle(scrollRef, handle => {
          handle.scrollBy(-pageSize(handle));
          onScroll?.(handle.isSticky(), handle);
        });
      },
      "scroll:pageDown": () => {
        withHandle(scrollRef, handle => {
          handle.scrollBy(pageSize(handle));
          onScroll?.(handle.isSticky(), handle);
        });
      },
      "scroll:top": () => {
        withHandle(scrollRef, handle => {
          handle.scrollTo(0);
          onScroll?.(handle.isSticky(), handle);
        });
      },
      "scroll:bottom": () => {
        withHandle(scrollRef, handle => {
          handle.scrollToBottom();
          onScroll?.(handle.isSticky(), handle);
        });
      },
      "scroll:wheelUp": () => {
        scrollBy(-3);
      },
      "scroll:wheelDown": () => {
        scrollBy(3);
      },
    }),
    [onScroll, pageSize, scrollBy, scrollRef],
  );

  useKeybindings(handlers, {
    context: "Chat",
    isActive,
  });
  useKeybindings(handlers, {
    context: "MessageActions",
    isActive,
  });
  useKeybindings(handlers, {
    context: "Transcript",
    isActive,
  });

  return null;
}
