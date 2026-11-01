import type { ReactNode } from 'react';

interface MetricCardProps {
  label: string;
  value: ReactNode;
  hint?: string;
}

export default function MetricCard({ label, value, hint }: MetricCardProps) {
  return (
    <div className="metric-card">
      <div className="text-xs uppercase tracking-[0.16em] text-muted">{label}</div>
      <div className="text-[2.75rem] font-semibold font-serif leading-none tracking-[-0.04em] mt-3">{value}</div>
      {hint ? (
        <>
          <div className="w-8 h-px bg-[color:var(--color-border-dark)] my-4" />
          <div className="text-sm text-muted font-serif">{hint}</div>
        </>
      ) : null}
    </div>
  );
}
