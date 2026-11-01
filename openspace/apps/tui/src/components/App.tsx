import React from "react";
import type { StructuredIO } from "../bridge/structuredIO.js";
import { FpsMetricsProvider } from "../context/fpsMetrics.js";
import { StatsProvider } from "../context/stats.js";
import instances from "../ink/instances.js";
import { KeybindingSetup } from "../keybindings/KeybindingProviderSetup.js";
import {
  type AppState,
  type AppStateStore,
  type ScreenName,
} from "../state/AppStateStore.js";
import { AppStateProvider } from "../state/AppState.js";
import { FpsTracker } from "../utils/fpsTracker.js";
import { Doctor } from "../screens/Doctor.js";
import { REPL } from "../screens/REPL.js";
import { ResumeConversation } from "../screens/ResumeConversation.js";

type Props = {
  screen: ScreenName;
  io: StructuredIO | null;
  store: AppStateStore;
  initialState?: AppState;
  stdout: NodeJS.WriteStream;
};

export function App({
  screen,
  io,
  store,
  initialState,
  stdout,
}: Props): React.ReactElement {
  const fpsTrackerRef = React.useRef<FpsTracker | null>(null);
  const [, setRenderEpoch] = React.useState(0);

  if (fpsTrackerRef.current === null) {
    fpsTrackerRef.current = new FpsTracker();
  }

  React.useEffect(() => {
    const controls = {
      invalidatePrevFrame: () => {
        setRenderEpoch(epoch => epoch + 1);
      },
      forceRedraw: () => {
        setRenderEpoch(epoch => epoch + 1);
      },
    };

    instances.set(stdout, controls);
    instances.set(process.stdout, controls);
    instances.set(process.stderr, controls);

    return () => {
      instances.delete(stdout);
      instances.delete(process.stdout);
      instances.delete(process.stderr);
    };
  }, [stdout]);

  const content = (() => {
    switch (screen) {
      case "doctor":
        return <Doctor io={io} />;
      case "resume":
        return <ResumeConversation io={io} />;
      case "repl":
      default:
        return (
          <KeybindingSetup>
            <REPL io={io} />
          </KeybindingSetup>
        );
    }
  })();

  return (
    <StatsProvider>
      <AppStateProvider
        store={store}
        initialState={initialState}
      >
        {/*
          Transitional: OpenSpace scopes prompt overlays inside the fullscreen
          message surface. OpenSpace now mounts that provider there instead of at
          the app root, but still relies on upstream Ink for frame tracking.
        */}
        <FpsMetricsProvider
          getFpsMetrics={() => fpsTrackerRef.current?.getMetrics()}
          recordFrame={durationMs => fpsTrackerRef.current?.record(durationMs)}
        >
          {content}
        </FpsMetricsProvider>
      </AppStateProvider>
    </StatsProvider>
  );
}
