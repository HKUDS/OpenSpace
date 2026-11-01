export const RESERVED_SHORTCUTS: ReadonlySet<string> = new Set([
  "ctrl+c",
  "ctrl+d",
]);

export function isReservedShortcut(shortcut: string): boolean {
  return RESERVED_SHORTCUTS.has(shortcut);
}
