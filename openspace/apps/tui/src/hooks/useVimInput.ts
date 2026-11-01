import React from "react";
import type { Key } from "ink";
import type {
  BaseTextInputProps,
  VimInputState,
  VimMode,
} from "../types/textInputTypes.js";
import {
  isBackspaceInput,
  isDeleteInput,
} from "../utils/keyInput.js";
import { useTextInput } from "./useTextInput.js";

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function lineStart(text: string, offset: number): number {
  const prevBreak = text.lastIndexOf("\n", Math.max(0, offset - 1));
  return prevBreak === -1 ? 0 : prevBreak + 1;
}

function lineEnd(text: string, offset: number): number {
  const nextBreak = text.indexOf("\n", offset);
  return nextBreak === -1 ? text.length : nextBreak;
}

type UseVimInputProps = BaseTextInputProps & {
  onModeChange?: (mode: VimMode) => void;
};

export function useVimInput({
  onModeChange,
  inputFilter,
  ...props
}: UseVimInputProps): VimInputState {
  const [mode, setModeState] = React.useState<VimMode>("INSERT");
  const textInput = useTextInput({
    ...props,
    inputFilter: undefined,
  });

  const setMode = React.useCallback(
    (nextMode: VimMode) => {
      setModeState(nextMode);
      onModeChange?.(nextMode);
    },
    [onModeChange],
  );

  const onInput = React.useCallback(
    (rawInput: string, key: Key): void => {
      const extendedKey = key as Key & {
        home?: boolean;
        end?: boolean;
      };
      const filtered = inputFilter ? inputFilter(rawInput, key) : rawInput;
      const isEditingKey =
        isBackspaceInput(rawInput, key) || isDeleteInput(rawInput, key);
      const input = mode === "INSERT" && !isEditingKey ? filtered : rawInput;

      if (key.ctrl) {
        textInput.onInput(rawInput, key);
        return;
      }

      if (mode === "INSERT") {
        if (key.escape) {
          setMode("NORMAL");
          textInput.setOffset(Math.max(0, textInput.offset - 1));
          return;
        }

        textInput.onInput(input, key);
        return;
      }

      if (key.escape) {
        return;
      }

      if (key.return) {
        textInput.onInput(input, key);
        return;
      }

      if (key.leftArrow || input === "h") {
        textInput.setOffset(textInput.offset - 1);
        return;
      }

      if (key.rightArrow || input === "l") {
        textInput.setOffset(textInput.offset + 1);
        return;
      }

      if (input === "0" || extendedKey.home) {
        textInput.setOffset(lineStart(props.value, textInput.offset));
        return;
      }

      if (input === "$" || extendedKey.end) {
        textInput.setOffset(lineEnd(props.value, textInput.offset));
        return;
      }

      if (input === "i") {
        setMode("INSERT");
        return;
      }

      if (input === "a") {
        textInput.setOffset(clamp(textInput.offset + 1, 0, props.value.length));
        setMode("INSERT");
        return;
      }

      if (input === "I") {
        textInput.setOffset(lineStart(props.value, textInput.offset));
        setMode("INSERT");
        return;
      }

      if (input === "A") {
        textInput.setOffset(lineEnd(props.value, textInput.offset));
        setMode("INSERT");
        return;
      }

      if (input === "x") {
        if (textInput.offset >= props.value.length) {
          return;
        }

        props.onChange(
          `${props.value.slice(0, textInput.offset)}${props.value.slice(textInput.offset + 1)}`,
        );
      }
    },
    [inputFilter, mode, props, setMode, textInput],
  );

  return {
    ...textInput,
    onInput,
    mode,
    setMode,
  };
}
