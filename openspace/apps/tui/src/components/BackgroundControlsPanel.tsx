import React from "react";
import { Box, Text } from "ink";
import { getColor } from "./design-system/theme.js";
import { BackgroundSessionSummary } from "./BackgroundSessionSummary.js";
import type { RuntimeTab } from "./AgentRuntimePane.js";

type ControlHint = {
  key: string;
  label: string;
  description?: string;
};

type Props = {
  session: Record<string, unknown> | null;
  title?: string;
  emptyLabel?: string;
  selectedTab?: RuntimeTab | null;
  actionHints?: string[];
  controls?: ControlHint[];
};

function tabLabel(tab: RuntimeTab | null | undefined): string {
  switch (tab) {
    case "list":
      return "agents";
    case "events":
      return "events";
    case "transcript":
      return "transcript";
    default:
      return "idle";
  }
}

export function BackgroundControlsPanel({
  session,
  title = "Background Controls",
  emptyLabel = "No background session active",
  selectedTab = null,
  actionHints = [],
  controls = [],
}: Props): React.ReactElement {
  return (
    <Box flexDirection="column">
      <Text bold color={getColor("primary")}>
        {title}
      </Text>

      <Box marginTop={1}>
        <BackgroundSessionSummary
          session={session}
          title="Session"
          emptyLabel={emptyLabel}
        />
      </Box>

      <Text color={getColor("textDim")}>
        Active pane: {tabLabel(selectedTab)}
      </Text>

      {actionHints.length > 0 ? (
        <Text color={getColor("textDim")}>
          {actionHints.join(" | ")}
        </Text>
      ) : null}

      {controls.length > 0 ? (
        <Box flexDirection="column" marginTop={1}>
          {controls.map(control => (
            <Text key={control.key} color={getColor("textDim")}>
              [{control.key}] {control.label}
              {control.description ? ` - ${control.description}` : ""}
            </Text>
          ))}
        </Box>
      ) : null}
    </Box>
  );
}
