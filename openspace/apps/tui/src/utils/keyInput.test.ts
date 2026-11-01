import assert from "node:assert/strict";
import test from "node:test";
import type { Key } from "ink";
import {
  isBackspaceInput,
  isDeleteInput,
} from "./keyInput.js";

const emptyKey = {} as Key;

test("recognizes terminal backspace variants", () => {
  assert.equal(isBackspaceInput("", { backspace: true } as Key), true);
  assert.equal(isBackspaceInput("\u0008", emptyKey), true);
  assert.equal(isBackspaceInput("\u007f", emptyKey), true);
  assert.equal(
    isBackspaceInput("", { sequence: "\u007f" } as Key & { sequence: string }),
    true,
  );
  assert.equal(
    isBackspaceInput("", { name: "backspace" } as Key & { name: string }),
    true,
  );
  assert.equal(isBackspaceInput("h", { ctrl: true } as Key), true);
});

test("recognizes terminal delete variants", () => {
  assert.equal(isDeleteInput("", { delete: true } as Key), true);
  assert.equal(isDeleteInput("\u001b[3~", emptyKey), true);
  assert.equal(
    isDeleteInput("", { sequence: "\u001b[3;5~" } as Key & { sequence: string }),
    true,
  );
  assert.equal(
    isDeleteInput("", { name: "delete" } as Key & { name: string }),
    true,
  );
  assert.equal(isDeleteInput("d", emptyKey), false);
});
