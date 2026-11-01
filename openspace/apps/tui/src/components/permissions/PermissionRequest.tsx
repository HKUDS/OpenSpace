import React from "react";
import { Box, Text, type Key } from "ink";
import {
  buildAskUserQuestionAllowResponse,
  buildAskUserQuestionDenyResponse,
  getAskUserQuestionQuestions,
  isAskUserQuestionRequest,
} from "../../bridge/askUserQuestionState.js";
import type {
  AskUserQuestionData,
  AskUserQuestionOptionData,
  PermissionRequestData,
  ToolPermissionResponseData,
  ToolPermissionOptionData,
} from "../../bridge/protocol.js";
import {
  getAllowAlwaysLabel,
  getPermissionRequestSummary,
  isSandboxPermissionRequest,
  isWorkerPermissionRequest,
} from "../../bridge/permissionRequestState.js";
import { useQueuedMessage } from "../../context/QueuedMessageContext.js";
import { useRegisterOverlay } from "../../context/overlayContext.js";
import type { PermissionResolution } from "../../hooks/toolPermission/PermissionContext.js";
import {
  useKeybindingInput,
  useKeybindings,
} from "../../keybindings/useKeybinding.js";
import { useRegisterKeybindingContext } from "../../keybindings/KeybindingContext.js";
import { useShortcutDisplay } from "../../keybindings/useShortcutDisplay.js";
import { stringifyUnknown, truncate } from "../../screens/shared.js";
import {
  isBackspaceInput,
  isDeleteInput,
} from "../../utils/keyInput.js";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import { truncateToDisplayWidth } from "../../utils/textWidth.js";

type Props = {
  request: PermissionRequestData;
  queueLength?: number;
  onResolve?: (resolution: PermissionResolution) => void;
};

function riskColor(riskLevel: string | undefined): string {
  switch (riskLevel) {
    case "high":
      return "red";
    case "low":
      return "green";
    case "medium":
    default:
      return "yellow";
  }
}

function hasAllowAlwaysOption(request: PermissionRequestData): boolean {
  return (
    request.response_channel !== "tool_permission_response" ||
    request.options?.some(option => option.option_id === "allow_always") === true
  );
}

function getCommandInput(request: PermissionRequestData): string | null {
  const command = request.tool_input.command;
  if (typeof command === "string" && command.trim()) {
    return command.trim();
  }
  return null;
}

function formatToolPermissionOptions(
  options: ToolPermissionOptionData[] | undefined,
): string | null {
  if (!options || options.length === 0) {
    return null;
  }
  return options.map((option, index) => `${index + 1}. ${option.label}`).join(" | ");
}

function getToolPermissionOptions(
  request: PermissionRequestData,
): ToolPermissionOptionData[] {
  if (request.response_channel !== "tool_permission_response") {
    return [];
  }

  if (request.options && request.options.length > 0) {
    return request.options;
  }

  return [
    {
      option_id: "allow_once",
      label: "Allow once",
    },
    {
      option_id: "deny",
      label: "Deny",
    },
  ];
}

function buildToolPermissionResponse(
  request: PermissionRequestData,
  option: ToolPermissionOptionData,
): ToolPermissionResponseData {
  return {
    tool_use_id: request.tool_use_id,
    option_id: option.option_id,
    suggestion_index: option.suggestion_index ?? null,
  };
}

function isProvideInputOption(
  option: ToolPermissionOptionData | undefined,
): boolean {
  if (!option) {
    return false;
  }
  return (
    option.option_id === "provide_input" ||
    /edit|edited|input|retry/i.test(option.label)
  );
}

function parseEditedToolInput(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (
      parsed &&
      typeof parsed === "object" &&
      !Array.isArray(parsed)
    ) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }
  return null;
}

function formatEditableToolInput(value: unknown): string {
  return JSON.stringify(value ?? {});
}

const DELETE_SEQUENCES = [
  "\u001b[3~",
  "\u001b[3;2~",
  "\u001b[3;3~",
  "\u001b[3;5~",
];

function isBackspaceChar(char: string): boolean {
  return char === "\u0008" || char === "\u007f";
}

function isPrintableInputChar(char: string): boolean {
  return char >= " " && char !== "\u007f";
}

function getDeleteSequenceAt(input: string, index: number): string | null {
  for (const sequence of DELETE_SEQUENCES) {
    if (input.startsWith(sequence, index)) {
      return sequence;
    }
  }

  return null;
}

