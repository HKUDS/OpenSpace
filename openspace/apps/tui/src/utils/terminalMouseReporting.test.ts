import assert from "node:assert/strict";
import test from "node:test";
import { shouldEnableTerminalMouseReporting } from "./terminalMouseReporting.js";

test("terminal mouse reporting is opt-in", () => {
  assert.equal(shouldEnableTerminalMouseReporting({}), false);
  assert.equal(
    shouldEnableTerminalMouseReporting({
      OPENSPACE_TUI_MOUSE_REPORTING: "1",
    }),
    true,
  );
  assert.equal(
    shouldEnableTerminalMouseReporting({
      OPENSPACE_TUI_ENABLE_MOUSE: "1",
    }),
    true,
  );
});
