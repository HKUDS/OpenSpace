import { useStdout } from "ink";

export type TerminalSize = {
  columns: number;
  rows: number;
};

export function useTerminalSize(): TerminalSize {
  const { stdout } = useStdout();

  return {
    columns: stdout.columns ?? 80,
    rows: stdout.rows ?? 24,
  };
}
