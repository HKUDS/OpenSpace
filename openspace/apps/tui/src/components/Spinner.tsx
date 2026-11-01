import React from "react";
import { Text } from "ink";

const FRAMES = ["-", "\\", "|", "/"];

type SpinnerWithVerbProps = {
  active: boolean;
  message?: string;
  color?: string;
};

export function SpinnerWithVerb({
  active,
  message = "Query running",
  color = "yellow",
}: SpinnerWithVerbProps): React.ReactElement | null {
  const [index, setIndex] = React.useState(0);

  React.useEffect(() => {
    if (!active) {
      setIndex(0);
      return;
    }

    const timer = setInterval(() => {
      setIndex(current => (current + 1) % FRAMES.length);
    }, 90);

    return () => {
      clearInterval(timer);
    };
  }, [active]);

  if (!active) {
    return null;
  }

  return (
    <Text color={color as never}>
      {FRAMES[index]} {message}
    </Text>
  );
}