function applyEditedInputChunk(
  current: string,
  input: string,
): { value: string; shouldSubmit: boolean } {
  let nextValue = current;
  let index = 0;

  while (index < input.length) {
    const deleteSequence = getDeleteSequenceAt(input, index);
    if (deleteSequence !== null) {
      nextValue = nextValue.slice(0, -1);
      index += deleteSequence.length;
      continue;
    }

    const char = Array.from(input.slice(index))[0] ?? "";
    index += char.length;

    if (char === "\r" || char === "\n") {
      return { value: nextValue, shouldSubmit: true };
    }

    if (isBackspaceChar(char)) {
      nextValue = nextValue.slice(0, -1);
      continue;
    }

    if (isPrintableInputChar(char)) {
      nextValue += char;
    }
  }

  return { value: nextValue, shouldSubmit: false };
}

function applyEditedInputKey(
  current: string,
  input: string,
  key: Key,
): { value: string; shouldSubmit: boolean } {
  if (key.return) {
    return { value: current, shouldSubmit: true };
  }

  if (isBackspaceInput(input, key) || isDeleteInput(input, key)) {
    return { value: current.slice(0, -1), shouldSubmit: false };
  }

  return applyEditedInputChunk(current, input);
}

function getQuestionKey(question: AskUserQuestionData): string {
  return question.question;
}

const OTHER_OPTION_LABEL = "__other__";

function buildSelectableOptions(
  question: AskUserQuestionData,
): AskUserQuestionOptionData[] {
  const hasOther = question.options.some(
    option => option.label === OTHER_OPTION_LABEL,
  );
  return hasOther
    ? question.options
    : [
        ...question.options,
        {
          label: OTHER_OPTION_LABEL,
          description: "Custom answer",
        },
      ];
}

function optionLabelForDisplay(option: AskUserQuestionOptionData): string {
  return option.label === OTHER_OPTION_LABEL ? "Other" : option.label;
}

function answerFromState(
  question: AskUserQuestionData,
  selected: Record<string, string[]>,
  customInput: Record<string, string>,
): string {
  const key = getQuestionKey(question);
  const labels = selected[key] ?? [];
  const custom = customInput[key]?.trim();
  const regularLabels = labels.filter(label => label !== OTHER_OPTION_LABEL);

  if (labels.includes(OTHER_OPTION_LABEL)) {
    return custom ? [...regularLabels, custom].join(", ") : regularLabels.join(", ");
  }

  return regularLabels.join(", ");
}

function buildAnswersFromState(
  questions: AskUserQuestionData[],
  selected: Record<string, string[]>,
  customInput: Record<string, string>,
): Record<string, string> {
  return Object.fromEntries(
    questions
      .map(question => [question.question, answerFromState(question, selected, customInput)])
      .filter((entry): entry is [string, string] => entry[1].trim().length > 0),
  );
}

function questionIsAnswered(
  question: AskUserQuestionData,
  selected: Record<string, string[]>,
  customInput: Record<string, string>,
): boolean {
  return answerFromState(question, selected, customInput).trim().length > 0;
}

function oneLinePreview(value: unknown, max: number): string {
  return truncate(stringifyUnknown(value).replace(/\s+/g, " ").trim(), max);
}

function compactPermissionHelp(
  columns: number,
  text: string,
): string {
  return truncateToDisplayWidth(text, Math.max(20, columns - 6));
}

