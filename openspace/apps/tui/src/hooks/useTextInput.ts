import React from "react";
import type { Key } from "ink";
import type {
  BaseTextInputProps,
  TextInputState,
} from "../types/textInputTypes.js";
import {
  isBackspaceInput,
  isDeleteInput,
} from "../utils/keyInput.js";
import { isRawTerminalControlInput } from "../utils/terminalInput.js";

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function insertAt(text: string, offset: number, input: string): string {
  return `${text.slice(0, offset)}${input}${text.slice(offset)}`;
}

function backspaceAt(text: string, offset: number): {
  value: string;
  offset: number;
} {
  const boundedOffset = clamp(offset, 0, text.length);
  if (boundedOffset <= 0) {
    return { value: text, offset: 0 };
  }

  return {
    value: `${text.slice(0, boundedOffset - 1)}${text.slice(boundedOffset)}`,
    offset: boundedOffset - 1,
  };
}

function deleteAt(text: string, offset: number): {
  value: string;
  offset: number;
} {
  const boundedOffset = clamp(offset, 0, text.length);
  if (boundedOffset >= text.length) {
    return { value: text, offset: boundedOffset };
  }

  return {
    value: `${text.slice(0, boundedOffset)}${text.slice(boundedOffset + 1)}`,
    offset: boundedOffset,
  };
}

function isBackspaceChar(char: string): boolean {
  return char === "\u0008" || char === "\u007f";
}

function isPrintableInputChar(char: string): boolean {
  return char >= " " && char !== "\u007f";
}

const DELETE_SEQUENCES = [
  "\u001b[3~",
  "\u001b[3;2~",
  "\u001b[3;3~",
  "\u001b[3;5~",
];

function deleteAtCursorOrBackspaceAtEnd(
  text: string,
  offset: number,
): {
  value: string;
  offset: number;
} {
  return offset >= text.length
    ? backspaceAt(text, offset)
    : deleteAt(text, offset);
}

function getDeleteSequenceAt(input: string, index: number): string | null {
  for (const sequence of DELETE_SEQUENCES) {
    if (input.startsWith(sequence, index)) {
      return sequence;
    }
  }

  return null;
}

function hasChunkEditingInput(input: string): boolean {
  return (
    Array.from(input).some(isBackspaceChar) ||
    DELETE_SEQUENCES.some(sequence => input.includes(sequence))
  );
}

function getChunkSubmitIndex(input: string): number {
  return input.indexOf("\r");
}

function applyPlainInputChunk(
  text: string,
  offset: number,
  input: string,
): {
  value: string;
  offset: number;
} {
  let nextValue = text;
  let nextOffset = offset;
  let index = 0;

  while (index < input.length) {
    const deleteSequence = getDeleteSequenceAt(input, index);
    if (deleteSequence !== null) {
      const next = deleteAtCursorOrBackspaceAtEnd(nextValue, nextOffset);
      nextValue = next.value;
      nextOffset = next.offset;
      index += deleteSequence.length;
      continue;
    }

    const char = Array.from(input.slice(index))[0] ?? "";
    index += char.length;

    if (isBackspaceChar(char)) {
      const next = backspaceAt(nextValue, nextOffset);
      nextValue = next.value;
      nextOffset = next.offset;
      continue;
    }

    if (!isPrintableInputChar(char)) {
      continue;
    }

    nextValue = insertAt(nextValue, nextOffset, char);
    nextOffset += char.length;
  }

  return {
    value: nextValue,
    offset: nextOffset,
  };
}

function lineStart(text: string, offset: number): number {
  const prevBreak = text.lastIndexOf("\n", Math.max(0, offset - 1));
  return prevBreak === -1 ? 0 : prevBreak + 1;
}

function lineEnd(text: string, offset: number): number {
  const nextBreak = text.indexOf("\n", offset);
  return nextBreak === -1 ? text.length : nextBreak;
}

function getCursorMetrics(
  text: string,
  offset: number,
  columns: number,
): {
  cursorLine: number;
  cursorColumn: number;
} {
  let line = 0;
  let column = 0;
  const safeColumns = Math.max(columns, 1);

  for (const char of text.slice(0, offset)) {
    if (char === "\n") {
      line += 1;
      column = 0;
      continue;
    }

    column += 1;
    if (column >= safeColumns) {
      line += Math.floor(column / safeColumns);
      column %= safeColumns;
    }
  }

  return {
    cursorLine: line,
    cursorColumn: column,
  };
}

