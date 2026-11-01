import type { VimState, SelectionRange, CursorPosition } from "./types.js";
import { createDefaultCommandState } from "./types.js";
import * as motions from "./motions.js";
import * as textObjects from "./textObjects.js";
import * as operators from "./operators.js";

export interface VimInputResult {
  state: VimState;
  text: string;
  handled: boolean;
}

function cloneState(state: VimState): VimState {
  return {
    mode: state.mode,
    command: { ...state.command },
    persistent: {
      ...state.persistent,
      registers: { ...state.persistent.registers },
      lastCommand: state.persistent.lastCommand
        ? { ...state.persistent.lastCommand }
        : null,
    },
    cursor: { ...state.cursor },
    selection: state.selection
      ? { start: { ...state.selection.start }, end: { ...state.selection.end } }
      : null,
  };
}

function storeYanked(state: VimState, yanked: string | undefined): void {
  if (yanked !== undefined) {
    state.persistent.registers['"'] = yanked;
    if (state.command.register) {
      state.persistent.registers[state.command.register] = yanked;
    }
  }
}

function getCount(state: VimState): number {
  const c = parseInt(state.command.count, 10);
  return isNaN(c) || c < 1 ? 1 : c;
}

function applyMotion(
  text: string,
  cursor: CursorPosition,
  key: string,
  count: number,
  charArg?: string,
): CursorPosition | null {
  switch (key) {
    case "h": return motions.h(text, cursor, count);
    case "l": return motions.l(text, cursor, count);
    case "j": return motions.j(text, cursor, count);
    case "k": return motions.k(text, cursor, count);
    case "w": return motions.w(text, cursor, count);
    case "b": return motions.b(text, cursor, count);
    case "e": return motions.e(text, cursor, count);
    case "W": return motions.W(text, cursor, count);
    case "B": return motions.B(text, cursor, count);
    case "E": return motions.E(text, cursor, count);
    case "0": return motions.lineStart(text, cursor);
    case "^": return motions.firstNonWhitespace(text, cursor);
    case "$": return motions.lineEnd(text, cursor);
    case "G": return motions.gotoLastLine(text);
    case "f": return charArg ? motions.findCharForward(text, cursor, charArg, count) : null;
    case "F": return charArg ? motions.findCharBackward(text, cursor, charArg, count) : null;
    case "t": return charArg ? motions.tillCharForward(text, cursor, charArg, count) : null;
    case "T": return charArg ? motions.tillCharBackward(text, cursor, charArg, count) : null;
    default: return null;
  }
}

function applyTextObject(
  text: string,
  cursor: CursorPosition,
  modifier: "i" | "a",
  obj: string,
): SelectionRange | null {
  switch (obj) {
    case "w":
      return modifier === "i"
        ? textObjects.innerWord(text, cursor)
        : textObjects.aWord(text, cursor);
    case "W":
      return modifier === "i"
        ? textObjects.innerWORD(text, cursor)
        : textObjects.aWORD(text, cursor);
    case '"':
    case "'":
    case "`":
      return modifier === "i"
        ? textObjects.innerQuote(text, cursor, obj)
        : textObjects.aQuote(text, cursor, obj);
    case "(":
    case ")":
    case "[":
    case "]":
    case "{":
    case "}":
    case "b":
    case "B":
      return modifier === "i"
        ? textObjects.innerParen(text, cursor, obj)
        : textObjects.aParen(text, cursor, obj);
    case "<":
    case ">":
      return modifier === "i"
        ? textObjects.innerAngle(text, cursor)
        : textObjects.aAngle(text, cursor);
    default:
      return null;
  }
}

function motionToRange(
  cursor: CursorPosition,
  target: CursorPosition,
): SelectionRange {
  if (
    cursor.line < target.line ||
    (cursor.line === target.line && cursor.col <= target.col)
  ) {
    return { start: { ...cursor }, end: { ...target } };
  }
  return { start: { ...target }, end: { ...cursor } };
}

