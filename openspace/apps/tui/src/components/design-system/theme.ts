export type ColorToken =
  | "primary"
  | "secondary"
  | "accent"
  | "success"
  | "warning"
  | "error"
  | "muted"
  | "text"
  | "textDim"
  | "textInverse"
  | "border"
  | "borderFocused"
  | "borderDim"
  | "bg"
  | "bgHighlight"
  | "bgOverlay"
  | "userMessage"
  | "assistantMessage"
  | "toolMessage"
  | "systemMessage"
  | "statusMessage"
  | "errorMessage"
  | "spinner"
  | "permissionBorder"
  | "elicitationBorder"
  | "inputBorder"
  | "inputBorderFocused"
  | "inputBorderDisabled";

export type ThemeColors = Record<ColorToken, string>;

export type ThemeSpacing = {
  none: 0;
  xs: 1;
  sm: 1;
  md: 2;
  lg: 3;
  xl: 4;
};

export type Theme = {
  name: string;
  colors: ThemeColors;
  spacing: ThemeSpacing;
};

const DARK_COLORS: ThemeColors = {
  primary: "cyan",
  secondary: "blue",
  accent: "magenta",
  success: "green",
  warning: "yellow",
  error: "red",
  muted: "gray",
  text: "white",
  textDim: "gray",
  textInverse: "black",
  border: "gray",
  borderFocused: "cyan",
  borderDim: "gray",
  bg: "",
  bgHighlight: "gray",
  bgOverlay: "",
  userMessage: "cyan",
  assistantMessage: "green",
  toolMessage: "magenta",
  systemMessage: "gray",
  statusMessage: "blue",
  errorMessage: "red",
  spinner: "yellow",
  permissionBorder: "yellow",
  elicitationBorder: "magenta",
  inputBorder: "green",
  inputBorderFocused: "green",
  inputBorderDisabled: "gray",
};

const LIGHT_COLORS: ThemeColors = {
  primary: "blue",
  secondary: "cyan",
  accent: "magenta",
  success: "green",
  warning: "yellow",
  error: "red",
  muted: "gray",
  text: "black",
  textDim: "gray",
  textInverse: "white",
  border: "gray",
  borderFocused: "blue",
  borderDim: "gray",
  bg: "",
  bgHighlight: "gray",
  bgOverlay: "",
  userMessage: "blue",
  assistantMessage: "green",
  toolMessage: "magenta",
  systemMessage: "gray",
  statusMessage: "cyan",
  errorMessage: "red",
  spinner: "yellow",
  permissionBorder: "yellow",
  elicitationBorder: "magenta",
  inputBorder: "green",
  inputBorderFocused: "blue",
  inputBorderDisabled: "gray",
};

const SPACING: ThemeSpacing = {
  none: 0,
  xs: 1,
  sm: 1,
  md: 2,
  lg: 3,
  xl: 4,
};

export const THEMES: Record<string, Theme> = {
  dark: { name: "dark", colors: DARK_COLORS, spacing: SPACING },
  light: { name: "light", colors: LIGHT_COLORS, spacing: SPACING },
};

let activeTheme: Theme = THEMES.dark!;

export function getTheme(): Theme {
  return activeTheme;
}

export function setTheme(name: string): void {
  const theme = THEMES[name];
  if (theme) {
    activeTheme = theme;
  }
}

export function getColor(token: ColorToken): string {
  return activeTheme.colors[token];
}

export function getSpacing(size: keyof ThemeSpacing): number {
  return activeTheme.spacing[size];
}
