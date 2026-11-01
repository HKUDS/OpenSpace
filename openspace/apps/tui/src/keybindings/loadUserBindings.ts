import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DEFAULT_BINDINGS } from "./defaultBindings.js";
import { parseBindings } from "./parser.js";
import { validateKeybindingsConfig } from "./validate.js";
import type {
  KeybindingBlock,
  KeybindingWarning,
  ParsedBinding,
} from "./types.js";

export type KeybindingsLoadResult = {
  bindings: ParsedBinding[];
  warnings: KeybindingWarning[];
};

let cachedResult: KeybindingsLoadResult | null = null;

export function getKeybindingsPath(): string {
  return path.join(os.homedir(), ".openspace", "keybindings.json");
}

function mergeBindings(
  defaults: KeybindingBlock[],
  overrides: KeybindingBlock[],
): KeybindingBlock[] {
  return [...defaults, ...overrides];
}

function parseUserConfig(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function loadKeybindingsSyncWithWarnings(): KeybindingsLoadResult {
  if (cachedResult) {
    return cachedResult;
  }

  const defaultBindings = parseBindings(DEFAULT_BINDINGS);
  const filePath = getKeybindingsPath();

  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = parseUserConfig(raw);

    if (!parsed) {
      cachedResult = {
        bindings: defaultBindings,
        warnings: [
          {
            type: "parse_error",
            severity: "error",
            message: `Invalid JSON in ${filePath}`,
          },
        ],
      };
      return cachedResult;
    }

    const validation = validateKeybindingsConfig(parsed);

    if (validation.bindings.length === 0) {
      cachedResult = {
        bindings: defaultBindings,
        warnings: validation.warnings,
      };
      return cachedResult;
    }

    const wrapper = parsed as { bindings: KeybindingBlock[] };
    cachedResult = {
      bindings: parseBindings(mergeBindings(DEFAULT_BINDINGS, wrapper.bindings)),
      warnings: validation.warnings,
    };
    return cachedResult;
  } catch (error) {
    if (
      error instanceof Error &&
      "code" in error &&
      error.code === "ENOENT"
    ) {
      cachedResult = {
        bindings: defaultBindings,
        warnings: [],
      };
      return cachedResult;
    }

    cachedResult = {
      bindings: defaultBindings,
      warnings: [
        {
          type: "parse_error",
          severity: "error",
          message: `Failed to load ${filePath}: ${error instanceof Error ? error.message : String(error)}`,
        },
      ],
    };
    return cachedResult;
  }
}

export function loadKeybindingsSync(): ParsedBinding[] {
  return loadKeybindingsSyncWithWarnings().bindings;
}

export function resetKeybindingsCache(): void {
  cachedResult = null;
}