function processNormalMode(
  state: VimState,
  text: string,
  key: string,
  ctrl: boolean,
): VimInputResult {
  const s = cloneState(state);
  const count = getCount(s);

  // awaiting character argument (f, F, t, T, r)
  if (s.command.awaitingCharArg) {
    const cmd = s.command.awaitingCharArg;
    s.command.awaitingCharArg = null;

    if (cmd === "r") {
      const result = operators.replaceChar(text, s.cursor, key);
      s.cursor = result.cursor;
      s.persistent.lastCommand = {
        operator: "r",
        motion: null,
        count,
        charArg: key,
        textInserted: null,
      };
      s.command = createDefaultCommandState();
      return { state: s, text: result.text, handled: true };
    }

    // f, F, t, T with operator pending
    if (s.command.operator) {
      const target = applyMotion(text, s.cursor, cmd, count, key);
      if (target) {
        const range = motionToRange(s.cursor, target);
        const result = applyOperator(s, text, s.command.operator, range);
        s.persistent.lastCommand = {
          operator: s.command.operator,
          motion: cmd,
          count,
          charArg: key,
          textInserted: null,
        };
        s.command = createDefaultCommandState();
        return result;
      }
      s.command = createDefaultCommandState();
      return { state: s, text, handled: true };
    }

    // standalone f, F, t, T
    const target = applyMotion(text, s.cursor, cmd, count, key);
    if (target) s.cursor = target;
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  // operator pending — awaiting motion or text object
  if (s.command.operator) {
    const op = s.command.operator;

    // doubled operator = line-wise (dd, OpenSpace, yy)
    if (key === op) {
      const result = applyLinewiseOperator(s, text, op, s.cursor.line, count);
      s.persistent.lastCommand = {
        operator: op,
        motion: op,
        count,
        charArg: null,
        textInserted: null,
      };
      s.command = createDefaultCommandState();
      return result;
    }

    // text object modifier
    if (key === "i" || key === "a") {
      s.command.partialCommand = key;
      return { state: s, text, handled: true };
    }

    // text object after modifier
    if (s.command.partialCommand === "i" || s.command.partialCommand === "a") {
      const modifier = s.command.partialCommand as "i" | "a";
      const range = applyTextObject(text, s.cursor, modifier, key);
      if (range) {
        const result = applyOperator(s, text, op, range);
        s.persistent.lastCommand = {
          operator: op,
          motion: modifier + key,
          count,
          charArg: null,
          textInserted: null,
        };
        return result;
      }
      s.command = createDefaultCommandState();
      return { state: s, text, handled: true };
    }

    // char-seeking motions
    if ("fFtT".includes(key)) {
      s.command.awaitingCharArg = key;
      return { state: s, text, handled: true };
    }

    // regular motion
    const target = applyMotion(text, s.cursor, key, count);
    if (target) {
      const range = motionToRange(s.cursor, target);
      const result = applyOperator(s, text, op, range);
      s.persistent.lastCommand = {
        operator: op,
        motion: key,
        count,
        charArg: null,
        textInserted: null,
      };
      return result;
    }

    // invalid — reset
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  // count prefix
  if (key >= "1" && key <= "9") {
    s.command.count += key;
    return { state: s, text, handled: true };
  }
  if (key === "0" && s.command.count.length > 0) {
    s.command.count += key;
    return { state: s, text, handled: true };
  }

  // mode switches
  if (key === "i") {
    s.mode = "insert";
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }
  if (key === "a") {
    s.mode = "insert";
    const lines = motions.getLines(text);
    const lineLen = lines[s.cursor.line]?.length ?? 0;
    s.cursor.col = Math.min(s.cursor.col + 1, lineLen);
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }
  if (key === "A") {
    s.mode = "insert";
    const lines = motions.getLines(text);
    s.cursor.col = lines[s.cursor.line]?.length ?? 0;
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }
  if (key === "I") {
    s.mode = "insert";
    s.cursor = motions.firstNonWhitespace(text, s.cursor);
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }
  if (key === "v") {
    s.mode = "visual";
    s.selection = { start: { ...s.cursor }, end: { ...s.cursor } };
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }
  if (key === "R") {
    s.mode = "replace";
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  // operators
  if (key === "d" || key === "c" || key === "y") {
    s.command.operator = key;
    return { state: s, text, handled: true };
  }

  // > and < (indent/dedent) — wait for second > or <
  if (key === ">") {
    s.command.operator = ">";
    return { state: s, text, handled: true };
  }
  if (key === "<") {
    s.command.operator = "<";
    return { state: s, text, handled: true };
  }

  // direct commands
  if (key === "x") {
    const result = operators.deleteCharUnderCursor(text, s, s.cursor, count);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    s.persistent.lastCommand = {
      operator: "x",
      motion: null,
      count,
      charArg: null,
      textInserted: null,
    };
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "r") {
    s.command.awaitingCharArg = "r";
    return { state: s, text, handled: true };
  }

  if (key === "~") {
    const result = operators.toggleCase(text, s.cursor, count);
    s.cursor = result.cursor;
    s.persistent.lastCommand = {
      operator: "~",
      motion: null,
      count,
      charArg: null,
      textInserted: null,
    };
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "J") {
    let result = { text, cursor: s.cursor };
    for (let i = 0; i < count; i++) {
      result = operators.joinLines(result.text, result.cursor);
    }
    s.cursor = result.cursor;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "p") {
    const content = s.persistent.registers['"'] ?? "";
    const result = operators.pasteAfter(text, s.cursor, content);
    s.cursor = result.cursor;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "P") {
    const content = s.persistent.registers['"'] ?? "";
    const result = operators.pasteBefore(text, s.cursor, content);
    s.cursor = result.cursor;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "o") {
    const result = operators.openLineBelow(text, s.cursor);
    s.cursor = result.cursor;
    s.mode = result.mode;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "O") {
    const result = operators.openLineAbove(text, s.cursor);
    s.cursor = result.cursor;
    s.mode = result.mode;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "u") {
    // undo placeholder — not handled, let the consumer manage undo
    s.command = createDefaultCommandState();
    return { state: s, text, handled: false };
  }

  if (key === ".") {
    const last = s.persistent.lastCommand;
    if (last) {
      return replayLastCommand(s, text, last);
    }
    return { state: s, text, handled: true };
  }

  // gg
  if (key === "g") {
    if (s.command.partialCommand === "g") {
      s.cursor = motions.gotoFirstLine(text);
      s.command = createDefaultCommandState();
      return { state: s, text, handled: true };
    }
    s.command.partialCommand = "g";
    return { state: s, text, handled: true };
  }

  // 0 as motion (when not part of count)
  if (key === "0") {
    s.cursor = motions.lineStart(text, s.cursor);
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  // char-seeking motions
  if ("fFtT".includes(key)) {
    s.command.awaitingCharArg = key;
    return { state: s, text, handled: true };
  }

  // simple motions
  const target = applyMotion(text, s.cursor, key, count);
  if (target) {
    s.cursor = target;
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  // ctrl shortcuts
  if (ctrl) {
    if (key === "f") {
      // page down — approximate with 20 lines
      s.cursor = motions.j(text, s.cursor, 20);
      s.command = createDefaultCommandState();
      return { state: s, text, handled: true };
    }
    if (key === "b") {
      s.cursor = motions.k(text, s.cursor, 20);
      s.command = createDefaultCommandState();
      return { state: s, text, handled: true };
    }
  }

  s.command = createDefaultCommandState();
  return { state: s, text, handled: false };
}

function applyOperator(
  state: VimState,
  text: string,
  op: string,
  range: SelectionRange,
): VimInputResult {
  const s = cloneState(state);
  s.command = createDefaultCommandState();

  if (op === "d") {
    const result = operators.deleteRange(text, s, range);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  if (op === "c") {
    const result = operators.changeRange(text, s, range);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    s.mode = result.mode;
    return { state: s, text: result.text, handled: true };
  }

  if (op === "y") {
    const result = operators.yankRange(text, s, range);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  return { state: s, text, handled: true };
}

function applyLinewiseOperator(
  state: VimState,
  text: string,
  op: string,
  line: number,
  count: number,
): VimInputResult {
  const s = cloneState(state);
  s.command = createDefaultCommandState();

  if (op === "d") {
    const result = operators.deleteLines(text, s, line, count);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  if (op === "c") {
    const result = operators.changeLines(text, s, line, count);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    s.mode = result.mode;
    return { state: s, text: result.text, handled: true };
  }

  if (op === "y") {
    const result = operators.yankLines(text, s, line, count);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  if (op === ">") {
    const result = operators.indentLines(text, line, count);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  if (op === "<") {
    const result = operators.dedentLines(text, line, count);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  return { state: s, text, handled: true };
}

function replayLastCommand(
  state: VimState,
  text: string,
  last: NonNullable<VimState["persistent"]["lastCommand"]>,
): VimInputResult {
  const s = cloneState(state);
  const count = last.count;

  if (last.operator === "x") {
    const result = operators.deleteCharUnderCursor(text, s, s.cursor, count);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  if (last.operator === "~") {
    const result = operators.toggleCase(text, s.cursor, count);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  if (last.operator === "r" && last.charArg) {
    const result = operators.replaceChar(text, s.cursor, last.charArg);
    s.cursor = result.cursor;
    return { state: s, text: result.text, handled: true };
  }

  if (last.operator && last.motion) {
    // line-wise doubled operator
    if (last.operator === last.motion) {
      return applyLinewiseOperator(s, text, last.operator, s.cursor.line, count);
    }

    // text object
    if (
      last.motion.length === 2 &&
      (last.motion[0] === "i" || last.motion[0] === "a")
    ) {
      const modifier = last.motion[0] as "i" | "a";
      const obj = last.motion[1]!;
      const range = applyTextObject(text, s.cursor, modifier, obj);
      if (range) {
        return applyOperator(s, text, last.operator, range);
      }
      return { state: s, text, handled: true };
    }

    // motion
    const target = applyMotion(text, s.cursor, last.motion, count, last.charArg ?? undefined);
    if (target) {
      const range = motionToRange(s.cursor, target);
      return applyOperator(s, text, last.operator, range);
    }
  }

  return { state: s, text, handled: true };
}

function processVisualMode(
  state: VimState,
  text: string,
  key: string,
  _ctrl: boolean,
): VimInputResult {
  const s = cloneState(state);

  if (key === "escape" || key === "Escape") {
    s.mode = "normal";
    s.selection = null;
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  // operators on selection
  if ((key === "d" || key === "x") && s.selection) {
    const result = operators.deleteRange(text, s, s.selection);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    s.mode = "normal";
    s.selection = null;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "c" && s.selection) {
    const result = operators.changeRange(text, s, s.selection);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    s.mode = "insert";
    s.selection = null;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "y" && s.selection) {
    const result = operators.yankRange(text, s, s.selection);
    storeYanked(s, result.yanked);
    s.cursor = result.cursor;
    s.mode = "normal";
    s.selection = null;
    s.command = createDefaultCommandState();
    return { state: s, text: result.text, handled: true };
  }

  if (key === "~" && s.selection) {
    const { start, end } = s.selection;
    const startAbs = toAbsoluteFromCursor(text, start);
    const endAbs = toAbsoluteFromCursor(text, end);
    const lo = Math.min(startAbs, endAbs);
    const hi = Math.max(startAbs, endAbs);
    let result = text;
    for (let i = lo; i <= hi; i++) {
      const ch = result[i]!;
      const toggled = ch === ch.toUpperCase() ? ch.toLowerCase() : ch.toUpperCase();
      result = result.slice(0, i) + toggled + result.slice(i + 1);
    }
    s.mode = "normal";
    s.selection = null;
    s.command = createDefaultCommandState();
    return { state: s, text: result, handled: true };
  }

  // extend selection with motions
  const count = getCount(s);
  if (key >= "1" && key <= "9") {
    s.command.count += key;
    return { state: s, text, handled: true };
  }
  if (key === "0" && s.command.count.length > 0) {
    s.command.count += key;
    return { state: s, text, handled: true };
  }

  const target = applyMotion(text, s.cursor, key, count);
  if (target) {
    s.cursor = target;
    if (s.selection) {
      s.selection.end = { ...target };
    }
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  if (key === "0") {
    s.cursor = motions.lineStart(text, s.cursor);
    if (s.selection) s.selection.end = { ...s.cursor };
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  return { state: s, text, handled: false };
}

function toAbsoluteFromCursor(text: string, pos: CursorPosition): number {
  const lines = motions.getLines(text);
  let offset = 0;
  for (let i = 0; i < pos.line && i < lines.length; i++) {
    offset += lines[i]!.length + 1;
  }
  return offset + pos.col;
}

function processReplaceMode(
  state: VimState,
  text: string,
  key: string,
): VimInputResult {
  const s = cloneState(state);

  if (key === "escape" || key === "Escape") {
    s.mode = "normal";
    s.command = createDefaultCommandState();
    return { state: s, text, handled: true };
  }

  if (key.length === 1) {
    const result = operators.replaceChar(text, s.cursor, key);
    s.cursor = { line: result.cursor.line, col: result.cursor.col + 1 };
    // clamp cursor
    const lines = motions.getLines(result.text);
    const lineLen = lines[s.cursor.line]?.length ?? 0;
    if (s.cursor.col >= lineLen) {
      s.cursor.col = Math.max(0, lineLen - 1);
    }
    return { state: s, text: result.text, handled: true };
  }

  return { state: s, text, handled: false };
}

export function processVimInput(
  state: VimState,
  text: string,
  key: string,
  ctrl: boolean,
  _meta: boolean,
): VimInputResult {
  if (state.mode === "insert") {
    if (key === "escape" || key === "Escape") {
      const s = cloneState(state);
      s.mode = "normal";
      // in vim, cursor moves back one when entering normal mode
      if (s.cursor.col > 0) {
        s.cursor.col--;
      }
      s.command = createDefaultCommandState();
      return { state: s, text, handled: true };
    }
    // ctrl+[ also exits insert mode
    if (ctrl && key === "[") {
      const s = cloneState(state);
      s.mode = "normal";
      if (s.cursor.col > 0) {
        s.cursor.col--;
      }
      s.command = createDefaultCommandState();
      return { state: s, text, handled: true };
    }
    return { state, text, handled: false };
  }

  if (state.mode === "normal") {
    return processNormalMode(state, text, key, ctrl);
  }

  if (state.mode === "visual") {
    return processVisualMode(state, text, key, ctrl);
  }

  if (state.mode === "replace") {
    return processReplaceMode(state, text, key);
  }

  return { state, text, handled: false };
}
