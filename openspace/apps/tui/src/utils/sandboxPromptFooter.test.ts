import assert from "node:assert/strict";
import test from "node:test";
import { sandboxHint } from "./sandboxPromptFooter.js";

test("sandbox violation hint points to implemented details command", () => {
  const hint = sandboxHint({
    status: "warn",
    sandboxing_enabled: true,
    enabled_in_settings: true,
    mode: "regular",
    recent_violation_count: 2,
  });

  assert.equal(
    hint?.text,
    "Sandbox blocked 2 operations | /sandbox status for details | /sandbox to configure",
  );
});

test("sandbox violation hint does not advertise transcript shortcut", () => {
  const hint = sandboxHint({
    status: "warn",
    sandboxing_enabled: true,
    enabled_in_settings: true,
    mode: "regular",
    recent_violations: [{ command: "cat .env" }],
  });

  assert.equal(hint?.text.includes("Ctrl+O"), false);
});
