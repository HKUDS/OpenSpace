import React from "react";
import {
  Box,
  Text,
  useApp,
  useInput,
} from "ink";
import type {
  CommandResultData,
  DoctorResultData,
  IPCMessage,
  NotificationData,
} from "../bridge/protocol.js";
import type { StructuredIO } from "../bridge/structuredIO.js";
import { NotificationBanner } from "../components/NotificationBanner.js";
import { PressEnterToContinue } from "../components/PressEnterToContinue.js";
import { SpinnerWithVerb } from "../components/Spinner.js";
import { Pane } from "../components/design-system/Pane.js";
import { useNotifications } from "../context/notifications.js";
import { loadKeybindingsSyncWithWarnings } from "../keybindings/loadUserBindings.js";
import { useAppState, useSetAppState } from "../state/AppState.js";
import { useStructuredIOListener } from "./shared.js";

type Props = {
  io: StructuredIO | null;
};

type DoctorCheck = DoctorResultData["checks"][number];
type DoctorSection = {
  name: string;
  title?: string;
  order: number;
  status?: DoctorResultData["section_status"];
  summary?: string;
  checks: DoctorCheck[];
  done: boolean;
};

function upsertSection(
  sections: DoctorSection[],
  data: DoctorResultData,
): DoctorSection[] {
  const index = sections.findIndex(
    section => section.name === data.section,
  );

  if (index === -1) {
    return [
      ...sections,
      {
        name: data.section,
        title: data.section_title,
        order: data.section_order ?? sections.length,
        status: data.section_status,
        summary: data.summary,
        checks: data.checks,
        done: data.section_done ?? data.done ?? false,
      },
    ];
  }

  const existing = sections[index]!;
  const mergedChecks = [...existing.checks];

  for (const nextCheck of data.checks) {
    const checkIndex = mergedChecks.findIndex(
      check => check.name === nextCheck.name,
    );
    if (checkIndex === -1) {
      mergedChecks.push(nextCheck);
    } else {
      mergedChecks[checkIndex] = nextCheck;
    }
  }

  const next = [...sections];
  next[index] = {
    ...existing,
    title: data.section_title ?? existing.title,
    order: data.section_order ?? existing.order,
    status: data.section_status ?? existing.status,
    summary: data.summary ?? existing.summary,
    checks: mergedChecks,
    done: data.section_done ?? data.done ?? existing.done,
  };
  return next;
}

function deriveSectionStatus(
  checks: DoctorCheck[],
): DoctorResultData["section_status"] {
  if (checks.some(check => check.status === "fail")) {
    return "fail";
  }

  if (checks.some(check => check.status === "warn")) {
    return "warn";
  }

  if (checks.some(check => check.status === "pass")) {
    return "pass";
  }

  return "info";
}

function statusGlyph(status: DoctorCheck["status"]): string {
  switch (status) {
    case "pass":
      return "[PASS]";
    case "warn":
      return "[WARN]";
    case "fail":
      return "[FAIL]";
    default:
      return "[INFO]";
  }
}

function statusColor(status: DoctorCheck["status"]): string {
  switch (status) {
    case "pass":
      return "green";
    case "warn":
      return "yellow";
    case "fail":
      return "red";
    default:
      return "white";
  }
}

function sectionStatusColor(
  status: DoctorSection["status"],
): string {
  switch (status) {
    case "pass":
      return "green";
    case "warn":
      return "yellow";
    case "fail":
      return "red";
    case "info":
      return "cyan";
    default:
      return "yellow";
  }
}

function buildLocalSections(
  pluginErrors: string[],
): DoctorSection[] {
  const localSections: DoctorSection[] = [];
  const keybindingWarnings = loadKeybindingsSyncWithWarnings().warnings;
  const keybindingChecks: DoctorCheck[] =
    keybindingWarnings.length > 0
      ? keybindingWarnings.map(warning => ({
          name:
            warning.type === "parse_error"
              ? "Keybinding Parse"
              : "Keybinding Validation",
          status: warning.severity === "error" ? "fail" : "warn",
          message: warning.message,
          ...(warning.suggestion
            ? { details: warning.suggestion }
            : {}),
        }))
      : [
          {
            name: "Keybinding Config",
            status: "pass",
            message: "Keybinding config loaded without warnings",
          },
        ];

  localSections.push({
    name: "keybindings",
    title: "Keybindings",
    order: 100,
    status: deriveSectionStatus(keybindingChecks),
    summary:
      keybindingWarnings.length > 0
        ? `${keybindingWarnings.length} keybinding warning(s)`
        : "Keybinding config OK",
    checks: keybindingChecks,
    done: true,
  });

  if (pluginErrors.length > 0) {
    const pluginChecks: DoctorCheck[] = pluginErrors.map(error => ({
      name: "Plugin Error",
      status: "fail",
      message: error,
    }));

    localSections.push({
      name: "plugin-errors",
      title: "Plugin Errors",
      order: 101,
      status: "fail",
      summary: `${pluginErrors.length} TUI plugin error(s)`,
      checks: pluginChecks,
      done: true,
    });
  }

  return localSections;
}

