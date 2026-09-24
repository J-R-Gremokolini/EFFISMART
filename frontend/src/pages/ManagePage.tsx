import { useState, type FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { api, type DeliveryPoint, type Fluid, type Organization, type Site } from '../api';
import { Badge, Card, ErrorMessage, Loading } from '../components/ui';
import { formatDateTime, formatNumber } from '../format';
import { t } from '../i18n';
import { useApi } from '../useApi';

export function ManagePage() {
  const { orgId } = useParams();
  const org = useApi<Organization>(`/organizations/${orgId}`);
  const sites = useApi<Site[]>(`/organizations/${orgId}/sites`);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(task: () => Promise<unknown>, success?: string) {
    setError(null);
    setMessage(null);
    try {
      await task();
      if (success) setMessage(success);
      sites.reload();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  }

  return (
    <div className="stack">
      <h1>{t.manage.title}{org.data ? ` — ${org.data.name}` : ''}</h1>
      <ErrorMessage message={sites.error ?? error} />
      {message && <p className="alert alert-success">{message}</p>}
      {sites.loading && !sites.data && <Loading />}

      {sites.data?.map((site) => (
        <Card key={site.id} title={site.name}
              actions={site.is_tertiary_decret ? <Badge tone="info">{t.manage.tertiary}</Badge> : undefined}>
          <p className="muted small">
            {site.address ?? '—'}{site.surface_m2 ? ` · ${formatNumber(site.surface_m2)} m²` : ''}
          </p>
          <div className="points">
            {site.delivery_points.map((dp) => <PointRow key={dp.id} dp={dp} run={run} />)}
          </div>
          <AddPointForm siteId={site.id} run={run} />
        </Card>
      ))}

      <div className="grid-2">
        <Card title={t.manage.addSite}><AddSiteForm orgId={orgId!} run={run} /></Card>
        <Card title={t.manage.viewers}><AddViewerForm orgId={orgId!} run={run} /></Card>
      </div>
    </div>
  );
}

type Run = (task: () => Promise<unknown>, success?: string) => Promise<boolean>;

function PointRow({ dp, run }: { dp: DeliveryPoint; run: Run }) {
  const [authorized, setAuthorized] = useState(false);
  const [proof, setProof] = useState('');

  function grant(event: FormEvent) {
    event.preventDefault();
    run(
      () => api.post(`/delivery-points/${dp.id}/consents`, { client_authorized: authorized, proof_ref: proof }),
      t.manage.backfillStarted,
    );
  }

  return (
    <div className="point">
      <div className="point-head">
        <strong>{t.fluids[dp.fluid]} · {dp.external_ref}</strong>
        {dp.is_primary && <Badge tone="neutral">{t.manage.primary}</Badge>}
        {dp.subscribed_power_kva && <span className="muted small">{t.manage.subscribed} : {dp.subscribed_power_kva} kVA</span>}
        <span className="spacer" />
        {dp.has_active_consent && dp.active_consent ? (
          <>
            <Badge tone="success">{t.manage.consent} : {t.manage.consentActive}</Badge>
            <span className="muted small">{formatDateTime(dp.active_consent.granted_at)} · {dp.active_consent.proof_ref}</span>
            <button className="btn btn-small btn-ghost" onClick={() => run(() => api.post(`/consents/${dp.active_consent!.id}/revoke`))}>
              {t.manage.revoke}
            </button>
          </>
        ) : (
          <Badge tone="warning">{t.manage.consentMissing}</Badge>
        )}
      </div>
      {!dp.has_active_consent && (
        <form className="consent-step" onSubmit={grant}>
          <strong>{t.manage.consentStep}</strong>
          <label className="check">
            <input type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} required />
            {t.manage.consentCheckbox}
          </label>
          <label>{t.manage.proofRef}
            <input value={proof} onChange={(e) => setProof(e.target.value)} required placeholder="ex. mandat signé n°…" />
          </label>
          <button className="btn btn-primary btn-small" type="submit" disabled={!authorized}>{t.manage.grant}</button>
        </form>
      )}
    </div>
  );
}

function AddPointForm({ siteId, run }: { siteId: number; run: Run }) {
  const [form, setForm] = useState({ external_ref: '', fluid: 'ELEC' as Fluid, is_primary: false });
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (await run(() => api.post(`/sites/${siteId}/delivery-points`, form))) {
      setForm({ external_ref: '', fluid: 'ELEC', is_primary: false });
    }
  }
  return (
    <form className="inline-form" onSubmit={submit}>
      <label>{t.manage.ref}
        <input value={form.external_ref} pattern="\d{14}" onChange={(e) => setForm({ ...form, external_ref: e.target.value })} required />
      </label>
      <label>{t.manage.fluid}
        <select value={form.fluid} onChange={(e) => setForm({ ...form, fluid: e.target.value as Fluid })}>
          <option value="ELEC">{t.fluids.ELEC}</option>
          <option value="GAS">{t.fluids.GAS}</option>
        </select>
      </label>
      <label className="check">
        <input type="checkbox" checked={form.is_primary} onChange={(e) => setForm({ ...form, is_primary: e.target.checked })} />
        {t.manage.primary}
      </label>
      <button className="btn btn-secondary btn-small" type="submit">{t.manage.addPoint}</button>
    </form>
  );
}

function AddSiteForm({ orgId, run }: { orgId: string; run: Run }) {
  const empty = { name: '', address: '', surface_m2: '', is_tertiary_decret: false };
  const [form, setForm] = useState(empty);
  async function submit(event: FormEvent) {
    event.preventDefault();
    const body = {
      name: form.name,
      address: form.address || null,
      surface_m2: form.surface_m2 ? Number(form.surface_m2) : null,
      is_tertiary_decret: form.is_tertiary_decret,
    };
    if (await run(() => api.post(`/organizations/${orgId}/sites`, body))) setForm(empty);
  }
  return (
    <form className="form" onSubmit={submit}>
      <label>{t.manage.siteName}<input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></label>
      <label>{t.manage.address}<input value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} /></label>
      <label>{t.manage.surface}
        <input type="number" min="1" value={form.surface_m2} onChange={(e) => setForm({ ...form, surface_m2: e.target.value })} />
      </label>
      <label className="check">
        <input type="checkbox" checked={form.is_tertiary_decret} onChange={(e) => setForm({ ...form, is_tertiary_decret: e.target.checked })} />
        {t.manage.tertiary}
      </label>
      <button className="btn btn-primary" type="submit">{t.common.create}</button>
    </form>
  );
}

function AddViewerForm({ orgId, run }: { orgId: string; run: Run }) {
  const [form, setForm] = useState({ email: '', password: '' });
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (await run(() => api.post(`/organizations/${orgId}/viewers`, form), t.manage.viewerCreated)) {
      setForm({ email: '', password: '' });
    }
  }
  return (
    <form className="form" onSubmit={submit}>
      <label>{t.manage.viewerEmail}<input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required /></label>
      <label>{t.manage.viewerPassword}
        <input type="password" minLength={8} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
      </label>
      <button className="btn btn-secondary" type="submit">{t.manage.createViewer}</button>
    </form>
  );
}
