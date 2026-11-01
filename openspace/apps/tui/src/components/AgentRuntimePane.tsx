import React from "react";
import { Box, Text } from "ink";
import type { AppMessage } from "../state/AppStateStore.js";
import { getColor } from "./design-system/theme.js";
import { AgentEventFeed } from "./AgentEventFeed.js";
import { AgentListPanel } from "./AgentListPanel.js";
import {
  AgentTranscriptPanel,
  type AgentTranscriptHandle,
} from "./AgentTranscriptPanel.js";
import { AgentTranscriptPreview } from "./AgentTranscriptPreview.js";
import { BackgroundSessionSummary } from "./BackgroundSessionSummary.js";

export type RuntimeTab = "list" | "events" | "transcript";

type Props = {
  title?: string;
  selectedTab: RuntimeTab;
  agents: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
  transcriptMessages: AppMessage[];
  backgroundSession?: Record<string, unknown> | null;
  selectedAgentId?: string | null;
  selectedEventIndex?: number | null;
  transcriptCursor?: number | null;
  selectedMessageId?: string | null;
  agentLabel?: string;
  listTitle?: string;
  eventTitle?: string;
  transcriptTitle?: string;
  transcriptEmptyLabel?: string;
  actionHints?: string[];
  maxAgents?: number;
  maxEvents?: number;
  maxTranscriptMessages?: number;
  transcriptPanelRef?: React.Ref<AgentTranscriptHandle | null>;
  transcriptSearchVisible?: boolean;
  transcriptSearchQuery?: string;
  transcriptSearchMatchCount?: number;
  transcriptSearchCurrentMatch?: number;
  onTranscriptSearchMatchesChange?: (count: number, current: number) => void;
  onTranscriptCursorChange?: (messageId: string | null, index: number | null) => void;
};

const TAB_LABELS: Record<RuntimeTab, string> = {
  list: "Agents",
  events: "Events",
  transcript: "Transcript",
};

function tabColor(tab: RuntimeTab, selectedTab: RuntimeTab): string {
  return tab === selectedTab ? getColor("primary") : getColor("textDim");
}

function tabWeight(tab: RuntimeTab, selectedTab: RuntimeTab): boolean {
  return tab === selectedTab;
}

function selectionStatus(props: {
  selectedTab: RuntimeTab;
  selectedAgentId?: string | null;
  selectedEventIndex?: number | null;
  transcriptCursor?: number | null;
  selectedMessageId?: string | null;
}): string {
  switch (props.selectedTab) {
    case "list":
      return props.selectedAgentId
        ? `Selected agent: ${props.selectedAgentId}`
        : "Selected agent: none";
    case "events":
      return props.selectedEventIndex !== null &&
        props.selectedEventIndex !== undefined
        ? `Selected event: ${props.selectedEventIndex + 1}`
        : "Selected event: none";
    case "transcript":
      if (props.selectedMessageId) {
        return `Selected message: ${props.selectedMessageId}`;
      }
      return props.transcriptCursor !== null &&
        props.transcriptCursor !== undefined
        ? `Transcript cursor: ${props.transcriptCursor + 1}`
        : "Transcript cursor: none";
    default:
      return "Selection: none";
  }
}

function renderTabStrip(selectedTab: RuntimeTab): React.ReactElement {
  return (
    <Box>
      {(Object.keys(TAB_LABELS) as RuntimeTab[]).map(tab => (
        <Text
          key={tab}
          color={tabColor(tab, selectedTab)}
          bold={tabWeight(tab, selectedTab)}
        >
          {tab === selectedTab ? "›" : " "} {TAB_LABELS[tab]}
          {" "}
        </Text>
      ))}
    </Box>
  );
}

export function AgentRuntimePane({
  title = "Runtime",
  selectedTab,
  agents,
  events,
  transcriptMessages,
  backgroundSession = null,
  selectedAgentId = null,
  selectedEventIndex = null,
  transcriptCursor = null,
  selectedMessageId = null,
  agentLabel = "Viewed agent",
  listTitle,
  eventTitle,
  transcriptTitle,
  transcriptEmptyLabel,
  actionHints = [],
  maxAgents = 8,
  maxEvents = 10,
  maxTranscriptMessages = 5,
  transcriptPanelRef,
  transcriptSearchVisible = false,
  transcriptSearchQuery = "",
  transcriptSearchMatchCount = 0,
  transcriptSearchCurrentMatch = 0,
  onTranscriptSearchMatchesChange,
  onTranscriptCursorChange,
}: Props): React.ReactElement {
  const activePanel =
    selectedTab === "list" ? (
      <AgentListPanel
        agents={agents}
        title={listTitle}
        selectedAgentId={selectedAgentId}
        maxAgents={maxAgents}
      />
    ) : selectedTab === "events" ? (
      <AgentEventFeed
        events={events}
        title={eventTitle}
        maxEvents={maxEvents}
        selectedEventIndex={selectedEventIndex}
        actionHints={actionHints}
      />
    ) : (
      <AgentTranscriptPanel
        ref={transcriptPanelRef}
        messages={transcriptMessages}
        agentLabel={agentLabel}
        title={transcriptTitle}
        emptyLabel={transcriptEmptyLabel}
        cursor={transcriptCursor}
        selectedMessageId={selectedMessageId}
        actionHints={actionHints}
        searchVisible={transcriptSearchVisible}
        searchQuery={transcriptSearchQuery}
        searchMatchCount={transcriptSearchMatchCount}
        searchCurrentMatch={transcriptSearchCurrentMatch}
        onSearchMatchesChange={onTranscriptSearchMatchesChange}
        onCursorChange={onTranscriptCursorChange}
      />
    );

  const transcriptPreview =
    selectedTab !== "transcript" && transcriptMessages.length > 0 ? (
      <Box marginTop={1}>
        <AgentTranscriptPreview
          messages={transcriptMessages}
          agentLabel={agentLabel}
          maxMessages={Math.min(3, maxTranscriptMessages)}
        />
      </Box>
    ) : null;

  return (
    <Box flexDirection="column">
      <Text bold color={getColor("primary")}>
        {title}
      </Text>

      <Box marginTop={1}>{renderTabStrip(selectedTab)}</Box>

      <Text color={getColor("textDim")}>
        {selectionStatus({
          selectedTab,
          selectedAgentId,
          selectedEventIndex,
          transcriptCursor,
          selectedMessageId,
        })}
      </Text>

      {backgroundSession ? (
        <Box marginTop={1}>
          <BackgroundSessionSummary session={backgroundSession} />
        </Box>
      ) : null}

      <Box marginTop={1}>{activePanel}</Box>

      {transcriptPreview}

      {actionHints.length > 0 ? (
        <Box marginTop={1}>
          <Text color={getColor("textDim")}>
            {actionHints.join(" | ")}
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}
