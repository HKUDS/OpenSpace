import type { CursorPosition } from "./types.js";

export function getLines(text: string): string[] {
  return text.split("\n");
}

export function getLineRange(
  text: string,
  line: number,
): { start: number; end: number } {
  const lines = getLines(text);
  const clamped = Math.max(0, Math.min(line, lines.length - 1));
  let start = 0;
  for (let i = 0; i < clamped; i++) {
    start += lines[i]!.length + 1;
  }
  return { start, end: start + (lines[clamped]?.length ?? 0) };
}

function clampCursor(text: string, pos: CursorPosition): CursorPosition {
  const lines = getLines(text);
  const line = Math.max(0, Math.min(pos.line, lines.length - 1));
  const lineLen = lines[line]?.length ?? 0;
  const maxCol = Math.max(0, lineLen - 1);
  return { line, col: Math.max(0, Math.min(pos.col, maxCol)) };
}

function clampCol(lines: string[], line: number, col: number): number {
  const lineLen = lines[line]?.length ?? 0;
  return Math.max(0, Math.min(col, Math.max(0, lineLen - 1)));
}

export function h(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  return clampCursor(text, { line: cursor.line, col: cursor.col - count });
}

export function l(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  return clampCursor(text, { line: cursor.line, col: cursor.col + count });
}

export function j(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  const lines = getLines(text);
  const newLine = Math.min(cursor.line + count, lines.length - 1);
  return { line: newLine, col: clampCol(lines, newLine, cursor.col) };
}

export function k(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  const lines = getLines(text);
  const newLine = Math.max(cursor.line - count, 0);
  return { line: newLine, col: clampCol(lines, newLine, cursor.col) };
}

function isWordChar(ch: string): boolean {
  return /\w/.test(ch);
}

function charClass(ch: string): "word" | "punct" | "space" {
  if (/\s/.test(ch)) return "space";
  if (isWordChar(ch)) return "word";
  return "punct";
}

function toAbsolute(text: string, pos: CursorPosition): number {
  const lines = getLines(text);
  let offset = 0;
  for (let i = 0; i < pos.line && i < lines.length; i++) {
    offset += lines[i]!.length + 1;
  }
  return offset + Math.min(pos.col, (lines[pos.line]?.length ?? 1) - 1);
}

function fromAbsolute(text: string, offset: number): CursorPosition {
  const clamped = Math.max(0, Math.min(offset, text.length - 1));
  const lines = getLines(text);
  let remaining = clamped;
  for (let i = 0; i < lines.length; i++) {
    if (remaining <= lines[i]!.length) {
      return { line: i, col: remaining };
    }
    remaining -= lines[i]!.length + 1;
  }
  const lastLine = lines.length - 1;
  return { line: lastLine, col: lines[lastLine]?.length ?? 0 };
}

export function w(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  if (text.length === 0) return { line: 0, col: 0 };
  let pos = toAbsolute(text, cursor);
  for (let i = 0; i < count; i++) {
    if (pos >= text.length - 1) break;
    const startClass = charClass(text[pos]!);
    // skip current word
    while (pos < text.length - 1 && charClass(text[pos]!) === startClass) pos++;
    // skip whitespace
    while (pos < text.length - 1 && /\s/.test(text[pos]!)) pos++;
  }
  return fromAbsolute(text, pos);
}

export function b(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  if (text.length === 0) return { line: 0, col: 0 };
  let pos = toAbsolute(text, cursor);
  for (let i = 0; i < count; i++) {
    if (pos <= 0) break;
    pos--;
    // skip whitespace backward
    while (pos > 0 && /\s/.test(text[pos]!)) pos--;
    const cls = charClass(text[pos]!);
    while (pos > 0 && charClass(text[pos - 1]!) === cls) pos--;
  }
  return fromAbsolute(text, pos);
}

export function e(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  if (text.length === 0) return { line: 0, col: 0 };
  let pos = toAbsolute(text, cursor);
  for (let i = 0; i < count; i++) {
    if (pos >= text.length - 1) break;
    pos++;
    // skip whitespace
    while (pos < text.length - 1 && /\s/.test(text[pos]!)) pos++;
    const cls = charClass(text[pos]!);
    while (pos < text.length - 1 && charClass(text[pos + 1]!) === cls) pos++;
  }
  return fromAbsolute(text, pos);
}

