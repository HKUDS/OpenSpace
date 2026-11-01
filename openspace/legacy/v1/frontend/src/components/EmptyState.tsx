interface EmptyStateProps {
  title: string;
  description: string;
}

export default function EmptyState({ title, description }: EmptyStateProps) {
  return (
    <div className="panel-surface p-8 text-center space-y-2">
      <div className="text-lg font-bold font-serif">{title}</div>
      <div className="text-sm text-muted">{description}</div>
    </div>
  );
}
