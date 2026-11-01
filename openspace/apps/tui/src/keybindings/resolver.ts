import type { Key } from "ink";
import { getKeyName } from "./match.js";
import { chordToString } from "./parser.js";
import type {
  KeybindingContextName,
  ParsedBinding,
  ParsedKeystroke,
} from "./types.js";

export type ResolveResult =
  | { type: "match"; action: string }
  | { type: "none" }
  | { type: "unbound" };

export type ChordResolveResult =
  | { type: "match"; action: string }
  | { type: "none" }
  | { type: "unbound" }
  | { type: "chord_started"; pending: ParsedKeystroke[] }
  | { type: "chord_cancelled" };

export function getBindingDisplayText(
  action: string,
  context: KeybindingContextName,
  bindings: ParsedBinding[],
): string | undefined {
  const matches = bindings.filter(
    binding => binding.action === action && binding.context === context,
  );
  const binding = matches[matches.length - 1];

  return binding ? chordToString(binding.chord) : undefined;
}

function buildKeystroke(input: string, key: Key): ParsedKeystroke | null {
  const extendedKey = key as Key & { super?: boolean };
  const keyName = getKeyName(input, extendedKey);

  if (!keyName) return null;

  const effectiveMeta = extendedKey.escape ? false : extendedKey.meta;

  return {
    key: keyName,
    ctrl: Boolean(extendedKey.ctrl),
    alt: Boolean(effectiveMeta),
    shift: Boolean(extendedKey.shift),
    meta: Boolean(effectiveMeta),
    super: Boolean(extendedKey.super),
  };
}

export function keystrokesEqual(
  left: ParsedKeystroke,
  right: ParsedKeystroke,
): boolean {
  return (
    left.key === right.key &&
    left.ctrl === right.ctrl &&
    left.shift === right.shift &&
    (left.alt || left.meta) === (right.alt || right.meta) &&
    left.super === right.super
  );
}

function chordPrefixMatches(
  prefix: ParsedKeystroke[],
  binding: ParsedBinding,
): boolean {
  if (prefix.length >= binding.chord.length) return false;

  for (let index = 0; index < prefix.length; index += 1) {
    const prefixKey = prefix[index];
    const bindingKey = binding.chord[index];

    if (!prefixKey || !bindingKey) return false;
    if (!keystrokesEqual(prefixKey, bindingKey)) return false;
  }

  return true;
}

function chordExactlyMatches(
  chord: ParsedKeystroke[],
  binding: ParsedBinding,
): boolean {
  if (chord.length !== binding.chord.length) return false;

  for (let index = 0; index < chord.length; index += 1) {
    const chordKey = chord[index];
    const bindingKey = binding.chord[index];

    if (!chordKey || !bindingKey) return false;
    if (!keystrokesEqual(chordKey, bindingKey)) return false;
  }

  return true;
}

export function resolveKeyWithChordState(
  input: string,
  key: Key,
  activeContexts: KeybindingContextName[],
  bindings: ParsedBinding[],
  pending: ParsedKeystroke[] | null,
): ChordResolveResult {
  if (key.escape && pending !== null) {
    return { type: "chord_cancelled" };
  }

  const currentKeystroke = buildKeystroke(input, key);

  if (!currentKeystroke) {
    if (pending !== null) {
      return { type: "chord_cancelled" };
    }

    return { type: "none" };
  }

  const testChord = pending
    ? [...pending, currentKeystroke]
    : [currentKeystroke];
  const contextSet = new Set(activeContexts);
  const contextBindings = bindings.filter(binding =>
    contextSet.has(binding.context),
  );

  const chordWinners = new Map<string, string | null>();

  for (const binding of contextBindings) {
    if (
      binding.chord.length > testChord.length &&
      chordPrefixMatches(testChord, binding)
    ) {
      chordWinners.set(
        chordToString(binding.chord),
        binding.action === null ? null : String(binding.action),
      );
    }
  }

  let hasLongerChords = false;
  for (const action of chordWinners.values()) {
    if (action !== null) {
      hasLongerChords = true;
      break;
    }
  }

  if (hasLongerChords) {
    return { type: "chord_started", pending: testChord };
  }

  let exactMatch: ParsedBinding | undefined;
  for (const binding of contextBindings) {
    if (chordExactlyMatches(testChord, binding)) {
      exactMatch = binding;
    }
  }

  if (exactMatch) {
    if (exactMatch.action === null) {
      return { type: "unbound" };
    }

    return { type: "match", action: String(exactMatch.action) };
  }

  if (pending !== null) {
    return { type: "chord_cancelled" };
  }

  return { type: "none" };
}
