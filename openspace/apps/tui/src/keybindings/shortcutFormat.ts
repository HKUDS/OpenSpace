import { loadKeybindingsSync } from "./loadUserBindings.js";
import { getBindingDisplayText } from "./resolver.js";
import type { KeybindingContextName } from "./types.js";

export function getShortcutDisplay(
  action: string,
  context: KeybindingContextName,
  fallback: string,
): string {
  const resolved = getBindingDisplayText(action, context, loadKeybindingsSync());
  return resolved ?? fallback;
}
