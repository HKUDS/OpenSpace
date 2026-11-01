import React from "react";
import { Box } from "ink";
import { getColor } from "./theme.js";

type Props = {
  children: React.ReactNode;
};

export function Pane({
  children,
}: Props): React.ReactElement {
  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={getColor("border")}
      paddingX={1}
    >
      {children}
    </Box>
  );
}