function GenericPermissionRequest({
  request,
  queueLength,
  onResolve,
}: Props): React.ReactElement {
  const { columns, rows } = useTerminalSize();
  const queuedMessage = useQueuedMessage();
  const alwaysShortcut = useShortcutDisplay(
    "permission:allowAlways",
    "Confirmation",
    "a",
  );
  const borderColor = riskColor(request.risk_level);
  const heading = isSandboxPermissionRequest(request)
    ? request.request_kind === "network"
      ? "Network Permission Request"
      : "Sandbox Permission Request"
    : request.tool_name.toLowerCase() === "bash"
      ? "Bash Command Permission"
      : "Permission Request";
  const workerOrigin = isWorkerPermissionRequest(request);
  const workerName = request.agent_name ?? request.agent_id;
  const originLabel = workerOrigin
    ? `Worker ${workerName ?? "worker"}`
    : "Primary session";
  const summary = request.message ?? getPermissionRequestSummary(request);
  const allowAlwaysLabel = getAllowAlwaysLabel(request);
  const command = getCommandInput(request);
  const host = request.host;
  const optionLabels = formatToolPermissionOptions(request.options);
  const contentWidth = Math.max(20, columns - 6);
  const compactLayout = rows < 22;
  const crampedLayout = rows < 18;
  const showOptionsSummary = optionLabels !== null && columns >= 80;
  const showAllowAlways = hasAllowAlwaysOption(request);
  const highRisk = request.risk_level === "high";
  const toolOptions = React.useMemo(
    () => getToolPermissionOptions(request),
    [request],
  );
  const isToolPermission =
    request.response_channel === "tool_permission_response";
  const [selectedOptionIndex, setSelectedOptionIndex] = React.useState(0);
  const [editingInput, setEditingInput] = React.useState(false);
  const [editedInput, setEditedInput] = React.useState(() =>
    formatEditableToolInput(request.tool_input),
  );
  const [error, setError] = React.useState<string | null>(null);
  const selectedOptionIndexRef = React.useRef(selectedOptionIndex);
  const editingInputRef = React.useRef(editingInput);
  const editedInputRef = React.useRef(editedInput);
  const editedInputTouchedRef = React.useRef(false);
  const toolOptionsRef = React.useRef(toolOptions);
  const selectedToolOption = toolOptions[selectedOptionIndex];
  const selectedOptionLabel =
    typeof selectedToolOption?.label === "string"
      ? selectedToolOption.label
      : "";
  const selectedOptionRequestsInput =
    selectedToolOption?.option_id === "provide_input" ||
    /edit|edited|input|retry/i.test(selectedOptionLabel);
  const effectiveEditingInput =
    editingInput || selectedOptionRequestsInput;
  editingInputRef.current = effectiveEditingInput;
  useRegisterKeybindingContext("PermissionEdit", effectiveEditingInput);
  const toolHelp = columns < 72
    ? `Enter/y select | n/Esc ${effectiveEditingInput ? "back" : "deny"} | 1-${toolOptions.length} | e edit${showAllowAlways ? ` | ${alwaysShortcut} always` : ""}`
    : `Enter/y select | n/Esc ${effectiveEditingInput ? "back" : "deny"} | up/down choose | 1-${toolOptions.length} select | e edit${showAllowAlways ? ` | ${alwaysShortcut} ${allowAlwaysLabel}` : ""}`;
  const genericHelp = columns < 72
    ? `y/Enter allow | n/Esc deny${showAllowAlways ? ` | ${alwaysShortcut} always` : ""}`
    : `y/Enter allow | n/Esc deny${showAllowAlways ? ` | ${alwaysShortcut} ${allowAlwaysLabel}` : ""}`;
  const maxVisibleOptions = crampedLayout ? 3 : compactLayout ? 4 : toolOptions.length;
  const optionWindowEnd = Math.min(
    toolOptions.length,
    Math.max(maxVisibleOptions, selectedOptionIndex + 1),
  );
  const optionWindowStart = Math.max(0, optionWindowEnd - maxVisibleOptions);
  const visibleToolOptions = toolOptions
    .slice(optionWindowStart, optionWindowEnd)
    .map((option, offset) => ({
      option,
      index: optionWindowStart + offset,
    }));
  const showOrigin = !compactLayout || workerOrigin || Boolean(host);
  const showRiskInstruction = highRisk && !compactLayout;
  const showInputPreview = effectiveEditingInput || !compactLayout;
  const showSummary = !crampedLayout || !command;

  const setSelectedToolOptionIndex = React.useCallback((index: number): void => {
    selectedOptionIndexRef.current = index;
    setSelectedOptionIndex(index);
  }, []);

  const setEditingInputState = React.useCallback((value: boolean): void => {
    editingInputRef.current = value;
    setEditingInput(value);
  }, []);

  const setEditedInputState = React.useCallback(
    (value: string | ((current: string) => string)): void => {
      const nextValue =
        typeof value === "function" ? value(editedInputRef.current) : value;
      editedInputRef.current = nextValue;
      setEditedInput(nextValue);
    },
    [],
  );

  React.useEffect(() => {
    setSelectedToolOptionIndex(0);
    setEditingInputState(false);
    setEditedInputState(formatEditableToolInput(request.tool_input));
    editedInputTouchedRef.current = false;
    setError(null);
  }, [
    request.tool_use_id,
    setEditingInputState,
    setEditedInputState,
    setSelectedToolOptionIndex,
  ]);

  React.useEffect(() => {
    toolOptionsRef.current = toolOptions;
  }, [toolOptions]);

  React.useEffect(() => {
    if (isToolPermission && selectedOptionRequestsInput && !editingInput) {
      setEditingInputState(true);
    }
  }, [
    editingInput,
    isToolPermission,
    selectedOptionRequestsInput,
    setEditingInputState,
  ]);

  const resolveToolOption = React.useCallback(
    (option: ToolPermissionOptionData | undefined): void => {
      if (!option) {
        return;
      }

      if (isProvideInputOption(option)) {
        editedInputTouchedRef.current = false;
        setEditingInputState(true);
        setError(null);
        return;
      }

      onResolve?.(buildToolPermissionResponse(request, option));
    },
    [onResolve, request, setEditingInputState],
  );
  const selectToolOptionByIndex = React.useCallback(
    (index: number): void | false => {
      const options = toolOptionsRef.current;
      if (!isToolPermission || editingInputRef.current) {
        return false;
      }
      if (index < 0 || index >= options.length) {
        return false;
      }
      const option = options[index];
      setSelectedToolOptionIndex(index);
      if (isProvideInputOption(option)) {
        editedInputTouchedRef.current = false;
        setEditingInputState(true);
        setError(null);
        return;
      }
      resolveToolOption(option);
    },
    [isToolPermission, resolveToolOption, setSelectedToolOptionIndex],
  );

  const enterEditedInputMode = React.useCallback((): void | false => {
    if (!isToolPermission || editingInputRef.current) {
      return false;
    }

    const editIndex = toolOptionsRef.current.findIndex(
      option => isProvideInputOption(option),
    );
    if (editIndex < 0) {
      return false;
    }

    setSelectedToolOptionIndex(editIndex);
    editedInputTouchedRef.current = false;
    setEditingInputState(true);
    setError(null);
  }, [isToolPermission, setEditingInputState, setSelectedToolOptionIndex]);

  const submitEditedInput = React.useCallback((value = editedInputRef.current): void => {
    const parsed = parseEditedToolInput(value);
    if (!parsed) {
      setError("Edited input must be a JSON object.");
      return;
    }

    onResolve?.({
      tool_use_id: request.tool_use_id,
      option_id: "provide_input",
      edited_input: parsed,
    });
  }, [onResolve, request.tool_use_id]);

  const applyEditedInputInteraction = React.useCallback(
    (value: string, key: Key): void => {
      if (!value || key.ctrl || key.meta) {
        return;
      }

      const deletion = isBackspaceInput(value, key) || isDeleteInput(value, key);
      const base =
        !editedInputTouchedRef.current && !deletion
          ? ""
          : editedInputRef.current;
      const next = applyEditedInputKey(base, value, key);
      editedInputTouchedRef.current = true;
      if (next.value !== editedInputRef.current) {
        setEditedInputState(next.value);
      }
      setError(null);
      if (next.shouldSubmit) {
        submitEditedInput(next.value);
      }
    },
    [setEditedInputState, submitEditedInput],
  );

  useKeybindings(
    {
      "confirm:yes": () => {
        if (!isToolPermission) {
          onResolve?.("allow");
          return;
        }
        if (editingInputRef.current) {
          submitEditedInput();
          return;
        }
        resolveToolOption(toolOptionsRef.current[selectedOptionIndexRef.current]);
      },
      "confirm:no": () => {
        if (!isToolPermission) {
          onResolve?.("deny");
          return;
        }
        if (editingInputRef.current) {
          setSelectedToolOptionIndex(0);
          setEditingInputState(false);
          setError(null);
          return;
        }
        onResolve?.({
          tool_use_id: request.tool_use_id,
          option_id: "deny",
          message: "Denied by user.",
        });
      },
      "confirm:next": () => {
        const options = toolOptionsRef.current;
        if (editingInputRef.current || options.length === 0) {
          return;
        }
        setSelectedToolOptionIndex(
          (selectedOptionIndexRef.current + 1) % options.length,
        );
      },
      "confirm:previous": () => {
        const options = toolOptionsRef.current;
        if (editingInputRef.current || options.length === 0) {
          return;
        }
        setSelectedToolOptionIndex(
          (selectedOptionIndexRef.current - 1 + options.length) % options.length,
        );
      },
      "confirm:nextField": () => {
        if (!isToolPermission) {
          return false;
        }
        const editIndex = toolOptionsRef.current.findIndex(isProvideInputOption);
        if (editIndex >= 0) {
          setSelectedToolOptionIndex(editIndex);
        }
      },
      "confirm:previousField": () => {
        if (!isToolPermission) {
          return false;
        }
        setSelectedToolOptionIndex(0);
        setEditingInputState(false);
        setError(null);
      },
      "permission:allowAlways": () => {
        if (!isToolPermission) {
          if (showAllowAlways) {
            onResolve?.("allow_always");
          }
          return;
        }
        const selectedOption = toolOptionsRef.current[selectedOptionIndexRef.current];
        const allowAlwaysIndex = selectedOption?.option_id === "allow_always"
          ? selectedOptionIndexRef.current
          : toolOptionsRef.current.findIndex(
          option => option.option_id === "allow_always",
        );
        if (allowAlwaysIndex < 0) {
          return false;
        }
        resolveToolOption(toolOptionsRef.current[allowAlwaysIndex]);
      },
      "permission:editInput": enterEditedInputMode,
      "confirm:digit1": () => selectToolOptionByIndex(0),
      "confirm:digit2": () => selectToolOptionByIndex(1),
      "confirm:digit3": () => selectToolOptionByIndex(2),
      "confirm:digit4": () => selectToolOptionByIndex(3),
      "confirm:digit5": () => selectToolOptionByIndex(4),
      "confirm:digit6": () => selectToolOptionByIndex(5),
      "confirm:digit7": () => selectToolOptionByIndex(6),
      "confirm:digit8": () => selectToolOptionByIndex(7),
      "confirm:digit9": () => selectToolOptionByIndex(8),
    },
    { context: effectiveEditingInput ? "PermissionEdit" : "Confirmation" },
  );

  useKeybindingInput(
    (value, key) => {
      const selectedOption =
        toolOptionsRef.current[selectedOptionIndexRef.current];
      if (editingInputRef.current || isProvideInputOption(selectedOption)) {
        if (!editingInputRef.current) {
          editedInputTouchedRef.current = false;
          setEditingInputState(true);
        }
        applyEditedInputInteraction(value, key);
        return;
      }

      if (!isToolPermission) {
        return;
      }

      const normalized = value.toLowerCase();
      if (normalized === "e") {
        enterEditedInputMode();
      }
    },
    { context: effectiveEditingInput ? "PermissionEdit" : "Confirmation" },
  );

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={borderColor}
      paddingX={1}
      marginTop={compactLayout ? 0 : 1}
      width="100%"
      overflowX="hidden"
    >
      <Text bold color={borderColor as never} wrap="truncate">
        {truncateToDisplayWidth(
          queuedMessage?.isQueued && !queuedMessage.isFirst
            ? `Queued ${heading}`
            : heading,
          contentWidth,
        )}
      </Text>
      {queueLength !== undefined && queueLength > 1 ? (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(
            `${queueLength} pending approval request(s)`,
            contentWidth,
          )}
        </Text>
      ) : null}
      <Text wrap="truncate">
        {truncateToDisplayWidth(
          `Tool: ${request.tool_name} | Risk: ${request.risk_level ?? "medium"}`,
          contentWidth,
        )}
      </Text>
      {showOrigin ? (
        <Text wrap="truncate">
          {truncateToDisplayWidth(`Origin: ${originLabel}`, contentWidth)}
        </Text>
      ) : null}
      {host ? (
        <Text wrap="truncate">
          {truncateToDisplayWidth(`Host: ${host}`, contentWidth)}
        </Text>
      ) : null}
      {request.blocked_path ? (
        <Text color={highRisk ? "red" : "yellow"} wrap="truncate">
          {truncateToDisplayWidth(
            `Blocked path: ${request.blocked_path}`,
            contentWidth,
          )}
        </Text>
      ) : null}
      {command ? (
        <Text color={highRisk ? "red" : undefined} wrap="truncate">
          {truncateToDisplayWidth(`Command: ${command}`, contentWidth)}
        </Text>
      ) : null}
      {showRiskInstruction ? (
        <Text color="red">High-risk command requires explicit approval.</Text>
      ) : null}
      {showSummary ? (
        <Text wrap="truncate">
          {truncateToDisplayWidth(summary, contentWidth)}
        </Text>
      ) : null}
      {request.decision_reason ? (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(
            `Reason: ${oneLinePreview(request.decision_reason, contentWidth)}`,
            contentWidth,
          )}
        </Text>
      ) : null}
      {showOptionsSummary ? (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(`Options: ${optionLabels}`, contentWidth)}
        </Text>
      ) : null}
      {isToolPermission ? (
        <Box flexDirection="column" marginTop={compactLayout ? 0 : 1}>
          {visibleToolOptions.map(({ option, index }) => (
            <Text
              key={`${option.option_id}-${option.suggestion_index ?? index}`}
              color={index === selectedOptionIndex ? "cyan" : "gray"}
              wrap="truncate"
            >
              {truncateToDisplayWidth(
                `${index === selectedOptionIndex ? ">" : " "} ${index + 1}. ${option.label}`,
                contentWidth,
              )}
            </Text>
          ))}
        </Box>
      ) : null}
      {effectiveEditingInput ? (
        <Box flexDirection="column" marginTop={1}>
          <Text color="yellow">Edited input JSON:</Text>
          <Text wrap="truncate">
            {truncateToDisplayWidth(
              editedInput.replace(/\s+/g, " "),
              contentWidth,
            )}
          </Text>
        </Box>
      ) : null}
      {error ? <Text color="red">{error}</Text> : null}
      {showInputPreview ? (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(
            `Input: ${oneLinePreview(
              request.tool_input,
              queuedMessage ? Math.max(120, 180 - queuedMessage.paddingWidth * 8) : 180,
            )}`,
            contentWidth,
          )}
        </Text>
      ) : null}
      {isToolPermission ? (
        <Text wrap="truncate">
          {compactPermissionHelp(columns, toolHelp)}
        </Text>
      ) : (
        <Text wrap="truncate">
          {compactPermissionHelp(columns, genericHelp)}
        </Text>
      )}
    </Box>
  );
}

