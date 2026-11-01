import React from "react";
import { useInput, useStdout, type Key } from "ink";
import { useNotifications } from "../context/notifications.js";
import {
  type HandlerRegistration,
  type InputHandlerRegistration,
  KeybindingProvider,
} from "./KeybindingContext.js";
import { loadKeybindingsSyncWithWarnings } from "./loadUserBindings.js";
import { resolveKeyWithChordState } from "./resolver.js";
import {
  getRawTerminalKeyNames,
  type RawTerminalKeyName,
} from "../utils/terminalInput.js";
import {
  disableTerminalMouseReporting,
  enableTerminalMouseReporting,
  shouldEnableTerminalMouseReporting,
} from "../utils/terminalMouseReporting.js";
import type {
  KeybindingContextName,
  KeybindingWarning,
  ParsedBinding,
  ParsedKeystroke,
} from "./types.js";
import { getKeybindingContextPriority as getContextPriority } from "./types.js";

const CHORD_TIMEOUT_MS = 1000;
const RAW_TERMINAL_KEY_ACTIONS: Partial<Record<RawTerminalKeyName, string>> = {
  pageup: "scroll:pageUp",
  pagedown: "scroll:pageDown",
  home: "scroll:top",
  end: "scroll:bottom",
  wheelup: "scroll:wheelUp",
  wheeldown: "scroll:wheelDown",
};
const PLAIN_INPUT_KEY = {} as Key;

type Props = {
  children: React.ReactNode;
};

function plural(count: number, noun: string): string {
  return count === 1 ? noun : `${noun}s`;
}

function useKeybindingWarnings(warnings: KeybindingWarning[]): void {
  const { addNotification, removeNotification } = useNotifications();

  React.useEffect(() => {
    const key = "keybinding-config-warning";

    if (warnings.length === 0) {
      removeNotification(key);
      return;
    }

    const errorCount = warnings.filter(
      warning => warning.severity === "error",
    ).length;
    const warningCount = warnings.filter(
      warning => warning.severity === "warning",
    ).length;

    let text: string;
    if (errorCount > 0 && warningCount > 0) {
      text =
        `Found ${errorCount} keybinding ${plural(errorCount, "error")} ` +
        `and ${warningCount} ${plural(warningCount, "warning")} · /doctor for details`;
    } else if (errorCount > 0) {
      text =
        `Found ${errorCount} keybinding ${plural(errorCount, "error")} · /doctor for details`;
    } else {
      text =
        `Found ${warningCount} keybinding ${plural(warningCount, "warning")} · /doctor for details`;
    }

    addNotification({
      key,
      text,
      color: errorCount > 0 ? "red" : "yellow",
      priority: errorCount > 0 ? "immediate" : "high",
      timeoutMs: 60_000,
    });
  }, [addNotification, removeNotification, warnings]);
}

function collectContexts(
  activeContexts: Set<KeybindingContextName>,
  handlerRegistry: Map<string, Array<{ context: KeybindingContextName }>>,
  inputRegistry: Array<{ context: KeybindingContextName }>,
): KeybindingContextName[] {
  const contexts = new Set<KeybindingContextName>(["Global"]);

  for (const context of activeContexts) {
    contexts.add(context);
  }

  for (const handlers of handlerRegistry.values()) {
    for (const registration of handlers) {
      if (isContextActive(registration.context, activeContexts)) {
        contexts.add(registration.context);
      }
    }
  }

  for (const registration of inputRegistry) {
    if (isContextActive(registration.context, activeContexts)) {
      contexts.add(registration.context);
    }
  }

  return [...contexts].sort(
    (left, right) => getContextPriority(right) - getContextPriority(left),
  );
}

function isContextActive(
  context: KeybindingContextName,
  activeContexts: Set<KeybindingContextName>,
): boolean {
  return context === "Global" || activeContexts.has(context);
}

