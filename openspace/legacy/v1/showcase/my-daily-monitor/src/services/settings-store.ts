/**
 * Settings store — manages API keys (secrets) and user preferences.
 * All data lives in localStorage. Secrets are stored under a separate key
 * from preferences so they can be independently backed up / cleared.
 */

import type { SecretKey } from '@/config/settings-keys';
import { DEFAULT_PREFERENCES, type UserPreferences } from '@/config/preferences';

const SECRETS_STORAGE_KEY = 'mdm-secrets-v1';
const PREFS_STORAGE_KEY = 'mdm-preferences-v1';

// ---- Events ----
export const SETTINGS_CHANGED_EVENT = 'mdm-settings-changed';
export const PREFS_CHANGED_EVENT = 'mdm-prefs-changed';

// ---- Secrets ----

function loadSecrets(): Record<string, string> {
  try {
    const raw = localStorage.getItem(SECRETS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch { return {}; }
}

function saveSecrets(secrets: Record<string, string>): void {
  localStorage.setItem(SECRETS_STORAGE_KEY, JSON.stringify(secrets));
  window.dispatchEvent(new CustomEvent(SETTINGS_CHANGED_EVENT));
}

export function getSecret(key: SecretKey): string {
  return loadSecrets()[key] || '';
}

export function setSecret(key: SecretKey, value: string): void {
  const s = loadSecrets();
  if (value) s[key] = value.trim();
  else delete s[key];
  saveSecrets(s);
}

export function hasSecret(key: SecretKey): boolean {
  return !!loadSecrets()[key];
}

export function getAllSecrets(): Record<string, string> {
  return loadSecrets();
}

export function setAllSecrets(secrets: Record<string, string>): void {
  saveSecrets(secrets);
}

export function clearAllSecrets(): void {
  localStorage.removeItem(SECRETS_STORAGE_KEY);
  window.dispatchEvent(new CustomEvent(SETTINGS_CHANGED_EVENT));
}

export function maskSecret(value: string): string {
  if (!value) return '';
  if (value.length <= 8) return '••••••••';
  return value.slice(0, 4) + '••••' + value.slice(-4);
}

// ---- Preferences ----

function loadPrefs(): UserPreferences {
  try {
    const raw = localStorage.getItem(PREFS_STORAGE_KEY);
    if (!raw) return { ...DEFAULT_PREFERENCES };
    return { ...DEFAULT_PREFERENCES, ...JSON.parse(raw) };
  } catch { return { ...DEFAULT_PREFERENCES }; }
}

function savePrefs(prefs: UserPreferences): void {
  localStorage.setItem(PREFS_STORAGE_KEY, JSON.stringify(prefs));
  window.dispatchEvent(new CustomEvent(PREFS_CHANGED_EVENT));
}

export function getPreferences(): UserPreferences {
  return loadPrefs();
}

export function setPreferences(partial: Partial<UserPreferences>): void {
  const current = loadPrefs();
  savePrefs({ ...current, ...partial });
}

export function resetPreferences(): void {
  savePrefs({ ...DEFAULT_PREFERENCES });
}

// ---- Convenience ----

export function getStockSymbols(): string[] {
  return getPreferences().stockWatchlist.map(w => w.symbol);
}

export function getGithubRepos(): string[] {
  return getPreferences().githubRepos;
}

export function subscribeSettingsChange(cb: () => void): () => void {
  const h1 = () => cb();
  const h2 = () => cb();
  window.addEventListener(SETTINGS_CHANGED_EVENT, h1);
  window.addEventListener(PREFS_CHANGED_EVENT, h2);
  return () => {
    window.removeEventListener(SETTINGS_CHANGED_EVENT, h1);
    window.removeEventListener(PREFS_CHANGED_EVENT, h2);
  };
}