function SectionPane({
  section,
}: {
  section: DoctorSection;
}): React.ReactElement {
  return (
    <Box marginTop={1}>
      <Pane>
        <Text
          bold
          color={sectionStatusColor(section.status) as never}
        >
          {section.title ?? section.name}
        </Text>
        {section.summary ? (
          <Text color="gray">{section.summary}</Text>
        ) : null}
        {section.checks.map(check => (
          <Box
            key={`${section.name}:${check.name}`}
            flexDirection="column"
            marginTop={1}
          >
            <Text color={statusColor(check.status) as never}>
              {statusGlyph(check.status)} {check.name}
            </Text>
            <Text>{check.message}</Text>
            {check.details ? (
              <Text color="gray">{check.details}</Text>
            ) : null}
          </Box>
        ))}
        {section.done ? (
          <Text color="green">Section complete</Text>
        ) : null}
      </Pane>
    </Box>
  );
}

export function Doctor({ io }: Props): React.ReactElement {
  const { exit } = useApp();
  const setAppState = useSetAppState();
  const notifications = useAppState(state => state.notifications);
  const pluginErrors = useAppState(state => state.plugins.errors);
  const { addNotification } = useNotifications();

  const [sections, setSections] = React.useState<DoctorSection[]>([]);
  const [running, setRunning] = React.useState(true);
  const [statusText, setStatusText] = React.useState(
    "Starting diagnostics...",
  );
  const requestedRef = React.useRef(false);
  const runIdRef = React.useRef<string | null>(null);

  const runDoctor = React.useCallback((): void => {
    if (!io) {
      setRunning(false);
      setStatusText("Structured IO is unavailable");
      return;
    }

    setSections([]);
    setRunning(true);
    setStatusText("Running doctor checks...");
    runIdRef.current = null;
    io.send({
      type: "slash_command",
      data: {
        command: "doctor",
        args: [],
      },
    });
  }, [io]);

  React.useEffect(() => {
    setAppState(prev => ({
      ...prev,
      runtime: {
        ...prev.runtime,
        screen: "doctor",
      },
    }));
  }, [setAppState]);

  React.useEffect(() => {
    if (requestedRef.current) {
      return;
    }

    requestedRef.current = true;
    runDoctor();
  }, [runDoctor]);

  const handleEvent = React.useCallback(
    (message: IPCMessage): void => {
      switch (message.type) {
        case "doctor_result": {
          const data = message.data as DoctorResultData;
          if (data.run_id && runIdRef.current !== data.run_id) {
            runIdRef.current = data.run_id;
            setSections([]);
          }
          setSections(current => upsertSection(current, data));

          if (data.run_done || (data.run_done === undefined && data.done)) {
            setRunning(false);
            setStatusText(data.summary ?? "Doctor run completed");
          } else if (data.checks.length > 0) {
            const latest = data.checks[data.checks.length - 1]!;
            setStatusText(`${latest.name}: ${latest.message}`);
          }
          return;
        }

        case "command_result": {
          const data = message.data as CommandResultData;
          if (data.command === "doctor" && data.message) {
            setStatusText(data.message);
          }
          return;
        }

        case "notification": {
          const data = message.data as NotificationData;
          addNotification({
            key: `doctor:${data.level}:${data.title}:${data.message}`,
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
          return;
        }

        case "cancel":
          setRunning(false);
          setStatusText("Doctor run cancelled");
          return;

        default:
          return;
      }
    },
    [addNotification],
  );

  useStructuredIOListener(io, handleEvent, {
    replayRecent: true,
  });

  useInput((value, key) => {
    if (key.ctrl && value === "c") {
      exit();
      return;
    }

    if (value === "q") {
      exit();
      return;
    }

    if (key.return && !running) {
      exit();
      return;
    }

    if (value === "r") {
      runDoctor();
    }
  });

  const orderedSections = [...sections].sort(
    (left, right) => left.order - right.order,
  );
  const localSections = buildLocalSections(pluginErrors);
  const visibleSections = [...orderedSections, ...localSections].sort(
    (left, right) => left.order - right.order,
  );
  const allChecks = visibleSections.flatMap(section => section.checks);
  const summary = {
    pass: allChecks.filter(check => check.status === "pass").length,
    warn: allChecks.filter(check => check.status === "warn").length,
    fail: allChecks.filter(check => check.status === "fail").length,
  };

  if (!running && visibleSections.length === 0) {
    return (
      <Pane>
        <Text bold>Diagnostics</Text>
        <Text>{statusText}</Text>
      </Pane>
    );
  }

  return (
    <Box flexDirection="column">
      <Text bold color="cyan">
        OpenSpace Doctor
      </Text>
      <Text color="gray">
        `r` rerun diagnostics | `q` exit
      </Text>

      <Box marginTop={1}>
        <NotificationBanner notification={notifications.current} />
      </Box>

      <Box marginTop={1}>
        <Pane>
          <Text bold>Diagnostics</Text>
          <Text>
            Status: {running ? "running" : "idle"} | pass {summary.pass} | warn {summary.warn} | fail {summary.fail}
          </Text>
          <Box marginTop={1}>
            <SpinnerWithVerb
              active={running}
              message={running ? "Checking installation status" : "Diagnostics complete"}
            />
          </Box>
          <Box marginTop={1}>
            <Text color={running ? "yellow" : "gray"}>{statusText}</Text>
          </Box>
        </Pane>
      </Box>

      {sections.length === 0 ? (
        <Box marginTop={1}>
          <Pane>
            <Text color="gray">Waiting for doctor results...</Text>
          </Pane>
        </Box>
      ) : null}

      {visibleSections.map(section => (
        <SectionPane key={section.name} section={section} />
      ))}

      {!running ? (
        <Box marginTop={1}>
          <PressEnterToContinue />
        </Box>
      ) : null}
    </Box>
  );
}
