import { Panel } from './Panel';
import { getDemoFinance, addTransaction, deleteTransaction, type DailySummary } from '@/services/finance';
import { formatCurrency, escapeHtml } from '@/utils';

// Category colors for donut chart and badges
const CATEGORY_COLORS: { [key: string]: string } = {
  'Housing': '#FF6B6B',
  'Food': '#4ECDC4',
  'Transport': '#45B7D1',
  'Tech': '#95A5A6',
  'Entertainment': '#F39C12',
  'Salary': '#44ff88',
  'Freelance': '#3498db',
  'default': '#666666'
};

export class FinancePanel extends Panel {
  private showForm = false;

  constructor() {
    super({ id: 'finance', title: 'Daily Finance' });
    this.refresh();
  }

  async refresh(): Promise<void> {
    if (this.isFetching) return;
    this.setFetching(true);
    try {
      const data = getDemoFinance();
      this.render(data);
    } catch {
      this.showError('Failed to load finance data', () => this.refresh());
    } finally {
      this.setFetching(false);
    }
  }

  private getCategoryColor(category: string): string {
    return CATEGORY_COLORS[category] || CATEGORY_COLORS['default'];
  }

  private sortTransactionsByDate(transactions: any[]): any[] {
    return [...transactions].sort((a, b) => 
      new Date(b.date).getTime() - new Date(a.date).getTime()
    );
  }

  private renderDonutChart(expenseByCategory: { [category: string]: number }): string {
    const totalExpense = Object.values(expenseByCategory).reduce((sum, val) => sum + val, 0);
    
    if (totalExpense === 0) {
      return `
        <div class="finance-donut">
          <div class="finance-donut-chart" style="background: conic-gradient(#333 0deg 360deg);">
            <div class="finance-donut-center">
              <div class="finance-donut-total">${formatCurrency(0)}</div>
              <div class="finance-donut-label">Expenses</div>
            </div>
          </div>
        </div>`;
    }

    // Calculate percentages and build conic-gradient
    let currentDegree = 0;
    const gradientStops: string[] = [];
    const legendItems: string[] = [];

    const sortedCategories = Object.entries(expenseByCategory)
      .sort(([, a], [, b]) => b - a);

    sortedCategories.forEach(([category, amount]) => {
      const percentage = (amount / totalExpense) * 100;
      const degrees = (percentage / 100) * 360;
      const color = this.getCategoryColor(category);
      
      gradientStops.push(`${color} ${currentDegree}deg ${currentDegree + degrees}deg`);
      
      legendItems.push(`
        <div class="finance-legend-item">
          <span class="finance-legend-dot" style="background-color: ${color};"></span>
          <span class="finance-legend-label">${escapeHtml(category)}</span>
          <span class="finance-legend-value">${percentage.toFixed(1)}%</span>
        </div>
      `);
      
      currentDegree += degrees;
    });

    const gradient = `conic-gradient(${gradientStops.join(', ')})`;

    return `
      <div class="finance-donut">
        <div class="finance-donut-chart" style="background: ${gradient};">
          <div class="finance-donut-center">
            <div class="finance-donut-total">${formatCurrency(totalExpense)}</div>
            <div class="finance-donut-label">Expenses</div>
          </div>
        </div>
        <div class="finance-legend">
          ${legendItems.join('')}
        </div>
      </div>`;
  }

  private renderAddForm(): string {
    if (!this.showForm) return '';

    return `
      <div class="finance-form" id="finance-form">
        <div class="finance-form-header">
          <span class="finance-form-title">Add Transaction</span>
          <button class="finance-form-close" data-action="close-form">×</button>
        </div>
        <div class="finance-form-body">
          <div class="finance-form-row">
            <label class="finance-form-label">Description</label>
            <input 
              type="text" 
              class="finance-form-input" 
              id="txn-description" 
              placeholder="e.g., Coffee"
            />
          </div>
          <div class="finance-form-row">
            <label class="finance-form-label">Amount</label>
            <input 
              type="number" 
              class="finance-form-input" 
              id="txn-amount" 
              placeholder="0.00"
              step="0.01"
              min="0"
            />
          </div>
          <div class="finance-form-row">
            <label class="finance-form-label">Type</label>
            <select class="finance-form-select" id="txn-type">
              <option value="expense">Expense</option>
              <option value="income">Income</option>
            </select>
          </div>
          <div class="finance-form-row">
            <label class="finance-form-label">Category</label>
            <select class="finance-form-select" id="txn-category">
              <optgroup label="Expense Categories">
                <option value="Housing">Housing</option>
                <option value="Food">Food</option>
                <option value="Transport">Transport</option>
                <option value="Tech">Tech</option>
                <option value="Entertainment">Entertainment</option>
              </optgroup>
              <optgroup label="Income Categories">
                <option value="Salary">Salary</option>
                <option value="Freelance">Freelance</option>
              </optgroup>
            </select>
          </div>
          <div class="finance-form-actions">
            <button class="finance-form-btn finance-form-btn-cancel" data-action="close-form">Cancel</button>
            <button class="finance-form-btn finance-form-btn-save" data-action="save-transaction">Save</button>
          </div>
        </div>
      </div>`;
  }

