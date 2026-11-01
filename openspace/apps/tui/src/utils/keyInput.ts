import type { Key } from "ink";

type TerminalKey = Key & {
  name?: string;
  sequence?: string;
};

const BACKSPACE_INPUTS = new Set([
  "\u0008",
  "\u007f",
  "\u001b\u0008",
  "\u001b\u007f",
]);
const DELETE_INPUTS = new Set([
  "\u001b[3~",
  "\u001b[3;2~",
  "\u001b[3;3~",
  "\u001b[3;5~",
]);

function getTerminalKeyValues(input: string, key: Key): string[] {
  const terminalKey = key as TerminalKey;
  const values = [input];

  if (terminalKey.sequence) {
    values.push(terminalKey.sequence);
  }

  if (terminalKey.name) {
    values.push(terminalKey.name.toLowerCase());
  }

  return values;
}

export function isBackspaceInput(input: string, key: Key): boolean {
  const values = getTerminalKeyValues(input, key);

  return (
    Boolean(key.backspace) ||
    values.some(value => BACKSPACE_INPUTS.has(value)) ||
    values.includes("backspace") ||
    (Boolean(key.ctrl) && values.some(value => value.toLowerCase() === "h"))
  );
}

export function isDeleteInput(input: string, key: Key): boolean {
  const values = getTerminalKeyValues(input, key);

  return (
    Boolean(key.delete) ||
    values.some(value => DELETE_INPUTS.has(value)) ||
    values.includes("delete")
  );
}
