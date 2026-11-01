/**
 * Escapes a value for safe interpolation into an innerHTML string.
 * Converts &, <, >, ", and ' to their HTML entity equivalents.
 *
 * Usage:
 *   this.setContent(`<div class="title">${esc(item.title)}</div>`);
 *
 * Do NOT use for:
 *   - href/src attributes with user-controlled URLs — validate scheme instead (use safeUrl)
 *   - CSS values — use a separate sanitizer
 */
export function esc(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Returns the URL only if it uses the http or https scheme.
 * Prevents javascript: and data: URL injection in href/src attributes.
 *
 * Usage:
 *   `<a href="${safeUrl(item.url)}">${esc(item.title)}</a>`
 */
export function safeUrl(raw: unknown): string {
  const s = String(raw ?? '').trim();
  return /^https?:\/\//i.test(s) ? s : '#';
}
