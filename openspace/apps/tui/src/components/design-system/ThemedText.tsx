import React from "react";
import { Text } from "ink";
import { getColor, type ColorToken } from "./theme.js";

type InkTextProps = React.ComponentProps<typeof Text>;

type ThemedTextProps = Omit<InkTextProps, "color"> & {
  colorToken?: ColorToken;
  color?: string;
};

export function ThemedText({
  colorToken,
  color,
  children,
  ...rest
}: ThemedTextProps): React.ReactElement {
  const resolvedColor = colorToken ? getColor(colorToken) : color;

  return (
    <Text color={resolvedColor} {...rest}>
      {children}
    </Text>
  );
}
