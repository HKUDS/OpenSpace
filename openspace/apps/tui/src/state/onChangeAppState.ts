import type { AppState } from "./AppStateStore.js";
import type { StructuredIO } from "../bridge/structuredIO.js";

export function createOnChangeAppState(io: StructuredIO | null) {
  return ({
    newState,
    oldState,
  }: {
    newState: AppState;
    oldState: AppState;
  }): void => {
    if (!io || io.isClosed) {
      return;
    }

    if (
      newState.toolPermissionContext.mode !== oldState.toolPermissionContext.mode
    ) {
      io.send({
        type: "settings_update",
        data: {
          key: "toolPermissionContext.mode",
          value: newState.toolPermissionContext.mode,
        },
      });
    }

    if (newState.mainLoopModel !== oldState.mainLoopModel && newState.mainLoopModel) {
      io.send({
        type: "settings_update",
        data: {
          key: "model",
          value: newState.mainLoopModel,
        },
      });
    }
  };
}
