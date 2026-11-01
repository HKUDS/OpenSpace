import type {
  Chord,
  KeybindingBlock,
  ParsedBinding,
  ParsedKeystroke,
} from "./types.js";

export function parseKeystroke(input: string): ParsedKeystroke {
  const parts = input.split("+");
  const keystroke: ParsedKeystroke = {
    key: "",
    ctrl: false,
    alt: false,
    shift: false,
    meta: false,
    super: false,
  };

  for (const part of parts) {
    const lower = part.toLowerCase();

    switch (lower) {
      case "ctrl":
      case "control":
        keystroke.ctrl = true;
        break;
      case "alt":
      case "opt":
      case "option":
        keystroke.alt = true;
        break;
      case "shift":
        keystroke.shift = true;
        break;
      case "meta":
        keystroke.meta = true;
        break;
      case "cmd":
      case "command":
      case "super":
      case "win":
        keystroke.super = true;
        break;
      case "esc":
        keystroke.key = "escape";
        break;
      case "return":
        keystroke.key = "enter";
        break;
      case "space":
        keystroke.key = " ";
        break;
      case "↑":
        keystroke.key = "up";
        break;
      case "↓":
        keystroke.key = "down";
        break;
      case "←":
        keystroke.key = "left";
        break;
      case "→":
        keystroke.key = "right";
        break;
      default:
        keystroke.key = lower;
        break;
    }
  }

  return keystroke;
}

export function parseChord(input: string): Chord {
  if (input === " ") {
    return [parseKeystroke("space")];
  }

  return input.trim().split(/\s+/).map(parseKeystroke);
}

function keyToDisplayName(key: string): string {
  switch (key) {
    case "escape":
      return "Esc";
    case " ":
      return "Space";
    case "tab":
      return "Tab";
    case "enter":
      return "Enter";
    case "backspace":
      return "Backspace";
    case "delete":
      return "Delete";
    case "up":
      return "Up";
    case "down":
      return "Down";
    case "left":
      return "Left";
    case "right":
      return "Right";
    case "pageup":
      return "PageUp";
    case "pagedown":
      return "PageDown";
    case "home":
      return "Home";
    case "end":
      return "End";
    default:
      return key;
  }
}

export function keystrokeToString(keystroke: ParsedKeystroke): string {
  const parts: string[] = [];

  if (keystroke.ctrl) parts.push("ctrl");
  if (keystroke.alt) parts.push("alt");
  if (keystroke.shift) parts.push("shift");
  if (keystroke.meta) parts.push("meta");
  if (keystroke.super) parts.push("cmd");
  parts.push(keyToDisplayName(keystroke.key));

  return parts.join("+");
}

export function chordToString(chord: Chord): string {
  return chord.map(keystrokeToString).join(" ");
}

export function parseBindings(blocks: KeybindingBlock[]): ParsedBinding[] {
  const bindings: ParsedBinding[] = [];

  for (const block of blocks) {
    for (const [key, action] of Object.entries(block.bindings)) {
      bindings.push({
        chord: parseChord(key),
        action,
        context: block.context,
      });
    }
  }

  return bindings;
}
