---
name: panel-event-forms
description: Create dashboard panel components with advanced event handling and form validation using vanilla TypeScript DOM API, following the worldmonitor Panel architecture.
---

# Enhanced Panel Component Pattern

Create dashboard panel components using **vanilla TypeScript** (no framework, no JSX). Each panel is a class extending a `Panel` base class, now with enhanced event handling and form validation examples.

## Architecture Overview

```
Panel (base class)
├── element: HTMLElement (outer container, .panel)
│   ├── header: HTMLElement (.panel-header)
│   │   ├── headerLeft (.panel-header-left)
│   │   │   ├── title (.panel-title)
│   │   │   └── newBadge (.panel-new-badge) [optional]
│   │   ├── statusBadge (.panel-data-badge) [optional]
│   │   └── countEl (.panel-count) [optional]
│   ├── content: HTMLElement (.panel-content)
│   └── resizeHandle (.panel-resize-handle)
```

## Base Panel Class (Simplified for this project)

Create `src/components/Panel.ts`:

```typescript
export interface PanelOptions {
  id: string;
  title: string;
  showCount?: boolean;
  className?: string;
}

export class Panel {
  protected element: HTMLElement;
  protected content: HTMLElement;
  protected header: HTMLElement;
  protected countEl: HTMLElement | null = null;
  protected panelId: string;
  private _fetching = false;

  constructor(options: PanelOptions) {
    this.panelId = options.id;
    this.element = document.createElement('div');
    this.element.className = `panel ${options.className || ''}`;
    this.element.dataset.panel = options.id;

    // Header
    this.header = document.createElement('div');
    this.header.className = 'panel-header';

    const headerLeft = document.createElement('div');
    headerLeft.className = 'panel-header-left';

    const title = document.createElement('span');
    title.className = 'panel-title';
    title.textContent = options.title;
    headerLeft.appendChild(title);
    this.header.appendChild(headerLeft);

    // Count badge (optional)
    if (options.showCount) {
      this.countEl = document.createElement('span');
      this.countEl.className = 'panel-count';
      this.countEl.textContent = '0';
      this.header.appendChild(this.countEl);
    }

    // Content area
    this.content = document.createElement('div');
    this.content.className = 'panel-content';
    this.content.id = `${options.id}Content`;

    this.element.appendChild(this.header);
    this.element.appendChild(this.content);
    this.showLoading();
  }

  public getElement(): HTMLElement { return this.element; }

  public showLoading(message = 'Loading...'): void {
    this.content.innerHTML = `
     <div class="panel-loading">
        <div class="panel-loading-spinner"></div>
        <div class="panel-loading-text">${message}</div>
      </div>`;
  }

  public showError(message = 'Failed to load', onRetry?: () => void): void {
    this.content.innerHTML = `
      <div class="panel-error-state">
        <div class="panel-error-msg">${message}</div>
        ${onRetry ? '<button class="panel-retry-btn" data-panel-retry>Retry</button>' : ''}
      </div>`;
    if (onRetry) {
      this.content.querySelector('[data-panel-retry]')?.addEventListener('click', onRetry);
    }
  }

  public setContent(html: string): void {
    this.content.innerHTML = html;
  }

  public setCount(count: number): void {
    if (this.countEl) this.countEl.textContent = count.toString();
  }

  public show(): void { this.element.classList.remove('hidden'); }
  public hide(): void { this.element.classList.add('hidden'); }

  protected setFetching(v: boolean): void { this._fetching = v; }
  protected get isFetching(): boolean { return this._fetching; }

  public destroy(): void {
    this.element.remove();
  }
}
```

## Creating a Concrete Panel (Example: FinancePanel)

Each panel extends `Panel` and manages its own data fetching + rendering, now with enhanced event handling and form validation:

