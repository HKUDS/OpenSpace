import { useMemo } from "react";
import { useOptionalKeybindingContext } from "./KeybindingContext.js";
import type { KeybindingContextName } from "./types.js";

export function useShortcutDisplay(
  action: string,
  context: KeybindingContextName,
  fallback: string,
): string {
  const keybindingContext = useOptionalKeybindingContext();

  return useMemo(() => {
    return keybindingContext?.getDisplayText(action, context) ?? fallback;
  }, [action, context, fallback, keybindingContext]);
}