function AskUserQuestionPermissionRequest({
  request,
  queueLength,
  onResolve,
}: Props): React.ReactElement {
  const { columns } = useTerminalSize();
  const contentWidth = Math.max(20, columns - 6);
  const questions = React.useMemo(
    () => getAskUserQuestionQuestions(request),
    [request],
  );
  const [questionIndex, setQuestionIndex] = React.useState(0);
  const [optionIndex, setOptionIndex] = React.useState(0);
  const [selected, setSelected] = React.useState<Record<string, string[]>>({});
  const [customInput, setCustomInput] = React.useState<Record<string, string>>({});
  const [notes, setNotes] = React.useState<Record<string, string>>({});
  const [inputMode, setInputMode] = React.useState<"options" | "other" | "notes">(
    "options",
  );
  const [error, setError] = React.useState<string | null>(null);
  const currentQuestion = questions[questionIndex];
  const options = currentQuestion ? buildSelectableOptions(currentQuestion) : [];
  const currentKey = currentQuestion ? getQuestionKey(currentQuestion) : "";
  const currentSelection = selected[currentKey] ?? [];
  const currentCustom = customInput[currentKey] ?? "";
  const currentNotes = notes[currentKey] ?? "";
  const focusedOption = options[optionIndex] ?? null;
  const readyAnswers = buildAnswersFromState(questions, selected, customInput);

  React.useEffect(() => {
    setQuestionIndex(0);
    setOptionIndex(0);
    setSelected({});
    setCustomInput({});
    setNotes({});
    setInputMode("options");
    setError(null);
  }, [request.tool_use_id]);

  const resolveAllow = React.useCallback(
    (answers: Record<string, string>) => {
      onResolve?.(buildAskUserQuestionAllowResponse(request, answers, notes));
    },
    [notes, onResolve, request],
  );

  const trySubmit = React.useCallback(
    (
      nextSelected = selected,
      nextCustomInput = customInput,
      targetIndex = questionIndex,
    ) => {
      const question = questions[targetIndex];
      if (!question) {
        onResolve?.(buildAskUserQuestionDenyResponse(request));
        return;
      }

      if (!questionIsAnswered(question, nextSelected, nextCustomInput)) {
        setError("Select an option or enter Other text.");
        return;
      }

      const nextAnswers = buildAnswersFromState(
        questions,
        nextSelected,
        nextCustomInput,
      );
      if (Object.keys(nextAnswers).length < questions.length) {
        const nextIndex = Math.min(targetIndex + 1, questions.length - 1);
        setQuestionIndex(nextIndex);
        setOptionIndex(0);
        setInputMode("options");
        setError(null);
        return;
      }

      resolveAllow(nextAnswers);
    },
    [customInput, onResolve, questionIndex, questions, request, resolveAllow, selected],
  );

  const selectOption = React.useCallback(
    (option: AskUserQuestionOptionData | null) => {
      if (!currentQuestion || !option) {
        return;
      }

      const key = getQuestionKey(currentQuestion);
      if (option.label === OTHER_OPTION_LABEL) {
        setSelected(previous => ({
          ...previous,
          [key]: currentQuestion.multiSelect
            ? Array.from(new Set([...(previous[key] ?? []), OTHER_OPTION_LABEL]))
            : [OTHER_OPTION_LABEL],
        }));
        setInputMode("other");
        setError(null);
        return;
      }

      if (currentQuestion.multiSelect) {
        setSelected(previous => {
          const existing = previous[key] ?? [];
          const next = existing.includes(option.label)
            ? existing.filter(label => label !== option.label)
            : [...existing, option.label];
          return {
            ...previous,
            [key]: next,
          };
        });
        setError(null);
        return;
      }

      const nextSelected = {
        ...selected,
        [key]: [option.label],
      };
      setSelected(nextSelected);
      trySubmit(nextSelected, customInput);
    },
    [currentQuestion, customInput, selected, trySubmit],
  );

  const handleConfirm = React.useCallback(() => {
    if (!currentQuestion) {
      onResolve?.(buildAskUserQuestionDenyResponse(request));
      return;
    }

    if (inputMode === "other" || inputMode === "notes") {
      setInputMode("options");
      setError(null);
      if (inputMode === "other" && !currentCustom.trim()) {
        setError("Enter text for Other before continuing.");
      }
      return;
    }

    if (currentQuestion.multiSelect) {
      trySubmit();
      return;
    }

    if (currentSelection.length === 0) {
      selectOption(focusedOption);
      return;
    }

    trySubmit();
  }, [
    currentCustom,
    currentQuestion,
    currentSelection.length,
    focusedOption,
    inputMode,
    onResolve,
    request,
    selectOption,
    trySubmit,
  ]);

  const handleDeny = React.useCallback(() => {
    onResolve?.(buildAskUserQuestionDenyResponse(request));
  }, [onResolve, request]);

  useKeybindings(
    {
      "confirm:yes": handleConfirm,
      "confirm:no": handleDeny,
      "confirm:next": () => {
        if (inputMode !== "options" || options.length === 0) {
          return;
        }
        setOptionIndex(current => (current + 1) % options.length);
      },
      "confirm:previous": () => {
        if (inputMode !== "options" || options.length === 0) {
          return;
        }
        setOptionIndex(current => (current - 1 + options.length) % options.length);
      },
      "confirm:nextField": () => {
        setInputMode(current => (current === "notes" ? "options" : "notes"));
      },
      "confirm:previousField": () => {
        setInputMode(current => (current === "options" ? "notes" : "options"));
      },
    },
    { context: "Confirmation" },
  );

  useKeybindingInput(
    (value, key) => {
      if (!currentQuestion) {
        return;
      }

      const updateText = (
        setter: React.Dispatch<React.SetStateAction<Record<string, string>>>,
      ) => {
        if (isBackspaceInput(value, key) || isDeleteInput(value, key)) {
          setter(previous => ({
            ...previous,
            [currentKey]: (previous[currentKey] ?? "").slice(0, -1),
          }));
          return;
        }
        if (key.return) {
          setInputMode("options");
          return;
        }
        if (key.escape) {
          setInputMode("options");
          return;
        }
        if (value) {
          setter(previous => ({
            ...previous,
            [currentKey]: `${previous[currentKey] ?? ""}${value}`,
          }));
        }
      };

      if (inputMode === "other") {
        updateText(setCustomInput);
        setError(null);
        return;
      }

      if (inputMode === "notes") {
        updateText(setNotes);
        setError(null);
        return;
      }

      const digit = Number.parseInt(value, 10);
      if (!Number.isNaN(digit) && digit >= 1 && digit <= options.length) {
        setOptionIndex(digit - 1);
        selectOption(options[digit - 1] ?? null);
        return;
      }

      if (value.toLowerCase() === "o") {
        selectOption(options.find(option => option.label === OTHER_OPTION_LABEL) ?? null);
        return;
      }

      if (value.toLowerCase() === "t") {
        setInputMode("notes");
      }
    },
    { context: "Confirmation" },
  );

  if (!currentQuestion) {
    return (
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor="yellow"
        paddingX={1}
        marginTop={1}
        width="100%"
        overflowX="hidden"
      >
        <Text bold color="yellow" wrap="truncate">
          {truncateToDisplayWidth("AskUserQuestion Permission", contentWidth)}
        </Text>
        <Text>No valid questions were provided.</Text>
        <Text>y deny | n deny</Text>
      </Box>
    );
  }

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="cyan"
      paddingX={1}
      marginTop={1}
      width="100%"
      overflowX="hidden"
    >
      <Text bold color="cyan" wrap="truncate">
        {truncateToDisplayWidth(
          `AskUserQuestion Permission${queueLength !== undefined && queueLength > 1
            ? ` (${queueLength} pending)`
            : ""}`,
          contentWidth,
        )}
      </Text>
      <Text wrap="truncate">
        {truncateToDisplayWidth(
          `${questionIndex + 1}/${questions.length}: ${currentQuestion.header ?? currentQuestion.question}`,
          contentWidth,
        )}
      </Text>
      {currentQuestion.header ? (
        <Text wrap="truncate">
          {truncateToDisplayWidth(currentQuestion.question, contentWidth)}
        </Text>
      ) : null}
      {options.map((option, index) => {
        const selectedMarker = currentSelection.includes(option.label) ? "x" : " ";
        const focusMarker = index === optionIndex ? ">" : " ";
        return (
          <Text key={`${currentQuestion.question}-${option.label}`} wrap="truncate">
            {truncateToDisplayWidth(
              `${focusMarker} [${selectedMarker}] ${index + 1}. ${optionLabelForDisplay(option)}${option.description ? ` - ${option.description}` : ""}`,
              contentWidth,
            )}
          </Text>
        );
      })}
      {focusedOption?.preview ? (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(`Preview: ${focusedOption.preview}`, contentWidth)}
        </Text>
      ) : null}
      {inputMode === "other" ? (
        <Text color="yellow" wrap="truncate">
          {truncateToDisplayWidth(`Other: ${currentCustom}`, contentWidth)}
        </Text>
      ) : (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(`Other: ${currentCustom || "(empty)"}`, contentWidth)}
        </Text>
      )}
      {inputMode === "notes" ? (
        <Text color="yellow" wrap="truncate">
          {truncateToDisplayWidth(`Notes: ${currentNotes}`, contentWidth)}
        </Text>
      ) : currentNotes ? (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(`Notes: ${currentNotes}`, contentWidth)}
        </Text>
      ) : null}
      {Object.keys(readyAnswers).length > 0 ? (
        <Text color="gray" wrap="truncate">
          {truncateToDisplayWidth(`Answers: ${stringifyUnknown(readyAnswers)}`, contentWidth)}
        </Text>
      ) : null}
      {error ? <Text color="red">{error}</Text> : null}
      <Text color="gray" wrap="truncate">
        {truncateToDisplayWidth(
          columns < 72
            ? `Enter/y | n | up/down | 1-${options.length} | o other | tab notes`
            : `Enter/y continue | n cancel | up/down choose | 1-${options.length} select | o other | tab notes`,
          contentWidth,
        )}
      </Text>
    </Box>
  );
}

export function PermissionRequest({
  request,
  queueLength,
  onResolve,
}: Props): React.ReactElement {
  useRegisterOverlay("permission-request");

  if (isAskUserQuestionRequest(request)) {
    return (
      <AskUserQuestionPermissionRequest
        request={request}
        queueLength={queueLength}
        onResolve={onResolve}
      />
    );
  }

  return (
    <GenericPermissionRequest
      request={request}
      queueLength={queueLength}
      onResolve={onResolve}
    />
  );
}
