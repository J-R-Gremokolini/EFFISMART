import { useState, type FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { api, type ExportFormat, type ExportJob } from '../api';
import { useCanWrite } from '../auth';
import { Badge, Card, ErrorMessage, Loading } from '../components/ui';
import { formatDate, formatDateTime, formatNumber } from '../format';
import { t } from '../i18n';
import { useApi } from '../useApi';

const lastYear = new Date().getFullYear() - 1;

export function ExportsPage() {
  const { orgId } = useParams();
  const canWrite = useCanWrite();
  const { data, error, loading, reload } = useApi<ExportJob[]>(`/organizations/${orgId}/exports`);
  const [form, setForm] = useState({ start: `${lastYear}-01-01`, end: `${lastYear}-12-31` });
  const [formats, setFormats] = useState<Record<ExportFormat, boolean>>({ OPERAT: true, VSME: true });
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function onGenerate(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setActionError(null);
    try {
      await api.post(`/organizations/${orgId}/exports`, {
        period_start: form.start,
        period_end: form.end,
        formats: (Object.keys(formats) as ExportFormat[]).filter((f) => formats[f]),
      });
      reload();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  async function download(job: ExportJob) {
    setActionError(null);
    try {
      await api.download(`/exports/${job.id}/download`, `export_${job.format.toLowerCase()}_${job.id}.zip`);
    } catch (e) {
      setActionError((e as Error).message);
    }
  }

  return (
    <div className="stack">
      <h1>{t.exports.title}</h1>
      <p className="hint">{canWrite ? t.exports.hint : t.exports.clientHint}</p>
      <ErrorMessage message={error ?? actionError} />

      {canWrite && (
        <Card>
          <form className="inline-form" onSubmit={onGenerate}>
            <label>{t.exports.periodStart}
              <input type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} required />
            </label>
            <label>{t.exports.periodEnd}
              <input type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} required />
            </label>
            <fieldset className="checks">
              <legend>{t.exports.formats}</legend>
              {(Object.keys(formats) as ExportFormat[]).map((f) => (
                <label key={f} className="check">
                  <input type="checkbox" checked={formats[f]} onChange={(e) => setFormats({ ...formats, [f]: e.target.checked })} />
                  {t.exports.formatLabels[f]}
                </label>
              ))}
            </fieldset>
            <button className="btn btn-primary" type="submit" disabled={submitting || !Object.values(formats).some(Boolean)}>
              {t.exports.generate}
            </button>
          </form>
        </Card>
      )}

      <Card title={t.exports.history}>
        {loading && !data && <Loading />}
        {data && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t.exports.format}</th>
                  <th>{t.exports.period}</th>
                  <th>{t.exports.createdAt}</th>
                  <th>{t.exports.factors}</th>
                  <th>{t.drifts.status}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.length === 0 && <tr><td colSpan={6} className="muted">{t.common.none}</td></tr>}
                {data.map((job) => (
                  <tr key={job.id}>
                    <td>{t.exports.formatLabels[job.format]}</td>
                    <td>{formatDate(job.period_start)} → {formatDate(job.period_end)}</td>
                    <td>{formatDateTime(job.created_at)}</td>
                    <td className="small">
                      {job.factors_used?.map((f) => (
                        <div key={f.fluid}>
                          {t.fluids[f.fluid]} : {formatNumber(f.factor_kgco2e_per_kwh, 3)} kgCO₂e/kWh — {f.version}
                          {' '}(valide dès {formatDate(f.valid_from)})
                        </div>
                      ))}
                    </td>
                    <td>
                      <Badge tone={job.status === 'DONE' ? 'success' : job.status === 'FAILED' ? 'danger' : 'neutral'}>
                        {t.exports.statuses[job.status]}
                      </Badge>
                    </td>
                    <td>
                      {job.status === 'DONE' && (
                        <button className="btn btn-small btn-secondary" onClick={() => download(job)}>{t.common.download}</button>
                      )}
                    </td>
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
