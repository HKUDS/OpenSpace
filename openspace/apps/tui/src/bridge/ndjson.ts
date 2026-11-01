// JSON.stringify emits U+2028/U+2029 raw (valid per ECMA-404). When the
// output is a single NDJSON line, any receiver that uses JavaScript
// line-terminator semantics (ECMA-262 §11.3 — \n \r U+2028 U+2029) to
// split the stream will cut the JSON mid-string. The \uXXXX form is
// equivalent JSON but can never be mistaken for a line terminator.

const JS_LINE_TERMINATORS = /\u2028|\u2029/g;

function escapeJsLineTerminators(json: string): string {
  return json.replace(
    JS_LINE_TERMINATORS,
    (c) => (c === "\u2028" ? "\\u2028" : "\\u2029"),
  );
}

/**
 * JSON.stringify for one-message-per-line transports. Escapes U+2028
 * LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR so the serialized output
 * cannot be broken by a line-splitting receiver.
 */
export function ndjsonSafeStringify(value: unknown): string {
  return escapeJsLineTerminators(JSON.stringify(value));
}

/**
 * Parse a single NDJSON line. Returns undefined on empty/whitespace-only
 * lines instead of throwing.
 */
export function ndjsonParse<T = unknown>(line: string): T | undefined {
  const trimmed = line.trim();
  if (!trimmed) return undefined;
  return JSON.parse(trimmed) as T;
}
