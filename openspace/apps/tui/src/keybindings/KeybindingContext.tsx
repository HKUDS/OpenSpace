import React, {
  createContext,
  useContext,
  useLayoutEffect,
  type MutableRefObject,
} from "react";
import type { Key } from "ink";
import {
  getBindingDisplayText,
  type ChordResolveResult,
  resolveKeyWithChordState,
} from "./resolver.js";
import type {
  KeybindingContextName,
  ParsedBinding,
  ParsedKeystroke,
} from "./types.js";

export type KeybindingHandler = () => void | false | Promise<void>;

export type HandlerRegistration = {
  action: string;
  context: KeybindingContextName;
  handler: KeybindingHandler;
};

export type InputHandlerRegistration = {
  context: KeybindingContextName;
  handler: (input: string, key: Key) => void | false;
};

type KeybindingContextValue = {
  bindings: ParsedBinding[];
  pendingChord: ParsedKeystroke[] | null;
  setPendingChord: (pending: ParsedKeystroke[] | null) => void;
  resolve: (
    input: string,
    key: Key,
    activeContexts: KeybindingContextName[],
  ) => ChordResolveResult;
  getDisplayText: (
    action: string,
    context: KeybindingContextName,
  ) => string | undefined;
  activeContexts: Set<KeybindingContextName>;
  registerActiveContext: (context: KeybindingContextName) => void;
  unregisterActiveContext: (context: KeybindingContextName) => void;
  registerHandler: (registration: HandlerRegistration) => () => void;
  invokeAction: (
    action: string,
    contexts: KeybindingContextName[],
  ) => boolean;
  registerInputHandler: (registration: InputHandlerRegistration) => () => void;
  dispatchInput: (
    input: string,
    key: Key,
    contexts: KeybindingContextName[],
  ) => boolean;
};

const KeybindingContext = createContext<KeybindingContextValue | null>(null);

type ProviderProps = {
  bindings: ParsedBinding[];
  pendingChordRef: MutableRefObject<ParsedKeystroke[] | null>;
  pendingChord: ParsedKeystroke[] | null;
  setPendingChord: (pending: ParsedKeystroke[] | null) => void;
  activeContexts: Set<KeybindingContextName>;
  registerActiveContext: (context: KeybindingContextName) => void;
  unregisterActiveContext: (context: KeybindingContextName) => void;
  handlerRegistryRef: MutableRefObject<Map<string, HandlerRegistration[]>>;
  inputRegistryRef: MutableRefObject<InputHandlerRegistration[]>;
  children: React.ReactNode;
};

export function KeybindingProvider({
  bindings,
  pendingChordRef,
  pendingChord,
  setPendingChord,
  activeContexts,
  registerActiveContext,
  unregisterActiveContext,
  handlerRegistryRef,
  inputRegistryRef,
  children,
}: ProviderProps): React.ReactElement {
  const registerHandler = React.useCallback(
    (registration: HandlerRegistration) => {
      const registry = handlerRegistryRef.current;
      const handlers = registry.get(registration.action) ?? [];
      handlers.push(registration);
      registry.set(registration.action, handlers);

      return () => {
        const currentHandlers = registry.get(registration.action);
        if (!currentHandlers) {
          return;
        }

        const nextHandlers = currentHandlers.filter(
          candidate => candidate !== registration,
        );

        if (nextHandlers.length === 0) {
          registry.delete(registration.action);
          return;
        }

        registry.set(registration.action, nextHandlers);
      };
    },
    [handlerRegistryRef],
  );

  const invokeAction = React.useCallback(
    (action: string, contexts: KeybindingContextName[]): boolean => {
      const registry = handlerRegistryRef.current;
      const handlers = registry.get(action) ?? [];
      const contextSet = new Set(contexts);

      for (let index = handlers.length - 1; index >= 0; index -= 1) {
        const registration = handlers[index];
        if (
          !registration ||
          !contextSet.has(registration.context) ||
          (registration.context !== "Global" &&
            !activeContexts.has(registration.context))
        ) {
          continue;
        }

        if (registration.handler() !== false) {
          return true;
        }
      }

      return false;
    },
    [activeContexts, handlerRegistryRef],
  );

  const registerInputHandler = React.useCallback(
    (registration: InputHandlerRegistration) => {
      inputRegistryRef.current.push(registration);

      return () => {
        inputRegistryRef.current = inputRegistryRef.current.filter(
          candidate => candidate !== registration,
        );
      };
    },
    [inputRegistryRef],
  );

  const dispatchInput = React.useCallback(
    (input: string, key: Key, contexts: KeybindingContextName[]): boolean => {
      const handlers = inputRegistryRef.current;

      for (const context of contexts) {
        for (let index = handlers.length - 1; index >= 0; index -= 1) {
          const registration = handlers[index];
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
    [inputRegistryRef],
  );

  const value = React.useMemo<KeybindingContextValue>(
    () => ({
      bindings,
      pendingChord,
      setPendingChord,
      resolve: (
        input: string,
        key: Key,
        contexts: KeybindingContextName[],
      ) =>
        resolveKeyWithChordState(
          input,
          key,
          contexts,
          bindings,
          pendingChordRef.current,
        ),
      getDisplayText: (action: string, context: KeybindingContextName) =>
        getBindingDisplayText(action, context, bindings),
      activeContexts,
      registerActiveContext,
      unregisterActiveContext,
      registerHandler,
      invokeAction,
      registerInputHandler,
      dispatchInput,
    }),
    [
      activeContexts,
      bindings,
      dispatchInput,
      invokeAction,
      pendingChord,
      pendingChordRef,
      registerActiveContext,
      registerHandler,
      registerInputHandler,
      setPendingChord,
      unregisterActiveContext,
    ],
  );

  return (
    <KeybindingContext.Provider value={value}>
      {children}
    </KeybindingContext.Provider>
  );
}

export function useKeybindingContext(): KeybindingContextValue {
  const context = useContext(KeybindingContext);

  if (!context) {
    throw new Error(
      "useKeybindingContext must be used within KeybindingProvider",
    );
  }

  return context;
}

export function useOptionalKeybindingContext(): KeybindingContextValue | null {
  return useContext(KeybindingContext);
}

export function useRegisterKeybindingContext(
  context: KeybindingContextName,
  isActive = true,
): void {
  const keybindingContext = useOptionalKeybindingContext();

  useLayoutEffect(() => {
    if (!keybindingContext || !isActive) {
      return;
    }

    keybindingContext.registerActiveContext(context);
    return () => {
      keybindingContext.unregisterActiveContext(context);
    };
  }, [context, isActive, keybindingContext]);
}
