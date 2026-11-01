/**
 * Theme manager — handles dark/light mode and variant (default / happy) switching.
 * Persists to localStorage. Uses data-theme and data-variant on <html>.
 */

export type Theme = 'dark' | 'light';
export type ThemeVariant = 'default' | 'happy';

const THEME_KEY = 'mdm-theme';
const VARIANT_KEY = 'mdm-variant';
const THEME_CHANGED_EVENT = 'mdm-theme-changed';

// ---- Read ----

export function getCurrentTheme(): Theme {
  const val = document.documentElement.dataset.theme;
  return val === 'light' ? 'light' : 'dark';
}

export function getCurrentVariant(): ThemeVariant {
  const val = document.documentElement.dataset.variant;
  return val === 'happy' ? 'happy' : 'default';
}

// ---- Write ----

export function setTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* noop */ }
  updateMetaThemeColor();
  window.dispatchEvent(new CustomEvent(THEME_CHANGED_EVENT, { detail: { theme } }));
}

export function setVariant(variant: ThemeVariant): void {
  if (variant === 'default') {
    delete document.documentElement.dataset.variant;
  } else {
    document.documentElement.dataset.variant = variant;
  }
  try { localStorage.setItem(VARIANT_KEY, variant); } catch { /* noop */ }
  updateMetaThemeColor();
  window.dispatchEvent(new CustomEvent(THEME_CHANGED_EVENT, { detail: { variant } }));
}

export function toggleTheme(): Theme {
  const next: Theme = getCurrentTheme() === 'dark' ? 'light' : 'dark';
  setTheme(next);
  return next;
}

// ---- Init (call once before mount) ----

export function applyStoredTheme(): void {
  let theme: Theme = 'dark';
  let variant: ThemeVariant = 'default';

  try {
    const storedTheme = localStorage.getItem(THEME_KEY);
    if (storedTheme === 'dark' || storedTheme === 'light') theme = storedTheme;

    const storedVariant = localStorage.getItem(VARIANT_KEY);
    if (storedVariant === 'happy') variant = storedVariant;
  } catch { /* noop */ }

  // Happy variant defaults to light if no explicit preference
  if (variant === 'happy' && !localStorage.getItem(THEME_KEY)) {
    theme = 'light';
  }

  document.documentElement.dataset.theme = theme;
  if (variant !== 'default') {
    document.documentElement.dataset.variant = variant;
  }
  updateMetaThemeColor();
}

// ---- Helpers ----

function updateMetaThemeColor(): void {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) return;
  const variant = getCurrentVariant();
  const theme = getCurrentTheme();
  if (variant === 'happy') {
    meta.content = theme === 'dark' ? '#1A2332' : '#FAFAF5';
  } else {
    meta.content = theme === 'dark' ? '#0a0a0a' : '#f8f9fa';
  }
}

