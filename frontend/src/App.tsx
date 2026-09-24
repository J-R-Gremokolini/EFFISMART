import type { ReactNode } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { useAuth } from './auth';
import { Layout } from './components/Layout';
import { Loading } from './components/ui';
import { t } from './i18n';
import { DashboardPage } from './pages/DashboardPage';
import { DriftsPage } from './pages/DriftsPage';
import { ExportsPage } from './pages/ExportsPage';
import { LoginPage } from './pages/LoginPage';
import { ManagePage } from './pages/ManagePage';
import { NewClientPage } from './pages/NewClientPage';
import { PortfolioPage } from './pages/PortfolioPage';
import { RegulatoryPage } from './pages/RegulatoryPage';

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="content"><Loading /></div>;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Réservé à l'auditeur : un client est renvoyé vers son propre tableau de bord. */
function AuditorOnly({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  if (user?.role === 'CLIENT_VIEWER') return <Navigate to={`/organizations/${user.organization_id}`} replace />;
  return <>{children}</>;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<AuditorOnly><PortfolioPage /></AuditorOnly>} />
        <Route path="regulatory" element={<AuditorOnly><RegulatoryPage /></AuditorOnly>} />
        <Route path="clients/new" element={<AuditorOnly><NewClientPage /></AuditorOnly>} />
        <Route path="organizations/:orgId" element={<DashboardPage />} />
        <Route path="organizations/:orgId/drifts" element={<DriftsPage />} />
        <Route path="organizations/:orgId/exports" element={<ExportsPage />} />
        <Route path="organizations/:orgId/manage" element={<AuditorOnly><ManagePage /></AuditorOnly>} />
        <Route path="*" element={<p>{t.notFound}</p>} />
      </Route>
    </Routes>
  );
}
