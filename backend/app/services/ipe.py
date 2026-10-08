"""F7 — Indicateurs de performance énergétique (IPE) adaptés à la structure ; passerelle ISO 50001.

IPE au sens de la norme ISO 50001 : la consommation rapportée à ce qui l'explique.
- kWh/m² : surface du site ;
- kWh/unité produite : production mensuelle, fournie par le client, dans l'unité propre au site ;
- kWh/ETP : effectif moyen en équivalents temps plein, fourni par le client ;
- kWh/DJU : consommation liée au chauffage (mois de chauffe, moins le niveau des mois sans chauffage)
  rapportée aux degrés-jours de ces mois.
Chaque IPE est comparé à la situation énergétique de référence (SER) : l'année choisie pour le site, à défaut
l'année civile précédente. Consommations : toutes énergies, compteurs (N1) et factures (N0).

Passerelle ISO 50001 : ces IPE et leur SER sont les briques d'un système de management de l'énergie ; une
entreprise certifiée ISO 50001 est exemptée de l'audit énergétique obligatoire tous les 4 ans.
Les variables d'ajustement sont saisies par l'auditeur ou par le responsable énergie du client ; un simple
compte client les consulte.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdjustmentKind, AdjustmentVariable, Organization, Role, Site, User
from app.providers.weather import WeatherProvider, WeatherUnavailableError
from app.repositories import TenantRepository
from app.services.energy_data import combined_daily
from app.timeutils import add_months, month_start, today_local, utcnow

KIND_LABELS = {AdjustmentKind.PRODUCTION: "Production", AdjustmentKind.HEADCOUNT: "Effectif (ETP)"}
INDICATORS = {
    "kwh_m2": "kWh/m²",
    "kwh_unit": "kWh par unité produite",
    "kwh_fte": "kWh/ETP",
    "kwh_dju": "kWh/DJU (chauffage)",
}
HEATING_MONTH_DJU = 50  # mois de chauffe : au moins 50 DJU
SUMMER_MONTH_DJU = 20  # mois sans chauffage : moins de 20 DJU


class IpeError(ValueError):
    pass


class IpePermissionError(PermissionError):
    pass


def can_enter_variables(user: User) -> bool:
    """Variables fournies par le client : son responsable énergie les saisit, ou l'auditeur pour lui."""
    if user.role == Role.AUDITOR:
        return user.auditor_id is not None
    return user.role == Role.CLIENT_VIEWER and bool(user.is_energy_manager)


def set_variable(db: Session, repo: TenantRepository, user: User, site_id: int, kind: AdjustmentKind, month: date,
                 value: float | None) -> AdjustmentVariable | None:
    """Enregistre (ou efface, si `value` est vide) la valeur d'un mois."""
    if not can_enter_variables(user):
        raise IpePermissionError("Les variables d'ajustement sont saisies par le responsable énergie ou l'auditeur.")
    site = repo.get_site(site_id)
    month = month_start(month)
    if month > month_start(today_local()):
        raise IpeError("Mois futur : saisissez des valeurs constatées.")
    row = db.scalar(select(AdjustmentVariable).where(AdjustmentVariable.site_id == site.id,
                                                     AdjustmentVariable.kind == kind,
                                                     AdjustmentVariable.month == month))
    if value is None:
        if row is not None:
            db.delete(row)
            db.commit()
        return None
    if value < 0:
        raise IpeError("Valeur négative impossible.")
    if row is None:
        row = AdjustmentVariable(organization_id=site.organization_id, site_id=site.id, kind=kind, month=month,
                                 value=value)
        db.add(row)
    row.value, row.updated_by, row.updated_at = float(value), user.id, utcnow()
    db.commit()
    return row


def variables(db: Session, site_id: int, start: date, end: date) -> dict[AdjustmentKind, dict[date, float]]:
    found: dict[AdjustmentKind, dict[date, float]] = defaultdict(dict)
    for row in db.scalars(select(AdjustmentVariable).where(
            AdjustmentVariable.site_id == site_id, AdjustmentVariable.month >= month_start(start),
            AdjustmentVariable.month <= end)):
        found[row.kind][row.month] = row.value
    return found


@dataclass
class MonthIpe:
    month: date
    kwh: float
    days: int
    dju: float | None
    production: float | None
    headcount: float | None


