import React from "react";
import { Box, Text } from "ink";
import type {
  MemorySelectorData,
  MemoryTargetData,
} from "../../bridge/protocol.js";
import { useRegisterOverlay } from "../../context/overlayContext.js";
import { useKeybindings } from "../../keybindings/useKeybinding.js";
import { getColor } from "../design-system/theme.js";

type Props = {
  data: MemorySelectorData;
  onSelect: (target: MemoryTargetData) => void;
  onCancel: () => void;
};

function clampIndex(index: number, length: number): number {
  if (length <= 0) return 0;
  return Math.max(0, Math.min(length - 1, index));
}

function targetDescription(target: MemoryTargetData): string {
  const parts = [
    target.description,
    target.exists === false && !target.is_folder ? "new" : "",
    target.is_folder ? "folder" : "",
  ].filter(Boolean);
  return parts.join(" - ");
}

function labelFor(target: MemoryTargetData): string {
  return target.label || target.display_path || target.path;
}

function pathFor(target: MemoryTargetData): string {
  return target.display_path || target.path;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 3))}...`;
}

export function MemoryFileSelector({
  data,
  onSelect,
  onCancel,
}: Props): React.ReactElement {
  useRegisterOverlay("memory-selector");
  const targets = data.targets ?? [];
  const [selectedIndex, setSelectedIndex] = React.useState(0);

  React.useEffect(() => {
    setSelectedIndex(index => clampIndex(index, targets.length));
  }, [targets.length]);

  const selectedTarget = targets[selectedIndex] ?? null;

  useKeybindings(
    {
      "confirm:yes": () => {
        if (selectedTarget) {
          onSelect(selectedTarget);
        }
      },
      "confirm:no": onCancel,
      "confirm:previous": () => {
        setSelectedIndex(index => clampIndex(index - 1, targets.length));
      },
      "confirm:next": () => {
        setSelectedIndex(index => clampIndex(index + 1, targets.length));
      },
    },
    { context: "Confirmation" },
  );

  const maxVisibleTargets = 12;
  const firstVisibleIndex = Math.min(
    Math.max(0, selectedIndex - maxVisibleTargets + 1),
    Math.max(0, targets.length - maxVisibleTargets),
  );
  const visibleTargets = targets.slice(
    firstVisibleIndex,
    firstVisibleIndex + maxVisibleTargets,
  );

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("primary")}
      paddingX={1}
      marginTop={1}
    >
      <Text bold color={getColor("primary")}>
        Memory
      </Text>
      {data.cwd ? <Text color={getColor("textDim")}>{data.cwd}</Text> : null}

      <Box flexDirection="column" marginTop={1}>
        {visibleTargets.length === 0 ? (
          <Text color={getColor("warning")}>No memory files are available.</Text>
        ) : (
          <>
            {firstVisibleIndex > 0 ? (
              <Text color={getColor("textDim")}>
                ... {firstVisibleIndex} earlier via up
              </Text>
            ) : null}
            {visibleTargets.map((target, index) => {
              const targetIndex = firstVisibleIndex + index;
              const focused = targetIndex === selectedIndex;
              const color = focused ? getColor("primary") : getColor("text");
              const description = targetDescription(target);
              return (
                <Box key={`${target.path}:${targetIndex}`} flexDirection="column">
                  <Text color={color as never}>
                    {focused ? "> " : "  "}
                    {truncate(labelFor(target), 48)}
                    <Text color={getColor("textDim")}>
                      {" "}
                      {truncate(pathFor(target), 72)}
                    </Text>
                  </Text>
                  {description ? (
                    <Text color={getColor("textDim")}>
                      {"    "}
                      {truncate(description, 96)}
                    </Text>
                  ) : null}
                </Box>
              );
            })}
          </>
        )}
        {targets.length > firstVisibleIndex + visibleTargets.length ? (
          <Text color={getColor("textDim")}>
            ... {targets.length - firstVisibleIndex - visibleTargets.length} more via down
          </Text>
        ) : null}
      </Box>

      <Box marginTop={1}>
        <Text color={getColor("textDim")}>
          Up/down select | Enter open | Esc cancel
        </Text>
      </Box>
    </Box>
  );
}
