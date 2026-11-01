/**
 * SettingsModal — full-screen overlay for configuring API keys and user preferences.
 *
 * FIX: Both tabs rendered simultaneously (hidden via CSS), so switching tabs
 * doesn't destroy inputs. Save always captures all values.
 */
import { SECRET_REGISTRY, SECRET_GROUPS, type SecretKey } from '@/config/settings-keys';
import {
  getSecret, setSecret, maskSecret, getAllSecrets, setAllSecrets,
  getPreferences, setPreferences,
} from '@/services/settings-store';
import { escapeHtml } from '@/utils';
import { getCurrentTheme, getCurrentVariant, setTheme, setVariant, type Theme, type ThemeVariant } from '@/utils/theme-manager';

let overlayEl: HTMLElement | null = null;

export function openSettings(): void {
  if (overlayEl) return;

  overlayEl = document.createElement('div');
  overlayEl.className = 'modal-overlay active';
  overlayEl.id = 'settingsModal';
  overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) closeSettings(); });

  const modal = document.createElement('div');
  modal.className = 'modal settings-modal';

  modal.innerHTML = `
    <div class="modal-header">
      <span class="modal-title">Settings</span>
      <button class="modal-close" aria-label="Close">&times;</button>
    </div>
    <div class="settings-body">
      <div class="settings-tabs" id="settingsTabs"></div>
      <div class="settings-content">
        <div id="settingsApiKeys"></div>
        <div id="settingsPrefs" style="display:none"></div>
      </div>
    </div>
    <div class="settings-footer">
      <button class="settings-save-btn" id="settingsSaveBtn">Save</button>
      <button class="settings-cancel-btn" id="settingsCancelBtn">Cancel</button>
    </div>
  `;

  overlayEl.appendChild(modal);
  document.body.appendChild(overlayEl);

  const tabContainer = modal.querySelector('#settingsTabs')!;
  const apiKeysPane = modal.querySelector('#settingsApiKeys') as HTMLElement;
  const prefsPane = modal.querySelector('#settingsPrefs') as HTMLElement;

  // Render BOTH tabs at once (never destroyed)
  renderApiKeysTab(apiKeysPane);
  renderPreferencesTab(prefsPane);

  // Tab switching (show/hide, no re-render)
  const panes = [apiKeysPane, prefsPane];
  const groups = ['API Keys', 'Preferences'];

  groups.forEach((g, i) => {
    const btn = document.createElement('button');
    btn.className = `panel-tab ${i === 0 ? 'active' : ''}`;
    btn.textContent = g;
    btn.addEventListener('click', () => {
      tabContainer.querySelectorAll('.panel-tab').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
      panes.forEach((p, j) => { p.style.display = j === i ? '' : 'none'; });
    });
    tabContainer.appendChild(btn);
  });

  // Live preview: apply theme changes immediately
  const variantSelect = modal.querySelector('#prefVariant') as HTMLSelectElement | null;
  const themeSelect = modal.querySelector('#prefTheme') as HTMLSelectElement | null;
  variantSelect?.addEventListener('change', () => {
    setVariant(variantSelect.value as ThemeVariant);
    // Default happy to light, default variant to dark
    if (variantSelect.value === 'happy' && themeSelect) {
      themeSelect.value = 'light';
      setTheme('light');
    } else if (variantSelect.value === 'default' && themeSelect) {
      themeSelect.value = 'dark';
      setTheme('dark');
    }
  });
  themeSelect?.addEventListener('change', () => {
    setTheme(themeSelect.value as Theme);
  });

  // Events
  modal.querySelector('.modal-close')!.addEventListener('click', closeSettings);
  modal.querySelector('#settingsCancelBtn')!.addEventListener('click', closeSettings);
  modal.querySelector('#settingsSaveBtn')!.addEventListener('click', () => {
    // Save from BOTH panes (they're always in the DOM)
    saveSecrets(apiKeysPane);
    savePrefs(prefsPane);
    closeSettings();
  });
}

export function closeSettings(): void {
  if (overlayEl) { overlayEl.remove(); overlayEl = null; }
}

// ---- API Keys Tab ----
function renderApiKeysTab(container: HTMLElement): void {
  let html = '';
  for (const group of SECRET_GROUPS) {
    const items = SECRET_REGISTRY.filter(s => s.group === group);
    html += `<div class="settings-group"><div class="settings-group-title">${escapeHtml(group)}</div>`;
    for (const item of items) {
      const val = getSecret(item.key);
      const masked = maskSecret(val);
      if (item.type === 'toggle') {
        html += `
          <div class="settings-row">
            <label class="settings-label">${escapeHtml(item.label)}</label>
            <label class="settings-toggle">
              <input type="checkbox" data-secret="${item.key}" ${val ? 'checked' : ''} />
              <span class="settings-toggle-slider"></span>
            </label>
            ${item.hint ? `<div class="settings-hint">${escapeHtml(item.hint)}</div>` : ''}
          </div>`;
      } else {
        html += `
          <div class="settings-row">
            <label class="settings-label">${escapeHtml(item.label)}${item.required ? ' *' : ''}</label>
            <input class="settings-input" type="${item.type === 'password' ? 'password' : 'text'}"
              data-secret="${item.key}"
              placeholder="${escapeHtml(item.placeholder)}"
              value="${val ? escapeHtml(masked) : ''}"
              autocomplete="off" />
            ${item.hint ? `<div class="settings-hint">${escapeHtml(item.hint)}</div>` : ''}
          </div>`;
      }
    }
    html += '</div>';
  }
  container.innerHTML = html;

  // Focus clears masked value so user can type fresh
  container.querySelectorAll<HTMLInputElement>('input[data-secret]').forEach(input => {
    if (input.type === 'checkbox') return;
    input.addEventListener('focus', () => {
      if (input.value.includes('••')) input.value = '';
    });
  });
}

