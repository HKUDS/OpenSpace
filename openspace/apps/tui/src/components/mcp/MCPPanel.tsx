import React from "react";
import { Box, Text } from "ink";
import type {
  MCPClientState,
  MCPToolState,
} from "../../state/AppStateStore.js";
import type { Command } from "../../types/command.js";
import { getColor } from "../design-system/theme.js";

type MCPPanelProps = {
  clients: MCPClientState[];
  tools?: MCPToolState[];
  commands?: Command[];
  resources?: Record<string, string[]>;
  onReconnect?: (serverName: string) => void;
};

function statusIcon(status: MCPClientState["status"]): string {
  switch (status) {
    case "connected":
      return "●";
    case "disconnected":
      return "○";
    case "error":
      return "✗";
  }
}

function statusColor(status: MCPClientState["status"]): string {
  switch (status) {
    case "connected":
      return getColor("success");
    case "disconnected":
      return getColor("textDim");
    case "error":
      return getColor("error");
  }
}

function formatTimeSince(timestamp: number): string {
  const seconds = Math.floor((Date.now() - timestamp) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

export function MCPPanel({
  clients,
  tools = [],
  commands = [],
  resources = {},
}: MCPPanelProps): React.ReactElement {
  if (
    clients.length === 0 &&
    tools.length === 0 &&
    commands.length === 0 &&
    Object.keys(resources).length === 0
  ) {
    return (
      <Box borderStyle="round" borderColor={getColor("border")} paddingX={1}>
        <Text color={getColor("textDim")}>No MCP servers or resources available</Text>
      </Box>
    );
  }

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("border")}
      paddingX={1}
    >
      <Text bold color={getColor("primary")}>
        MCP Servers
      </Text>

      {clients.map(client => (
        <Box key={client.serverName} marginTop={1} flexDirection="column">
          <Box>
            <Text color={statusColor(client.status)}>
              {statusIcon(client.status)}{" "}
            </Text>
            <Text bold>{client.serverName}</Text>
            <Text color={getColor("textDim")}>
              {" "}— {client.status} ({formatTimeSince(client.updatedAt)})
            </Text>
          </Box>

          {client.error ? (
            <Text color={getColor("error")}>  Error: {client.error}</Text>
          ) : null}
        </Box>
      ))}

      {tools.length > 0 ? (
        <Box marginTop={1} flexDirection="column">
          <Text bold color={getColor("primary")}>Tools</Text>
          {tools.map(tool => (
            <Text key={`${tool.serverName ?? "local"}:${tool.name}`}>
              - {tool.name}
              {tool.serverName ? ` [${tool.serverName}]` : ""}
              {tool.description ? ` — ${tool.description}` : ""}
            </Text>
          ))}
        </Box>
      ) : null}

      {commands.length > 0 ? (
        <Box marginTop={1} flexDirection="column">
          <Text bold color={getColor("primary")}>Commands</Text>
          {commands.map(command => (
            <Text key={command.name}>
              - /{command.name}
              {command.description ? ` — ${command.description}` : ""}
            </Text>
          ))}
        </Box>
      ) : null}

      {Object.keys(resources).length > 0 ? (
        <Box marginTop={1} flexDirection="column">
          <Text bold color={getColor("primary")}>Resources</Text>
          {Object.entries(resources).map(([serverName, serverResources]) => (
            <Box key={serverName} flexDirection="column">
              <Text>{serverName}</Text>
              {serverResources.length > 0 ? (
                serverResources.map(resource => (
                  <Text key={`${serverName}:${resource}`} color={getColor("textDim")}>
                    - {resource}
                  </Text>
                ))
              ) : (
                <Text color={getColor("textDim")}>- none</Text>
              )}
            </Box>
          ))}
        </Box>
      ) : null}
    </Box>
  );
}
