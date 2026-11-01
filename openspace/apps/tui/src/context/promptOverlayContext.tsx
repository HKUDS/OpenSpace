import React, {
  createContext,
  type ReactNode,
  useContext,
  useLayoutEffect,
  useState,
} from "react";
import type { CompletionItem } from "../components/PromptInput/SlashCommandComplete.js";

export type PromptOverlayData = {
  suggestions: CompletionItem[];
  selectedSuggestion: number;
  maxColumnWidth?: number;
};

type Setter<T> = (data: T | null) => void;

const DataContext = createContext<PromptOverlayData | null>(null);
const SetContext = createContext<Setter<PromptOverlayData> | null>(null);
const DialogContext = createContext<ReactNode>(null);
const SetDialogContext = createContext<Setter<ReactNode> | null>(null);

type Props = {
  children: React.ReactNode;
};

export function PromptOverlayProvider({ children }: Props): React.ReactNode {
  const [data, setData] = useState<PromptOverlayData | null>(null);
  const [dialog, setDialog] = useState<ReactNode>(null);

  return (
    <SetContext.Provider value={setData}>
      <SetDialogContext.Provider value={setDialog}>
        <DataContext.Provider value={data}>
          <DialogContext.Provider value={dialog}>
            {children}
          </DialogContext.Provider>
        </DataContext.Provider>
      </SetDialogContext.Provider>
    </SetContext.Provider>
  );
}

export function usePromptOverlay(): PromptOverlayData | null {
  return useContext(DataContext);
}

export function usePromptOverlayDialog(): ReactNode {
  return useContext(DialogContext);
}

export function useSetPromptOverlay(data: PromptOverlayData | null): void {
  const set = useContext(SetContext);

  useLayoutEffect(() => {
    if (!set) {
      return;
    }

    set(data);
    return () => {
      set(null);
    };
  }, [data, set]);
}

export function useSetPromptOverlayDialog(node: ReactNode): void {
  const set = useContext(SetDialogContext);

  useLayoutEffect(() => {
    if (!set) {
      return;
    }

    set(node);
    return () => {
      set(null);
    };
  }, [node, set]);
}
