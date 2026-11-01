import React from "react";
import { Box, Text } from "ink";
import { getColor } from "../design-system/theme.js";

export type DiffLine = {
  type: "add" | "remove" | "context" | "header";
  content: string;
  oldLineNo?: number;
  newLineNo?: number;
};

export type DiffHunk = {
  header: string;
  lines: DiffLine[];
};

export type DiffFile = {
  path: string;
  hunks: DiffHunk[];
  isBinary?: boolean;
  isNew?: boolean;
  isDeleted?: boolean;
  isRenamed?: { from: string; to: string };
};

type DiffViewProps = {
  file: DiffFile;
  maxLines?: number;
  collapsed?: boolean;
};

function lineTypeColor(type: DiffLine["type"]): string {
  switch (type) {
    case "add":
      return getColor("success");
    case "remove":
      return getColor("error");
    case "header":
      return getColor("primary");
    case "context":
    default:
      return getColor("text");
  }
}

function linePrefix(type: DiffLine["type"]): string {
  switch (type) {
    case "add":
      return "+";
    case "remove":
      return "-";
    case "header":
      return "@";
    case "context":
    default:
      return " ";
  }
}

function formatLineNo(n: number | undefined, width: number): string {
  if (n === undefined) return " ".repeat(width);
  return String(n).padStart(width);
}

export function DiffView({
  file,
  maxLines,
  collapsed,
}: DiffViewProps): React.ReactElement {
  const allLines = file.hunks.flatMap(hunk => [
    { type: "header" as const, content: hunk.header },
    ...hunk.lines,
  ]);

  const visibleLines = maxLines ? allLines.slice(0, maxLines) : allLines;
  const truncated = maxLines !== undefined && allLines.length > maxLines;
  const lineNoWidth = Math.max(
    3,
    String(Math.max(...allLines.map(l => l.newLineNo ?? l.oldLineNo ?? 0))).length,
  );

  const label = file.isNew
    ? "(new file)"
    : file.isDeleted
      ? "(deleted)"
      : file.isRenamed
        ? `(renamed from ${file.isRenamed.from})`
        : "";

  return (
    <Box flexDirection="column">
      <Box>
        <Text color={getColor("primary")} bold>
          {collapsed ? "▸ " : "▾ "}
          {file.path}
        </Text>
        {label ? <Text color={getColor("textDim")}> {label}</Text> : null}
      </Box>

      {file.isBinary ? (
        <Text color={getColor("textDim")}>  Binary file</Text>
      ) : collapsed ? null : (
        <Box flexDirection="column" marginLeft={1}>
          {visibleLines.map((line, i) => (
            <Box key={i}>
              <Text color={getColor("textDim")}>
                {formatLineNo(line.oldLineNo, lineNoWidth)}{" "}
                {formatLineNo(line.newLineNo, lineNoWidth)}{" "}
              </Text>
              <Text color={lineTypeColor(line.type)}>
                {linePrefix(line.type)} {line.content}
              </Text>
            </Box>
          ))}

          {truncated ? (
            <Text color={getColor("textDim")}>
              … {allLines.length - visibleLines.length} more lines
            </Text>
          ) : null}
        </Box>
      )}
    </Box>
  );
}
