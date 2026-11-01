import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_BINDINGS } from "./defaultBindings.js";
import { parseBindings } from "./parser.js";
import {
  getBindingDisplayText,
  resolveKeyWithChordState,
} from "./resolver.js";
import { getKeybindingContextPriority } from "./types.js";
import {
  getRawTerminalKeyNames,
  isRawTerminalControlInput,
} from "../utils/terminalInput.js";

function createKey(overrides: Record<string, boolean> = {}) {
  return {
    upArrow: false,
    downArrow: false,
    leftArrow: false,
    rightArrow: false,
    pageDown: false,
    pageUp: false,
    return: false,
    escape: false,
    ctrl: false,
    shift: false,
    tab: false,
    backspace: false,
    delete: false,
    meta: false,
    ...overrides,
  };
}

test("resolveKeyWithChordState starts and completes chat chords", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);
  const first = resolveKeyWithChordState(
    "x",
    createKey({ ctrl: true }),
    ["Chat", "Global"],
    bindings,
    null,
  );

  assert.deepEqual(first.type, "chord_started");

  if (first.type !== "chord_started") {
    return;
  }

  const second = resolveKeyWithChordState(
    "e",
    createKey({ ctrl: true }),
    ["Chat", "Global"],
    bindings,
    first.pending,
  );

  assert.deepEqual(second, {
    type: "match",
    action: "chat:externalEditor",
  });
});

test("resolveKeyWithChordState prioritizes autocomplete bindings", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);
  for (const [input, key, action] of [
    ["", createKey({ tab: true }), "autocomplete:accept"],
    ["", createKey({ upArrow: true }), "autocomplete:previous"],
    ["", createKey({ downArrow: true }), "autocomplete:next"],
  ] as const) {
    const result = resolveKeyWithChordState(
      input,
      key,
      ["Autocomplete", "Chat", "Global"],
      bindings,
      null,
    );

    assert.deepEqual(result, { type: "match", action });
  }

  assert.deepEqual(
    resolveKeyWithChordState(
      "",
      createKey({ return: true }),
      ["Autocomplete", "Chat", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "chat:submit" },
  );
});

test("prompt context leaves confirmation shortcuts available for text input", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);
  const promptContexts = ["Prompt", "Global"] as const;

  for (const [input, key] of [
    ["y", createKey()],
    ["n", createKey()],
    ["a", createKey()],
    ["3", createKey()],
    ["", createKey({ return: true })],
    ["", createKey({ escape: true })],
  ] as const) {
    assert.deepEqual(
      resolveKeyWithChordState(input, key, [...promptContexts], bindings, null),
      { type: "none" },
    );
  }

  assert.ok(
    getKeybindingContextPriority("Prompt") >
      getKeybindingContextPriority("Confirmation"),
  );
});

test("confirmation digit shortcuts select explicit option slots", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);

  assert.deepEqual(
    resolveKeyWithChordState(
      "3",
      createKey(),
      ["Confirmation", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "confirm:digit3" },
  );
});

test("permission edit context unbinds text shortcuts for raw JSON editing", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);

  assert.deepEqual(
    resolveKeyWithChordState(
      "y",
      createKey(),
      ["PermissionEdit", "Confirmation", "Global"],
      bindings,
      null,
    ),
    { type: "unbound" },
  );
  assert.deepEqual(
    resolveKeyWithChordState(
      "e",
      createKey(),
      ["Confirmation", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "permission:editInput" },
  );
  assert.deepEqual(
    resolveKeyWithChordState(
      "e",
      createKey(),
      ["PermissionEdit", "Confirmation", "Global"],
      bindings,
      null,
    ),
    { type: "unbound" },
  );
  assert.deepEqual(
    resolveKeyWithChordState(
      "",
      createKey({ return: true }),
      ["PermissionEdit", "Confirmation", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "confirm:yes" },
  );
});

test("raw terminal delete keys resolve as configured key names", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);

  assert.deepEqual(
    resolveKeyWithChordState(
      "\u007f",
      createKey(),
      ["Transcript", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "transcript:clearSelection" },
  );

  assert.deepEqual(
    resolveKeyWithChordState(
      "",
      createKey(),
      ["Transcript", "Global"],
      bindings,
      null,
    ),
    { type: "none" },
  );
});

test("raw terminal page and wheel sequences resolve to scroll actions", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);

  assert.deepEqual(
    resolveKeyWithChordState(
      "\u001b[5~",
      createKey(),
      ["Chat", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "scroll:pageUp" },
  );

  assert.deepEqual(
    resolveKeyWithChordState(
      "\u001b[<64;20;10M",
      createKey(),
      ["Chat", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "scroll:wheelUp" },
  );

  assert.deepEqual(
    resolveKeyWithChordState(
      "\u001b[<65;20;10M",
      createKey(),
      ["Chat", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "scroll:wheelDown" },
  );

  assert.deepEqual(getRawTerminalKeyNames("\u001b[1~"), ["home"]);
  assert.deepEqual(getRawTerminalKeyNames("[1~"), ["home"]);
  assert.deepEqual(getRawTerminalKeyNames("\u001b[4~"), ["end"]);
  assert.deepEqual(getRawTerminalKeyNames("[4~"), ["end"]);

  assert.deepEqual(
    resolveKeyWithChordState(
      "\u001b[1~",
      createKey(),
      ["Chat", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "scroll:top" },
  );

  assert.deepEqual(
    resolveKeyWithChordState(
      "\u001b[4~",
      createKey(),
      ["Chat", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "scroll:bottom" },
  );
});

test("terminal mouse input without escape prefix is consumed as raw control input", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);
  const rawWheelChunk = "[<64;52;18M[<64;52;18M[<65;52;18M";

  assert.deepEqual(getRawTerminalKeyNames(rawWheelChunk), [
    "wheelup",
    "wheelup",
    "wheeldown",
  ]);
  assert.equal(isRawTerminalControlInput(rawWheelChunk), true);
  assert.deepEqual(
    resolveKeyWithChordState(
      "[<64;52;18M",
      createKey(),
      ["Chat", "Global"],
      bindings,
      null,
    ),
    { type: "match", action: "scroll:wheelUp" },
  );
});

test("getBindingDisplayText returns the configured shortcut", () => {
  const bindings = parseBindings(DEFAULT_BINDINGS);
  assert.equal(
    getBindingDisplayText("chat:submit", "Chat", bindings),
    "Enter",
  );
  assert.equal(
    getBindingDisplayText(
      "transcript:toggleShowAll",
      "Transcript",
      bindings,
    ),
    "ctrl+e",
  );
});
