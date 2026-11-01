import { truncateToDisplayWidth } from "./textWidth.js";

type FormatOptions = {
  allowGenericRecord?: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

function compact(value: unknown, width = 180): string {
  if (typeof value === "string") {
    return truncateToDisplayWidth(value.replace(/\s+/g, " ").trim(), width);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return truncateToDisplayWidth(
      value.map(item => compact(item, 60)).join(", "),
      width,
    );
  }
  if (isRecord(value)) {
    const entries = Object.entries(value)
      .filter(([, entryValue]) => entryValue !== undefined && entryValue !== null)
      .slice(0, 4)
      .map(([key, entryValue]) => `${key}=${compact(entryValue, 60)}`);
    return truncateToDisplayWidth(entries.join(", "), width);
  }
  return "";
}

function getString(
  record: Record<string, unknown>,
  keys: string[],
): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      return String(value);
    }
  }
  return null;
}

function formatEvolutionSuggestion(value: unknown, index: number): string | null {
  if (!isRecord(value)) {
    return null;
  }

  const type = getString(value, ["type", "evolution_type", "kind"]);
  const target = value.target_skills ?? value.target_skill ?? value.skill_id;
  const title = [
    `${index + 1}.`,
    type ? type.toUpperCase() : "SUGGESTION",
    target !== undefined ? `target=${compact(target, 90)}` : "",
  ]
    .filter(Boolean)
    .join(" ");
  const reason = getString(value, ["reason", "rationale", "why"]);
  const change = getString(value, [
    "proposed_change",
    "change",
    "summary",
    "description",
  ]);
  const confidence = getString(value, ["confidence", "score"]);
  const lines = [title];
  if (reason) {
    lines.push(`   reason: ${truncateToDisplayWidth(reason, 180)}`);
  }
  if (change) {
    lines.push(`   change: ${truncateToDisplayWidth(change, 180)}`);
  }
  if (confidence) {
    lines.push(`   confidence: ${confidence}`);
  }
  return lines.join("\n");
}

function formatEvolutionSuggestions(value: unknown): string | null {
  const suggestions = Array.isArray(value)
    ? value
    : isRecord(value) && Array.isArray(value.evolution_suggestions)
      ? value.evolution_suggestions
      : null;

  if (!suggestions || suggestions.length === 0) {
    return null;
  }

  const rendered = suggestions
    .slice(0, 8)
    .map(formatEvolutionSuggestion)
    .filter((item): item is string => item !== null);

  if (rendered.length === 0) {
    return null;
  }

  const suffix =
    suggestions.length > rendered.length
      ? `\n... ${suggestions.length - rendered.length} more suggestion(s)`
      : "";
  return `Evolution suggestions:\n${rendered.join("\n")}${suffix}`;
}

function formatToolQuality(value: Record<string, unknown>): string | null {
  const toolKey = getString(value, ["tool_key", "toolKey"]);
  const successRate = getString(value, ["recent_success_rate", "success_rate"]);
  const totalCalls = getString(value, ["total_calls"]);
  const history = Array.isArray(value.history) ? value.history : [];
  if (!toolKey && successRate === null && history.length === 0) {
    return null;
  }

  const lines = [
    `Tool quality: ${toolKey ?? "unknown tool"}${
      successRate ? ` success=${successRate}` : ""
    }${totalCalls ? ` calls=${totalCalls}` : ""}`,
  ];

  for (const item of history.slice(0, 3)) {
    if (!isRecord(item)) {
      continue;
    }
    const status =
      item.success === true
        ? "success"
        : item.success === false
          ? "failed"
          : getString(item, ["status"]) ?? "recorded";
    const error = getString(item, ["error_message", "error"]);
    lines.push(
      `- ${status}${error ? `: ${truncateToDisplayWidth(error, 140)}` : ""}`,
    );
  }

  return lines.join("\n");
}

function formatListSummary(
  label: string,
  value: unknown,
  width = 160,
): string | null {
  if (!Array.isArray(value) || value.length === 0) {
    return null;
  }

  const rendered = value
    .slice(0, 6)
    .map(item => `- ${compact(item, width)}`)
    .filter(line => line.trim().length > 2);
  if (rendered.length === 0) {
    return null;
  }

  const suffix =
    value.length > rendered.length
      ? `\n... ${value.length - rendered.length} more`
      : "";
  return `${label}:\n${rendered.join("\n")}${suffix}`;
}

