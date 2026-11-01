import type { CursorPosition, SelectionRange, VimState } from "./types.js";
import { getLines, getLineRange } from "./motions.js";

function toAbsolute(text: string, pos: CursorPosition): number {
  const lines = getLines(text);
  let offset = 0;
  for (let i = 0; i < pos.line && i < lines.length; i++) {
    offset += lines[i]!.length + 1;
  }
  return offset + pos.col;
}

function fromAbsolute(text: string, offset: number): CursorPosition {
  const clamped = Math.max(0, Math.min(offset, Math.max(0, text.length - 1)));
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

function normalizeRange(range: SelectionRange): { startAbs: number; endAbs: number } {
  const s = range.start;
  const e = range.end;
  if (s.line < e.line || (s.line === e.line && s.col <= e.col)) {
    return { startAbs: -1, endAbs: -1 }; // placeholder, calculated below
  }
  return { startAbs: -1, endAbs: -1 };
}

function rangeToAbsolute(
  text: string,
  range: SelectionRange,
): { start: number; end: number } {
  const s = toAbsolute(text, range.start);
  const e = toAbsolute(text, range.end);
  return s <= e ? { start: s, end: e } : { start: e, end: s };
}

export function deleteRange(
  text: string,
  _state: VimState,
  range: SelectionRange,
): { text: string; cursor: CursorPosition; yanked: string } {
  if (text.length === 0) {
    return { text: "", cursor: { line: 0, col: 0 }, yanked: "" };
  }

  const { start, end } = rangeToAbsolute(text, range);
  const yanked = text.slice(start, end + 1);
  const newText = text.slice(0, start) + text.slice(end + 1);

  if (newText.length === 0) {
    return { text: "", cursor: { line: 0, col: 0 }, yanked };
  }

  const cursor = fromAbsolute(newText, Math.min(start, newText.length - 1));
  return { text: newText, cursor, yanked };
}

export function changeRange(
  text: string,
  state: VimState,
  range: SelectionRange,
): { text: string; cursor: CursorPosition; yanked: string; mode: "insert" } {
  const result = deleteRange(text, state, range);
  const cursor =
    result.text.length === 0
      ? { line: 0, col: 0 }
      : fromAbsolute(
          result.text,
          Math.min(
            toAbsolute(result.text, result.cursor),
            result.text.length,
          ),
        );
  return { ...result, cursor, mode: "insert" };
}

export function yankRange(
  text: string,
  _state: VimState,
  range: SelectionRange,
): { text: string; cursor: CursorPosition; yanked: string } {
  const { start, end } = rangeToAbsolute(text, range);
  const yanked = text.slice(start, end + 1);
  // cursor goes to start of range
  const cursorPos = Math.min(start, end);
  return { text, cursor: fromAbsolute(text, cursorPos), yanked };
}

function lineRangeForLines(
  text: string,
  startLine: number,
  count: number,
): SelectionRange {
  const lines = getLines(text);
  const endLine = Math.min(startLine + count - 1, lines.length - 1);
  const startRange = getLineRange(text, startLine);
  const endRange = getLineRange(text, endLine);
  return {
    start: fromAbsolute(text, startRange.start),
    end: fromAbsolute(text, endRange.end + (endLine < lines.length - 1 ? 0 : -1)),
  };
}

export function deleteLines(
  text: string,
  state: VimState,
  startLine: number,
  count: number,
): { text: string; cursor: CursorPosition; yanked: string } {
  const lines = getLines(text);
  const endLine = Math.min(startLine + count - 1, lines.length - 1);

  const startOffset = getLineRange(text, startLine).start;
  const endOffset = getLineRange(text, endLine).end;

  let deleteStart = startOffset;
  let deleteEnd = endOffset;

  // include trailing newline if possible, else leading newline
  if (endLine < lines.length - 1) {
    deleteEnd += 1; // include the \n
  } else if (startLine > 0) {
    deleteStart -= 1; // include preceding \n
  }

  const yanked = text.slice(startOffset, endOffset) + "\n";
  const newText = text.slice(0, deleteStart) + text.slice(deleteEnd);

  if (newText.length === 0) {
    return { text: "", cursor: { line: 0, col: 0 }, yanked };
  }

  void normalizeRange;
  void state;
  const newLines = getLines(newText);
  const newLine = Math.min(startLine, newLines.length - 1);
  const lineContent = newLines[newLine] ?? "";
  const firstNonWs = lineContent.match(/\S/);
  return {
    text: newText,
    cursor: { line: newLine, col: firstNonWs ? firstNonWs.index! : 0 },
    yanked,
  };
}

export function changeLines(
  text: string,
  state: VimState,
  startLine: number,
  count: number,
): { text: string; cursor: CursorPosition; yanked: string; mode: "insert" } {
  const lines = getLines(text);
  const endLine = Math.min(startLine + count - 1, lines.length - 1);

  const startOffset = getLineRange(text, startLine).start;
  const endOffset = getLineRange(text, endLine).end;

  const yanked = text.slice(startOffset, endOffset) + "\n";

  // keep the line but clear its content
  let newText = text.slice(0, startOffset) + text.slice(endOffset);
  if (endLine < lines.length - 1 && newText[startOffset] === "\n") {
    // remove extra newlines except one
  }

  void state;

  if (newText.length === 0) {
    return { text: "", cursor: { line: 0, col: 0 }, yanked, mode: "insert" };
  }

  return {
    text: newText,
    cursor: fromAbsolute(newText, Math.min(startOffset, newText.length - 1)),
    yanked,
    mode: "insert",
  };
}

export function yankLines(
  text: string,
  _state: VimState,
  startLine: number,
  count: number,
): { text: string; cursor: CursorPosition; yanked: string } {
  const lines = getLines(text);
  const endLine = Math.min(startLine + count - 1, lines.length - 1);
  const startOffset = getLineRange(text, startLine).start;
  const endOffset = getLineRange(text, endLine).end;
  const yanked = text.slice(startOffset, endOffset) + "\n";

  const lineContent = lines[startLine] ?? "";
  const firstNonWs = lineContent.match(/\S/);

  return {
    text,
    cursor: { line: startLine, col: firstNonWs ? firstNonWs.index! : 0 },
    yanked,
  };
}

export function deleteCharUnderCursor(
  text: string,
  state: VimState,
  cursor: CursorPosition,
  count = 1,
): { text: string; cursor: CursorPosition; yanked: string } {
  const lines = getLines(text);
  const line = lines[cursor.line] ?? "";
  if (line.length === 0) {
    return { text, cursor, yanked: "" };
  }

  const endCol = Math.min(cursor.col + count, line.length);
  const range: SelectionRange = {
    start: cursor,
    end: { line: cursor.line, col: endCol - 1 },
  };
  return deleteRange(text, state, range);
}

export function replaceChar(
  text: string,
  cursor: CursorPosition,
  ch: string,
): { text: string; cursor: CursorPosition } {
  const abs = toAbsolute(text, cursor);
  if (abs >= text.length) return { text, cursor };
  const newText = text.slice(0, abs) + ch + text.slice(abs + 1);
  return { text: newText, cursor };
}

export function toggleCase(
  text: string,
  cursor: CursorPosition,
  count = 1,
): { text: string; cursor: CursorPosition } {
  const lines = getLines(text);
  const line = lines[cursor.line] ?? "";
  const abs = toAbsolute(text, cursor);
  let result = text;

  const end = Math.min(abs + count, abs + (line.length - cursor.col));
  for (let i = abs; i < end; i++) {
    const ch = result[i]!;
    const toggled = ch === ch.toUpperCase() ? ch.toLowerCase() : ch.toUpperCase();
    result = result.slice(0, i) + toggled + result.slice(i + 1);
  }

  const newCol = Math.min(cursor.col + count, Math.max(0, line.length - 1));
  return { text: result, cursor: { line: cursor.line, col: newCol } };
}

export function joinLines(
  text: string,
  cursor: CursorPosition,
): { text: string; cursor: CursorPosition } {
  const lines = getLines(text);
  if (cursor.line >= lines.length - 1) return { text, cursor };

  const currentLine = lines[cursor.line]!;
  const nextLine = lines[cursor.line + 1]!;
  const trimmedNext = nextLine.trimStart();

  const joinCol = currentLine.length;
  const separator = trimmedNext.length > 0 ? " " : "";

  const before = lines.slice(0, cursor.line).join("\n");
  const joined = currentLine + separator + trimmedNext;
  const after = lines.slice(cursor.line + 2).join("\n");

  let newText = before;
  if (before.length > 0) newText += "\n";
  newText += joined;
  if (after.length > 0) newText += "\n" + after;

  return { text: newText, cursor: { line: cursor.line, col: joinCol } };
}

export function pasteAfter(
  text: string,
  cursor: CursorPosition,
  content: string,
): { text: string; cursor: CursorPosition } {
  if (!content) return { text, cursor };

  const isLinewise = content.endsWith("\n");

  if (isLinewise) {
    const lines = getLines(text);
    const lineRange = getLineRange(text, cursor.line);
    const insertPos = lineRange.end + (cursor.line < lines.length - 1 ? 1 : 0);
    const prefix = cursor.line < lines.length - 1 ? "" : "\n";
    const insertContent = prefix + content.slice(0, -1); // remove trailing \n
    const newText = text.slice(0, insertPos) + insertContent + text.slice(insertPos);
    const newLine = cursor.line + 1;
    const insertedLine = getLines(newText)[newLine] ?? "";
    const firstNonWs = insertedLine.match(/\S/);
    return {
      text: newText,
      cursor: { line: newLine, col: firstNonWs ? firstNonWs.index! : 0 },
    };
  }

  const abs = toAbsolute(text, cursor);
  const insertPos = Math.min(abs + 1, text.length);
  const newText = text.slice(0, insertPos) + content + text.slice(insertPos);
  return {
    text: newText,
    cursor: fromAbsolute(newText, insertPos + content.length - 1),
  };
}

export function pasteBefore(
  text: string,
  cursor: CursorPosition,
  content: string,
): { text: string; cursor: CursorPosition } {
  if (!content) return { text, cursor };

  const isLinewise = content.endsWith("\n");

  if (isLinewise) {
    const lineRange = getLineRange(text, cursor.line);
    const insertContent = content.slice(0, -1) + "\n";
    const newText =
      text.slice(0, lineRange.start) + insertContent + text.slice(lineRange.start);
    const insertedLine = getLines(newText)[cursor.line] ?? "";
    const firstNonWs = insertedLine.match(/\S/);
    return {
      text: newText,
      cursor: { line: cursor.line, col: firstNonWs ? firstNonWs.index! : 0 },
    };
  }

  const abs = toAbsolute(text, cursor);
  const newText = text.slice(0, abs) + content + text.slice(abs);
  return {
    text: newText,
    cursor: fromAbsolute(newText, abs),
  };
}

export function openLineBelow(
  text: string,
  cursor: CursorPosition,
): { text: string; cursor: CursorPosition; mode: "insert" } {
  const lines = getLines(text);
  const lineRange = getLineRange(text, cursor.line);
  const insertPos = lineRange.end + (cursor.line < lines.length - 1 ? 0 : 0);

  const newText = text.slice(0, lineRange.end) + "\n" + text.slice(lineRange.end);
  return {
    text: newText,
    cursor: { line: cursor.line + 1, col: 0 },
    mode: "insert",
  };
}

export function openLineAbove(
  text: string,
  cursor: CursorPosition,
): { text: string; cursor: CursorPosition; mode: "insert" } {
  const lineRange = getLineRange(text, cursor.line);
  const newText =
    text.slice(0, lineRange.start) + "\n" + text.slice(lineRange.start);
  return {
    text: newText,
    cursor: { line: cursor.line, col: 0 },
    mode: "insert",
  };
}

export function indentLines(
  text: string,
  startLine: number,
  count: number,
  indentStr = "  ",
): { text: string; cursor: CursorPosition } {
  const lines = getLines(text);
  const endLine = Math.min(startLine + count - 1, lines.length - 1);

  for (let i = startLine; i <= endLine; i++) {
    lines[i] = indentStr + lines[i]!;
  }

  const newText = lines.join("\n");
  const lineContent = lines[startLine]!;
  const firstNonWs = lineContent.match(/\S/);
  return {
    text: newText,
    cursor: { line: startLine, col: firstNonWs ? firstNonWs.index! : 0 },
  };
}

export function dedentLines(
  text: string,
  startLine: number,
  count: number,
  indentStr = "  ",
): { text: string; cursor: CursorPosition } {
  const lines = getLines(text);
  const endLine = Math.min(startLine + count - 1, lines.length - 1);

  for (let i = startLine; i <= endLine; i++) {
    const line = lines[i]!;
    if (line.startsWith(indentStr)) {
      lines[i] = line.slice(indentStr.length);
    } else {
      // remove as much leading whitespace as possible up to indent length
      let removeCount = 0;
      while (removeCount < indentStr.length && removeCount < line.length && line[removeCount] === " ") {
        removeCount++;
      }
      lines[i] = line.slice(removeCount);
    }
  }

  const newText = lines.join("\n");
  const lineContent = lines[startLine]!;
  const firstNonWs = lineContent.match(/\S/);
  return {
    text: newText,
    cursor: { line: startLine, col: firstNonWs ? firstNonWs.index! : 0 },
  };
}