// ---- Preferences Tab ----
function renderPreferencesTab(container: HTMLElement): void {
  const p = getPreferences();
  const currentTheme = getCurrentTheme();
  const currentVariant = getCurrentVariant();
  container.innerHTML = `
    <div class="settings-group">
      <div class="settings-group-title">Appearance</div>
      <div class="settings-row">
        <label class="settings-label">Color Scheme</label>
        <select class="settings-input" id="prefVariant" style="padding:6px 8px;">
          <option value="default" ${currentVariant === 'default' ? 'selected' : ''}>Default (Dark Ops)</option>
          <option value="happy" ${currentVariant === 'happy' ? 'selected' : ''}>Happy (Calm & Serene)</option>
        </select>
        <div class="settings-hint">Happy theme uses sage green + warm gold tones with rounded panels.</div>
      </div>
      <div class="settings-row">
        <label class="settings-label">Theme Mode</label>
        <select class="settings-input" id="prefTheme" style="padding:6px 8px;">
          <option value="dark" ${currentTheme === 'dark' ? 'selected' : ''}>Dark</option>
          <option value="light" ${currentTheme === 'light' ? 'selected' : ''}>Light</option>
        </select>
      </div>
    </div>
    <div class="settings-group">
      <div class="settings-group-title">Stock Watchlist</div>
      <div class="settings-row">
        <label class="settings-label">Symbols (one per line: SYMBOL|Name)</label>
        <textarea class="settings-textarea" data-pref="stockWatchlist" rows="6">${p.stockWatchlist.map(w => w.name ? `${w.symbol}|${w.name}` : w.symbol).join('\n')}</textarea>
      </div>
    </div>
    <div class="settings-group">
      <div class="settings-group-title">News</div>
      <div class="settings-row">
        <label class="settings-label">Categories (comma-separated)</label>
        <input class="settings-input" data-pref="newsCategories" value="${escapeHtml(p.newsCategories.join(', '))}" />
      </div>
      <div class="settings-row">
        <label class="settings-label">Alert keywords (comma-separated)</label>
        <input class="settings-input" data-pref="newsKeywords" value="${escapeHtml(p.newsKeywords.join(', '))}" />
      </div>
    </div>
    <div class="settings-group">
      <div class="settings-group-title">GitHub Repos</div>
      <div class="settings-row">
        <label class="settings-label">Repos to watch (one per line: owner/repo)</label>
        <textarea class="settings-textarea" data-pref="githubRepos" rows="4">${p.githubRepos.join('\n')}</textarea>
      </div>
    </div>
    <div class="settings-group">
      <div class="settings-group-title">Feishu</div>
      <div class="settings-row">
        <label class="settings-label">Chat IDs to monitor (one per line)</label>
        <textarea class="settings-textarea" data-pref="feishuChatIds" rows="3">${p.feishuChatIds.join('\n')}</textarea>
      </div>
    </div>
    <div class="settings-group">
      <div class="settings-group-title">AI</div>
      <div class="settings-row">
        <label class="settings-label">Enable AI summaries</label>
        <label class="settings-toggle">
          <input type="checkbox" data-pref-toggle="aiSummaryEnabled" ${p.aiSummaryEnabled ? 'checked' : ''} />
          <span class="settings-toggle-slider"></span>
        </label>
      </div>
    </div>
  `;
}

// ---- Save Secrets (from API Keys pane) ----
function saveSecrets(container: HTMLElement): void {
  const secrets = getAllSecrets();
  container.querySelectorAll<HTMLInputElement>('input[data-secret]').forEach(input => {
    const key = input.dataset.secret as SecretKey;
    if (input.type === 'checkbox') {
      if (input.checked) secrets[key] = '1';
      else delete secrets[key];
    } else {
      const val = input.value.trim();
      // Only save if user typed something real (not masked placeholder)
      if (val && !val.includes('••')) secrets[key] = val;
    }
  });
  setAllSecrets(secrets);
}

// ---- Save Preferences (from Preferences pane) ----
function savePrefs(container: HTMLElement): void {
  const prefs: Record<string, unknown> = {};

  container.querySelectorAll<HTMLInputElement>('input[data-pref]').forEach(input => {
    const key = input.dataset.pref!;
    const val = input.value.trim();
    if (key === 'newsCategories' || key === 'newsKeywords' || key === 'socialKeywords') {
      prefs[key] = val.split(',').map(s => s.trim()).filter(Boolean);
    } else {
      prefs[key] = val;
    }
  });

  container.querySelectorAll<HTMLTextAreaElement>('textarea[data-pref]').forEach(ta => {
    const key = ta.dataset.pref!;
    if (key === 'stockWatchlist') {
      prefs[key] = ta.value.split('\n').map(line => {
        const [sym, ...rest] = line.trim().split('|');
        const symbol = sym?.trim();
        if (!symbol) return null;
        const name = rest.join('|').trim() || undefined;
        return { symbol, name };
      }).filter(Boolean);
    } else {
      prefs[key] = ta.value.split('\n').map(s => s.trim()).filter(Boolean);
    }
  });

  container.querySelectorAll<HTMLInputElement>('input[data-pref-toggle]').forEach(input => {
    const key = input.dataset.prefToggle!;
    prefs[key] = input.checked;
  });

  setPreferences(prefs as any);
}