export function W(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  if (text.length === 0) return { line: 0, col: 0 };
  let pos = toAbsolute(text, cursor);
  for (let i = 0; i < count; i++) {
    if (pos >= text.length - 1) break;
    while (pos < text.length - 1 && !/\s/.test(text[pos]!)) pos++;
    while (pos < text.length - 1 && /\s/.test(text[pos]!)) pos++;
  }
  return fromAbsolute(text, pos);
}

export function B(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  if (text.length === 0) return { line: 0, col: 0 };
  let pos = toAbsolute(text, cursor);
  for (let i = 0; i < count; i++) {
    if (pos <= 0) break;
    pos--;
    while (pos > 0 && /\s/.test(text[pos]!)) pos--;
    while (pos > 0 && !/\s/.test(text[pos - 1]!)) pos--;
  }
  return fromAbsolute(text, pos);
}

export function E(
  text: string,
  cursor: CursorPosition,
  count = 1,
): CursorPosition {
  if (text.length === 0) return { line: 0, col: 0 };
  let pos = toAbsolute(text, cursor);
  for (let i = 0; i < count; i++) {
    if (pos >= text.length - 1) break;
    pos++;
    while (pos < text.length - 1 && /\s/.test(text[pos]!)) pos++;
    while (pos < text.length - 1 && !/\s/.test(text[pos + 1]!)) pos++;
  }
  return fromAbsolute(text, pos);
}

export function lineStart(
  _text: string,
  cursor: CursorPosition,
): CursorPosition {
  return { line: cursor.line, col: 0 };
}

export function firstNonWhitespace(
  text: string,
  cursor: CursorPosition,
): CursorPosition {
  const lines = getLines(text);
  const line = lines[cursor.line] ?? "";
  const match = line.match(/\S/);
  return { line: cursor.line, col: match ? match.index! : 0 };
}

export function lineEnd(
  text: string,
  cursor: CursorPosition,
): CursorPosition {
  const lines = getLines(text);
  const lineLen = lines[cursor.line]?.length ?? 0;
  return { line: cursor.line, col: Math.max(0, lineLen - 1) };
}

export function gotoLastLine(text: string): CursorPosition {
  const lines = getLines(text);
  const lastLine = Math.max(0, lines.length - 1);
  const col = Math.max(0, (lines[lastLine]?.length ?? 1) - 1);
  return { line: lastLine, col };
}

export function gotoFirstLine(text: string): CursorPosition {
  const lines = getLines(text);
  const line = lines[0] ?? "";
  const match = line.match(/\S/);
  return { line: 0, col: match ? match.index! : 0 };
}

export function findCharForward(
  text: string,
  cursor: CursorPosition,
  ch: string,
  count = 1,
): CursorPosition {
  const lines = getLines(text);
  const line = lines[cursor.line] ?? "";
  let found = 0;
  for (let i = cursor.col + 1; i < line.length; i++) {
    if (line[i] === ch) {
      found++;
      if (found === count) return { line: cursor.line, col: i };
    }
  }
  return cursor;
}

export function findCharBackward(
  text: string,
  cursor: CursorPosition,
  ch: string,
  count = 1,
): CursorPosition {
  const lines = getLines(text);
  const line = lines[cursor.line] ?? "";
  let found = 0;
  for (let i = cursor.col - 1; i >= 0; i--) {
    if (line[i] === ch) {
      found++;
      if (found === count) return { line: cursor.line, col: i };
    }
  }
  return cursor;
}

export function tillCharForward(
  text: string,
  cursor: CursorPosition,
  ch: string,
  count = 1,
): CursorPosition {
  const result = findCharForward(text, cursor, ch, count);
  if (result.col === cursor.col && result.line === cursor.line) return cursor;
  return { line: result.line, col: result.col - 1 };
}

export function tillCharBackward(
  text: string,
  cursor: CursorPosition,
  ch: string,
  count = 1,
): CursorPosition {
  const result = findCharBackward(text, cursor, ch, count);
  if (result.col === cursor.col && result.line === cursor.line) return cursor;
  return { line: result.line, col: result.col + 1 };
}