function splitLeadingControlInput(input: string): {
  input: string;
  key: Key;
  rest: string;
} | null {
  if (input.length <= 1) {
    return null;
  }

  const code = input.charCodeAt(0);
  if (code < 1 || code > 26) {
    return null;
  }

  return {
    input: String.fromCharCode(code + 96),
    key: { ctrl: true } as Key,
    rest: input.slice(1),
  };
}

function isPlainTextInput(input: string, key: Key): boolean {
  return input.length > 0 && !key.ctrl && !key.meta;
}

function KeybindingInputRouter({
  bindings,
  pendingChordRef,
  setPendingChord,
  activeContextsRef,
  handlerRegistryRef,
  inputRegistryRef,
  invokeAction,
  dispatchInput,
}: {
  bindings: ParsedBinding[];
  pendingChordRef: React.MutableRefObject<ParsedKeystroke[] | null>;
  setPendingChord: (pending: ParsedKeystroke[] | null) => void;
  activeContextsRef: React.MutableRefObject<Set<KeybindingContextName>>;
  handlerRegistryRef: React.MutableRefObject<
    Map<string, HandlerRegistration[]>
  >;
  inputRegistryRef: React.MutableRefObject<
    InputHandlerRegistration[]
  >;
  invokeAction: (action: string, contexts: KeybindingContextName[]) => boolean;
  dispatchInput: (
    input: string,
    key: Key,
    contexts: KeybindingContextName[],
  ) => boolean;
}): null {
  useInput((input, key) => {
    const contexts = collectContexts(
      activeContextsRef.current,
      handlerRegistryRef.current,
      inputRegistryRef.current,
    );
    const leadingControlInput = splitLeadingControlInput(input);
    if (leadingControlInput) {
      const result = resolveKeyWithChordState(
        leadingControlInput.input,
        leadingControlInput.key,
        contexts,
        bindings,
        pendingChordRef.current,
      );

      if (result.type === "match") {
        setPendingChord(null);
        if (invokeAction(result.action, contexts)) {
          if (
            result.action === "app:toggleTranscript" &&
            leadingControlInput.rest.length > 0
          ) {
            setTimeout(() => {
              const nextContexts = collectContexts(
                activeContextsRef.current,
                handlerRegistryRef.current,
                inputRegistryRef.current,
              );
              dispatchInput(leadingControlInput.rest, PLAIN_INPUT_KEY, nextContexts);
            }, 0);
          }
          return;
        }
      }
    }

    const rawTerminalKeyNames = getRawTerminalKeyNames(input);
    if (rawTerminalKeyNames.length > 1) {
      setPendingChord(null);
      for (const rawTerminalKeyName of rawTerminalKeyNames) {
        const action = RAW_TERMINAL_KEY_ACTIONS[rawTerminalKeyName];
        if (action) {
          invokeAction(action, contexts);
        }
      }
      return;
    }

    const rawTerminalKeyName = rawTerminalKeyNames[0] ?? null;
    if (rawTerminalKeyName === "mouse") {
      return;
    }

    const result = resolveKeyWithChordState(
      input,
      key,
      contexts,
      bindings,
      pendingChordRef.current,
    );

    switch (result.type) {
      case "chord_started":
        setPendingChord(result.pending);
        return;
      case "match":
        setPendingChord(null);
        if (invokeAction(result.action, contexts)) {
          return;
        }
        break;
      case "chord_cancelled":
        setPendingChord(null);
        return;
      case "unbound":
        setPendingChord(null);
        if (isPlainTextInput(input, key)) {
          dispatchInput(input, key, contexts);
        }
        return;
      case "none":
      default:
        break;
    }

    dispatchInput(input, key, contexts);
  });

  return null;
}

function TerminalMouseTracking(): null {
  const { stdout } = useStdout();

  React.useEffect(() => {
    disableTerminalMouseReporting(stdout);
    if (!shouldEnableTerminalMouseReporting()) {
      return;
    }

    enableTerminalMouseReporting(stdout);
    return () => {
      disableTerminalMouseReporting(stdout);
    };
  }, [stdout]);

  return null;
}

