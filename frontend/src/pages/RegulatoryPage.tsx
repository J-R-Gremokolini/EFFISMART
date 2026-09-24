import { useMemo, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api, type ActionLog, type Deadline, type Obligation, type Organization } from '../api';
import { Badge, Card, DEADLINE_STATUS_TONE, ErrorMessage, Loading } from '../components/ui';
import { formatDate, formatDateTime } from '../format';
import { t } from '../i18n';
import { useApi } from '../useApi';

const OBLIGATIONS = Object.keys(t.regulatory.obligations) as Obligation[];

export function RegulatoryPage() {
  const [orgFilter, setOrgFilter] = useState('');
  const organizations = useApi<Organization[]>('/organizations');
  const deadlines = useApi<Deadline[]>(`/regulatory/deadlines${orgFilter ? `?organization_id=${orgFilter}` : ''}`);
  const [actionError, setActionError] = useState<string | null>(null);

  const sites = useMemo(() => {
    const map = new Map<number, string>();
    deadlines.data?.forEach((d) => map.set(d.site_id, `${d.organization_name} — ${d.site_name}`));
    return [...map.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [deadlines.data]);

  const [siteId, setSiteId] = useState<number | null>(null);
  const actions = useApi<ActionLog[]>(siteId ? `/sites/${siteId}/actions` : null);

  const [newDeadline, setNewDeadline] = useState({ site_id: '', obligation: 'AUDIT_EED' as Obligation, due_date: '', notes: '' });
  const [newAction, setNewAction] = useState({ obligation: 'DECRET_TERTIAIRE_OPERAT' as Obligation, description: '' });

  async function run(task: () => Promise<unknown>, after: () => void) {
    setActionError(null);
    try {
      await task();
      after();
    } catch (e) {
      setActionError((e as Error).message);
    }
  }

  function setStatus(deadline: Deadline, status: 'DONE' | 'UPCOMING') {
    run(() => api.patch(`/deadlines/${deadline.id}`, { status }), () => { deadlines.reload(); actions.reload(); });
  }

  function onAddDeadline(event: FormEvent) {
    event.preventDefault();
    run(
      () => api.post(`/sites/${newDeadline.site_id}/deadlines`, {
        obligation: newDeadline.obligation, due_date: newDeadline.due_date, notes: newDeadline.notes || null,
      }),
      () => { setNewDeadline({ ...newDeadline, due_date: '', notes: '' }); deadlines.reload(); },
    );
  }

  function onLogAction(event: FormEvent) {
    event.preventDefault();
    run(
      () => api.post(`/sites/${siteId}/actions`, newAction),
      () => { setNewAction({ ...newAction, description: '' }); actions.reload(); },
    );
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{t.regulatory.title}</h1>
        <select value={orgFilter} onChange={(e) => setOrgFilter(e.target.value)} aria-label={t.regulatory.organization}>
          <option value="">{t.regulatory.allOrganizations}</option>
          {organizations.data?.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
        </select>
      </div>
      <ErrorMessage message={deadlines.error ?? actionError} />

      <Card>
        {deadlines.loading && !deadlines.data && <Loading />}
        {deadlines.data && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t.regulatory.dueDate}</th>
                  <th>{t.regulatory.obligation}</th>
                  <th>{t.regulatory.site}</th>
                  <th className="num">{t.regulatory.daysLeft}</th>
                  <th>{t.regulatory.status}</th>
                  <th>{t.regulatory.notes}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {deadlines.data.length === 0 && <tr><td colSpan={7} className="muted">{t.common.none}</td></tr>}
                {deadlines.data.map((d) => (
                  <tr key={d.id} className={d.status === 'DONE' ? 'row-done' : ''}>
                    <td>{formatDate(d.due_date)}</td>
                    <td>{t.regulatory.obligations[d.obligation]}</td>
                    <td>
                      <Link to={`/organizations/${d.organization_id}`}>{d.organization_name}</Link>
                      <div className="muted small">{d.site_name}</div>
                    </td>
                    <td className="num">
                      {d.status === 'DONE' ? '—' : d.days_left < 0
                        ? <Badge tone="danger">{t.regulatory.overdue}</Badge>
                        : d.days_left}
                    </td>
                    <td><Badge tone={DEADLINE_STATUS_TONE[d.status]}>{t.regulatory.statuses[d.status]}</Badge></td>
                    <td className="small">{d.notes}</td>
                    <td className="actions">
                      {d.status === 'DONE'
                        ? <button className="btn btn-small btn-ghost" onClick={() => setStatus(d, 'UPCOMING')}>{t.regulatory.reopen}</button>
                        : <button className="btn btn-small btn-primary" onClick={() => setStatus(d, 'DONE')}>{t.regulatory.markDone}</button>}
                      <button className="btn btn-small btn-ghost" onClick={() => setSiteId(d.site_id)}>{t.regulatory.actions}</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid-2">
        <Card title={t.regulatory.addDeadline}>
          <form className="form" onSubmit={onAddDeadline}>
            <label>{t.regulatory.site}
              <select value={newDeadline.site_id} onChange={(e) => setNewDeadline({ ...newDeadline, site_id: e.target.value })} required>
                <option value="" disabled>—</option>
                {sites.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
              </select>
            </label>
            <label>{t.regulatory.obligation}
              <select value={newDeadline.obligation} onChange={(e) => setNewDeadline({ ...newDeadline, obligation: e.target.value as Obligation })}>
                {OBLIGATIONS.map((o) => <option key={o} value={o}>{t.regulatory.obligations[o]}</option>)}
              </select>
            </label>
            <label>{t.regulatory.dueDate}
              <input type="date" value={newDeadline.due_date} onChange={(e) => setNewDeadline({ ...newDeadline, due_date: e.target.value })} required />
            </label>
            <label>{t.regulatory.notes}
              <input value={newDeadline.notes} onChange={(e) => setNewDeadline({ ...newDeadline, notes: e.target.value })} />
            </label>
            <button className="btn btn-primary" type="submit">{t.common.create}</button>
          </form>
        </Card>

        <Card title={t.regulatory.actions}>
          <select value={siteId ?? ''} onChange={(e) => setSiteId(Number(e.target.value) || null)} aria-label={t.regulatory.site}>
            <option value="">—</option>
            {sites.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
          {!siteId && <p className="muted">{t.regulatory.selectSite}</p>}
          {siteId && (
            <>
              <form className="form" onSubmit={onLogAction}>
                <label>{t.regulatory.obligation}
                  <select value={newAction.obligation} onChange={(e) => setNewAction({ ...newAction, obligation: e.target.value as Obligation })}>
                    {OBLIGATIONS.map((o) => <option key={o} value={o}>{t.regulatory.obligations[o]}</option>)}
                  </select>
                </label>
                <label>{t.regulatory.description}
                  <input value={newAction.description} onChange={(e) => setNewAction({ ...newAction, description: e.target.value })} required />
                </label>
                <button className="btn btn-secondary" type="submit">{t.regulatory.logAction}</button>
              </form>
              <ul className="feed">
                {actions.data?.length === 0 && <li className="muted">{t.common.none}</li>}
                {actions.data?.map((a) => (
                  <li key={a.id}>
                    <span className="feed-date">{formatDateTime(a.performed_at)}</span>
                    <Badge tone="neutral">{t.regulatory.obligations[a.obligation]}</Badge>
                    <span>{a.description}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}
