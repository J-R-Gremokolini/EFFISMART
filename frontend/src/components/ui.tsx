import type { ReactNode } from 'react';
import { formatDate } from '../format';
import { t } from '../i18n';

export function Loading() {
  return <p className="muted">{t.common.loading}</p>;
}

export function ErrorMessage({ message }: { message: string | null }) {
  if (!message) return null;
  return <p className="alert alert-error" role="alert">{message}</p>;
}

export function DataAsOfBanner({ date }: { date: string | null }) {
  if (!date) return <p className="alert alert-warning">{t.noData}</p>;
  return <p className="alert alert-info">{t.dataAsOf(formatDate(date))}</p>;
}

export function Kpi({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: 'elec' | 'gas' }) {
  return (
    <div className={`kpi ${tone ? `kpi-${tone}` : ''}`}>
      <span className="kpi-label">{label}</span>
      <span className="kpi-value">{value}</span>
      {hint && <span className="kpi-hint">{hint}</span>}
    </div>
  );
}

export function Card({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          {title && <h2>{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

type Tone = 'neutral' | 'info' | 'warning' | 'danger' | 'success';

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export const DRIFT_STATUS_TONE: Record<string, Tone> = { OPEN: 'danger', QUALIFIED: 'info', IGNORED: 'neutral' };
export const DEADLINE_STATUS_TONE: Record<string, Tone> = { UPCOMING: 'neutral', DUE_SOON: 'warning', DONE: 'success' };
