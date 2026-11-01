import React from "react";
import { Box } from "ink";

export type ScrollBoxHandle = {
  scrollTo: (y: number) => void;
  scrollBy: (dy: number) => void;
  scrollToBottom: () => void;
  getScrollTop: () => number;
  getPendingDelta: () => number;
  getScrollHeight: () => number;
  getFreshScrollHeight: () => number;
  getViewportHeight: () => number;
  getViewportTop: () => number;
  isSticky: () => boolean;
  subscribe: (listener: () => void) => () => void;
  setClampBounds: (min: number | undefined, max: number | undefined) => void;
};

type ScrollBoxProps = React.PropsWithChildren<{
  height?: number;
  flexGrow?: number;
  stickyScroll?: boolean;
  contentHeight?: number;
  borderStyle?: React.ComponentProps<typeof Box>["borderStyle"];
  borderColor?: React.ComponentProps<typeof Box>["borderColor"];
  paddingTop?: number;
  paddingX?: number;
  marginTop?: number;
  flexDirection?: React.ComponentProps<typeof Box>["flexDirection"];
  overflowY?: React.ComponentProps<typeof Box>["overflowY"];
}>;

/*
  Transitional: OpenSpace's ScrollBox depends on OpenSpace's local ink DOM
  and renderer hooks. OpenSpace still runs on upstream Ink, so this keeps a
  closer viewport contract and sticky scroll behavior while leaving real DOM
  culling to consumer components like Messages.
*/
function ScrollBoxInner(
  {
    children,
    height,
    flexGrow,
    stickyScroll,
    contentHeight,
    borderStyle,
    borderColor,
    paddingTop,
    paddingX,
    marginTop,
    flexDirection = "column",
    overflowY,
}: ScrollBoxProps,
  ref: React.ForwardedRef<ScrollBoxHandle | null>,
): React.ReactElement {
  const [, forceRender] = React.useReducer(
    (value: number) => (value + 1) % 1_000_000,
    0,
  );
  const listenersRef = React.useRef(new Set<() => void>());
  const scrollTopRef = React.useRef(0);
  const pendingDeltaRef = React.useRef(0);
  const stickyRef = React.useRef(Boolean(stickyScroll));
  const clampMinRef = React.useRef<number | undefined>(undefined);
  const clampMaxRef = React.useRef<number | undefined>(undefined);
  const viewportHeightRef = React.useRef(height ?? 0);
  const scrollHeightRef = React.useRef(
    Math.max(contentHeight ?? (height ?? 0), height ?? 0),
  );
  const previousStickyPropRef = React.useRef(Boolean(stickyScroll));
  const hasFixedHeight = height !== undefined;

  const notify = React.useCallback(() => {
    for (const listener of listenersRef.current) {
      listener();
    }
    forceRender();
  }, []);

  const clampScrollTop = React.useCallback((value: number): number => {
    const min = clampMinRef.current ?? 0;
    const max =
      clampMaxRef.current ??
      Math.max(0, scrollHeightRef.current - viewportHeightRef.current);
    return Math.max(min, Math.min(max, Math.floor(value)));
  }, []);

  React.useEffect(() => {
    viewportHeightRef.current = height ?? 0;
    scrollHeightRef.current = Math.max(
      contentHeight ?? viewportHeightRef.current,
      viewportHeightRef.current,
    );

    const nextScrollTop = stickyRef.current
      ? Math.max(
          0,
          scrollHeightRef.current - viewportHeightRef.current,
        )
      : clampScrollTop(scrollTopRef.current);

    const changed =
      scrollTopRef.current !== nextScrollTop ||
      pendingDeltaRef.current !== 0;

    scrollTopRef.current = nextScrollTop;
    pendingDeltaRef.current = 0;

    if (changed) {
      notify();
    }
  }, [clampScrollTop, contentHeight, height, notify]);

  React.useEffect(() => {
    const nextSticky = Boolean(stickyScroll);
    const becameSticky = nextSticky && !previousStickyPropRef.current;
    previousStickyPropRef.current = nextSticky;

    if (!becameSticky) {
      return;
    }

    stickyRef.current = true;
    scrollTopRef.current = Math.max(
      0,
      scrollHeightRef.current - viewportHeightRef.current,
    );
    pendingDeltaRef.current = 0;
    notify();
  }, [notify, stickyScroll]);

  React.useImperativeHandle(
    ref,
    () => ({
      scrollTo(y: number) {
        stickyRef.current = false;
        pendingDeltaRef.current = 0;
        scrollTopRef.current = clampScrollTop(y);
        notify();
      },
      scrollBy(dy: number) {
        stickyRef.current = false;
        pendingDeltaRef.current += Math.floor(dy);
        const nextScrollTop = clampScrollTop(
          scrollTopRef.current + pendingDeltaRef.current,
        );
        const max =
          clampMaxRef.current ??
          Math.max(0, scrollHeightRef.current - viewportHeightRef.current);
        scrollTopRef.current = nextScrollTop;
        stickyRef.current = nextScrollTop >= max;
        pendingDeltaRef.current = 0;
        notify();
      },
      scrollToBottom() {
        stickyRef.current = true;
        pendingDeltaRef.current = 0;
        scrollTopRef.current = Math.max(
          0,
          scrollHeightRef.current - viewportHeightRef.current,
        );
        notify();
      },
      getScrollTop() {
        return scrollTopRef.current;
      },
      getPendingDelta() {
        return pendingDeltaRef.current;
      },
      getScrollHeight() {
        return scrollHeightRef.current;
      },
      getFreshScrollHeight() {
        return scrollHeightRef.current;
      },
      getViewportHeight() {
        return viewportHeightRef.current;
      },
      getViewportTop() {
        return 0;
      },
      isSticky() {
        return stickyRef.current;
      },
      subscribe(listener: () => void) {
        listenersRef.current.add(listener);
        return () => {
          listenersRef.current.delete(listener);
        };
      },
      setClampBounds(min: number | undefined, max: number | undefined) {
        clampMinRef.current = min;
        clampMaxRef.current = max;
      },
    }),
    [clampScrollTop, notify],
  );

  return (
    <Box
      flexDirection={flexDirection}
      height={height}
      flexGrow={hasFixedHeight ? 0 : flexGrow}
      borderStyle={borderStyle}
      borderColor={borderColor}
      paddingTop={paddingTop}
      paddingX={paddingX}
      marginTop={marginTop}
      overflowY={overflowY}
      flexShrink={0}
      width="100%"
    >
      <Box
        flexDirection="column"
        flexGrow={1}
        flexShrink={0}
        width="100%"
      >
        {children}
      </Box>
    </Box>
  );
}

const ScrollBox = React.forwardRef<ScrollBoxHandle | null, ScrollBoxProps>(
  ScrollBoxInner,
);
export default ScrollBox;
