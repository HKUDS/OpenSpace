import fs from "node:fs";
import process from "node:process";
import tty from "node:tty";
import { render, type Instance } from "ink";
import { StructuredIO } from "./bridge/structuredIO.js";
import { App } from "./components/App.js";
import { setSettingsIO } from "./hooks/useSettings.js";
import {
  createAppStateStore,
  getDefaultAppState,
  type AppState,
  type ScreenName,
} from "./state/AppStateStore.js";
import { createOnChangeAppState } from "./state/onChangeAppState.js";
import { disableTerminalMouseReporting } from "./utils/terminalMouseReporting.js";

type TerminalStreams = {
  stdin: NodeJS.ReadStream;
  stdout: NodeJS.WriteStream;
  hasInteractiveTty: boolean;
  unavailableReason?: string;
  cleanup: () => void;
};

function parseScreen(argv: string[]): ScreenName {
  if (argv.includes("--doctor")) {
    return "doctor";
  }

  if (argv.includes("--resume")) {
    return "resume";
  }

  return "repl";
}

function resolveTerminalStreams(ipcMode: boolean): TerminalStreams {
  if (!ipcMode && process.stdin.isTTY && process.stderr.isTTY) {
    return {
      stdin: process.stdin,
      stdout: process.stderr,
      hasInteractiveTty: true,
      cleanup: () => {},
    };
  }

  const ttyInputPath = process.platform === "win32" ? "CONIN$" : "/dev/tty";
  const ttyOutputPath = process.platform === "win32" ? "CONOUT$" : "/dev/tty";

  try {
    const stdinFd = fs.openSync(ttyInputPath, "r");
    const stdoutFd = fs.openSync(ttyOutputPath, "w");
    const stdin = new tty.ReadStream(stdinFd);
    const stdout = new tty.WriteStream(stdoutFd);

    return {
      stdin,
      stdout,
      hasInteractiveTty: true,
      cleanup: () => {
        stdin.destroy();
        stdout.destroy();
      },
    };
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      stdin: process.stdin,
      stdout: process.stderr,
      hasInteractiveTty: false,
      unavailableReason: message,
      cleanup: () => {},
    };
  }
}

async function startInboundLoop(
  io: StructuredIO,
  shutdown: (reason: string) => void,
): Promise<void> {
  try {
    for await (const _message of io.receive()) {
      // Screen components subscribe to StructuredIO directly.
    }
  } finally {
    shutdown("stdin closed");
  }
}

async function main(): Promise<void> {
  const screen = parseScreen(process.argv.slice(2));
  const ipcMode = process.env.OPENSPACE_TUI_IPC === "1";
  const io = ipcMode ? new StructuredIO() : null;
  setSettingsIO(io);
  const initialState: AppState = {
    ...getDefaultAppState(screen),
    runtime: {
      ...getDefaultAppState(screen).runtime,
      screen,
    },
  };
  const store = createAppStateStore(
    initialState,
    createOnChangeAppState(io),
  );
  const terminal = resolveTerminalStreams(ipcMode);
  disableTerminalMouseReporting(terminal.stdout);

  let app: Instance | null = null;
  let shuttingDown = false;
  let finish: (() => void) | null = null;
  let terminalCleanedUp = false;
  const finished = new Promise<void>(resolve => {
    finish = resolve;
  });

  const cleanupTerminal = (): void => {
    if (terminalCleanedUp) {
      return;
    }

    terminalCleanedUp = true;
    disableTerminalMouseReporting(terminal.stdout);
    terminal.cleanup();
  };

  const shutdown = (reason: string): void => {
    if (shuttingDown) {
      return;
    }

    shuttingDown = true;
    disableTerminalMouseReporting(terminal.stdout);
    io?.rejectAllPending(reason);
    io?.close();
    app?.unmount();
    disableTerminalMouseReporting(terminal.stdout);
    if (ipcMode) {
      process.stdin.pause();
    }
    cleanupTerminal();
    finish?.();
  };

  const restoreTerminalOnExit = (): void => {
    disableTerminalMouseReporting(terminal.stdout);
  };

  process.once("beforeExit", restoreTerminalOnExit);
  process.once("exit", restoreTerminalOnExit);

  process.on("SIGINT", () => {
    io?.send({
      type: "cancel",
      data: { reason: "signal:SIGINT" },
    });
    shutdown("Received SIGINT");
  });

  process.on("SIGTERM", () => {
    io?.send({
      type: "cancel",
      data: { reason: "signal:SIGTERM" },
    });
    shutdown("Received SIGTERM");
  });

  if (terminal.hasInteractiveTty) {
    app = render(
      <App
        screen={screen}
        io={io}
        store={store}
        initialState={initialState}
        stdout={terminal.stdout}
      />,
      {
        stdin: terminal.stdin,
        stdout: terminal.stdout,
        exitOnCtrlC: false,
      },
    );
  } else {
    const message = "OpenSpace TUI could not attach to an interactive terminal.";
    if (ipcMode && io) {
      io.send({
        type: "tui_unavailable",
        data: {
          message,
          reason: terminal.unavailableReason,
        },
      });
      io.close();
      cleanupTerminal();
      process.exitCode = 2;
      return;
    }

    terminal.stdout.write("OpenSpace TUI Ready\n");
    terminal.stdout.write(`Screen: ${screen}\n`);
    cleanupTerminal();
    return;
  }

  if (ipcMode && io) {
    void startInboundLoop(io, shutdown);
  }

  if (app) {
    await Promise.race([app.waitUntilExit(), finished]);
    shutdown("app exited");
    return;
  }

  await finished;
}

void main().catch((error: unknown) => {
  const message =
    error instanceof Error ? error.stack ?? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