```typescript
import { Panel } from './Panel';

interface Transaction {
  id: string;
  amount: number;
  description: string;
  date: string;
  category: string;
}

export class FinancePanel extends Panel {
  private transactions: Transaction[] = [];
  private balance: number = 0;

  constructor() {
    super({ id: 'finance', title: 'Finance Tracker', showCount: true });
    this.loadTransactions();
    this.render();
  }

  private loadTransactions(): void {
    const saved = localStorage.getItem('finance-transactions');
    if (saved) {
      this.transactions = JSON.parse(saved);
      this.balance = this.transactions.reduce((sum, t) => sum + t.amount, 0);
    }
  }

  private saveTransactions(): void {
    localStorage.setItem('finance-transactions', JSON.stringify(this.transactions));
  }

  private render(): void {
    const formHtml = `
      <form id="financeForm" class="finance-form">
        <div class="form-group">
          <label for="amount">Amount</label>
          <input type="number" id="amount" name="amount" required>
          <div class="error-message" id="amountError"></div>
        </div>
        <div class="form-group">
          <label for="description">Description</label>
          <input type="text" id="description" name="description" required>
          <div class="error-message" id="descriptionError"></div>
        </div>
        <button type="submit">Add Transaction</button>
      </form>
    `;

    const transactionsHtml = this.transactions.map(t => `
      <div class="transaction">
        <span class="transaction-date">${t.date}</span>
        <span class="transaction-description">${t.description}</span>
        <span class="transaction-amount ${t.amount >= 0 ? 'positive' : 'negative'}">
          ${t.amount >= 0 ? '+' : ''}${t.amount.toFixed(2)}
        </span>
      </div>
    `).join('');

    this.setContent(`
      <div class="finance-container">
        <div class="balance">Balance: $${this.balance.toFixed(2)}</div>
        ${formHtml}
        <div class="transactions">${transactionsHtml}</div>
      </div>
    `);

    this.setupForm();
  }

  private setupForm(): void {
    const form = this.content.querySelector('#financeForm') as HTMLFormElement;
    if (!form) return;

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if (this.validateForm()) {
        this.addTransaction();
      }
    });
  }

  private validateForm(): boolean {
    let isValid = true;
    const amountInput = this.content.querySelector('#amount') as HTMLInputElement;
    const descriptionInput = this.content.querySelector('#description') as HTMLInputElement;
    const amountError = this.content.querySelector('#amountError') as HTMLElement;
    const descriptionError = this.content.querySelector('#descriptionError') as HTMLElement;

    // Reset errors
    amountError.textContent = '';
    descriptionError.textContent = '';

    // Validate amount
    if (!amountInput.value) {
      amountError.textContent = 'Amount is required';
      isValid = false;
    } else if (isNaN(parseFloat(amountInput.value))) {
      amountError.textContent = 'Amount must be a number';
      isValid = false;
    }

    // Validate description
    if (!descriptionInput.value) {
      descriptionError.textContent = 'Description is required';
      isValid = false;
    }

    return isValid;
  }

  private addTransaction(): void {
    const amountInput = this.content.querySelector('#amount') as HTMLInputElement;
    const descriptionInput = this.content.querySelector('#description') as HTMLInputElement;

    const transaction: Transaction = {
      id: Date.now().toString(),
      amount: parseFloat(amountInput.value),
      description: descriptionInput.value,
      date: new Date().toLocaleDateString(),
      category: 'General',
    };

    this.transactions.push(transaction);
    this.balance += transaction.amount;
    this.saveTransactions();
    this.render();
  }

  public override destroy(): void {
    // Clean up any event listeners
    super.destroy();
  }
}
```

## Key Patterns

1. **Constructor** calls `super()` with panel config, then loads initial data
2. **Form Handling** includes validation and submission logic
3. **Event Listeners** are properly set up and cleaned up in `destroy()`
4. **Local Storage** is used for persistent data
5. **Validation** provides user feedback on form errors
6. **Dynamic Updates** reflect changes immediately in the UI

## Sparkline Utility

```typescript
export function miniSparkline(data: number[] | undefined, change: number | null, w = 50, h = 16): string {
  if (!data || data.length < 2) return '';
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const color = change != null && change >= 0 ? 'var(--green)' : 'var(--red)';
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
```
