import React from "react";
import instances from "../ink/instances.js";
import { useKeybinding } from "../keybindings/useKeybinding.js";
import { useSetAppState } from "../state/AppState.js";

type UseGlobalKeybindingsProps = {
  onInterrupt: () => void;
  onExit: () => void;
  canInterrupt: boolean;
  canExit?: boolean;
};

export function useGlobalKeybindings({
  onInterrupt,
  onExit,
  canInterrupt,
  canExit = true,
}: UseGlobalKeybindingsProps): void {
  const setAppState = useSetAppState();

  const toggleTasks = React.useCallback(() => {
    setAppState(prev => {
      const hasRunningBackgroundAgents = Object.values(
        prev.agents.backgroundTasks,
      ).some(task =>
        ["running", "pending", "starting"].includes(task.status.toLowerCase()),
      );
      const nextExpandedView = hasRunningBackgroundAgents
        ? prev.expandedView === "none"
          ? "tasks"
          : prev.expandedView === "tasks"
            ? "teammates"
            : "none"
        : prev.expandedView === "tasks"
          ? "none"
          : "tasks";
      return {
        ...prev,
        expandedView: nextExpandedView,
        footerSelection:
          prev.expandedView === "tasks" && prev.footerSelection === "tasks"
            ? null
            : prev.footerSelection,
      };
    });
  }, [setAppState]);

  const redraw = React.useCallback(() => {
    instances.get(process.stdout)?.forceRedraw();
    instances.get(process.stderr)?.forceRedraw();
  }, []);

  useKeybinding("app:interrupt", onInterrupt, {
    context: "Global",
    isActive: canInterrupt,
  });

  useKeybinding("app:exit", onExit, {
    context: "Global",
    isActive: canExit,
  });

  useKeybinding("app:redraw", redraw, {
    context: "Global",
  });

  useKeybinding("app:toggleTodos", toggleTasks, {
    context: "Global",
  });
}
