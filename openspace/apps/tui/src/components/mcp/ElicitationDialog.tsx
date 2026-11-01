import React from "react";
import { Box, Text } from "ink";
import type { ElicitationRequestData } from "../../bridge/protocol.js";
import { useModalOrTerminalSize } from "../../context/modalContext.js";
import { useRegisterOverlay } from "../../context/overlayContext.js";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";

export type ElicitationField = {
  key: string;
  type: string;
  title: string;
  required: boolean;
  description?: string;
  defaultValue?: string;
};

type Draft = {
  request: ElicitationRequestData;
  values: Record<string, string>;
  activeField: number;
  error: string | null;
};

type Props = {
  draft: Draft;
  fields: ElicitationField[];
};

export function ElicitationDialog({
  draft,
  fields,
}: Props): React.ReactElement {
  const terminalSize = useTerminalSize();
  const { rows } = useModalOrTerminalSize(terminalSize);
  const maxVisibleFields = Math.max(3, rows - 8);
  useRegisterOverlay("mcp-elicitation");
  const visibleFields =
    fields.length > maxVisibleFields
      ? fields.slice(
          Math.max(
            0,
            Math.min(
              draft.activeField - Math.floor(maxVisibleFields / 2),
              fields.length - maxVisibleFields,
            ),
          ),
          Math.max(
            0,
            Math.min(
              draft.activeField - Math.floor(maxVisibleFields / 2),
              fields.length - maxVisibleFields,
            ),
          ) + maxVisibleFields,
        )
      : fields;

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="magenta"
      paddingX={1}
      marginTop={1}
    >
      <Text bold color="magenta">
        MCP Elicitation
      </Text>
      <Text>Server: {draft.request.server_name || "unknown"}</Text>
      <Text>{draft.request.message}</Text>

      {fields.length === 0 ? (
        <Text color="gray">
          No schema fields. Press Enter to submit an empty response.
        </Text>
      ) : (
        <Box flexDirection="column" marginTop={1}>
          {visibleFields.map(field => {
            const index = fields.findIndex(candidate => candidate.key === field.key);
            const isActive = index === draft.activeField;
            const value = draft.values[field.key] ?? field.defaultValue ?? "";

            return (
              <Box key={field.key} flexDirection="column" marginBottom={1}>
                <Text color={isActive ? "cyan" : "white"} bold={isActive}>
                  {isActive ? ">" : " "} {field.title}
                  {field.required ? " *" : ""} <Text color="gray">({field.type})</Text>
                </Text>
                {field.description ? (
                  <Text color="gray">{field.description}</Text>
                ) : null}
                <Text>{value || "<empty>"}</Text>
              </Box>
            );
          })}
          {visibleFields.length < fields.length ? (
            <Text color="gray">
              Showing {visibleFields.length} of {fields.length} fields
            </Text>
          ) : null}
        </Box>
      )}

      {draft.error ? <Text color="red">{draft.error}</Text> : null}

      <Text color="gray">
        Type to edit | Tab or Up/Down to switch | Enter submit | Esc send empty response
      </Text>
    </Box>
  );
}
