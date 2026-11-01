import { useContext, useEffect, useLayoutEffect } from "react";
import instances from "../ink/instances.js";
import {
  AppStoreContext,
  useAppState,
} from "../state/AppState.js";

const NON_MODAL_OVERLAYS = new Set(["autocomplete"]);

export function useRegisterOverlay(
  id: string,
  enabled = true,
): void {
  const store = useContext(AppStoreContext);
  const setAppState = store?.setState;

  useEffect(() => {
    if (!enabled || !setAppState) {
      return;
    }

    setAppState(prev => {
      if (prev.activeOverlays.has(id)) {
        return prev;
      }

      const next = new Set(prev.activeOverlays);
      next.add(id);
      return { ...prev, activeOverlays: next };
    });

    return () => {
      setAppState(prev => {
        if (!prev.activeOverlays.has(id)) {
          return prev;
        }

        const next = new Set(prev.activeOverlays);
        next.delete(id);
        return { ...prev, activeOverlays: next };
      });
    };
  }, [enabled, id, setAppState]);

  useLayoutEffect(() => {
    if (!enabled) {
      return;
    }

    return () => {
      instances.get(process.stdout)?.invalidatePrevFrame();
      instances.get(process.stderr)?.invalidatePrevFrame();
    };
  }, [enabled]);
}

export function useIsOverlayActive(): boolean {
  return useAppState(state => state.activeOverlays.size > 0);
}

export function useIsModalOverlayActive(): boolean {
  return useAppState(state => {
    for (const id of state.activeOverlays) {
      if (!NON_MODAL_OVERLAYS.has(id)) {
        return true;
      }
    }
    return false;
  });
}
