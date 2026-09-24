import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { LoadCurve } from '../api';
import { formatDate, formatEnergy, formatMonth, formatNumber } from '../format';
import { locale, t } from '../i18n';

export const FLUID_COLORS = { ELEC: '#2563eb', GAS: '#d97706' };
const GRID = '#e5e7eb';
const AXIS = { fontSize: 12, fill: '#6b7280' };

function formatSlot(iso: string, withTime: boolean): string {
  if (!withTime) return formatDate(iso);
  const d = new Date(iso);
  return d.toLocaleString(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export function LoadCurveChart({ curve, color }: { curve: LoadCurve; color: string }) {
  const halfHourly = curve.step === 'PT30M';
  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={curve.points} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis
          dataKey="t"
          tick={AXIS}
          minTickGap={48}
          tickFormatter={(v: string) => (halfHourly ? formatSlot(v, true).slice(0, 5) : formatDate(v).slice(0, 5))}
        />
        <YAxis tick={AXIS} width={56} tickFormatter={(v: number) => formatNumber(v)} unit={` ${curve.unit}`} />
        <Tooltip
          labelFormatter={(v: string) => formatSlot(v, halfHourly)}
          formatter={(v: number) => [`${formatNumber(v, 1)} ${curve.unit}`, halfHourly ? 'Puissance moyenne' : 'Consommation']}
        />
        <Line type="monotone" dataKey="value" stroke={color} strokeWidth={1.5} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function MonthlyChart({ data }: { data: { month: string; elec_kwh: number; gas_kwh: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="month" tick={AXIS} tickFormatter={formatMonth} />
        <YAxis tick={AXIS} width={64} tickFormatter={(v: number) => formatEnergy(v)} />
        <Tooltip labelFormatter={formatMonth} formatter={(v: number, name: string) => [formatEnergy(v), name]} />
        <Legend />
        <Bar dataKey="elec_kwh" name={t.fluids.ELEC} stackId="a" fill={FLUID_COLORS.ELEC} />
        <Bar dataKey="gas_kwh" name={t.fluids.GAS} stackId="a" fill={FLUID_COLORS.GAS} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function SplitChart({ elec, gas }: { elec: number; gas: number }) {
  const data = [
    { name: t.fluids.ELEC, value: elec, color: FLUID_COLORS.ELEC },
    { name: t.fluids.GAS, value: gas, color: FLUID_COLORS.GAS },
  ].filter((d) => d.value > 0);
  if (data.length === 0) return <p className="muted">{t.common.none}</p>;
  const total = elec + gas;
  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius={60} outerRadius={95} paddingAngle={2}
             label={({ value }: { value: number }) => `${formatNumber((value / total) * 100)} %`}>
          {data.map((d) => <Cell key={d.name} fill={d.color} />)}
        </Pie>
        <Tooltip formatter={(v: number) => formatEnergy(v)} />
        <Legend />
      </PieChart>
    </ResponsiveContainer>
  );
}
