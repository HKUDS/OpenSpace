import path from "node:path";
import React from "react";
import {
  Box,
  Text,
  useApp,
  useInput,
} from "ink";
import type {
  IPCMessage,
  NotificationData,
  SessionListData,
  SessionRestoredData,
} from "../bridge/protocol.js";
import type { StructuredIO } from "../bridge/structuredIO.js";
import { NotificationBanner } from "../components/NotificationBanner.js";
import { SpinnerWithVerb } from "../components/Spinner.js";
import { Pane } from "../components/design-system/Pane.js";
import { useNotifications } from "../context/notifications.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { KeybindingSetup } from "../keybindings/KeybindingProviderSetup.js";
import { useAppState, useSetAppState } from "../state/AppState.js";
import {
  isBackspaceInput,
  isDeleteInput,
} from "../utils/keyInput.js";
import { truncateToDisplayWidth } from "../utils/textWidth.js";
import { REPL } from "./REPL.js";
import {
  formatDateTime,
  groupSessionsByDate,
  normalizeSessionContext,
  normalizeExternalMessages,
  type SessionSummary,
  truncate,
  useStructuredIOListener,
} from "./shared.js";

type Props = {
  io: StructuredIO | null;
};

type RestoreState = {
  sessionId: string;
  cost?: number | null;
  messages: ReturnType<typeof normalizeExternalMessages>;
  sessionContext: ReturnType<typeof normalizeSessionContext>;
};

type ListRow =
  | {
      type: "group";
      label: string;
    }
  | {
      type: "session";
      index: number;
      session: SessionSummary;
    };

function filterSessions(
  sessions: SessionSummary[],
  query: string,
): SessionSummary[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) {
    return sessions;
  }

  return sessions.filter(session => {
    const haystack = [
      session.session_id,
      session.title,
      session.project_path,
      session.agent_name,
      session.preview,
    ]
      .join(" ")
      .toLowerCase();

    return haystack.includes(normalized);
  });
}

function buildRows(sessions: SessionSummary[]): ListRow[] {
  const rows: ListRow[] = [];
  const groups = groupSessionsByDate(sessions);

  groups.forEach(group => {
    rows.push({
      type: "group",
      label: group.label,
    });

    group.sessions.forEach(session => {
      rows.push({
        type: "session",
        index: sessions.findIndex(
          candidate => candidate.session_id === session.session_id,
        ),
        session,
      });
    });
  });

  return rows;
}

function normalizeCost(value: number | null | undefined): number | null {
  return typeof value === "number" && !Number.isNaN(value)
    ? value
    : null;
}

function windowRows(
  rows: ListRow[],
  selectedIndex: number,
  maxRows: number,
): ListRow[] {
  const selectedRow = rows.findIndex(
    row => row.type === "session" && row.index === selectedIndex,
  );

  if (selectedRow <= maxRows / 2) {
    return rows.slice(0, maxRows);
  }

  const start = Math.max(
    0,
    Math.min(rows.length - maxRows, selectedRow - Math.floor(maxRows / 2)),
  );
  return rows.slice(start, start + maxRows);
}

function sameProject(projectPath: string | undefined): boolean {
  if (!projectPath) {
    return false;
  }

  try {
    return path.resolve(projectPath) === path.resolve(process.cwd());
  } catch {
    return false;
  }
}

function NoConversationsMessage(): React.ReactElement {
  return (
    <Pane>
      <Text>No conversations found to resume.</Text>
      <Text dimColor>
        Press `q` or Ctrl+C to exit and start a new conversation.
      </Text>
    </Pane>
  );
}

function SessionListPane({
  visibleRows,
  selectedIndex,
  searchMode,
  searchQuery,
}: {
  visibleRows: ListRow[];
  selectedIndex: number;
  searchMode: boolean;
  searchQuery: string;
}): React.ReactElement {
  const { columns } = useTerminalSize();
  const rowWidth = Math.max(24, columns - 8);
  const titleWidth = Math.max(12, Math.floor(rowWidth * 0.45));
  const projectWidth = Math.max(10, rowWidth - titleWidth - 5);
  return (
    <Pane>
      <Box
        borderStyle="round"
        borderColor={searchMode ? "cyan" : "gray"}
        paddingX={1}
      >
        <Text color={searchMode ? "cyan" : "gray"}>Search</Text>
        <Text> : </Text>
        <Text color={searchQuery ? "white" : "gray"}>
          {searchQuery || "Type / to filter sessions"}
        </Text>
      </Box>

      <Box flexDirection="column" marginTop={1}>
        {visibleRows.map((row, rowIndex) => {
          if (row.type === "group") {
            return (
              <Text key={`group:${row.label}:${rowIndex}`} bold color="yellow">
                {row.label}
              </Text>
            );
          }

          const selected = row.index === selectedIndex;
          const titleText = truncateToDisplayWidth(
            row.session.title || row.session.session_id,
            titleWidth,
          );
          const projectText = truncateToDisplayWidth(
            row.session.project_path || "unknown project",
            projectWidth,
          );
          const previewText = truncateToDisplayWidth(
            row.session.preview || "No preview",
            rowWidth,
          );

          return (
            <Box
              key={row.session.session_id}
              flexDirection="column"
              marginBottom={1}
            >
              <Text color={selected ? "green" : "white"} bold={selected} wrap="truncate">
                {selected ? ">" : " "} {titleText} | {projectText}
              </Text>
              <Text color="gray">
                {formatDateTime(row.session.updated_at)} | {row.session.message_count} messages
              </Text>
              {row.session.agent_name || row.session.mode ? (
                <Text color="gray">
                  {row.session.agent_name ?? "main"} · {row.session.mode ?? "default"}
                </Text>
              ) : null}
              <Text color={selected ? "cyan" : "gray"} wrap="truncate">{previewText}</Text>
            </Box>
          );
        })}
      </Box>
    </Pane>
  );
}

