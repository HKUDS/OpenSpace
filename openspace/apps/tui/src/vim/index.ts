export type {
  VimMode,
  CursorPosition,
  SelectionRange,
  CommandState,
  PersistentState,
  VimState,
} from "./types.js";

export {
  createDefaultVimState,
  createDefaultCommandState,
  createDefaultPersistentState,
} from "./types.js";

export {
  h, l, j, k,
  w, b, e, W, B, E,
  lineStart, firstNonWhitespace, lineEnd,
  gotoLastLine, gotoFirstLine,
  findCharForward, findCharBackward,
  tillCharForward, tillCharBackward,
  getLines, getLineRange,
} from "./motions.js";

export {
  innerWord, aWord,
  innerWORD, aWORD,
  innerQuote, aQuote,
  innerParen, aParen,
  innerAngle, aAngle,
} from "./textObjects.js";

export {
  deleteRange, changeRange, yankRange,
  deleteLines, changeLines, yankLines,
  deleteCharUnderCursor, replaceChar,
  toggleCase, joinLines,
  pasteAfter, pasteBefore,
  openLineBelow, openLineAbove,
  indentLines, dedentLines,
} from "./operators.js";

export type { VimInputResult } from "./transitions.js";
export { processVimInput } from "./transitions.js";
