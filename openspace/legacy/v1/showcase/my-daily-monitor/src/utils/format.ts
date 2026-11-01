export function formatTime(date: Date): string {
  const now = new Date();
  const diff = Math.floor((now.getTime() - date.getTime()) / 1000);

  try {
    const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
    if (diff < 60) return rtf.format(-Math.round(diff), 'second');
    if (diff < 3600) return rtf.format(-Math.round(diff / 60), 'minute');
    if (diff < 86400) return rtf.format(-Math.round(diff / 3600), 'hour');
    return rtf.format(-Math.round(diff / 86400), 'day');
  } catch {
    if (diff < 60) return 'Just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }
}

export function formatPrice(price: number): string {
  if (price >= 1000) {
    return `$${price.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
  }
  return `$${price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatChange(change: number | null): string {
  if (change == null) return '—';
  const sign = change >= 0 ? '+' : '';
  return `${sign}${change.toFixed(2)}%`;
}

export function getChangeClass(change: number | null): string {
  if (change == null) return '';
  return change >= 0 ? 'positive' : 'negative';
}

export function escapeHtml(str: string): string {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

export function formatCurrency(amount: number, currency = 'CNY'): string {
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(amount);
}

export function formatDate(date: Date): string {
  return new Intl.DateTimeFormat('en', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  }).format(date);
}

