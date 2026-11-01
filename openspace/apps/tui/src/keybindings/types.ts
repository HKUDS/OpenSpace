export const KEYBINDING_CONTEXTS = [
  "Global",
  "Chat",
  "Autocomplete",
  "Confirmation",
  "PermissionEdit",
  "Prompt",
  "MessageActions",
  "Transcript",
  "HistorySearch",
  "Task",
  "Help",
  "Footer",
] as const;

export type KeybindingContextName = (typeof KEYBINDING_CONTEXTS)[number];

export const KEYBINDING_ACTIONS = [
  "app:interrupt",
  "app:exit",
  "app:redraw",
  "app:toggleTodos",
  "app:toggleTranscript",
  "app:toggleBackgroundPanel",
  "app:toggleAgentsPanel",
  "history:search",
  "transcript:toggleShowAll",
  "transcript:export",
  "transcript:externalEditor",
  "transcript:targetCursor",
  "transcript:searchNext",
  "transcript:searchPrev",
  "transcript:openSelector",
  "transcript:confirmSelection",
  "transcript:clearSelection",
  "transcript:rewind",
  "transcript:restore",
  "transcript:selectorUp",
  "transcript:selectorDown",
  "history:previous",
  "history:next",
  "chat:cancel",
  "chat:killAgents",
  "chat:cycleMode",
  "chat:submit",
  "chat:newline",
  "chat:externalEditor",
  "autocomplete:accept",
  "autocomplete:dismiss",
  "autocomplete:previous",
  "autocomplete:next",
  "confirm:yes",
  "confirm:no",
  "confirm:previous",
  "confirm:next",
  "confirm:nextField",
  "confirm:previousField",
  "confirm:digit1",
  "confirm:digit2",
  "confirm:digit3",
  "confirm:digit4",
  "confirm:digit5",
  "confirm:digit6",
  "confirm:digit7",
  "confirm:digit8",
  "confirm:digit9",
  "permission:allowAlways",
  "permission:editInput",
  "scroll:pageUp",
  "scroll:pageDown",
  "scroll:top",
  "scroll:bottom",
  "scroll:wheelUp",
  "scroll:wheelDown",
  "messageActions:enter",
  "messageActions:escape",
  "messageActions:prev",
  "messageActions:next",
  "messageActions:top",
  "messageActions:bottom",
  "agent:focusNext",
  "agent:focusPrev",
  "agent:openViewed",
  "agent:sendInput",
  "footer:openSelected",
  "footer:clearSelection",
] as const;

export type KeybindingActionName = (typeof KEYBINDING_ACTIONS)[number];

export type ParsedKeystroke = {
  key: string;
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
  meta: boolean;
  super: boolean;
};

export type Chord = ParsedKeystroke[];

export type KeybindingActionValue = KeybindingActionName | `command:${string}` | null;

export type KeybindingBlock = {
  context: KeybindingContextName;
  bindings: Record<string, KeybindingActionValue>;
};

export type ParsedBinding = {
  chord: Chord;
  action: KeybindingActionValue;
  context: KeybindingContextName;
};

export type KeybindingWarning = {
  type: "parse_error" | "validation";
  severity: "warning" | "error";
  message: string;
  suggestion?: string;
};

export function getKeybindingContextPriority(
  context: KeybindingContextName,
): number {
  switch (context) {
    case "Prompt":
      return 95;
    case "PermissionEdit":
      return 92;
    case "Confirmation":
      return 90;
    case "Autocomplete":
      return 80;
    case "MessageActions":
      return 70;
    case "HistorySearch":
      return 65;
    case "Task":
      return 60;
    case "Chat":
      return 50;
    case "Footer":
      return 40;
    case "Transcript":
      return 30;
    case "Help":
      return 20;
    case "Global":
    default:
      return 0;
  }
}
