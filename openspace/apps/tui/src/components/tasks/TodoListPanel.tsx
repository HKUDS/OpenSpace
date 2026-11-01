import React from "react";
import { Box, Text } from "ink";
import type { TodoItemState, TodoRuntimeState } from "../../state/AppStateStore.js";
import { getColor } from "../design-system/theme.js";

type Props = {
  state: TodoRuntimeState;
};

function statusTone(todo: TodoItemState): { mark: string; color: string; text: string } {
  switch (todo.status) {
    case "in_progress":
      return { mark: ">", color: getColor("spinner"), text: "in progress" };
    case "completed":
      return { mark: "x", color: getColor("success"), text: "completed" };
    case "pending":
    default:
      return { mark: " ", color: getColor("textDim"), text: "pending" };
  }
}

function todoLabel(todo: TodoItemState): string {
  if (todo.status === "in_progress" && todo.activeForm) {
    return todo.activeForm;
  }
  return todo.content;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 3))}...`;
}

export function TodoListPanel({ state }: Props): React.ReactElement | null {
  const todos = state.todos;
  if (todos.length === 0) {
    return null;
  }

  const pending = todos.filter(todo => todo.status === "pending").length;
  const inProgress = todos.filter(todo => todo.status === "in_progress").length;
  const completed = todos.filter(todo => todo.status === "completed").length;

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("border")}
      paddingX={1}
    >
      <Text bold color={getColor("primary")}>
        Todos ({completed}/{todos.length} done, {inProgress} active, {pending} pending)
      </Text>
      {todos.map((todo, index) => {
        const tone = statusTone(todo);
        return (
          <Box key={`${index}-${todo.content}`}>
            <Text color={tone.color as never}>[{tone.mark}] </Text>
            <Text>{truncate(todoLabel(todo), 100)}</Text>
            <Text color={getColor("textDim")}> {tone.text}</Text>
          </Box>
        );
      })}
    </Box>
  );
}