export function KeybindingSetup({ children }: Props): React.ReactElement {
  const [{ bindings, warnings }] = React.useState(() =>
    loadKeybindingsSyncWithWarnings(),
  );

  useKeybindingWarnings(warnings);

  const pendingChordRef = React.useRef<ParsedKeystroke[] | null>(null);
  const [pendingChord, setPendingChordState] = React.useState<
    ParsedKeystroke[] | null
  >(null);
  const chordTimeoutRef = React.useRef<NodeJS.Timeout | null>(null);

  const handlerRegistryRef = React.useRef(
    new Map<string, HandlerRegistration[]>(),
  );
  const inputRegistryRef = React.useRef<InputHandlerRegistration[]>([]);
  const activeContextsRef = React.useRef<Set<KeybindingContextName>>(new Set());

  const registerActiveContext = React.useCallback(
    (context: KeybindingContextName) => {
      activeContextsRef.current.add(context);
    },
    [],
  );

  const unregisterActiveContext = React.useCallback(
    (context: KeybindingContextName) => {
      activeContextsRef.current.delete(context);
    },
    [],
  );

  const clearChordTimeout = React.useCallback(() => {
    if (!chordTimeoutRef.current) {
      return;
    }

    clearTimeout(chordTimeoutRef.current);
    chordTimeoutRef.current = null;
  }, []);

  const setPendingChord = React.useCallback(
    (pending: ParsedKeystroke[] | null) => {
      clearChordTimeout();

      if (pending !== null) {
        chordTimeoutRef.current = setTimeout(() => {
          pendingChordRef.current = null;
          setPendingChordState(null);
        }, CHORD_TIMEOUT_MS);
      }

      pendingChordRef.current = pending;
      setPendingChordState(pending);
    },
    [clearChordTimeout],
  );

  React.useEffect(() => {
    return () => {
      clearChordTimeout();
    };
  }, [clearChordTimeout]);

  const invokeAction = React.useCallback(
    (action: string, contexts: KeybindingContextName[]) => {
      const registry = handlerRegistryRef.current;
      const handlers = registry.get(action) ?? [];
      const activeContexts = activeContextsRef.current;

      for (const context of contexts) {
        for (let index = handlers.length - 1; index >= 0; index -= 1) {
          const registration = handlers[index];
          if (
            !registration ||
            registration.context !== context ||
            !isContextActive(registration.context, activeContexts)
          ) {
            continue;
          }

          if (registration.handler() !== false) {
            return true;
          }
        }
      }

      return false;
    },
    [],
  );

  const dispatchInput = React.useCallback(
    (input: string, key: Key, contexts: KeybindingContextName[]) => {
      const registrations = inputRegistryRef.current;

      for (const context of contexts) {
        for (let index = registrations.length - 1; index >= 0; index -= 1) {
          const registration = registrations[index];
          if (!registration || registration.context !== context) {
            continue;
          }

          if (registration.handler(input, key) !== false) {
            return true;
          }
        }
      }

      return false;
    },
    [],
  );

  return (
    <KeybindingProvider
      bindings={bindings}
      pendingChordRef={pendingChordRef}
      pendingChord={pendingChord}
      setPendingChord={setPendingChord}
      activeContexts={activeContextsRef.current}
      registerActiveContext={registerActiveContext}
      unregisterActiveContext={unregisterActiveContext}
      handlerRegistryRef={handlerRegistryRef}
      inputRegistryRef={inputRegistryRef}
    >
      <KeybindingInputRouter
        bindings={bindings}
        pendingChordRef={pendingChordRef}
        setPendingChord={setPendingChord}
        activeContextsRef={activeContextsRef}
        handlerRegistryRef={handlerRegistryRef}
        inputRegistryRef={inputRegistryRef}
        invokeAction={invokeAction}
        dispatchInput={dispatchInput}
      />
      <TerminalMouseTracking />
      {children}
    </KeybindingProvider>
  );
}
