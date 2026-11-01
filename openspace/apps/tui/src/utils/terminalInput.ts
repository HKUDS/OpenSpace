export type RawTerminalKeyName =
  | "pageup"
  | "pagedown"
  | "home"
  | "end"
  | "wheelup"
  | "wheeldown"
  | "mouse";

const RAW_KEY_SEQUENCES: Array<[string, RawTerminalKeyName]> = [
  ["\u001b[5~", "pageup"],
  ["[5~", "pageup"],
  ["\u001b[6~", "pagedown"],
  ["[6~", "pagedown"],
  ["\u001b[H", "home"],
  ["[H", "home"],
  ["\u001bOH", "home"],
  ["OH", "home"],
  ["\u001b[1~", "home"],
  ["[1~", "home"],
  ["\u001b[7~", "home"],
  ["[7~", "home"],
  ["\u001b[F", "end"],
  ["[F", "end"],
  ["\u001bOF", "end"],
  ["OF", "end"],
  ["\u001b[4~", "end"],
  ["[4~", "end"],
  ["\u001b[8~", "end"],
  ["[8~", "end"],
];

function mouseButtonKeyName(button: number): RawTerminalKeyName {
  if (!Number.isFinite(button)) {
    return "mouse";
  }

  if (button === 64) {
    return "wheelup";
  }
  if (button === 65) {
    return "wheeldown";
  }

  return "mouse";
}

function collectSgrMouseKeyNames(input: string): RawTerminalKeyName[] {
  const names: RawTerminalKeyName[] = [];
  const pattern = /(?:\u001b)?\[<(\d+);\d+;\d+[Mm]/g;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(input)) !== null) {
    names.push(mouseButtonKeyName(Number(match[1])));
  }

  return names;
}

function x10MouseKeyName(input: string): RawTerminalKeyName | null {
  const start = input.indexOf("\u001b[M");
  if (start < 0 || input.length < start + 6) {
    return null;
  }

  return mouseButtonKeyName(input.charCodeAt(start + 3) - 32);
}

export function getRawTerminalKeyNames(
  input: string,
): RawTerminalKeyName[] {
  const names: RawTerminalKeyName[] = [];

  for (const [sequence, keyName] of RAW_KEY_SEQUENCES) {
    if (input === sequence) {
      names.push(keyName);
    }
  }

  names.push(...collectSgrMouseKeyNames(input));

  const x10Name = x10MouseKeyName(input);
  if (x10Name !== null) {
    names.push(x10Name);
  }

  return names;
}

export function getRawTerminalKeyName(
  input: string,
): RawTerminalKeyName | null {
  return getRawTerminalKeyNames(input)[0] ?? null;
}

export function isRawTerminalControlInput(input: string): boolean {
  return getRawTerminalKeyNames(input).length > 0;
}
