import { useEffect, useLayoutEffect, useRef } from "react";
import type { Key } from "ink";
import { useOptionalKeybindingContext } from "./KeybindingContext.js";
import type { KeybindingContextName } from "./types.js";

type Options = {
  context?: KeybindingContextName;
  isActive?: boolean;
};

export function useKeybinding(
  action: string,
  handler: () => void | false | Promise<void>,
  options: Options = {},
): void {
  const { context = "Global", isActive = true } = options;
  const keybindingContext = useOptionalKeybindingContext();
  const handlerRef = useRef(handler);

  useLayoutEffect(() => {
    handlerRef.current = handler;
  }, [handler]);

  useEffect(() => {
    if (!keybindingContext || !isActive) {
      return;
    }

    return keybindingContext.registerHandler({
      action,
      context,
      handler: () => handlerRef.current(),
    });
  }, [action, context, isActive, keybindingContext]);
}

export function useKeybindings(
  handlers: Record<string, () => void | false | Promise<void>>,
  options: Options = {},
): void {
  const { context = "Global", isActive = true } = options;
  const keybindingContext = useOptionalKeybindingContext();
  const handlersRef = useRef(handlers);
  const actionNames = Object.keys(handlers);
  const actionNamesKey = actionNames.join("\u0000");

  useLayoutEffect(() => {
    handlersRef.current = handlers;
  }, [handlers]);

  useEffect(() => {
    if (!keybindingContext || !isActive) {
      return;
    }

    const unregister = actionNames.map(action =>
      keybindingContext.registerHandler({
        action,
        context,
        handler: () => handlersRef.current[action]?.(),
      }),
    );

    return () => {
      for (const dispose of unregister) {
        dispose();
      }
    };
  }, [actionNamesKey, context, isActive, keybindingContext]);
}

export function useKeybindingInput(
  handler: (input: string, key: Key) => void | false,
  options: Options = {},
): void {
  const { context = "Chat", isActive = true } = options;
  const keybindingContext = useOptionalKeybindingContext();
  const handlerRef = useRef(handler);

  useLayoutEffect(() => {
    handlerRef.current = handler;
  }, [handler]);

  useEffect(() => {
    if (!keybindingContext || !isActive) {
      return;
    }

    return keybindingContext.registerInputHandler({
      context,
      handler: (input, key) => handlerRef.current(input, key),
    });
  }, [context, isActive, keybindingContext]);
}
