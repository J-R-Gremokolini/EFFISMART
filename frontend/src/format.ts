import { locale } from './i18n';

const nf0 = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
const eur = new Intl.NumberFormat(locale, { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 });

export function formatEnergy(kwh: number): string {
  if (Math.abs(kwh) >= 10_000) return `${nf1.format(kwh / 1000)} MWh`;
  return `${nf0.format(kwh)} kWh`;
}

export function formatEmissions(kg: number): string {
  if (Math.abs(kg) >= 1000) return `${nf1.format(kg / 1000)} tCO₂e`;
  return `${nf0.format(kg)} kgCO₂e`;
}

export const formatEur = (value: number) => eur.format(value);
export const formatNumber = (value: number, digits = 0) =>
  new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(value);

export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${value > 0 ? '+' : ''}${nf1.format(value)} %`;
}

/** Date ISO « AAAA-MM-JJ » → « JJ/MM/AAAA » sans décalage de fuseau. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const [y, m, d] = iso.slice(0, 10).split('-');
  return `${d}/${m}/${y}`;
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(locale, {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export function formatMonth(yyyyMm: string): string {
  const [y, m] = yyyyMm.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(locale, { month: 'short', year: '2-digit' });
}

export function toIsoDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}
