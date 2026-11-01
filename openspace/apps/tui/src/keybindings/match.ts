import type { Key } from "ink";
import {
  isBackspaceInput,
  isDeleteInput,
} from "../utils/keyInput.js";
import { getRawTerminalKeyName } from "../utils/terminalInput.js";
import type { ParsedBinding, ParsedKeystroke } from "./types.js";

type InputKey = Key & {
  home?: boolean;
  end?: boolean;
  wheelUp?: boolean;
  wheelDown?: boolean;
  super?: boolean;
};

type InkModifiers = Pick<InputKey, "ctrl" | "shift" | "meta" | "super">;

function getInkModifiers(key: InputKey): InkModifiers {
  return {
    ctrl: key.ctrl,
    shift: key.shift,
    meta: key.meta,
    super: key.super,
  };
}

export function getKeyName(input: string, key: Key): string | null {
  const extendedKey = key as InputKey;
  const rawKeyName = getRawTerminalKeyName(input);
  if (rawKeyName && rawKeyName !== "mouse") {
    return rawKeyName;
  }

  if (extendedKey.escape) return "escape";
  if (extendedKey.return) return "enter";
  if (extendedKey.tab) return "tab";
  if (isBackspaceInput(input, extendedKey)) return "backspace";
  if (isDeleteInput(input, extendedKey)) return "delete";
  if (extendedKey.upArrow) return "up";
  if (extendedKey.downArrow) return "down";
  if (extendedKey.leftArrow) return "left";
  if (extendedKey.rightArrow) return "right";
  if (extendedKey.pageUp) return "pageup";
  if (extendedKey.pageDown) return "pagedown";
  if (extendedKey.wheelUp) return "wheelup";
  if (extendedKey.wheelDown) return "wheeldown";
  if (extendedKey.home) return "home";
  if (extendedKey.end) return "end";
  if (input.length === 1) return input.toLowerCase();
  return null;
}

function modifiersMatch(
  inkModifiers: InkModifiers,
  target: ParsedKeystroke,
): boolean {
  if (inkModifiers.ctrl !== target.ctrl) return false;
  if (inkModifiers.shift !== target.shift) return false;

  const targetNeedsMeta = target.alt || target.meta;
  if (inkModifiers.meta !== targetNeedsMeta) return false;

  return inkModifiers.super === target.super;
}

export function matchesKeystroke(
  input: string,
  key: Key,
  target: ParsedKeystroke,
): boolean {
  const extendedKey = key as InputKey;
  const keyName = getKeyName(input, extendedKey);

  if (keyName !== target.key) return false;

  const inkModifiers = getInkModifiers(extendedKey);

  if (extendedKey.escape) {
    return modifiersMatch({ ...inkModifiers, meta: false }, target);
  }

  return modifiersMatch(inkModifiers, target);
}

export function matchesBinding(
  input: string,
  key: Key,
  binding: ParsedBinding,
): boolean {
  if (binding.chord.length !== 1) return false;
  const keystroke = binding.chord[0];
  if (!keystroke) return false;
  return matchesKeystroke(input, key, keystroke);
}
