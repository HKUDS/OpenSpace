import React from "react";
import { Box, type DOMElement } from "ink";
import { getColor, getSpacing, type ColorToken, type ThemeSpacing } from "./theme.js";

type InkBoxProps = React.ComponentProps<typeof Box>;

type ThemedBoxProps = Omit<InkBoxProps, "borderColor" | "paddingX" | "paddingY" | "marginTop" | "marginBottom"> & {
  borderToken?: ColorToken;
  paddingSize?: keyof ThemeSpacing;
  marginTopSize?: keyof ThemeSpacing;
  marginBottomSize?: keyof ThemeSpacing;
  ref?: React.Ref<DOMElement>;
};

export function ThemedBox({
  borderToken,
  paddingSize,
  marginTopSize,
  marginBottomSize,
  children,
  ...rest
}: ThemedBoxProps): React.ReactElement {
  const props: InkBoxProps = {
    ...rest,
    ...(borderToken ? { borderColor: getColor(borderToken) } : {}),
    ...(paddingSize ? { paddingX: getSpacing(paddingSize) } : {}),
    ...(marginTopSize ? { marginTop: getSpacing(marginTopSize) } : {}),
    ...(marginBottomSize ? { marginBottom: getSpacing(marginBottomSize) } : {}),
  };

  return <Box {...props}>{children}</Box>;
}
