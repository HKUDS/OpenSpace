/**
 * CommandPalette — Cmd+K quick-search overlay.
 * Search across panels, jump to sections, trigger actions.
 * 
 * Enhanced Features:
 * - Fuzzy search with highlight
 * - Recent commands tracking
 * - Keyboard navigation (arrow keys, Enter, Escape)
 * - Category grouping
 */

let overlayEl: HTMLElement | null = null;

// Command group categories
type CommandGroup = 'Navigation' | 'Actions' | 'Settings';

interface Command {
  label: string;
  description: string;
  action: () => void;
  keywords?: string[];
  group?: CommandGroup; // Category for grouping
}

let registeredCommands: Command[] = [];
const RECENT_COMMANDS_KEY = 'mdm-recent-commands';
const MAX_RECENT_COMMANDS = 3;

export function registerCommands(commands: Command[]): void {
  registeredCommands = commands;
}

export function openCommandPalette(): void {
  if (overlayEl) return;

  overlayEl = document.createElement('div');
  overlayEl.className = 'modal-overlay active';
  overlayEl.id = 'commandPalette';
  overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) closeCommandPalette(); });

  const palette = document.createElement('div');
  palette.className = 'cmd-palette';

  palette.innerHTML = `
    <input class="cmd-palette-input" placeholder="Search panels, actions..." autofocus id="cmdInput" />
    <div class="cmd-palette-results" id="cmdResults"></div>
  `;

  overlayEl.appendChild(palette);
  document.body.appendChild(overlayEl);

  const input = palette.querySelector<HTMLInputElement>('#cmdInput')!;
  const results = palette.querySelector('#cmdResults')!;

  input.addEventListener('input', () => renderResults(input.value, results as HTMLElement));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeCommandPalette();
    }
    if (e.key === 'Enter') {
      const active = results.querySelector<HTMLElement>('.cmd-item.active');
      if (active) {
        active.click();
      }
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const items = results.querySelectorAll<HTMLElement>('.cmd-item');
      if (items.length === 0) return;
      
      const active = results.querySelector<HTMLElement>('.cmd-item.active');
      let newIndex = 0;
      
      if (active) {
        const currentIndex = Array.from(items).indexOf(active);
        if (e.key === 'ArrowDown') {
          newIndex = Math.min(currentIndex + 1, items.length - 1);
        } else {
          newIndex = Math.max(currentIndex - 1, 0);
        }
      }
      
      items.forEach(i => i.classList.remove('active'));
      items[newIndex]?.classList.add('active');
      items[newIndex]?.scrollIntoView({ block: 'nearest' });
    }
  });

  renderResults('', results as HTMLElement);
  setTimeout(() => input.focus(), 50);
}

export function closeCommandPalette(): void {
  if (overlayEl) { 
    overlayEl.remove(); 
    overlayEl = null; 
  }
}

/**
 * Fuzzy search: check if query characters appear in target string in order
 */
function fuzzyMatch(query: string, target: string): boolean {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qIndex = 0;
  
  for (let i = 0; i < t.length && qIndex < q.length; i++) {
    if (t[i] === q[qIndex]) {
      qIndex++;
    }
  }
  
  return qIndex === q.length;
}

/**
 * Highlight matching characters in text with <mark> tags
 */
function highlightMatches(text: string, query: string): string {
  if (!query) return text;
  
  const q = query.toLowerCase();
  const result: string[] = [];
  let qIndex = 0;
  
  for (let i = 0; i < text.length && qIndex < q.length; i++) {
    if (text[i].toLowerCase() === q[qIndex]) {
      result.push(`<mark>${text[i]}</mark>`);
      qIndex++;
    } else {
      result.push(text[i]);
    }
  }
  
  // Add remaining characters
  if (result.length < text.length) {
    result.push(text.slice(result.length));
  }
  
  return result.join('');
}

/**
 * Get recent commands from localStorage
 */
function getRecentCommands(): string[] {
  try {
    const stored = localStorage.getItem(RECENT_COMMANDS_KEY);
    return stored ? JSON.parse(stored) : [];
  } catch {
    return [];
  }
}

/**
 * Save command to recent history
 */
