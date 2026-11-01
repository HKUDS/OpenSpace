const ANSI_PATTERN =
  // eslint-disable-next-line no-control-regex
  /[\u001B\u009B][[\]()#;?]*(?:(?:(?:[a-zA-Z\d]*(?:;[a-zA-Z\d]*)*)?\u0007)|(?:(?:\d{1,4}(?:;\d{0,4})*)?[\dA-PR-TZcf-nq-uy=><~]))/g;

export function stripAnsi(value: string): string {
  return value.replace(ANSI_PATTERN, "");
}

function isCombiningCodePoint(codePoint: number): boolean {
  return (
    (codePoint >= 0x0300 && codePoint <= 0x036f) ||
    (codePoint >= 0x1ab0 && codePoint <= 0x1aff) ||
    (codePoint >= 0x1dc0 && codePoint <= 0x1dff) ||
    (codePoint >= 0x20d0 && codePoint <= 0x20ff) ||
    (codePoint >= 0xfe20 && codePoint <= 0xfe2f)
  );
}

function isFullWidthCodePoint(codePoint: number): boolean {
  return (
    codePoint >= 0x1100 &&
    (
      codePoint <= 0x115f ||
      codePoint === 0x2329 ||
      codePoint === 0x232a ||
      (codePoint >= 0x2e80 && codePoint <= 0xa4cf && codePoint !== 0x303f) ||
      (codePoint >= 0xac00 && codePoint <= 0xd7a3) ||
      (codePoint >= 0xf900 && codePoint <= 0xfaff) ||
      (codePoint >= 0xfe10 && codePoint <= 0xfe19) ||
      (codePoint >= 0xfe30 && codePoint <= 0xfe6f) ||
      (codePoint >= 0xff00 && codePoint <= 0xff60) ||
      (codePoint >= 0xffe0 && codePoint <= 0xffe6) ||
      (codePoint >= 0x1f300 && codePoint <= 0x1faff) ||
      (codePoint >= 0x20000 && codePoint <= 0x3fffd)
    )
  );
}

export function charDisplayWidth(char: string): number {
  const codePoint = char.codePointAt(0);
  if (codePoint === undefined) {
    return 0;
  }

  if (codePoint === 0 || codePoint < 32 || (codePoint >= 0x7f && codePoint < 0xa0)) {
    return 0;
  }

  if (isCombiningCodePoint(codePoint)) {
    return 0;
  }

  return isFullWidthCodePoint(codePoint) ? 2 : 1;
}

export function stringDisplayWidth(value: string): number {
  return Array.from(stripAnsi(value)).reduce(
    (width, char) => width + charDisplayWidth(char),
    0,
  );
}

export function estimateWrappedRows(value: string, columns: number): number {
  const safeColumns = Math.max(1, columns);
  const lines = stripAnsi(value).split("\n");

  return lines.reduce((rows, line) => {
    const width = stringDisplayWidth(line);
    return rows + Math.max(1, Math.ceil(width / safeColumns));
  }, 0);
}

export function truncateToDisplayWidth(value: string, columns: number): string {
  const safeColumns = Math.max(0, columns);
  if (stringDisplayWidth(value) <= safeColumns) {
    return value;
  }
  if (safeColumns <= 1) {
    return "…";
  }

  let width = 0;
  let output = "";
  for (const char of Array.from(value)) {
    const nextWidth = charDisplayWidth(char);
    if (width + nextWidth > safeColumns - 1) {
      break;
    }
    output += char;
    width += nextWidth;
  }

  return `${output}…`;
}
