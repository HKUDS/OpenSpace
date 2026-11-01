import React from "react";
import { Text } from "ink";

type StreamingTextProps = {
  text: string;
  streaming: boolean;
  color?: string;
};

const CURSOR_FRAMES = ["▌", " "];

export function StreamingText({
  text,
  streaming,
  color,
}: StreamingTextProps): React.ReactElement {
  return (
    <Text color={color}>
      {text}
      {streaming ? CURSOR_FRAMES[0] : ""}
    </Text>
  );
}
