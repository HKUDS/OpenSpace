export function formatPercent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatDate(value?: string | null): string {
  if (!value) {
    return '—';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function truncate(value: string, max = 120): string {
  if (value.length <= max) {
    return value;
  }
  return `${value.slice(0, max)}…`;
}

/**
 * Shorten absolute file paths in instruction text for display.
 *
 * "/Users/foo/bar/project/src/components/Panel.tsx"
 *   → "…/src/components/Panel.tsx"
 *
 * Keeps the last `keep` path segments so context is preserved.
 */
function shortenPaths(text: string, keep = 3): string {
  // Match absolute paths: /Xxx/Yyy/.../file or dir
  return text.replace(
    /\/(?:Users|home|tmp|var|opt)\/[^\s,;)}\]]+/g,
    (match) => {
      const parts = match.split('/').filter(Boolean);
      if (parts.length <= keep) return match;
      return '…/' + parts.slice(-keep).join('/');
    },
  );
}

/**
 * Format a raw instruction string for display.
 *
 *  1. Shortens long absolute file paths to last 3 segments
 *  2. Collapses excessive whitespace / newlines
 *  3. Trims to `maxLen` characters (with ellipsis)
 */
export function formatInstruction(
  raw: string | null | undefined,
  maxLen?: number,
  fallback = 'No instruction captured',
): string {
  if (!raw) return fallback;

  let text = shortenPaths(raw);

  // Collapse newlines → spaces, normalise whitespace
  text = text.replace(/\n+/g, ' ').replace(/\s{2,}/g, ' ').trim();

  if (maxLen && text.length > maxLen) {
    text = text.slice(0, maxLen) + '…';
  }

  return text;
}
