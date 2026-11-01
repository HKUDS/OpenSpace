import { useCallback, useRef, useEffect } from "react";
import { useAppState, useAppStateStore } from "../state/AppState.js";
import type { StructuredIO } from "../bridge/structuredIO.js";

type SubscriberMap = Map<string, Set<(value: unknown) => void>>;

let sharedIO: StructuredIO | null = null;

export function setSettingsIO(io: StructuredIO | null): void {
  sharedIO = io;
}

export type UseSettingsResult = {
  settings: Record<string, unknown>;
  getSetting: <T = unknown>(key: string, defaultValue?: T) => T;
  setSetting: (key: string, value: unknown) => void;
  subscribe: (key: string, callback: (value: unknown) => void) => () => void;
};

export function useSettings(): UseSettingsResult {
  const settings = useAppState(s => s.settings);
  const store = useAppStateStore();
  const subscribersRef = useRef<SubscriberMap>(new Map());
  const prevSettingsRef = useRef<Record<string, unknown>>(settings);

  useEffect(() => {
    const unsubscribe = store.subscribe(() => {
      const current = store.getState().settings;
      const prev = prevSettingsRef.current;
      if (current === prev) return;

      prevSettingsRef.current = current;

      for (const [key, callbacks] of subscribersRef.current) {
        if (current[key] !== prev[key]) {
          for (const cb of callbacks) {
            cb(current[key]);
          }
        }
      }
    });

    return unsubscribe;
  }, [store]);

  const getSetting = useCallback(
    <T = unknown>(key: string, defaultValue?: T): T => {
      const val = settings[key];
      return (val !== undefined ? val : defaultValue) as T;
    },
    [settings],
  );

  const setSetting = useCallback(
    (key: string, value: unknown) => {
      if (sharedIO) {
        sharedIO.send({
          type: "settings_update",
          data: { key, value },
        });
      }
    },
    [],
  );

  const subscribe = useCallback(
    (key: string, callback: (value: unknown) => void): (() => void) => {
      const map = subscribersRef.current;
      let set = map.get(key);
      if (!set) {
        set = new Set();
        map.set(key, set);
      }
      set.add(callback);

      return () => {
        set!.delete(callback);
        if (set!.size === 0) {
          map.delete(key);
        }
      };
    },
    [],
  );

  return { settings, getSetting, setSetting, subscribe };
}