@dataclass
class PeriodIpe:
    label: str
    start: date
    end: date
    months: list[MonthIpe]
    values: dict[str, float | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _months(start: date, end: date) -> list[date]:
    months, current = [], month_start(start)
    while current <= end:
        months.append(current)
        current = add_months(current, 1)
    return months


def monthly(db: Session, site: Site, start: date, end: date, weather: WeatherProvider | None) -> list[MonthIpe]:
    """Énergie (toutes énergies, N1 et N0), DJU et variables d'ajustement, mois par mois."""
    energy: dict[date, float] = defaultdict(float)
    for dp in site.delivery_points:
        for day, (kwh, _) in combined_daily(db, dp, start, end).items():
            energy[month_start(day)] += kwh
    dju: dict[date, float] = defaultdict(float)
    if weather is not None:
        try:
            for day, value in weather.daily_dju(start, end).items():
                dju[month_start(day)] += value
        except WeatherUnavailableError:
            dju = {}
    adjust = variables(db, site.id, start, end)
    rows = []
    for month in _months(start, end):
        last = min(add_months(month, 1) - timedelta(days=1), end)
        rows.append(MonthIpe(month, energy.get(month, 0.0), (last - max(month, start)).days + 1,
                             dju.get(month) if dju else None, adjust[AdjustmentKind.PRODUCTION].get(month),
                             adjust[AdjustmentKind.HEADCOUNT].get(month)))
    return rows


def compute(site: Site, label: str, rows: list[MonthIpe]) -> PeriodIpe:
    """IPE d'une période (12 mois en général) à partir de ses mois."""
    period = PeriodIpe(label, rows[0].month, rows[-1].month, rows)
    total = sum(r.kwh for r in rows)
    values = period.values
    values["kwh"] = total
    values["kwh_m2"] = total / site.surface_m2 if site.surface_m2 and total else None
    if not site.surface_m2:
        period.notes.append("Surface du site inconnue : kWh/m² non calculé.")
    produced = [r for r in rows if r.production]
    if produced:
        values["kwh_unit"] = sum(r.kwh for r in produced) / sum(r.production for r in produced)
        if len(produced) < len(rows):
            period.notes.append(f"Production renseignée pour {len(produced)} mois sur {len(rows)} : kWh par unité "
                                "calculé sur ces mois.")
    else:
        values["kwh_unit"] = None
    staffed = [r.headcount for r in rows if r.headcount]
    values["kwh_fte"] = total / (sum(staffed) / len(staffed)) if staffed else None  # énergie / effectif moyen
    if staffed and len(staffed) < len(rows):
        period.notes.append(f"Effectif renseigné pour {len(staffed)} mois sur {len(rows)} : effectif moyen de ces mois.")
    heated = [r for r in rows if r.dju is not None and r.dju >= HEATING_MONTH_DJU]
    summer = [r for r in rows if r.dju is not None and r.dju < SUMMER_MONTH_DJU and r.days]
    if heated and summer:
        base_per_day = sum(r.kwh for r in summer) / sum(r.days for r in summer)
        heating = sum(max(0.0, r.kwh - base_per_day * r.days) for r in heated)
        values["kwh_dju"] = heating / sum(r.dju for r in heated)
        values["heating_kwh"] = heating
    else:
        values["kwh_dju"] = None
        period.notes.append("kWh/DJU : il faut des mois de chauffe et des mois sans chauffage dans la période.")
    return period


@dataclass
class IpeReport:
    current: PeriodIpe
    baseline: PeriodIpe | None
    baseline_year: int

    def variation(self, key: str) -> float | None:
        if self.baseline is None:
            return None
        now, before = self.current.values.get(key), self.baseline.values.get(key)
        return (now - before) / before if now is not None and before else None


def site_report(db: Session, site: Site, last_month: date, weather: WeatherProvider | None) -> IpeReport:
    """IPE des 12 mois qui finissent avec `last_month` (inclus), comparés à la SER du site."""
    end = add_months(month_start(last_month), 1) - timedelta(days=1)
    start = add_months(month_start(last_month), -11)
    current = compute(site, "12 derniers mois", monthly(db, site, start, end, weather))
    year = site.energy_baseline_year or end.year - 1
    if year >= end.year:
        return IpeReport(current, None, year)
    rows = monthly(db, site, date(year, 1, 1), date(year, 12, 31), weather)
    baseline = compute(site, f"Référence {year}", rows) if any(r.kwh for r in rows) else None
    return IpeReport(current, baseline, year)


def iso50001_status(org: Organization, at: date | None = None) -> tuple[bool, str]:
    """(certifiée à cette date, texte) : la certification exempte de l'audit énergétique obligatoire."""
    at = at or today_local()
    until = org.iso50001_certified_until
    if until is None:
        return False, ("Non certifiée ISO 50001 : l'audit énergétique obligatoire reste dû tous les 4 ans. Ces IPE et "
                       "leur situation de référence sont les briques d'une certification, qui en exempterait.")
    if until < at:
        return False, f"Certification ISO 50001 expirée le {until:%d/%m/%Y} : l'audit énergétique est de nouveau dû."
    return True, (f"Certifiée ISO 50001 jusqu'au {until:%d/%m/%Y} : exemptée de l'audit énergétique obligatoire "
                  "tous les 4 ans.")


def set_site_settings(db: Session, repo: TenantRepository, user: User, site_id: int, *,
                      production_unit: str | None, baseline_year: int | None) -> Site:
    """Unité de production et année de la situation énergétique de référence (SER) du site."""
    if not can_enter_variables(user):
        raise IpePermissionError("Ces réglages sont saisis par le responsable énergie ou l'auditeur.")
    site = repo.get_site(site_id)
    unit = (production_unit or "").strip()
    if len(unit) > 60:
        raise IpeError("Unité de production : 60 caractères au plus.")
    if baseline_year is not None and not 2000 <= baseline_year < today_local().year:
        raise IpeError("Année de référence : une année passée, depuis 2000.")
    site.production_unit, site.energy_baseline_year = unit or None, baseline_year
    db.commit()
    return site


def set_certification(db: Session, repo: TenantRepository, user: User, organization_id: int,
                      until: date | None) -> Organization:
    """Date de fin de validité du certificat ISO 50001 (vide : non certifiée). Saisie par l'auditeur."""
    if user.role != Role.AUDITOR or user.auditor_id is None:
        raise IpePermissionError("La certification ISO 50001 est renseignée par l'auditeur.")
    org = repo.get_organization(organization_id)
    org.iso50001_certified_until = until
    db.commit()
    return org
