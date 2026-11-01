export interface FinanceTransaction {
  id: string;
  description: string;
  amount: number;
  type: 'income' | 'expense';
  category: string;
  date: string;
}

export interface DailySummary {
  totalIncome: number;
  totalExpense: number;
  balance: number;
  transactions: FinanceTransaction[];
  expenseByCategory: { [category: string]: number };
}

const STORAGE_KEY = 'mdm-finance-transactions';

/**
 * Get user transactions from localStorage
 */
function getUserTransactions(): FinanceTransaction[] {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) return [];
    return JSON.parse(stored);
  } catch {
    return [];
  }
}

/**
 * Save user transactions to localStorage
 */
export function saveUserTransactions(transactions: FinanceTransaction[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(transactions));
  } catch (error) {
    console.error('Failed to save transactions:', error);
  }
}

/**
 * Add a new transaction
 */
export function addTransaction(transaction: Omit<FinanceTransaction, 'id'>): void {
  const userTransactions = getUserTransactions();
  const newTransaction: FinanceTransaction = {
    ...transaction,
    id: `user-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
  };
  userTransactions.unshift(newTransaction);
  saveUserTransactions(userTransactions);
}

/**
 * Delete a transaction
 */
export function deleteTransaction(id: string): void {
  const userTransactions = getUserTransactions();
  const filtered = userTransactions.filter(t => t.id !== id);
  saveUserTransactions(filtered);
}

/**
 * Calculate expense breakdown by category
 */
function calculateExpenseByCategory(transactions: FinanceTransaction[]): { [category: string]: number } {
  const expenses = transactions.filter(t => t.type === 'expense');
  const byCategory: { [category: string]: number } = {};
  
  expenses.forEach(t => {
    byCategory[t.category] = (byCategory[t.category] || 0) + t.amount;
  });
  
  return byCategory;
}

/**
 * Fetch daily finance data.
 * Could be backed by a spreadsheet API, YNAB, or local storage.
 */
export async function fetchDailyFinance(): Promise<DailySummary> {
  try {
    const resp = await fetch('/api/finance');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const apiData = await resp.json();
    
    // Merge with user transactions
    const userTransactions = getUserTransactions();
    const allTransactions = [...userTransactions, ...apiData.transactions];
    
    const totalIncome = allTransactions.filter(t => t.type === 'income').reduce((s, t) => s + t.amount, 0);
    const totalExpense = allTransactions.filter(t => t.type === 'expense').reduce((s, t) => s + t.amount, 0);
    const expenseByCategory = calculateExpenseByCategory(allTransactions);
    
    return {
      totalIncome,
      totalExpense,
      balance: totalIncome - totalExpense,
      transactions: allTransactions,
      expenseByCategory,
    };
  } catch {
    // Fallback: return only user's own transactions from localStorage (no fake data)
    return getUserOnlyFinance();
  }
}

/** Returns ONLY user-entered transactions — zero fake/demo data. */
export function getDemoFinance(): DailySummary {
  return getUserOnlyFinance();
}

function getUserOnlyFinance(): DailySummary {
  const transactions = getUserTransactions();
  const totalIncome = transactions.filter(t => t.type === 'income').reduce((s, t) => s + t.amount, 0);
  const totalExpense = transactions.filter(t => t.type === 'expense').reduce((s, t) => s + t.amount, 0);
  const expenseByCategory = calculateExpenseByCategory(transactions);
  return {
    totalIncome,
    totalExpense,
    balance: totalIncome - totalExpense,
    transactions,
    expenseByCategory,
  };
}