  private render(data: DailySummary): void {
    // Update header with add button
    this.updateHeaderWithAddButton();

    const summary = `
      <div class="finance-summary-bar">
        <div class="finance-summary-item finance-income">
          <span class="finance-summary-label">Income</span>
          <span class="finance-summary-value">${formatCurrency(data.totalIncome)}</span>
        </div>
        <div class="finance-summary-item finance-expense">
          <span class="finance-summary-label">Expenses</span>
          <span class="finance-summary-value">${formatCurrency(data.totalExpense)}</span>
        </div>
        <div class="finance-summary-item finance-balance">
          <span class="finance-summary-label">Net Balance</span>
          <span class="finance-summary-value">${formatCurrency(data.balance)}</span>
        </div>
      </div>`;

    const donutChart = this.renderDonutChart(data.expenseByCategory);

    const sortedTransactions = this.sortTransactionsByDate(data.transactions);
    const transactions = sortedTransactions.map(t => {
      const categoryColor = this.getCategoryColor(t.category);
      const isUserTransaction = t.id.startsWith('user-');
      
      return `
        <div class="finance-item" data-transaction-id="${escapeHtml(t.id)}">
          <div class="finance-item-left">
            <div class="finance-desc">${escapeHtml(t.description)}</div>
            <span 
              class="finance-category-badge" 
              style="background-color: ${categoryColor};"
            >
              ${escapeHtml(t.category)}
            </span>
          </div>
          <div class="finance-item-right">
            <span class="finance-amount finance-${t.type}">
              ${t.type === 'income' ? '+' : '-'}${formatCurrency(t.amount)}
            </span>
            ${isUserTransaction ? `
              <button class="finance-delete-btn" data-action="delete-transaction" data-id="${escapeHtml(t.id)}">×</button>
            ` : ''}
          </div>
        </div>`
    }).join('');

    const addForm = this.renderAddForm();

    this.setContent(`
      ${summary}
      ${donutChart}
      ${addForm}
      <div class="finance-list">${transactions}</div>
    `);

    // Attach event listeners
    this.attachEventListeners();
  }

  private updateHeaderWithAddButton(): void {
    const header = this.element.querySelector('.panel-header-left');
    if (!header) return;

    // Remove existing add button if any
    const existingBtn = header.querySelector('.finance-add-btn');
    if (existingBtn) existingBtn.remove();

    // Add new button
    const addBtn = document.createElement('button');
    addBtn.className = 'finance-add-btn';
    addBtn.innerHTML = '+';
    addBtn.title = 'Add Transaction';
    addBtn.addEventListener('click', () => this.toggleForm());
    
    header.appendChild(addBtn);
  }

  private toggleForm(): void {
    this.showForm = !this.showForm;
    this.refresh();
  }

  private attachEventListeners(): void {
    const content = this.element.querySelector('.panel-content');
    if (!content) return;

    // Close form buttons
    content.querySelectorAll('[data-action="close-form"]').forEach(btn => {
      btn.addEventListener('click', () => this.toggleForm());
    });

    // Save transaction button
    const saveBtn = content.querySelector('[data-action="save-transaction"]');
    if (saveBtn) {
      saveBtn.addEventListener('click', () => this.handleSaveTransaction());
    }

    // Delete transaction buttons
    content.querySelectorAll('[data-action="delete-transaction"]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const target = e.target as HTMLElement;
        const id = target.dataset.id;
        if (id) this.handleDeleteTransaction(id);
      });
    });
  }

  private handleSaveTransaction(): void {
    const descInput = document.getElementById('txn-description') as HTMLInputElement;
    const amountInput = document.getElementById('txn-amount') as HTMLInputElement;
    const typeSelect = document.getElementById('txn-type') as HTMLSelectElement;
    const categorySelect = document.getElementById('txn-category') as HTMLSelectElement;

    if (!descInput || !amountInput || !typeSelect || !categorySelect) return;

    const description = descInput.value.trim();
    const amount = parseFloat(amountInput.value);
    const type = typeSelect.value as 'income' | 'expense';
    const category = categorySelect.value;

    // Validation
    if (!description) {
      alert('Please enter a description');
      return;
    }

    if (isNaN(amount) || amount <= 0) {
      alert('Please enter a valid amount');
      return;
    }

    // Add transaction
    const today = new Date().toISOString().slice(0, 10);
    addTransaction({
      description,
      amount,
      type,
      category,
      date: today,
    });

    // Reset form and refresh
    this.showForm = false;
    this.refresh();
  }

  private handleDeleteTransaction(id: string): void {
    if (confirm('Are you sure you want to delete this transaction?')) {
      deleteTransaction(id);
      this.refresh();
    }
  }
}
