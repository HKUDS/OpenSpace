import type { InputMode } from "../../state/AppStateStore.js";

export function getModeFromInput(value: string): InputMode {
  return value.trimStart().startsWith("/") ? "command" : "insert";
}