function saveToRecent(commandLabel: string): void {
  try {
    let recent = getRecentCommands();
    
    // Remove if already exists (move to top)
    recent = recent.filter(r => r !== commandLabel);
    
    // Add to beginning
    recent.unshift(commandLabel);
    
    // Keep only last N commands
    recent = recent.slice(0, MAX_RECENT_COMMANDS);
    
    localStorage.setItem(RECENT_COMMANDS_KEY, JSON.stringify(recent));
  } catch (err) {
    console.warn('Failed to save recent command:', err);
  }
}

/**
 * Group commands by category
 */
function groupCommands(commands: Command[]): Map<string, Command[]> {
  const groups = new Map<string, Command[]>();
  
  commands.forEach(cmd => {
    const group = cmd.group || 'Actions';
    if (!groups.has(group)) {
      groups.set(group, []);
    }
    groups.get(group)!.push(cmd);
  });
  
  return groups;
}

function renderResults(query: string, container: HTMLElement): void {
  const q = query.toLowerCase().trim();
  
  // Fuzzy filter commands
  let filtered: Command[];
  if (q) {
    filtered = registeredCommands.filter(c => {
      // Check label, description, and keywords
      const labelMatch = fuzzyMatch(q, c.label);
      const descMatch = fuzzyMatch(q, c.description);
      const keywordMatch = (c.keywords || []).some(kw => fuzzyMatch(q, kw));
      
      return labelMatch || descMatch || keywordMatch;
    });
  } else {
    filtered = [...registeredCommands];
  }

  // Get recent commands
  const recentLabels = getRecentCommands();
  const recentCommands = recentLabels
    .map(label => registeredCommands.find(c => c.label === label))
    .filter((c): c is Command => c !== undefined);

  let html = '';
  let activeSet = false;

  // Render recent commands section (only when no query)
  if (!q && recentCommands.length > 0) {
    html += `<div class="cmd-group-header cmd-recent">Recent</div>`;
    recentCommands.forEach((c, i) => {
      const isActive = !activeSet;
      if (isActive) activeSet = true;
      
      html += `
        <div class="cmd-item ${isActive ? 'active' : ''}" data-label="${escapeHtml(c.label)}">
          <span class="cmd-item-label">${escapeHtml(c.label)}</span>
          <span class="cmd-item-desc">${escapeHtml(c.description)}</span>
        </div>
      `;
    });
  }

  // Group filtered commands by category
  const grouped = groupCommands(filtered);
  const groupOrder: CommandGroup[] = ['Navigation', 'Actions', 'Settings'];
  
  groupOrder.forEach(groupName => {
    const groupCommands = grouped.get(groupName);
    if (!groupCommands || groupCommands.length === 0) return;
    
    // Add group header
    html += `<div class="cmd-group-header">${groupName}</div>`;
    
    // Add commands in this group
    groupCommands.forEach((c) => {
      const isActive = !activeSet;
      if (isActive) activeSet = true;
      
      const highlightedLabel = q ? highlightMatches(c.label, q) : escapeHtml(c.label);
      const highlightedDesc = q ? highlightMatches(c.description, q) : escapeHtml(c.description);
      
      html += `
        <div class="cmd-item ${isActive ? 'active' : ''}" data-label="${escapeHtml(c.label)}">
          <span class="cmd-item-label">${highlightedLabel}</span>
          <span class="cmd-item-desc">${highlightedDesc}</span>
        </div>
      `;
    });
  });

  // Show empty state if no results
  if (!html) {
    html = `
      <div class="cmd-item" style="justify-content: center; opacity: 0.5; cursor: default;">
        <span class="cmd-item-desc">No commands found</span>
      </div>
    `;
  }

  container.innerHTML = html;

  // Attach click handlers
  container.querySelectorAll<HTMLElement>('.cmd-item').forEach((el) => {
    const label = el.dataset.label;
    if (!label) return;
    
    const command = registeredCommands.find(c => c.label === label);
    if (!command) return;
    
    el.addEventListener('click', () => {
      // Save to recent history
      saveToRecent(command.label);
      
      // Execute action
      command.action();
      
      // Close palette
      closeCommandPalette();
    });
  });
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text: string): string {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Global keyboard shortcut
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault();
    if (overlayEl) closeCommandPalette();
    else openCommandPalette();
  }
});
