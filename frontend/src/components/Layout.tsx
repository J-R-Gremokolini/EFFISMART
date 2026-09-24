import { NavLink, Outlet, useMatch } from 'react-router-dom';
import { useAuth, useCanWrite } from '../auth';
import { t } from '../i18n';

export function Layout() {
  const { user, logout } = useAuth();
  const canWrite = useCanWrite();
  const orgMatch = useMatch('/organizations/:orgId/*');
  const orgId = orgMatch?.params.orgId;
  const isClient = user?.role === 'CLIENT_VIEWER';

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden>⚡</span>
          <span className="brand-name">{t.app.name}</span>
          <span className="brand-tagline">{t.app.tagline}</span>
        </div>
        <nav className="main-nav">
          {!isClient && <NavLink to="/" end>{t.nav.portfolio}</NavLink>}
          {!isClient && <NavLink to="/regulatory">{t.nav.regulatory}</NavLink>}
          {canWrite && <NavLink to="/clients/new">{t.nav.newClient}</NavLink>}
        </nav>
        <div className="user-box">
          <span className="user-email">{user?.email}</span>
          <span className="badge badge-neutral">{user ? t.roles[user.role] : ''}</span>
          {isClient && <span className="badge badge-info">{t.common.readOnly}</span>}
          <button className="btn btn-ghost" onClick={logout}>{t.auth.logout}</button>
        </div>
      </header>

      {orgId && (
        <nav className="sub-nav">
          <NavLink to={`/organizations/${orgId}`} end>{t.nav.dashboard}</NavLink>
          <NavLink to={`/organizations/${orgId}/drifts`}>{t.nav.drifts}</NavLink>
          <NavLink to={`/organizations/${orgId}/exports`}>{t.nav.exports}</NavLink>
          {canWrite && <NavLink to={`/organizations/${orgId}/manage`}>{t.nav.manage}</NavLink>}
        </nav>
      )}

      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