export function ResumeConversation({
  io,
}: Props): React.ReactElement {
  const { exit } = useApp();
  const setAppState = useSetAppState();
  const notifications = useAppState(state => state.notifications);
  const { addNotification } = useNotifications();
  const { rows, columns } = useTerminalSize();

  const [sessions, setSessions] = React.useState<SessionSummary[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [restoring, setRestoring] = React.useState<"resume" | "fork" | null>(null);
  const [searchQuery, setSearchQuery] = React.useState("");
  const [searchMode, setSearchMode] = React.useState(false);
  const [selectedIndex, setSelectedIndex] = React.useState(0);
  const [statusText, setStatusText] = React.useState("Loading resumable sessions...");
  const [restored, setRestored] = React.useState<RestoreState | null>(null);
  const requestedRef = React.useRef(false);

  React.useEffect(() => {
    setAppState(prev => ({
      ...prev,
      runtime: {
        ...prev.runtime,
        screen: "resume",
      },
    }));
  }, [setAppState]);

  React.useEffect(() => {
    if (!io || requestedRef.current) {
      return;
    }

    requestedRef.current = true;
    io.send({
      type: "resume_session",
      data: { action: "list" },
    });
  }, [io]);

  const filteredSessions = React.useMemo(
    () => filterSessions(sessions, searchQuery),
    [searchQuery, sessions],
  );

  React.useEffect(() => {
    if (filteredSessions.length === 0) {
      setSelectedIndex(0);
      return;
    }

    if (selectedIndex >= filteredSessions.length) {
      setSelectedIndex(filteredSessions.length - 1);
    }
  }, [filteredSessions.length, selectedIndex]);

  const rowsData = React.useMemo(
    () => buildRows(filteredSessions),
    [filteredSessions],
  );

  const listRows = Math.max(6, rows - 12);
  const visibleRows = React.useMemo(
    () => windowRows(rowsData, selectedIndex, listRows),
    [listRows, rowsData, selectedIndex],
  );

  const selectedSession = filteredSessions[selectedIndex] ?? null;
  const selectedSessionMatchesProject =
    selectedSession?.same_project ??
    sameProject(selectedSession?.project_path);
  const selectedSessionCost = normalizeCost(selectedSession?.cost);

  const restoreSelected = React.useCallback(
    (action: "resume" | "fork"): void => {
      if (!io || !selectedSession) {
        return;
      }

      setRestoring(action);
      setStatusText(
        `${action === "fork" ? "Forking" : "Restoring"} session ${selectedSession.session_id}...`,
      );
      io.send({
        type: "resume_session",
        data: {
          action: action === "fork" ? "fork" : "restore",
          session_id: selectedSession.session_id,
        },
      });
    },
    [io, selectedSession],
  );

  const handleEvent = React.useCallback(
    (message: IPCMessage): void => {
      switch (message.type) {
        case "session_list": {
          const data = message.data as SessionListData;
          setSessions(data.sessions);
          setLoading(false);
          setRestoring(null);
          setStatusText(
            data.sessions.length === 0
              ? "No saved sessions found"
              : `Loaded ${data.sessions.length} session(s)`,
          );
          return;
        }

        case "session_restored": {
          const data = message.data as SessionRestoredData;
          setRestoring(null);
          setRestored({
            sessionId: data.session_id,
            cost: normalizeCost(data.cost),
            messages: normalizeExternalMessages(
              Array.isArray(data.messages) ? data.messages : [],
            ),
            sessionContext: normalizeSessionContext(data),
          });
          return;
        }

        case "notification": {
          const data = message.data as NotificationData;
          addNotification({
            key: `resume:${data.level}:${data.title}:${data.message}`,
            text: `${data.title}: ${data.message}`,
            color:
              data.level === "error"
                ? "red"
                : data.level === "warn"
                  ? "yellow"
                  : "cyan",
            priority: data.level === "error" ? "high" : "medium",
          });
          setStatusText(`${data.title}: ${data.message}`);
          if (restoring) {
            setRestoring(null);
          }
          return;
        }

        case "cancel":
          setRestoring(null);
          setStatusText("Cancelled");
          return;

        default:
          return;
      }
    },
    [addNotification, restoring],
  );

  useStructuredIOListener(io, handleEvent, {
    replayRecent: true,
  });

  useInput((value, key) => {
    if (key.ctrl && value === "c") {
      exit();
      return;
    }

    if (restoring) {
      return;
    }

    if (searchMode) {
      if (key.escape) {
        if (searchQuery) {
          setSearchQuery("");
        } else {
          setSearchMode(false);
        }
        return;
      }

      if (key.return) {
        setSearchMode(false);
        return;
      }

      if (isBackspaceInput(value, key) || isDeleteInput(value, key)) {
        setSearchQuery(current => current.slice(0, -1));
        return;
      }

      if (value.length === 1 && !key.ctrl && !key.meta) {
        setSearchQuery(current => current + value);
      }
      return;
    }

    if (value === "q") {
      exit();
      return;
    }

    if (value === "/") {
      setSearchMode(true);
      return;
    }

    if ((key.downArrow || value === "j") && filteredSessions.length > 0) {
      setSelectedIndex(current =>
        Math.min(filteredSessions.length - 1, current + 1),
      );
      return;
    }

    if ((key.upArrow || value === "k") && filteredSessions.length > 0) {
      setSelectedIndex(current => Math.max(0, current - 1));
      return;
    }

    if (key.pageDown && filteredSessions.length > 0) {
      setSelectedIndex(current =>
        Math.min(filteredSessions.length - 1, current + 5),
      );
      return;
    }

    if (key.pageUp && filteredSessions.length > 0) {
      setSelectedIndex(current => Math.max(0, current - 5));
      return;
    }

    if (key.return || value === "r") {
      restoreSelected("resume");
      return;
    }

    if (value === "f") {
      restoreSelected("fork");
    }
  });

  if (restored) {
    return (
      <KeybindingSetup>
        <REPL
          io={io}
          initialSessionContext={{
            sessionId: restored.sessionId,
            cost: restored.cost,
            messages: restored.messages,
            context: restored.sessionContext,
          }}
        />
      </KeybindingSetup>
    );
  }

  if (loading) {
    return (
      <Box>
        <SpinnerWithVerb active={true} message="Loading conversations" />
      </Box>
    );
  }

  if (restoring) {
    return (
      <Box>
        <SpinnerWithVerb active={true} message="Resuming conversation" />
      </Box>
    );
  }

  if (filteredSessions.length === 0) {
    return (
      <Box flexDirection="column">
        <Text bold color="cyan">
          Resume Conversation
        </Text>
        <Text color="gray">
          Enter or `r` restore | `f` fork | `/` search | `j`/`k` or arrows move | `q` exit
        </Text>
        <Box marginTop={1}>
          <NotificationBanner notification={notifications.current} />
        </Box>
        <Box marginTop={1}>
          <NoConversationsMessage />
        </Box>
        <Box marginTop={1}>
          <Text color="gray">{statusText}</Text>
        </Box>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      <Text bold color="cyan">
        Resume Conversation
      </Text>
      <Text color="gray">
        Enter or `r` restore | `f` fork | `/` search | `j`/`k` or arrows move | `q` exit
      </Text>

      <Box marginTop={1}>
        <NotificationBanner notification={notifications.current} />
      </Box>

      <Box marginTop={1}>
        <SessionListPane
          visibleRows={visibleRows}
          selectedIndex={selectedIndex}
          searchMode={searchMode}
          searchQuery={searchQuery}
        />
      </Box>

      {selectedSession ? (
        <Box marginTop={1}>
          <Pane>
            <Text>
              Selected {selectedIndex + 1}/{filteredSessions.length}:{" "}
              {truncateToDisplayWidth(selectedSession.session_id, Math.max(12, columns - 28))}
            </Text>
            <Text color="gray" wrap="truncate">
              Project: {truncateToDisplayWidth(
                selectedSession.project_path || "unknown",
                Math.max(12, columns - 14),
              )}
            </Text>
            {selectedSession.worktree_path ? (
              <Text color="gray" wrap="truncate">
                Worktree: {truncateToDisplayWidth(
                  selectedSession.worktree_path,
                  Math.max(12, columns - 15),
                )}
              </Text>
            ) : null}
            {selectedSessionCost !== null ? (
              <Text color="gray">
                Cost: ${selectedSessionCost.toFixed(4)}
              </Text>
            ) : null}
            <Text color={selectedSessionMatchesProject ? "green" : "yellow"}>
              {selectedSessionMatchesProject
                ? "This session matches the current project."
                : "This session was created in a different project directory."}
            </Text>
          </Pane>
        </Box>
      ) : null}

      <Box marginTop={1}>
        <Text color={restoring ? "yellow" : "gray"}>{statusText}</Text>
      </Box>
    </Box>
  );
}
