interface ProgressBarProps {
  label: string;
  value: number;
  colorClass?: string;
}

export default function ProgressBar({ label, value, colorClass = 'bg-primary' }: ProgressBarProps) {
  const percent = Math.max(0, Math.min(100, Math.round(value * 1000) / 10));

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs text-muted">
        <span>{label}</span>
        <span>{percent}%</span>
      </div>
      <div className="h-3 rounded-full bg-[color:var(--color-border)] overflow-hidden">
        <div className={`h-full ${colorClass}`} style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
