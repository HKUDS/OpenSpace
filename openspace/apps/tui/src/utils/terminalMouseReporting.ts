export const ENABLE_MOUSE_REPORTING = "\u001b[?1000h\u001b[?1006h";

export const DISABLE_MOUSE_REPORTING =
  "\u001b[?1000l" +
  "\u001b[?1002l" +
  "\u001b[?1003l" +
  "\u001b[?1005l" +
  "\u001b[?1006l" +
  "\u001b[?1015l";

export function shouldEnableTerminalMouseReporting(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  return (
    env.OPENSPACE_TUI_MOUSE_REPORTING === "1" ||
    env.OPENSPACE_TUI_ENABLE_MOUSE === "1"
  );
}

export function enableTerminalMouseReporting(
  output: NodeJS.WriteStream | null | undefined,
): void {
  if (!output?.isTTY || output.destroyed) {
    return;
  }

  output.write(ENABLE_MOUSE_REPORTING);
}

export function disableTerminalMouseReporting(
  output: NodeJS.WriteStream | null | undefined,
): void {
  if (!output?.isTTY || output.destroyed) {
    return;
  }

  output.write(DISABLE_MOUSE_REPORTING);
}