export function useTextInput({
  value,
  onChange,
  onSubmit,
  onHistoryUp,
  onHistoryDown,
  onHistoryReset,
  onClearInput,
  focus = true,
  multiline = true,
  columns,
  cursorOffset,
  onChangeCursorOffset,
  inputFilter,
  handleSubmitKeys = true,
  handleHistoryKeys = true,
  handleClearKey = true,
  handleNewlineKeys = true,
}: BaseTextInputProps): TextInputState {
  const valueRef = React.useRef(value);
  const cursorOffsetRef = React.useRef(cursorOffset);

  React.useLayoutEffect(() => {
    valueRef.current = value;
    cursorOffsetRef.current = clamp(cursorOffset, 0, value.length);

    if (cursorOffset > value.length) {
      onChangeCursorOffset(value.length);
    }
  }, [cursorOffset, onChangeCursorOffset, value]);

  const setOffset = React.useCallback(
    (offset: number) => {
      const nextOffset = clamp(offset, 0, valueRef.current.length);
      cursorOffsetRef.current = nextOffset;
      onChangeCursorOffset(nextOffset);
    },
    [onChangeCursorOffset],
  );

  const commitChange = React.useCallback(
    (nextValue: string, nextOffset: number) => {
      const boundedOffset = clamp(nextOffset, 0, nextValue.length);
      valueRef.current = nextValue;
      cursorOffsetRef.current = boundedOffset;
      onChangeCursorOffset(boundedOffset);
      onChange(nextValue);
    },
    [onChange, onChangeCursorOffset],
  );

  const onInput = React.useCallback(
    (rawInput: string, key: Key): void => {
      const extendedKey = key as Key & {
        home?: boolean;
        end?: boolean;
      };
      const currentValue = valueRef.current;
      const currentOffset = cursorOffsetRef.current;

      if (!focus) {
        return;
      }

      const chunkSubmitIndex = handleSubmitKeys && !key.return
        ? getChunkSubmitIndex(rawInput)
        : -1;
      if (chunkSubmitIndex >= 0) {
        const beforeSubmit = rawInput.slice(0, chunkSubmitIndex);
        const next = beforeSubmit
          ? applyPlainInputChunk(currentValue, currentOffset, beforeSubmit)
          : {
              value: currentValue,
              offset: currentOffset,
            };
        onSubmit?.(next.value);
        return;
      }

      if (isRawTerminalControlInput(rawInput)) {
        return;
      }

      if (key.leftArrow) {
        setOffset(currentOffset - 1);
        return;
      }

      if (key.rightArrow) {
        setOffset(currentOffset + 1);
        return;
      }

      if (handleHistoryKeys && key.upArrow) {
        if (currentOffset === 0) {
          onHistoryUp?.();
        }
        return;
      }

      if (handleHistoryKeys && key.downArrow) {
        if (currentOffset === currentValue.length) {
          onHistoryDown?.();
        }
        return;
      }

      if (extendedKey.home || (key.ctrl && rawInput === "a")) {
        setOffset(lineStart(currentValue, currentOffset));
        return;
      }

      if (extendedKey.end || (key.ctrl && rawInput === "e")) {
        setOffset(lineEnd(currentValue, currentOffset));
        return;
      }

      if (handleSubmitKeys && key.return) {
        if (handleNewlineKeys && multiline && (key.shift || key.meta)) {
          const next = insertAt(currentValue, currentOffset, "\n");
          commitChange(next, currentOffset + 1);
          onHistoryReset?.();
          return;
        }

        onSubmit?.(currentValue);
        return;
      }

      if (isBackspaceInput(rawInput, key)) {
        const next = backspaceAt(currentValue, currentOffset);
        commitChange(next.value, next.offset);
        onHistoryReset?.();
        return;
      }

      if (isDeleteInput(rawInput, key)) {
        const next = deleteAtCursorOrBackspaceAtEnd(
          currentValue,
          currentOffset,
        );
        commitChange(next.value, next.offset);
        onHistoryReset?.();
        return;
      }

      const input = inputFilter ? inputFilter(rawInput, key) : rawInput;
      if (!input && rawInput) {
        return;
      }

      if (handleClearKey && key.escape) {
        onClearInput?.();
        return;
      }

      if (key.ctrl || key.meta) {
        return;
      }

      if (!input) {
        return;
      }

      if (hasChunkEditingInput(input)) {
        const next = applyPlainInputChunk(
          currentValue,
          currentOffset,
          input,
        );
        commitChange(next.value, next.offset);
        onHistoryReset?.();
        return;
      }

      const next = insertAt(currentValue, currentOffset, input);
      commitChange(next, currentOffset + input.length);
      onHistoryReset?.();
    },
    [
      commitChange,
      focus,
      inputFilter,
      multiline,
      onClearInput,
      onHistoryDown,
      onHistoryReset,
      onHistoryUp,
      onSubmit,
      setOffset,
      handleClearKey,
      handleHistoryKeys,
      handleNewlineKeys,
      handleSubmitKeys,
    ],
  );

  const metrics = React.useMemo(
    () => getCursorMetrics(value, cursorOffset, columns),
    [columns, cursorOffset, value],
  );

  return {
    onInput,
    renderedValue: value,
    offset: cursorOffset,
    setOffset,
    cursorLine: metrics.cursorLine,
    cursorColumn: metrics.cursorColumn,
    viewportCharOffset: 0,
    viewportCharEnd: value.length,
  };
}
