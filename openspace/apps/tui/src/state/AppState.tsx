import React, {
  useContext,
  useState,
  useSyncExternalStore,
} from "react";
import { MailboxProvider } from "../context/mailbox.js";
import {
  createAppStateStore,
  getDefaultAppState,
  type AppState,
  type AppStateStore,
} from "./AppStateStore.js";

export const AppStoreContext = React.createContext<AppStateStore | null>(null);
const HasAppStateContext = React.createContext<boolean>(false);

type AppStateProviderProps = {
  children: React.ReactNode;
  initialState?: AppState;
  store?: AppStateStore;
  onChangeAppState?: (args: {
    newState: AppState;
    oldState: AppState;
  }) => void;
};

export function AppStateProvider({
  children,
  initialState,
  store: externalStore,
  onChangeAppState,
}: AppStateProviderProps): React.ReactNode {
  const hasAppStateContext = useContext(HasAppStateContext);
  if (hasAppStateContext) {
    throw new Error(
      "AppStateProvider can not be nested within another AppStateProvider",
    );
  }

  const [store] = useState(() => {
    return (
      externalStore ??
      createAppStateStore(
        initialState ?? getDefaultAppState(),
        onChangeAppState,
      )
    );
  });

  return (
    <HasAppStateContext.Provider value={true}>
      <AppStoreContext.Provider value={store}>
        <MailboxProvider>{children}</MailboxProvider>
      </AppStoreContext.Provider>
    </HasAppStateContext.Provider>
  );
}

function useAppStore(): AppStateStore {
  const store = useContext(AppStoreContext);

  if (!store) {
    throw new ReferenceError(
      "useAppState/useSetAppState cannot be called outside of an <AppStateProvider />",
    );
  }

  return store;
}

export function useAppState<T>(selector: (state: AppState) => T): T {
  const store = useAppStore();
  const getSnapshot = () => selector(store.getState());
  return useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot);
}

export function useSetAppState(): (
  updater: (prev: AppState) => AppState,
) => void {
  return useAppStore().setState;
}

export function useAppStateStore(): AppStateStore {
  return useAppStore();
}

const NOOP_SUBSCRIBE = () => () => {};

export function useAppStateMaybeOutsideOfProvider<T>(
  selector: (state: AppState) => T,
): T | undefined {
  const store = useContext(AppStoreContext);
  const getSnapshot = () => (store ? selector(store.getState()) : undefined);

  return useSyncExternalStore(
    store ? store.subscribe : NOOP_SUBSCRIBE,
    getSnapshot,
    getSnapshot,
  );
}
