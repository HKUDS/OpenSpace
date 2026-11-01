import { createContext, type RefObject, useContext } from "react";
import type { ScrollBoxHandle } from "../ink/components/ScrollBox.js";

type ModalContextValue = {
  rows: number;
  columns: number;
  scrollRef: RefObject<ScrollBoxHandle | null> | null;
};

export const ModalContext = createContext<ModalContextValue | null>(null);

export function useIsInsideModal(): boolean {
  return useContext(ModalContext) !== null;
}

export function useModalOrTerminalSize(fallback: {
  rows: number;
  columns: number;
}): { rows: number; columns: number } {
  const context = useContext(ModalContext);

  return context
    ? {
        rows: context.rows,
        columns: context.columns,
      }
    : fallback;
}

export function useModalScrollRef(): RefObject<ScrollBoxHandle | null> | null {
  return useContext(ModalContext)?.scrollRef ?? null;
}
