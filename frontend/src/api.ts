// Client HTTP de l'API EffiSmart et types des réponses.

export type Role = 'AUDITOR' | 'CLIENT_VIEWER' | 'ADMIN';
export type Fluid = 'ELEC' | 'GAS';
export type DriftKind = 'THRESHOLD' | 'CLIMATE_DEVIATION' | 'BASELOAD';
export type DriftStatus = 'OPEN' | 'QUALIFIED' | 'IGNORED';
export type Obligation = 'DECRET_TERTIAIRE_OPERAT' | 'AUDIT_EED' | 'VSME';
export type DeadlineStatus = 'UPCOMING' | 'DUE_SOON' | 'DONE';
export type ExportFormat = 'OPERAT' | 'VSME';
export type Period = '7d' | '30d' | '12m' | 'custom';

export interface User {
  id: number;
  email: string;
  role: Role;
  auditor_id: number | null;
  organization_id: number | null;
}

export interface Organization {
  id: number;
  name: string;
  siren: string | null;
  address: string | null;
}

export interface PortfolioRow {
  organization_id: number;
  name: string;
  sites_count: number;
  delivery_points_count: number;
  month: string;
  last_month_kwh: number;
  previous_month_kwh: number;
  variation_pct: number | null;
  open_drifts: number;
  data_as_of: string | null;
}

export interface DashboardPoint {
  id: number;
  site_name: string;
  fluid: Fluid;
  external_ref: string;
  is_primary: boolean;
  has_active_consent: boolean;
}

export interface Dashboard {
  organization: { id: number; name: string; siren: string | null };
  data_as_of: string | null;
  period: { start: string; end: string };
  totals: { elec_kwh: number; gas_kwh: number; total_kwh: number; cost_eur: number; emissions_kgco2e: number };
  monthly: { month: string; elec_kwh: number; gas_kwh: number }[];
  delivery_points: DashboardPoint[];
  emission_factors: { fluid: Fluid; factor_kgco2_per_kwh: number; version: string; source: string; valid_from: string }[];
  estimated_prices_eur_kwh: Record<Fluid, number>;
  open_drifts: number;
}

export interface LoadCurve {
  step: 'PT30M' | 'P1D';
  unit: 'kW' | 'kWh';
  aggregated: boolean;
  points: { t: string; value: number }[];
}

export interface Consent {
  id: number;
  delivery_point_id: number;
  granted_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  scope: string;
  proof_ref: string;
}

export interface DeliveryPoint {
  id: number;
  site_id: number;
  fluid: Fluid;
  external_ref: string;
  provider: string;
  is_primary: boolean;
  subscribed_power_kva: number | null;
  has_active_consent: boolean;
  active_consent: Consent | null;
}

export interface Site {
  id: number;
  organization_id: number;
  name: string;
  address: string | null;
  surface_m2: number | null;
  is_tertiary_decret: boolean;
  delivery_points: DeliveryPoint[];
}

export interface Drift {
  id: number;
  delivery_point_id: number;
  external_ref: string;
  fluid: Fluid;
  site_name: string;
  kind: DriftKind;
  day: string;
  measured_value: number;
  reference_value: number;
  deviation_pct: number;
  unit: string;
  details: string;
  status: DriftStatus;
  comment: string | null;
  detected_at: string;
}

export interface Notification {
  id: number;
  drift_id: number | null;
  organization_id: number | null;
  message: string;
  created_at: string;
}

export interface Deadline {
  id: number;
  site_id: number;
  site_name: string;
  organization_id: number;
  organization_name: string;
  obligation: Obligation;
  due_date: string;
  status: DeadlineStatus;
  days_left: number;
  notes: string | null;
}

export interface ActionLog {
  id: number;
  site_id: number;
  obligation: Obligation;
  description: string;
  performed_at: string;
}

export interface EmissionFactorUsed {
  fluid: Fluid;
  factor_kgco2e_per_kwh: number;
  valid_from: string;
  version: string;
  source: string;
  applied_at: string;
}

export interface ExportJob {
  id: number;
  organization_id: number;
  format: ExportFormat;
  period_start: string;
  period_end: string;
  status: 'PENDING' | 'DONE' | 'FAILED';
  factors_used: EmissionFactorUsed[] | null;
  error: string | null;
  created_at: string;
}

// --- Transport -------------------------------------------------------------

const TOKEN_KEY = 'effismart.token';
export const LOGOUT_EVENT = 'effismart:logout';

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Stockage indisponible (navigation privée) : la session durera le temps de l'onglet.
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function send(method: string, path: string, body?: unknown): Promise<Response> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401 && token) {
    setToken(null);
    window.dispatchEvent(new Event(LOGOUT_EVENT));
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === 'string'
        ? payload.detail
        : (payload.detail ?? []).map((e: { msg: string }) => e.msg).join(' · ');
    } catch {
      // Réponse non JSON : on garde le libellé HTTP.
    }
    throw new ApiError(response.status, detail);
  }
  return response;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await send(method, path, body);
  return response.status === 204 ? (undefined as T) : response.json();
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  async download(path: string, fallbackName: string): Promise<void> {
    const response = await send('GET', path);
    const disposition = response.headers.get('Content-Disposition') ?? '';
    const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? fallbackName;
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
  },
};