function formatTaskSummary(value: Record<string, unknown>): string | null {
  const hasTaskSummaryShape =
    "task_completed" in value ||
    "task_complete" in value ||
    "execution_note" in value ||
    "tool_issues" in value ||
    "skill_judgments" in value ||
    "skill_phase_failed_skill_ids" in value ||
    "evolution_suggestions" in value;
  if (!hasTaskSummaryShape) {
    return null;
  }

  const completed = value.task_completed ?? value.task_complete;
  const title =
    completed === true
      ? "Task complete"
      : completed === false
        ? "Task incomplete"
        : "Task summary";
  const lines = [title];
  const note = getString(value, [
    "execution_note",
    "summary",
    "message",
    "reason",
  ]);
  if (note) {
    lines.push(`  ${truncateToDisplayWidth(note, 220)}`);
  }

  const toolIssues = formatListSummary("Tool issues", value.tool_issues);
  if (toolIssues) {
    lines.push(toolIssues);
  }

  const skillJudgments = formatListSummary(
    "Skill judgments",
    value.skill_judgments,
  );
  if (skillJudgments) {
    lines.push(skillJudgments);
  }

  const failedSkills = formatListSummary(
    "Failed skill phases",
    value.skill_phase_failed_skill_ids,
  );
  if (failedSkills) {
    lines.push(failedSkills);
  }

  const evolution = formatEvolutionSuggestions(value.evolution_suggestions);
  if (evolution) {
    lines.push(evolution);
  }

  return lines.length > 1 || completed !== undefined
    ? lines.join("\n")
    : null;
}

function formatLogRecord(value: Record<string, unknown>): string {
  const level = getString(value, ["level", "severity", "status", "type"]);
  const message =
    getString(value, ["message", "summary", "text", "content", "error"]) ??
    compact(value, 180);
  const timestamp = getString(value, ["timestamp", "time", "created_at"]);
  return [
    timestamp ? `[${timestamp}]` : "",
    level ? level.toUpperCase() : "",
    truncateToDisplayWidth(message, 180),
  ]
    .filter(Boolean)
    .join(" ");
}

function formatLogRecords(value: unknown): string | null {
  const records = Array.isArray(value)
    ? value
    : isRecord(value) && Array.isArray(value.logs)
      ? value.logs
      : isRecord(value) && Array.isArray(value.records)
        ? value.records
        : null;

  if (!records || records.length === 0) {
    return null;
  }

  const rendered = records
    .slice(0, 12)
    .map(item => (isRecord(item) ? formatLogRecord(item) : compact(item, 180)))
    .filter(Boolean);
  if (rendered.length === 0) {
    return null;
  }

  const suffix =
    records.length > rendered.length
      ? `\n... ${records.length - rendered.length} more log record(s)`
      : "";
  return `Logs:\n${rendered.map(line => `- ${line}`).join("\n")}${suffix}`;
}

function formatGenericRecord(value: Record<string, unknown>): string | null {
  const direct = getString(value, [
    "message",
    "summary",
    "text",
    "content",
    "error",
    "reason",
    "status",
  ]);
  const title =
    getString(value, ["title", "name", "event", "event_type", "type"]) ??
    "Record";
  const lines = [direct ? `${title}: ${direct}` : title];

  for (const [key, entryValue] of Object.entries(value)) {
    if (
      [
        "message",
        "summary",
        "text",
        "content",
        "error",
        "reason",
        "status",
        "title",
        "name",
        "event",
        "event_type",
        "type",
      ].includes(key)
    ) {
      continue;
    }
    if (entryValue === undefined || entryValue === null) {
      continue;
    }
    lines.push(`- ${key}: ${compact(entryValue, 160)}`);
    if (lines.length >= 8) {
      break;
    }
  }

  return lines.join("\n");
}

export function formatStructuredValueForDisplay(
  value: unknown,
  options: FormatOptions = {},
): string | null {
  const allowGenericRecord = options.allowGenericRecord !== false;
  if (isRecord(value)) {
    const taskSummary = formatTaskSummary(value);
    if (taskSummary) {
      return taskSummary;
    }
  }

  const evolution = formatEvolutionSuggestions(value);
  if (evolution) {
    return evolution;
  }

  const logs = formatLogRecords(value);
  if (logs) {
    return logs;
  }

  if (isRecord(value)) {
    const toolQuality = formatToolQuality(value);
    if (toolQuality) {
      return toolQuality;
    }
    return allowGenericRecord ? formatGenericRecord(value) : null;
  }

  if (Array.isArray(value)) {
    if (!allowGenericRecord) {
      return null;
    }
    const rows = value
      .slice(0, 12)
      .map(item =>
        isRecord(item) ? formatGenericRecord(item) ?? compact(item) : compact(item),
      )
      .filter(Boolean);
    return rows.length > 0
      ? rows.map((row, index) => `${index + 1}. ${row}`).join("\n")
      : null;
  }

  return null;
}

export function formatStructuredTextForDisplay(
  text: string,
  options: FormatOptions = {},
): string | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }

  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      return formatStructuredValueForDisplay(JSON.parse(trimmed), options);
    } catch {
      // May be JSONL where the whole block starts and ends with braces.
    }
  }

  const lines = trimmed.split(/\r?\n/);
  if (lines.length > 1 && lines.every(line => line.trim().startsWith("{"))) {
    const records: unknown[] = [];
    for (const line of lines) {
      try {
        records.push(JSON.parse(line));
      } catch {
        return null;
      }
    }
    return (
      formatLogRecords(records) ??
      formatStructuredValueForDisplay(records, options)
    );
  }

  return null;
}
