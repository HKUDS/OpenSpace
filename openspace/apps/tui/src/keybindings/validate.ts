import { chordToString, parseBindings, parseChord } from "./parser.js";
import { isReservedShortcut } from "./reservedShortcuts.js";
import {
  KEYBINDING_ACTIONS,
  KEYBINDING_CONTEXTS,
  type KeybindingBlock,
  type KeybindingWarning,
  type ParsedBinding,
} from "./types.js";

export type ValidationResult = {
  bindings: ParsedBinding[];
  warnings: KeybindingWarning[];
};

const VALID_CONTEXTS = new Set<string>(KEYBINDING_CONTEXTS);
const VALID_ACTIONS = new Set<string>(KEYBINDING_ACTIONS);

function toWarning(
  message: string,
  severity: KeybindingWarning["severity"] = "error",
  suggestion?: string,
): KeybindingWarning {
  return {
    type: "validation",
    severity,
    message,
    suggestion,
  };
}

function isKeybindingBlockArray(value: unknown): value is KeybindingBlock[] {
  return (
    Array.isArray(value) &&
    value.every(item => typeof item === "object" && item !== null)
  );
}

export function validateKeybindingsConfig(config: unknown): ValidationResult {
  const defaultResult: ValidationResult = {
    bindings: [],
    warnings: [],
  };

  if (!config || typeof config !== "object") {
    return {
      bindings: [],
      warnings: [
        toWarning(
          'keybindings.json must be an object with a "bindings" array',
          "error",
          'Use the format: { "bindings": [ ... ] }',
        ),
      ],
    };
  }

  const wrapper = config as Record<string, unknown>;

  if (!isKeybindingBlockArray(wrapper.bindings)) {
    return {
      bindings: [],
      warnings: [
        toWarning(
          '"bindings" must be an array of keybinding blocks',
          "error",
          'Each block must look like { "context": "Chat", "bindings": { "enter": "chat:submit" } }',
        ),
      ],
    };
  }

  const warnings: KeybindingWarning[] = [];
  const seenShortcuts = new Map<string, string>();

  for (const [blockIndex, block] of wrapper.bindings.entries()) {
    if (!VALID_CONTEXTS.has(block.context)) {
      warnings.push(
        toWarning(
          `bindings[${blockIndex}] uses unknown context "${block.context}"`,
          "error",
        ),
      );
      continue;
    }

    for (const [shortcut, action] of Object.entries(block.bindings)) {
      try {
        parseChord(shortcut);
      } catch (error) {
        warnings.push(
          toWarning(
            `Invalid keybinding "${shortcut}" in ${block.context}: ${error instanceof Error ? error.message : String(error)}`,
            "error",
          ),
        );
        continue;
      }

      if (
        action !== null &&
        typeof action === "string" &&
        !VALID_ACTIONS.has(action) &&
        !action.startsWith("command:")
      ) {
        warnings.push(
          toWarning(
            `Unsupported action "${action}" in ${block.context}`,
            "error",
          ),
        );
      }

      const parsedShortcut = chordToString(parseChord(shortcut));
      const qualifiedShortcut = `${block.context}:${parsedShortcut}`;

      if (
        typeof action === "string" &&
        isReservedShortcut(parsedShortcut) &&
        action !== "app:interrupt" &&
        action !== "app:exit"
      ) {
        warnings.push(
          toWarning(
            `"${parsedShortcut}" is reserved and cannot be rebound to "${action}"`,
            "error",
          ),
        );
      }

      const previous = seenShortcuts.get(qualifiedShortcut);
      if (previous && previous !== String(action)) {
        warnings.push(
          toWarning(
            `"${parsedShortcut}" in ${block.context} overrides "${previous}" with "${String(action)}"`,
            "warning",
          ),
        );
      }
      seenShortcuts.set(qualifiedShortcut, String(action));
    }
  }

  const hasErrors = warnings.some(warning => warning.severity === "error");
  if (hasErrors) {
    return {
      bindings: [],
      warnings,
    };
  }

  return {
    bindings: parseBindings(wrapper.bindings),
    warnings,
  };
}
