import { Component, ErrorInfo, ReactNode } from 'react';
import i18n from '../i18n';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    if (import.meta.env.DEV) {
      console.error('Error caught by boundary:', error, errorInfo);
    }
  }

  render() {
    if (this.state.hasError) {
      const t = i18n.t.bind(i18n);
      return (
        this.props.fallback || (
          <div className="min-h-screen flex items-center justify-center bg-[color:var(--color-bg-page)]">
            <div className="text-center">
              <h1 className="text-2xl font-bold text-[color:var(--color-danger)] mb-4">
                {t('errorBoundary.title')}
              </h1>
              <p className="text-[color:var(--color-muted)] mb-6">
                {t('errorBoundary.message')}
              </p>
              <button
                onClick={() => window.location.href = '/dashboard'}
                className="btn-primary"
              >
                {t('errorBoundary.goToDashboard')}
              </button>
              {import.meta.env.DEV && this.state.error && (
                <details className="mt-4 text-left text-xs text-[color:var(--color-muted)]">
                  <summary>{t('errorBoundary.details')}</summary>
                  <pre className="mt-2 p-4 bg-[color:var(--color-surface)] overflow-auto">
                    {this.state.error.stack}
                  </pre>
                </details>
              )}
            </div>
          </div>
        )
      );
    }

    return this.props.children;
  }
}
