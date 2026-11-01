import React, { createContext, useContext } from "react";
import type { FpsMetrics } from "../utils/fpsTracker.js";

type FpsMetricsGetter = () => FpsMetrics | undefined;
type FpsFrameRecorder = (durationMs: number) => void;

type FpsMetricsContextValue = {
  getFpsMetrics: FpsMetricsGetter;
  recordFrame: FpsFrameRecorder;
};

const FpsMetricsContext = createContext<FpsMetricsContextValue | undefined>(
  undefined,
);

type Props = {
  getFpsMetrics: FpsMetricsGetter;
  recordFrame?: FpsFrameRecorder;
  children: React.ReactNode;
};

export function FpsMetricsProvider({
  getFpsMetrics,
  recordFrame,
  children,
}: Props): React.ReactNode {
  return (
    <FpsMetricsContext.Provider
      value={{
        getFpsMetrics,
        recordFrame: recordFrame ?? (() => {}),
      }}
    >
      {children}
    </FpsMetricsContext.Provider>
  );
}

export function useFpsMetrics(): FpsMetricsGetter | undefined {
  return useContext(FpsMetricsContext)?.getFpsMetrics;
}

export function useRecordFpsFrame(): FpsFrameRecorder | undefined {
  return useContext(FpsMetricsContext)?.recordFrame;
}
