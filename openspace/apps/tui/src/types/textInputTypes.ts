import type { Key } from "ink";

export type InlineGhostText = {
  text: string;
  fullCommand: string;
  insertPosition: number;
};

export type VimMode = "INSERT" | "NORMAL";

export type BaseTextInputProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit?: (value: string) => void;
  onExit?: () => void;
  onExitMessage?: (show: boolean, key?: string) => void;
  onHistoryUp?: () => void;
  onHistoryDown?: () => void;
  onHistoryReset?: () => void;
  onClearInput?: () => void;
  focus?: boolean;
  multiline?: boolean;
  columns: number;
  cursorOffset: number;
  onChangeCursorOffset: (offset: number) => void;
  inlineGhostText?: InlineGhostText;
  inputFilter?: (input: string, key: Key) => string;
  handleSubmitKeys?: boolean;
  handleHistoryKeys?: boolean;
  handleClearKey?: boolean;
  handleNewlineKeys?: boolean;
};

export type BaseInputState = {
  onInput: (input: string, key: Key) => void;
  renderedValue: string;
  offset: number;
  setOffset: (offset: number) => void;
  cursorLine: number;
  cursorColumn: number;
  viewportCharOffset: number;
  viewportCharEnd: number;
};

export type TextInputState = BaseInputState;

export type VimInputState = BaseInputState & {
  mode: VimMode;
  setMode: (mode: VimMode) => void;
};
