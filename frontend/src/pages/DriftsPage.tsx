import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { api, type Drift, type DriftStatus } from '../api';
import { useCanWrite } from '../auth';
import { Badge, Card, DRIFT_STATUS_TONE, ErrorMessage, Loading } from '../components/ui';
import { formatDate, formatNumber, formatPct } from '../format';
import { t } from '../i18n';
import { useApi } from '../useApi';

const FILTERS: (DriftStatus | '')[] = ['', 'OPEN', 'QUALIFIED', 'IGNORED'];

export function DriftsPage() {
  const { orgId } = useParams();
  const canWrite = useCanWrite();
  const [filter, setFilter] = useState<DriftStatus | ''>('OPEN');
  const { data, error, loading, reload } = useApi<Drift[]>(
    `/organizations/${orgId}/drifts${filter ? `?status=${filter}` : ''}`,
  );
  const [comments, setComments] = useState<Record<number, string>>({});
  const [actionError, setActionError] = useState<string | null>(null);

  async function update(drift: Drift, status: DriftStatus) {
    setActionError(null);
    try {
      await api.patch(`/drifts/${drift.id}`, { status, comment: comments[drift.id] ?? drift.comment });
      reload();
    } catch (e) {
      setActionError((e as Error).message);
    }
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{t.drifts.title}</h1>
        <div className="period-selector">
          {FILTERS.map((f) => (
            <button key={f || 'all'} className={`chip ${filter === f ? 'chip-active' : ''}`} onClick={() => setFilter(f)}>
              {f ? t.drifts.statuses[f] : t.drifts.filterAll}
            </button>
          ))}
        </div>
      </div>
      <p className="hint">{t.drifts.hint}</p>
      <ErrorMessage message={error ?? actionError} />

      <Card>
        {loading && !data && <Loading />}
        {data && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t.drifts.day}</th>
                  <th>{t.drifts.kind}</th>
                  <th>{t.drifts.point}</th>
                  <th className="num">{t.drifts.deviation}</th>
                  <th>{t.drifts.details}</th>
                  <th>{t.drifts.status}</th>
                  <th>{t.drifts.comment}</th>
                  {canWrite && <th />}
                </tr>
              </thead>
              <tbody>
                {data.length === 0 && <tr><td colSpan={8} className="muted">{t.common.none}</td></tr>}
                {data.map((d) => (
                  <tr key={d.id}>
                    <td>{formatDate(d.day)}</td>
                    <td>{t.drifts.kinds[d.kind]}</td>
                    <td>
                      {d.site_name}
                      <div className="muted small">{t.fluids[d.fluid]} {d.external_ref}</div>
                    </td>
                    <td className="num">
                      <strong>{formatPct(d.deviation_pct)}</strong>
                      <div className="muted small">
                        {formatNumber(d.measured_value)} / {formatNumber(d.reference_value)} {d.unit}
                      </div>
                    </td>
                    <td>{d.details}</td>
                    <td><Badge tone={DRIFT_STATUS_TONE[d.status]}>{t.drifts.statuses[d.status]}</Badge></td>
                    <td>
                      {canWrite ? (
                        <input
                          className="input-small"
                          value={comments[d.id] ?? d.comment ?? ''}
                          onChange={(e) => setComments({ ...comments, [d.id]: e.target.value })}
                          placeholder={t.drifts.comment}
                        />
                      ) : (d.comment ?? '—')}
                    </td>
                    {canWrite && (
                      <td className="actions">
                        {d.status === 'OPEN' ? (
                          <>
                            <button className="btn btn-small btn-primary" onClick={() => update(d, 'QUALIFIED')}>{t.drifts.qualify}</button>
                            <button className="btn btn-small btn-ghost" onClick={() => update(d, 'IGNORED')}>{t.drifts.ignore}</button>
                          </>
                        ) : (
                          <button className="btn btn-small btn-ghost" onClick={() => update(d, 'OPEN')}>{t.drifts.reopen}</button>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
