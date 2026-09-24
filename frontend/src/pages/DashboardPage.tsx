import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import type { Dashboard, Drift, LoadCurve, Period } from '../api';
import { FLUID_COLORS, LoadCurveChart, MonthlyChart, SplitChart } from '../components/charts';
import { Badge, Card, DataAsOfBanner, ErrorMessage, Kpi, Loading } from '../components/ui';
import { formatDate, formatEmissions, formatEnergy, formatEur, formatNumber, formatPct } from '../format';
import { t } from '../i18n';
import { useApi } from '../useApi';

const PRESETS: Period[] = ['7d', '30d', '12m', 'custom'];

export function DashboardPage() {
  const { orgId } = useParams();
  const [period, setPeriod] = useState<Period>('30d');
  const [custom, setCustom] = useState({ start: '', end: '' });
  const [appliedCustom, setAppliedCustom] = useState({ start: '', end: '' });

  const dashboardPath = useMemo(() => {
    const params = new URLSearchParams({ period });
    if (period === 'custom') {
      if (!appliedCustom.start || !appliedCustom.end) return null;
      params.set('start', appliedCustom.start);
      params.set('end', appliedCustom.end);
    }
    return `/organizations/${orgId}/dashboard?${params}`;
  }, [orgId, period, appliedCustom]);

  const { data, error, loading } = useApi<Dashboard>(dashboardPath);
  const drifts = useApi<Drift[]>(`/organizations/${orgId}/drifts?status=OPEN`);

  const consentedPoints = useMemo(
    () => data?.delivery_points.filter((p) => p.has_active_consent) ?? [],
    [data],
  );
  const [pointId, setPointId] = useState<number | null>(null);
  useEffect(() => {
    if (consentedPoints.length && !consentedPoints.some((p) => p.id === pointId)) {
      setPointId((consentedPoints.find((p) => p.fluid === 'ELEC') ?? consentedPoints[0]).id);
    }
  }, [consentedPoints, pointId]);

  const curvePath = data && pointId
    ? `/delivery-points/${pointId}/load-curve?start=${data.period.start}&end=${data.period.end}`
    : null;
  const curve = useApi<LoadCurve>(curvePath);
  const selectedPoint = consentedPoints.find((p) => p.id === pointId);

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{data?.organization.name ?? t.nav.dashboard}</h1>
        <div className="period-selector" role="group" aria-label={t.dashboard.period}>
          {PRESETS.map((p) => (
            <button key={p} className={`chip ${period === p ? 'chip-active' : ''}`} onClick={() => setPeriod(p)}>
              {t.dashboard.presets[p]}
            </button>
          ))}
        </div>
      </div>

      {period === 'custom' && (
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); setAppliedCustom(custom); }}>
          <label>{t.dashboard.from}
            <input type="date" value={custom.start} onChange={(e) => setCustom({ ...custom, start: e.target.value })} required />
          </label>
          <label>{t.dashboard.to}
            <input type="date" value={custom.end} onChange={(e) => setCustom({ ...custom, end: e.target.value })} required />
          </label>
          <button className="btn btn-secondary" type="submit">{t.dashboard.apply}</button>
        </form>
      )}

      <ErrorMessage message={error} />
      {loading && !data && <Loading />}

      {data && (
        <>
          <DataAsOfBanner date={data.data_as_of} />
          <p className="muted">
            {t.dashboard.period} : {formatDate(data.period.start)} → {formatDate(data.period.end)}
          </p>

          <div className="kpi-grid">
            <Kpi label={t.dashboard.total} value={formatEnergy(data.totals.total_kwh)} />
            <Kpi label={t.dashboard.elec} value={formatEnergy(data.totals.elec_kwh)} tone="elec" />
            <Kpi label={t.dashboard.gas} value={formatEnergy(data.totals.gas_kwh)} tone="gas" />
            <Kpi label={t.dashboard.cost} value={formatEur(data.totals.cost_eur)} hint={t.dashboard.costNote} />
            <Kpi label={t.dashboard.emissions} value={formatEmissions(data.totals.emissions_kgco2e)}
                 hint={data.emission_factors.map((f) => f.version).filter((v, i, a) => a.indexOf(v) === i).join(', ')} />
          </div>

          <Card
            title={curve.data?.step === 'P1D' ? t.dashboard.loadCurveDaily : t.dashboard.loadCurve}
            actions={
              <select value={pointId ?? ''} onChange={(e) => setPointId(Number(e.target.value))}
                      aria-label={t.dashboard.deliveryPoint}>
                {data.delivery_points.map((p) => (
                  <option key={p.id} value={p.id} disabled={!p.has_active_consent}>
                    {p.site_name} — {t.fluids[p.fluid]} {p.external_ref}
                    {p.has_active_consent ? '' : ` (${t.dashboard.consentMissing})`}
                  </option>
                ))}
              </select>
            }
          >
            <ErrorMessage message={curve.error} />
            {curve.loading && !curve.data && <Loading />}
            {curve.data && selectedPoint && (
              <>
                <LoadCurveChart curve={curve.data} color={FLUID_COLORS[selectedPoint.fluid]} />
                {curve.data.aggregated && <p className="hint">{t.dashboard.aggregatedNote}</p>}
              </>
            )}
          </Card>

          <div className="grid-2">
            <Card title={t.dashboard.monthly}>
              <MonthlyChart data={data.monthly} />
            </Card>
            <Card title={t.dashboard.split}>
              <SplitChart elec={data.totals.elec_kwh} gas={data.totals.gas_kwh} />
            </Card>
          </div>

          <Card
            title={`${t.dashboard.recentDrifts} (${data.open_drifts})`}
            actions={<Link to={`/organizations/${orgId}/drifts`}>{t.dashboard.seeAll}</Link>}
          >
            {drifts.data?.length === 0 && <p className="muted">{t.common.none}</p>}
            <ul className="feed">
              {drifts.data?.slice(0, 5).map((d) => (
                <li key={d.id}>
                  <span className="feed-date">{formatDate(d.day)}</span>
                  <Badge tone="danger">{t.drifts.kinds[d.kind]}</Badge>
                  <span>{d.site_name} — {d.details}</span>
                  <strong>{formatPct(d.deviation_pct)}</strong>
                </li>
              ))}
            </ul>
          </Card>

          <p className="hint">
            {t.dashboard.factorsNote} :{' '}
            {data.emission_factors.map((f) => (
              `${t.fluids[f.fluid]} ${formatNumber(f.factor_kgco2_per_kwh, 3)} kgCO₂e/kWh (${f.source}, ${f.version})`
            )).join(' · ')}
          </p>
        </>
      )}
    </div>
  );
}
