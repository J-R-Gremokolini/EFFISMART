import { Link } from 'react-router-dom';
import type { Notification, PortfolioRow } from '../api';
import { Badge, Card, ErrorMessage, Loading } from '../components/ui';
import { formatDate, formatDateTime, formatEnergy, formatMonth, formatPct } from '../format';
import { t } from '../i18n';
import { useApi } from '../useApi';

export function PortfolioPage() {
  const portfolio = useApi<PortfolioRow[]>('/portfolio');
  const notifications = useApi<Notification[]>('/notifications?limit=10');
  const month = portfolio.data?.[0]?.month;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{t.portfolio.title}</h1>
        <Link className="btn btn-primary" to="/clients/new">{t.nav.newClient}</Link>
      </div>

      <Card>
        {portfolio.loading && !portfolio.data && <Loading />}
        <ErrorMessage message={portfolio.error} />
        {portfolio.data && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t.portfolio.organization}</th>
                  <th className="num">{t.portfolio.sites}</th>
                  <th className="num">{t.portfolio.lastMonth}{month ? ` (${formatMonth(month)})` : ''}</th>
                  <th className="num">{t.portfolio.variation}</th>
                  <th className="num">{t.portfolio.openDrifts}</th>
                  <th>{t.portfolio.dataAsOf}</th>
                </tr>
              </thead>
              <tbody>
                {portfolio.data.length === 0 && (
                  <tr><td colSpan={6} className="muted">{t.common.none}</td></tr>
                )}
                {portfolio.data.map((row) => (
                  <tr key={row.organization_id}>
                    <td><Link to={`/organizations/${row.organization_id}`}>{row.name}</Link></td>
                    <td className="num">{row.sites_count}</td>
                    <td className="num">{formatEnergy(row.last_month_kwh)}</td>
                    <td className={`num ${row.variation_pct !== null && row.variation_pct > 0 ? 'up' : 'down'}`}>
                      {formatPct(row.variation_pct)}
                    </td>
                    <td className="num">
                      {row.open_drifts > 0
                        ? <Link to={`/organizations/${row.organization_id}/drifts`}><Badge tone="danger">{row.open_drifts}</Badge></Link>
                        : <Badge tone="success">0</Badge>}
                    </td>
                    <td>{formatDate(row.data_as_of)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title={t.portfolio.notifications}>
        <ErrorMessage message={notifications.error} />
        {notifications.data?.length === 0 && <p className="muted">{t.common.none}</p>}
        <ul className="feed">
          {notifications.data?.map((n) => (
            <li key={n.id}>
              <span className="feed-date">{formatDateTime(n.created_at)}</span>
              {n.organization_id
                ? <Link to={`/organizations/${n.organization_id}/drifts`}>{n.message}</Link>
                : n.message}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
