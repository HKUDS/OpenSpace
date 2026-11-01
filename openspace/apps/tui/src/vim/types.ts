export type VimMode = "insert" | "normal" | "visual" | "replace";

export interface CursorPosition {
  line: number;
  col: number;
}

export interface SelectionRange {
  start: CursorPosition;
  end: CursorPosition;
}

export interface CommandState {
  operator: string | null;
  count: string;
  register: string | null;
  awaitingCharArg: string | null; // for f, F, t, T, r commands
  partialCommand: string;
}

export interface PersistentState {
  registers: Record<string, string>;
  lastCommand: {
    operator: string | null;
    motion: string | null;
    count: number;
    charArg: string | null;
    textInserted: string | null;
  } | null;
  searchPattern: string | null;
  searchDirection: "forward" | "backward";
}

export interface VimState {
  mode: VimMode;
  command: CommandState;
  persistent: PersistentState;
  cursor: CursorPosition;
  selection: SelectionRange | null;
}

export function createDefaultCommandState(): CommandState {
  return {
    operator: null,
    count: "",
    register: null,
    awaitingCharArg: null,
    partialCommand: "",
  };
}

export function createDefaultPersistentState(): PersistentState {
  return {
    registers: { '"': "" },
    lastCommand: null,
    searchPattern: null,
    searchDirection: "forward",
  };
}

export function createDefaultVimState(): VimState {
  return {
    mode: "insert",
    command: createDefaultCommandState(),
    persistent: createDefaultPersistentState(),
    cursor: { line: 0, col: 0 },
    selection: null,
  };
}
