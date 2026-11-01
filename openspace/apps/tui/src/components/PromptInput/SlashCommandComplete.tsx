import React from "react";
import { Box, Text } from "ink";
import { getColor } from "../design-system/theme.js";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import {
  stringDisplayWidth,
  truncateToDisplayWidth,
} from "../../utils/textWidth.js";

export type CompletionItem = {
  name: string;
  summary: string;
  category?: string;
};

type SlashCommandCompleteProps = {
  items: CompletionItem[];
  selectedIndex: number;
  visible: boolean;
  maxVisibleItems?: number;
  maxColumnWidth?: number;
  bordered?: boolean;
};

function oneLine(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function padRight(value: string, width: number): string {
  const padding = Math.max(0, width - stringDisplayWidth(value));
  return `${value}${" ".repeat(padding)}`;
}

export function SlashCommandComplete({
  items,
  selectedIndex,
  visible,
  maxVisibleItems,
  maxColumnWidth,
  bordered = false,
}: SlashCommandCompleteProps): React.ReactElement | null {
  const { columns } = useTerminalSize();

  if (!visible || items.length === 0) return null;

  const visibleCount = Math.max(
    1,
    Math.min(items.length, maxVisibleItems ?? items.length),
  );
  const startIndex = Math.max(
    0,
    Math.min(items.length - visibleCount, selectedIndex - Math.floor(visibleCount / 2)),
  );
  const visibleItems = items.slice(startIndex, startIndex + visibleCount);
  const hasMore = visibleItems.length < items.length;
  const contentWidth = Math.max(20, columns - (bordered ? 4 : 2));
  const longestNameWidth = Math.max(
    ...items.map(item => stringDisplayWidth(`/${item.name}`)),
  );
  const nameColumnWidth = Math.min(
    Math.max(12, longestNameWidth + 2),
    maxColumnWidth ?? Math.max(12, Math.floor(contentWidth * 0.4)),
  );
  const summaryWidth = Math.max(0, contentWidth - nameColumnWidth - 5);
  const list = (
    <Box flexDirection="column" width="100%">
      {visibleItems.map((item, offset) => {
        const absoluteIndex = startIndex + offset;
        const isSelected = absoluteIndex === selectedIndex;
        const commandName = truncateToDisplayWidth(`/${item.name}`, nameColumnWidth - 1);
        const paddedName = padRight(commandName, nameColumnWidth);
        const summary = summaryWidth > 0
          ? truncateToDisplayWidth(oneLine(item.summary), summaryWidth)
          : "";

        return (
          <Text key={item.name} wrap="truncate">
            <Text
              color={isSelected ? getColor("primary") : getColor("text")}
              bold={isSelected}
              dimColor={!isSelected}
            >
              {isSelected ? "> " : "  "}
              {paddedName}
            </Text>
            {summary ? (
              <Text
                color={isSelected ? getColor("primary") : getColor("textDim")}
                dimColor={!isSelected}
              >
                {" - "}
                {summary}
              </Text>
            ) : null}
          </Text>
        );
      })}

      {hasMore ? (
        <Text color={getColor("textDim")} wrap="truncate">
          Showing {startIndex + 1}-{startIndex + visibleItems.length} of{" "}
          {items.length}
        </Text>
      ) : null}
    </Box>
  );

  return bordered ? (
    <Box
      flexDirection="column"
      borderStyle="single"
      borderColor={getColor("borderFocused")}
      paddingX={1}
      width="100%"
    >
      {list}
    </Box>
  ) : (
    list
  );
}
