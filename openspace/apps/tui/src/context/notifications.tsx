import type * as React from "react";
import { useCallback, useEffect } from "react";
import {
  useAppStateStore,
  useSetAppState,
} from "../state/AppState.js";

type Priority = "low" | "medium" | "high" | "immediate";

type BaseNotification = {
  key: string;
  invalidates?: string[];
  priority: Priority;
  timeoutMs?: number;
  fold?: (
    accumulator: Notification,
    incoming: Notification,
  ) => Notification;
};

type TextNotification = BaseNotification & {
  text: string;
  color?: string;
};

type JSXNotification = BaseNotification & {
  jsx: React.ReactNode;
};

type AddNotificationFn = (content: Notification) => void;
type RemoveNotificationFn = (key: string) => void;

export type Notification = TextNotification | JSXNotification;

const DEFAULT_TIMEOUT_MS = 8000;

let currentTimeoutId: NodeJS.Timeout | null = null;

export function useNotifications(): {
  addNotification: AddNotificationFn;
  removeNotification: RemoveNotificationFn;
} {
  const store = useAppStateStore();
  const setAppState = useSetAppState();

  const processQueue = useCallback(() => {
    setAppState(prev => {
      const next = getNext(prev.notifications.queue);
      if (prev.notifications.current !== null || !next) {
        return prev;
      }

      currentTimeoutId = setTimeout(
        (setAppState, nextKey, processQueue) => {
          currentTimeoutId = null;
          setAppState(prev => {
            if (prev.notifications.current?.key !== nextKey) {
              return prev;
            }

            return {
              ...prev,
              notifications: {
                queue: prev.notifications.queue,
                current: null,
              },
            };
          });
          processQueue();
        },
        next.timeoutMs ?? DEFAULT_TIMEOUT_MS,
        setAppState,
        next.key,
        processQueue,
      );

      return {
        ...prev,
        notifications: {
          queue: prev.notifications.queue.filter(
            notification => notification !== next,
          ),
          current: next,
        },
      };
    });
  }, [setAppState]);

  const addNotification = useCallback<AddNotificationFn>(
    notif => {
      if (notif.priority === "immediate") {
        if (currentTimeoutId) {
          clearTimeout(currentTimeoutId);
          currentTimeoutId = null;
        }

        currentTimeoutId = setTimeout(
          (setAppState, notif, processQueue) => {
            currentTimeoutId = null;
            setAppState(prev => {
              if (prev.notifications.current?.key !== notif.key) {
                return prev;
              }

              return {
                ...prev,
                notifications: {
                  queue: prev.notifications.queue.filter(
                    queued =>
                      !notif.invalidates?.includes(queued.key),
                  ),
                  current: null,
                },
              };
            });
            processQueue();
          },
          notif.timeoutMs ?? DEFAULT_TIMEOUT_MS,
          setAppState,
          notif,
          processQueue,
        );

        setAppState(prev => ({
          ...prev,
          notifications: {
            current: notif,
            queue: [
              ...(prev.notifications.current
                ? [prev.notifications.current]
                : []),
              ...prev.notifications.queue,
            ].filter(
              queued =>
                queued.priority !== "immediate" &&
                !notif.invalidates?.includes(queued.key),
            ),
          },
        }));
        return;
      }

      setAppState(prev => {
        if (notif.fold) {
          if (prev.notifications.current?.key === notif.key) {
            const folded = notif.fold(prev.notifications.current, notif);
            if (currentTimeoutId) {
              clearTimeout(currentTimeoutId);
              currentTimeoutId = null;
            }

            currentTimeoutId = setTimeout(
              (setAppState, foldedKey, processQueue) => {
                currentTimeoutId = null;
                setAppState(current => {
                  if (current.notifications.current?.key !== foldedKey) {
                    return current;
                  }

                  return {
                    ...current,
                    notifications: {
                      queue: current.notifications.queue,
                      current: null,
                    },
                  };
                });
                processQueue();
              },
              folded.timeoutMs ?? DEFAULT_TIMEOUT_MS,
              setAppState,
              folded.key,
              processQueue,
            );

            return {
              ...prev,
              notifications: {
                current: folded,
                queue: prev.notifications.queue,
              },
            };
          }

          const queueIndex = prev.notifications.queue.findIndex(
            queued => queued.key === notif.key,
          );

          if (queueIndex !== -1) {
            const folded = notif.fold(
              prev.notifications.queue[queueIndex]!,
              notif,
            );
            const newQueue = [...prev.notifications.queue];
            newQueue[queueIndex] = folded;

            return {
              ...prev,
              notifications: {
                current: prev.notifications.current,
                queue: newQueue,
              },
            };
          }
        }

        const queuedKeys = new Set(
          prev.notifications.queue.map(queued => queued.key),
        );
        const shouldAdd =
          !queuedKeys.has(notif.key) &&
          prev.notifications.current?.key !== notif.key;

        if (!shouldAdd) {
          return prev;
        }

        const invalidatesCurrent =
          prev.notifications.current !== null &&
          notif.invalidates?.includes(prev.notifications.current.key);

        if (invalidatesCurrent && currentTimeoutId) {
          clearTimeout(currentTimeoutId);
          currentTimeoutId = null;
        }

        return {
          ...prev,
          notifications: {
            current: invalidatesCurrent
              ? null
              : prev.notifications.current,
            queue: [
              ...prev.notifications.queue.filter(
                queued =>
                  queued.priority !== "immediate" &&
                  !notif.invalidates?.includes(queued.key),
              ),
              notif,
            ],
          },
        };
      });

      processQueue();
    },
    [setAppState, processQueue],
  );

  const removeNotification = useCallback<RemoveNotificationFn>(
    key => {
      setAppState(prev => {
        const isCurrent = prev.notifications.current?.key === key;
        const inQueue = prev.notifications.queue.some(
          notification => notification.key === key,
        );

        if (!isCurrent && !inQueue) {
          return prev;
        }

        if (isCurrent && currentTimeoutId) {
          clearTimeout(currentTimeoutId);
          currentTimeoutId = null;
        }

        return {
          ...prev,
          notifications: {
            current: isCurrent ? null : prev.notifications.current,
            queue: prev.notifications.queue.filter(
              notification => notification.key !== key,
            ),
          },
        };
      });
      processQueue();
    },
    [setAppState, processQueue],
  );

  useEffect(() => {
    if (store.getState().notifications.queue.length > 0) {
      processQueue();
    }
  }, [store, processQueue]);

  return { addNotification, removeNotification };
}

const PRIORITIES: Record<Priority, number> = {
  immediate: 0,
  high: 1,
  medium: 2,
  low: 3,
};

export function getNext(
  queue: Notification[],
): Notification | undefined {
  if (queue.length === 0) {
    return undefined;
  }

  return queue.reduce((min, notification) =>
    PRIORITIES[notification.priority] < PRIORITIES[min.priority]
      ? notification
      : min,
  );
}
