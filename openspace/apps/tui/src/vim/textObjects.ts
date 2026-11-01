import type { CursorPosition, SelectionRange } from "./types.js";
import { getLines } from "./motions.js";

function toAbsolute(text: string, pos: CursorPosition): number {
  const lines = getLines(text);
  let offset = 0;
  for (let i = 0; i < pos.line && i < lines.length; i++) {
    offset += lines[i]!.length + 1;
  }
  return offset + pos.col;
}

function fromAbsolute(text: string, offset: number): CursorPosition {
  const clamped = Math.max(0, Math.min(offset, text.length));
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

function isWordChar(ch: string): boolean {
  return /\w/.test(ch);
}

function charClass(ch: string): "word" | "punct" | "space" {
  if (/\s/.test(ch)) return "space";
  if (isWordChar(ch)) return "word";
  return "punct";
}

export function innerWord(
  text: string,
  cursor: CursorPosition,
): SelectionRange | null {
  if (text.length === 0) return null;
  const pos = toAbsolute(text, cursor);
  if (pos >= text.length) return null;

  const cls = charClass(text[pos]!);
  let start = pos;
  let end = pos;

  while (start > 0 && charClass(text[start - 1]!) === cls) start--;
  while (end < text.length - 1 && charClass(text[end + 1]!) === cls) end++;

  return { start: fromAbsolute(text, start), end: fromAbsolute(text, end) };
}

export function aWord(
  text: string,
  cursor: CursorPosition,
): SelectionRange | null {
  const inner = innerWord(text, cursor);
  if (!inner) return null;

  let endAbs = toAbsolute(text, inner.end);
  let startAbs = toAbsolute(text, inner.start);

  // include trailing whitespace, or leading if no trailing
  if (endAbs < text.length - 1 && /\s/.test(text[endAbs + 1]!)) {
    endAbs++;
    while (endAbs < text.length - 1 && /\s/.test(text[endAbs + 1]!)) endAbs++;
  } else if (startAbs > 0 && /\s/.test(text[startAbs - 1]!)) {
    startAbs--;
    while (startAbs > 0 && /\s/.test(text[startAbs - 1]!)) startAbs--;
  }

  return { start: fromAbsolute(text, startAbs), end: fromAbsolute(text, endAbs) };
}

export function innerWORD(
  text: string,
  cursor: CursorPosition,
): SelectionRange | null {
  if (text.length === 0) return null;
  const pos = toAbsolute(text, cursor);
  if (pos >= text.length) return null;

  const isSpace = /\s/.test(text[pos]!);
  let start = pos;
  let end = pos;

  if (isSpace) {
    while (start > 0 && /\s/.test(text[start - 1]!)) start--;
    while (end < text.length - 1 && /\s/.test(text[end + 1]!)) end++;
  } else {
    while (start > 0 && !/\s/.test(text[start - 1]!)) start--;
    while (end < text.length - 1 && !/\s/.test(text[end + 1]!)) end++;
  }

  return { start: fromAbsolute(text, start), end: fromAbsolute(text, end) };
}

export function aWORD(
  text: string,
  cursor: CursorPosition,
): SelectionRange | null {
  const inner = innerWORD(text, cursor);
  if (!inner) return null;

  let endAbs = toAbsolute(text, inner.end);
  let startAbs = toAbsolute(text, inner.start);

  if (endAbs < text.length - 1 && /\s/.test(text[endAbs + 1]!)) {
    endAbs++;
    while (endAbs < text.length - 1 && /\s/.test(text[endAbs + 1]!)) endAbs++;
  } else if (startAbs > 0 && /\s/.test(text[startAbs - 1]!)) {
    startAbs--;
    while (startAbs > 0 && /\s/.test(text[startAbs - 1]!)) startAbs--;
  }

  return { start: fromAbsolute(text, startAbs), end: fromAbsolute(text, endAbs) };
}

function findMatchingPair(
  text: string,
  pos: number,
  open: string,
  close: string,
): { openPos: number; closePos: number } | null {
  let openPos = -1;
  let depth: number;

  // search backward for opening
  depth = 0;
  for (let i = pos; i >= 0; i--) {
    if (text[i] === close && i !== pos) depth++;
    if (text[i] === open) {
      if (depth === 0) {
        openPos = i;
        break;
      }
      depth--;
    }
  }

  if (openPos === -1) return null;

  // search forward for closing
  depth = 0;
  for (let i = openPos + 1; i < text.length; i++) {
    if (text[i] === open) depth++;
    if (text[i] === close) {
      if (depth === 0) {
        return { openPos, closePos: i };
      }
      depth--;
    }
  }

  return null;
}

function findQuotePair(
  text: string,
  pos: number,
  quote: string,
): { openPos: number; closePos: number } | null {
  const lines = getLines(text);
  let lineStart = 0;
  let lineIdx = 0;
  let remaining = pos;
  for (let i = 0; i < lines.length; i++) {
    if (remaining <= lines[i]!.length) {
      lineStart = pos - remaining;
      lineIdx = i;
      break;
    }
    remaining -= lines[i]!.length + 1;
  }

  const line = lines[lineIdx] ?? "";
  const lineEnd = lineStart + line.length;

  // find all quote positions on this line
  const quotes: number[] = [];
  for (let i = lineStart; i < lineEnd; i++) {
    if (text[i] === quote && (i === lineStart || text[i - 1] !== "\\")) {
      quotes.push(i);
    }
  }

  // find the pair that contains pos
  for (let i = 0; i < quotes.length - 1; i += 2) {
    if (quotes[i]! <= pos && quotes[i + 1]! >= pos) {
      return { openPos: quotes[i]!, closePos: quotes[i + 1]! };
    }
  }

  // if cursor is before first quote, use first pair
  if (quotes.length >= 2 && pos <= quotes[0]!) {
    return { openPos: quotes[0]!, closePos: quotes[1]! };
  }

  return null;
}

export function innerQuote(
  text: string,
  cursor: CursorPosition,
  quote: string,
): SelectionRange | null {
  const pos = toAbsolute(text, cursor);
  const pair = findQuotePair(text, pos, quote);
  if (!pair) return null;

  if (pair.closePos - pair.openPos <= 1) {
    // empty quotes — return zero-width range inside
    return {
      start: fromAbsolute(text, pair.openPos + 1),
      end: fromAbsolute(text, pair.openPos + 1),
    };
  }

  return {
    start: fromAbsolute(text, pair.openPos + 1),
    end: fromAbsolute(text, pair.closePos - 1),
  };
}

export function aQuote(
  text: string,
  cursor: CursorPosition,
  quote: string,
): SelectionRange | null {
  const pos = toAbsolute(text, cursor);
  const pair = findQuotePair(text, pos, quote);
  if (!pair) return null;

  return {
    start: fromAbsolute(text, pair.openPos),
    end: fromAbsolute(text, pair.closePos),
  };
}

const PAIR_MAP: Record<string, [string, string]> = {
  "(": ["(", ")"],
  ")": ["(", ")"],
  "[": ["[", "]"],
  "]": ["[", "]"],
  "{": ["{", "}"],
  "}": ["{", "}"],
  "b": ["(", ")"],
  "B": ["{", "}"],
};

export function innerParen(
  text: string,
  cursor: CursorPosition,
  ch: string,
): SelectionRange | null {
  const pair = PAIR_MAP[ch];
  if (!pair) return null;

  const pos = toAbsolute(text, cursor);
  const match = findMatchingPair(text, pos, pair[0], pair[1]);
  if (!match) return null;

  if (match.closePos - match.openPos <= 1) {
    return {
      start: fromAbsolute(text, match.openPos + 1),
      end: fromAbsolute(text, match.openPos + 1),
    };
  }

  return {
    start: fromAbsolute(text, match.openPos + 1),
    end: fromAbsolute(text, match.closePos - 1),
  };
}

export function aParen(
  text: string,
  cursor: CursorPosition,
  ch: string,
): SelectionRange | null {
  const pair = PAIR_MAP[ch];
  if (!pair) return null;

  const pos = toAbsolute(text, cursor);
  const match = findMatchingPair(text, pos, pair[0], pair[1]);
  if (!match) return null;

  return {
    start: fromAbsolute(text, match.openPos),
    end: fromAbsolute(text, match.closePos),
  };
}

export function innerAngle(
  text: string,
  cursor: CursorPosition,
): SelectionRange | null {
  const pos = toAbsolute(text, cursor);
  const match = findMatchingPair(text, pos, "<", ">");
  if (!match) return null;

  if (match.closePos - match.openPos <= 1) {
    return {
      start: fromAbsolute(text, match.openPos + 1),
      end: fromAbsolute(text, match.openPos + 1),
    };
  }

  return {
    start: fromAbsolute(text, match.openPos + 1),
    end: fromAbsolute(text, match.closePos - 1),
  };
}

export function aAngle(
  text: string,
  cursor: CursorPosition,
): SelectionRange | null {
  const pos = toAbsolute(text, cursor);
  const match = findMatchingPair(text, pos, "<", ">");
  if (!match) return null;

  return {
    start: fromAbsolute(text, match.openPos),
    end: fromAbsolute(text, match.closePos),
  };
}
