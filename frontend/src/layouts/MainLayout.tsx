import { NavLink, Outlet } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

const linkClass = ({ isActive }: { isActive: boolean }) =>
  isActive
    ? 'font-bold text-primary underline decoration-2 underline-offset-4'
    : 'hover:text-primary';

export default function MainLayout() {
  const { t, i18n } = useTranslation();

  const toggleLang = () => {
    i18n.changeLanguage(i18n.language === 'zh' ? 'en' : 'zh');
  };

  return (
    <div className="h-screen min-w-[1180px] relative flex flex-col overflow-x-auto overflow-y-hidden bg-bg-page text-ink">
      <nav className="relative z-10 flex justify-between items-center px-4 py-3 border-b border-[color:var(--color-border)] bg-bg-page">
        <div className="flex items-center gap-8">
          <div className="font-bold text-3xl tracking-tighter font-serif">OpenSpace</div>
          <div className="flex gap-4 text-sm">
            <NavLink to="/dashboard" className={linkClass}>
              {t('nav.dashboard')}
            </NavLink>
            <NavLink to="/skills" className={linkClass}>
              {t('nav.skills')}
            </NavLink>
            <NavLink to="/workflows" className={linkClass}>
              {t('nav.workflows')}
            </NavLink>
          </div>
        </div>
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={toggleLang}
            className="px-2.5 py-1 text-xs border border-[color:var(--color-border-dark)] rounded hover:bg-[color:var(--color-surface)] transition-colors cursor-pointer bg-transparent text-ink"
          >
            {i18n.language === 'zh' ? 'EN' : '中文'}
          </button>
          <div className="text-xs text-muted">API: `localhost:7788` · Vite: `localhost:3888`</div>
        </div>
      </nav>

      <main className="app-scroll-region relative z-10 min-h-0 flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}
