"""F7 — IPE personnalisés : créés par un humain, ou proposés par l'IA de la plateforme.

Un IPE rapporte une consommation (toutes énergies, électricité ou gaz) à ce qui l'explique :
- facteurs : surface, degrés-jours de chauffage (DJU) et de froid (DJF), jours ouvrés, production, effectif, ou une
  variable personnalisée du site (repas servis, nuitées, heures d'ouverture…) dont le client fournit les valeurs ;
- deux formes (ISO 50001 / ISO 50006) :
  - RATIO : kWh par unité du facteur (kWh/m², kWh par repas…) ;
  - MODEL : IPE modélisé, base 100. Consommation mesurée / consommation attendue par une régression sur un ou deux
    facteurs, apprise sur l'année de référence (SER). Préférable dès qu'une part de la consommation ne dépend pas
    du facteur (talon) : un ratio simple varierait alors avec l'activité, même sans gain réel.

L'IA de la plateforme propose des IPE : sur les 12 à 24 derniers mois, elle teste chaque facteur disponible par
régression, garde ceux qui expliquent la consommation (R² ≥ 0,5, effet positif), essaie les couples de facteurs,
choisit le modèle qui prédit le mieux les mois retirés de l'apprentissage (validation croisée « un mois retiré »),
puis la forme : ratio si le talon est faible, modèle sinon. Chaque proposition suit le principe P1 : raisonnement,
niveau de confiance, validation humaine avant d'être active. Un IPE créé par un humain est actif d'emblée.
"""
from __future__ import annotations

import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AdjustmentKind,
    Fluid,
    IpeDefinition,
    IpeKind,
    IpeVariable,
    IpeVariableValue,
    ReviewStatus,
    Site,
    User,
)
from app.providers.weather import WeatherProvider, WeatherUnavailableError
from app.repositories import TenantRepository
from app.services import ipe
from app.services.energy_data import combined_daily
from app.services.forecasting import COOLING_BASE
from app.services.validation import Assessment, apply_assessment, fr, notify
from app.timeutils import add_months, ensure_utc, month_start, today_local, utcnow

logger = logging.getLogger(__name__)

ALGORITHM = "IPE proposés v1 (régressions mensuelles, validation croisée un mois retiré, ISO 50006)"
ENERGY_LABELS = {"ALL": "toutes énergies", "ELEC": "électricité", "GAS": "gaz"}
ENERGY_FLUIDS = {"ALL": (Fluid.ELEC, Fluid.GAS), "ELEC": (Fluid.ELEC,), "GAS": (Fluid.GAS,)}
KIND_LABELS = {IpeKind.RATIO: "Ratio", IpeKind.MODEL: "IPE modélisé (base 100)"}
BUILTIN_DRIVERS = ("SURFACE", "DJU", "DJF", "WORKDAYS", "PRODUCTION", "HEADCOUNT")
MIN_MONTHS = 12
MAX_MONTHS = 24
MIN_FIT_MONTHS = 10
MIN_R2 = 0.5
RATIO_MAX_INTERCEPT_SHARE = 0.15  # talon au-delà duquel un ratio simple trompe (ISO 50006)
MIN_VARIABLE_SHARE = 0.20  # le facteur doit expliquer au moins 20 % de la consommation, sinon l'IPE reste plat
PAIR_GAIN = 0.10  # un second facteur doit réduire l'erreur hors échantillon d'au moins 10 %
REJECTION_MEMORY_DAYS = 180


class IpeDefinitionError(ValueError):
    pass


# --- Facteurs ---------------------------------------------------------------------------------------------


def variables_of(db: Session, site_id: int) -> list[IpeVariable]:
    return list(db.scalars(select(IpeVariable).where(IpeVariable.site_id == site_id).order_by(IpeVariable.name)))


def driver_label(db: Session, site: Site, driver: str) -> tuple[str, str]:
    """(nom, unité) d'un facteur."""
    if driver.startswith("VAR:"):
        variable = db.get(IpeVariable, int(driver[4:]))
        return (variable.name, variable.unit) if variable else ("variable supprimée", "")
    return {
        "SURFACE": ("surface", "m²"), "DJU": ("degrés-jours de chauffage", "DJU"),
        "DJF": ("degrés-jours de froid", "DJF"), "WORKDAYS": ("jours ouvrés", "jour ouvré"),
        "PRODUCTION": ("production", site.production_unit or "unités produites"),
        "HEADCOUNT": ("effectif", "ETP"),
    }[driver]


def available_drivers(db: Session, site: Site) -> list[str]:
    drivers = [d for d in BUILTIN_DRIVERS if d != "SURFACE" or site.surface_m2]
    return drivers + [f"VAR:{v.id}" for v in variables_of(db, site.id)]


def _months(first: date, last: date) -> list[date]:
    months, current = [], month_start(first)
    while current <= last:
        months.append(current)
        current = add_months(current, 1)
    return months


def monthly_energy(db: Session, site: Site, energy: str, months: list[date]) -> dict[date, float]:
    """Consommation mensuelle (compteurs N1 et factures N0) ; seuls les mois entièrement couverts par chaque point
    qui a des données (un point sans consentement ni facture est ignoré)."""
    start, end = months[0], add_months(months[-1], 1) - timedelta(days=1)
    totals: dict[date, float] = defaultdict(float)
    days: dict[tuple, set] = defaultdict(set)
    points = []
    for dp in site.delivery_points:
        if dp.fluid not in ENERGY_FLUIDS[energy]:
            continue
        daily = combined_daily(db, dp, start, end)
        if daily:
            points.append(dp)
        for day, (kwh, _) in daily.items():
            totals[month_start(day)] += kwh
            days[(month_start(day), dp.id)].add(day)
    complete = {}
    for month in months:
        length = (add_months(month, 1) - month).days
        if points and all(len(days.get((month, dp.id), ())) >= length - 1 for dp in points):
            complete[month] = totals[month]
    return complete


def driver_series(db: Session, site: Site, driver: str, months: list[date],
                  weather: WeatherProvider | None) -> dict[date, float]:
    """Valeur mensuelle d'un facteur (mois sans valeur absents)."""
    start, end = months[0], add_months(months[-1], 1) - timedelta(days=1)
    if driver == "SURFACE":
        return {m: site.surface_m2 for m in months} if site.surface_m2 else {}
    if driver == "WORKDAYS":
        return {m: sum(1 for k in range((add_months(m, 1) - m).days) if (m + timedelta(days=k)).weekday() < 5)
                for m in months}
    if driver in ("DJU", "DJF"):
        if weather is None:
            return {}
        try:
            if driver == "DJU":
                daily = weather.daily_dju(start, end)
            else:
                daily = {d: max(0.0, t - COOLING_BASE) for d, t in weather.daily_temperature(start, end).items()}
        except WeatherUnavailableError:
            return {}
        series: dict[date, float] = defaultdict(float)
        for day, value in daily.items():
            series[month_start(day)] += value
        return {m: series[m] for m in months if m in series}
    if driver in ("PRODUCTION", "HEADCOUNT"):
        values = ipe.variables(db, site.id, start, end)[AdjustmentKind[driver]]
        return {m: values[m] for m in months if m in values}
    rows = db.scalars(select(IpeVariableValue).where(IpeVariableValue.variable_id == int(driver[4:]),
                                                     IpeVariableValue.month >= start, IpeVariableValue.month <= end))
    return {row.month: row.value for row in rows}


# --- Régression -------------------------------------------------------------------------------------------


@dataclass
class Fit:
    drivers: list[str]
    intercept: float
    coefs: list[float]
    r2: float
    cv: float  # erreur hors échantillon (un mois retiré), rapportée à la consommation moyenne
    months: list[date]
    mean: float

    @property
    def intercept_share(self) -> float:
        return abs(self.intercept) / self.mean if self.mean else 1.0

    def predict(self, values: list[float]) -> float:
        return self.intercept + sum(b * x for b, x in zip(self.coefs, values))


def fit(energy: dict[date, float], series: list[dict[date, float]], drivers: list[str]) -> Fit | None:
    """Consommation = a + Σ b × facteur, par moindres carrés, sur les mois où tout est connu."""
    months = sorted(m for m in energy if all(m in s for s in series))
    if len(months) < MIN_FIT_MONTHS:
        return None
    y = np.array([energy[m] for m in months])
    x = np.array([[1.0, *[s[m] for s in series]] for m in months])
    if np.linalg.matrix_rank(x) < x.shape[1]:
        return None  # facteur constant ou redondant
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residuals = y - x @ beta
    total = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((residuals ** 2).sum()) / total if total > 0 else 0.0
    errors = []
    for i in range(len(months)):
        keep = np.arange(len(months)) != i
        b = np.linalg.lstsq(x[keep], y[keep], rcond=None)[0]
        errors.append(float(y[i] - x[i] @ b))
    cv = float(np.sqrt(np.mean(np.square(errors)))) / float(y.mean()) if y.mean() else 1.0
    return Fit(drivers, float(beta[0]), [float(b) for b in beta[1:]], r2, cv, months, float(y.mean()))


def model_json(fitted: Fit) -> dict:
    return {"intercept": fitted.intercept, "coefs": dict(zip(fitted.drivers, fitted.coefs)), "r2": fitted.r2,
            "cv": fitted.cv, "fit_start": fitted.months[0].isoformat(), "fit_end": fitted.months[-1].isoformat(),
            "months": len(fitted.months), "intercept_share": fitted.intercept_share}


def formula(db: Session, site: Site, definition_or_fit) -> str:
    """Formule lisible : « kWh = 1 200 + 85 × degrés-jours de chauffage »."""
    model = definition_or_fit.model if isinstance(definition_or_fit, IpeDefinition) else model_json(definition_or_fit)
    terms = [f"{fr(b, 2 if abs(b) < 10 else 0)} × {driver_label(db, site, d)[0]}" for d, b in model["coefs"].items()]
    return f"kWh/mois = {fr(model['intercept'])} + " + " + ".join(terms)


# --- Calcul d'un IPE -----------------------------------------------------------------------------------------


@dataclass
class IpeValue:
    unit: str
    current: float | None
    baseline: float | None
    baseline_year: int
    monthly: list[dict] = field(default_factory=list)  # [{"month", "kwh", "value"}]
    notes: list[str] = field(default_factory=list)

    @property
    def variation(self) -> float | None:
        if self.current is None or self.baseline in (None, 0):
            return None
        return (self.current - self.baseline) / self.baseline


def last_complete_month(today: date | None = None) -> date:
    return add_months(month_start(today or today_local()), -1)


def _period_value(definition: IpeDefinition, energy: dict[date, float], series: list[dict[date, float]],
                  months: list[date]) -> tuple[float | None, list[dict], int]:
    """Valeur de l'IPE sur des mois, et détail mensuel ; renvoie aussi le nombre de mois utilisés."""
    usable = [m for m in months if m in energy and all(m in s for s in series)]
    detail = []
    if not usable:
        return None, detail, 0
    if definition.kind == IpeKind.RATIO:
        driver = definition.drivers[0]
        for m in usable:
            denominator = series[0][m]
            detail.append({"month": m.isoformat(), "kwh": energy[m],
                           "value": energy[m] / denominator if denominator and driver != "SURFACE" else None})
        total = sum(energy[m] for m in usable)
        if driver == "SURFACE":
            return total / series[0][usable[0]] * 12 / len(usable), detail, len(usable)  # kWh/m² ramené à l'an
        denominator = sum(series[0][m] for m in usable)
        return (total / denominator if denominator else None), detail, len(usable)
    coefs = [definition.model["coefs"][d] for d in definition.drivers]
    expected_total = actual_total = 0.0
    for m in usable:
        expected = definition.model["intercept"] + sum(b * s[m] for b, s in zip(coefs, series))
        expected_total += expected
        actual_total += energy[m]
        detail.append({"month": m.isoformat(), "kwh": energy[m], "expected": expected,
                       "value": energy[m] / expected * 100 if expected > 0 else None})
    return (actual_total / expected_total * 100 if expected_total > 0 else None), detail, len(usable)


def evaluate(db: Session, definition: IpeDefinition, weather: WeatherProvider | None,
             last_month: date | None = None) -> IpeValue:
    """Valeur sur les 12 derniers mois complets, comparée à l'année de référence (SER) du site."""
    site = definition.site
    last_month = last_month or last_complete_month()
    year = site.energy_baseline_year or last_month.year - 1
    current_months = _months(add_months(last_month, -11), last_month)
    baseline_months = _months(date(year, 1, 1), date(year, 12, 1))
    all_months = sorted(set(baseline_months) | set(current_months))
    energy = monthly_energy(db, site, definition.energy, all_months)
    series = [driver_series(db, site, d, all_months, weather) for d in definition.drivers]
    current, detail, used = _period_value(definition, energy, series, current_months)
    baseline, _, base_used = _period_value(definition, energy, series, baseline_months)
    if definition.kind == IpeKind.RATIO:
        name, unit = driver_label(db, site, definition.drivers[0])
        unit_label = "kWh/m²/an" if definition.drivers[0] == "SURFACE" else f"kWh par {unit or name}"
    else:
        unit_label = "indice (base 100 = modèle de référence)"
    value = IpeValue(unit_label, current, baseline, year, detail)
    if used < len(current_months):
        value.notes.append(f"{used} mois sur 12 avec consommation et facteurs connus.")
    if base_used < 12:
        value.notes.append(f"Référence {year} : {base_used} mois complets.")
    return value


# --- Variables personnalisées et IPE créés par un humain ------------------------------------------------------


def _require_editor(user: User) -> None:
    if not ipe.can_enter_variables(user):
        raise ipe.IpePermissionError("Les IPE et leurs variables sont gérés par le responsable énergie ou l'auditeur.")


def create_variable(db: Session, repo: TenantRepository, user: User, site_id: int, name: str, unit: str) -> IpeVariable:
    _require_editor(user)
    site = repo.get_site(site_id)
    name, unit = (name or "").strip(), (unit or "").strip()
    if not name or len(name) > 80 or not unit or len(unit) > 40:
        raise IpeDefinitionError("Nom (80 caractères au plus) et unité (40 au plus) obligatoires.")
    if any(v.name.lower() == name.lower() for v in variables_of(db, site.id)):
        raise IpeDefinitionError("Une variable porte déjà ce nom sur ce site.")
    variable = IpeVariable(organization_id=site.organization_id, site_id=site.id, name=name, unit=unit,
                           created_by=user.id)
    db.add(variable)
    db.commit()
    return variable


def set_variable_value(db: Session, repo: TenantRepository, user: User, variable_id: int, month: date,
                       value: float | None) -> None:
    _require_editor(user)
    variable = db.get(IpeVariable, variable_id)
    if variable is None:
        raise IpeDefinitionError("Variable introuvable.")
    repo.get_site(variable.site_id)  # périmètre de l'utilisateur
    month = month_start(month)
    if month > month_start(today_local()):
        raise IpeDefinitionError("Mois futur : saisissez des valeurs constatées.")
    row = db.scalar(select(IpeVariableValue).where(IpeVariableValue.variable_id == variable.id,
                                                   IpeVariableValue.month == month))
    if value is None:
        if row is not None:
            db.delete(row)
    else:
        if value < 0:
            raise IpeDefinitionError("Valeur négative impossible.")
        if row is None:
            row = IpeVariableValue(variable_id=variable.id, month=month, value=value)
            db.add(row)
        row.value, row.updated_by, row.updated_at = float(value), user.id, utcnow()
    db.commit()


def default_name(db: Session, site: Site, energy: str, kind: IpeKind, drivers: list[str]) -> str:
    labels = [driver_label(db, site, d) for d in drivers]
    if kind == IpeKind.RATIO:
        name, unit = labels[0]
        return f"kWh {ENERGY_LABELS[energy]} par {'m²' if drivers[0] == 'SURFACE' else unit or name}"
    return f"IPE modélisé {ENERGY_LABELS[energy]} ({', '.join(n for n, _ in labels)})"


def _check_shape(db: Session, site: Site, energy: str, kind: IpeKind, drivers: list[str]) -> None:
    if energy not in ENERGY_FLUIDS:
        raise IpeDefinitionError("Énergie inconnue.")
    if not any(dp.fluid in ENERGY_FLUIDS[energy] for dp in site.delivery_points):
        raise IpeDefinitionError(f"Le site n'a aucun point de livraison en {ENERGY_LABELS[energy]}.")
    allowed = available_drivers(db, site)
    if not drivers or any(d not in allowed for d in drivers) or len(set(drivers)) != len(drivers):
        raise IpeDefinitionError("Facteur inconnu pour ce site.")
    if kind == IpeKind.RATIO and len(drivers) != 1:
        raise IpeDefinitionError("Un ratio rapporte la consommation à un seul facteur.")
    if kind == IpeKind.MODEL and (len(drivers) > 2 or "SURFACE" in drivers):
        raise IpeDefinitionError("Un IPE modélisé utilise un ou deux facteurs variables (la surface est constante).")


def create_definition(db: Session, repo: TenantRepository, user: User, site_id: int, *, energy: str, kind: IpeKind,
                      drivers: list[str], weather: WeatherProvider | None, name: str | None = None) -> IpeDefinition:
    """IPE créé par un humain : actif d'emblée. Un IPE modélisé apprend sa référence sur l'année SER du site."""
    _require_editor(user)
    site = repo.get_site(site_id)
    _check_shape(db, site, energy, kind, drivers)
    a = Assessment(algorithm="IPE défini par l'utilisateur")
    model = None
    if kind == IpeKind.MODEL:
        year = site.energy_baseline_year or last_complete_month().year - 1
        months = _months(date(year, 1, 1), date(year, 12, 1))
        fitted = fit(monthly_energy(db, site, energy, months),
                     [driver_series(db, site, d, months, weather) for d in drivers], drivers)
        if fitted is None:
            raise IpeDefinitionError(f"Référence {year} : il faut au moins {MIN_FIT_MONTHS} mois avec consommation et "
                                     "valeurs de chaque facteur.")
        model = model_json(fitted)
        a.step(f"Modèle de référence appris sur {year} : {formula(db, site, fitted)} (R² = {fr(fitted.r2, 2)}, erreur "
               f"hors échantillon {fr(fitted.cv * 100, 1)} %). IPE = consommation mesurée / consommation du modèle × 100.")
    name = (name or "").strip() or default_name(db, site, energy, kind, drivers)
    a.step(f"IPE créé par {'l’auditeur' if user.auditor_id else 'le responsable énergie'} : « {name} ».")
    definition = IpeDefinition(organization_id=site.organization_id, site_id=site.id, name=name[:160], energy=energy,
                               kind=kind, drivers=list(drivers), model=model, origin="USER",
                               status=ReviewStatus.VALIDATED, created_by=user.id, reviewed_by=user.id,
                               reviewed_at=utcnow())
    a.gain_basis = "Indicateur de suivi : pas de gain estimé."
    apply_assessment(db, definition, a, Fluid.ELEC, today_local())
    definition.confidence = None
    db.add(definition)
    db.commit()
    return definition


def delete_definition(db: Session, repo: TenantRepository, user: User, definition_id: int) -> None:
    """Retire un IPE créé par un humain (un IPE proposé par l'IA s'écarte depuis la file de validation)."""
    _require_editor(user)
    definition = repo.get_ipe_definition(definition_id)
    if definition.origin != "USER":
        raise IpeDefinitionError("Un IPE proposé par l'IA s'écarte depuis sa carte (principe P1), il ne se supprime pas.")
    db.delete(definition)
    db.commit()


# --- Propositions de l'IA ----------------------------------------------------------------------------------


def _same_shape(definition: IpeDefinition, energy: str, kind: IpeKind, drivers: list[str]) -> bool:
    return definition.energy == energy and definition.kind == kind and sorted(definition.drivers) == sorted(drivers)


def _blocked(db: Session, site: Site, energy: str, kind: IpeKind, drivers: list[str]) -> bool:
    """Déjà actif, déjà proposé, ou écarté récemment par un humain."""
    recent = utcnow() - timedelta(days=REJECTION_MEMORY_DAYS)
    for definition in db.scalars(select(IpeDefinition).where(IpeDefinition.site_id == site.id)):
        if not _same_shape(definition, energy, kind, drivers):
            continue
        if definition.status in (ReviewStatus.PROPOSED, ReviewStatus.VALIDATED):
            return True
        if definition.status == ReviewStatus.REJECTED and definition.reviewed_at \
                and ensure_utc(definition.reviewed_at) >= recent:
            return True
    return False


def propose_for_site(db: Session, site: Site, weather: WeatherProvider | None,
                     last_month: date | None = None) -> list[IpeDefinition]:
    """Analyse les 12 à 24 derniers mois et propose, par énergie, l'IPE le plus pertinent (sans l'enregistrer)."""
    last_month = last_month or last_complete_month()
    months = _months(add_months(last_month, -(MAX_MONTHS - 1)), last_month)
    fluids = {dp.fluid for dp in site.delivery_points}
    energies = (["ALL"] if len(fluids) > 1 else []) + [e for e in ("ELEC", "GAS") if ENERGY_FLUIDS[e][0] in fluids]
    candidates = [d for d in available_drivers(db, site) if d != "SURFACE"]
    series = {d: driver_series(db, site, d, months, weather) for d in candidates}
    proposals = []
    # Par énergie d'abord (plus précis) ; « toutes énergies » seulement si aucune énergie n'a d'IPE, proposé
    # maintenant ou déjà proposé ou suivi.
    covered = {d.energy for d in db.scalars(select(IpeDefinition).where(
        IpeDefinition.site_id == site.id, IpeDefinition.status.in_([ReviewStatus.PROPOSED, ReviewStatus.VALIDATED])))}
    for energy in [e for e in energies if e != "ALL"] + [e for e in energies if e == "ALL"]:
        if energy == "ALL" and (proposals or covered - {"ALL"}):
            break
        consumption = monthly_energy(db, site, energy, months)
        if len(consumption) < MIN_MONTHS:
            continue
        singles, rejected = {}, {}
        for driver in candidates:
            fitted = fit(consumption, [series[driver]], [driver])
            if fitted is None:
                continue
            if fitted.r2 >= MIN_R2 and fitted.coefs[0] > 0 and 1 - fitted.intercept_share >= MIN_VARIABLE_SHARE:
                singles[driver] = fitted
            else:
                rejected[driver] = fitted
        if not singles:
            continue
        best = min(singles.values(), key=lambda f: f.cv)
        for d1, d2 in itertools.combinations(sorted(singles), 2):
            pair = fit(consumption, [series[d1], series[d2]], [d1, d2])
            if pair and all(b > 0 for b in pair.coefs) and pair.cv <= best.cv * (1 - PAIR_GAIN):
                best = pair
        kind = IpeKind.RATIO if len(best.drivers) == 1 and best.intercept_share <= RATIO_MAX_INTERCEPT_SHARE \
            else IpeKind.MODEL
        if _blocked(db, site, energy, kind, best.drivers):
            continue
        proposals.append(_proposal(db, site, energy, kind, best, singles, rejected, weather))
    return proposals


def _proposal(db: Session, site: Site, energy: str, kind: IpeKind, best: Fit, singles: dict[str, Fit],
              rejected: dict[str, Fit], weather: WeatherProvider | None) -> IpeDefinition:
    a = Assessment(algorithm=ALGORITHM)
    a.step(f"Données : consommation mensuelle ({ENERGY_LABELS[energy]}) du site {site.name}, {len(best.months)} mois "
           f"complets du {best.months[0]:%m/%Y} au {best.months[-1]:%m/%Y} (compteurs et factures).")
    tested = sorted(singles.values(), key=lambda f: -f.r2)
    a.step("Facteurs qui expliquent la consommation (R² ≥ 0,5, effet positif, au moins 20 % de la consommation) : "
           + " ; ".join(f"{driver_label(db, site, f.drivers[0])[0]} R² = {fr(f.r2, 2)}" for f in tested) + ".")
    if rejected:
        a.step("Facteurs testés sans lien suffisant : "
               + " ; ".join(f"{driver_label(db, site, d)[0]} R² = {fr(f.r2, 2)}"
                            for d, f in sorted(rejected.items(), key=lambda item: -item[1].r2)) + ".")
    labels = " et ".join(driver_label(db, site, d)[0] for d in best.drivers)
    a.step(f"Modèle retenu : {formula(db, site, best)}, soit {labels} ; R² = {fr(best.r2, 2)}, erreur sur les mois "
           f"retirés de l'apprentissage {fr(best.cv * 100, 1)} %"
           + (" ; le second facteur réduit cette erreur d'au moins 10 %." if len(best.drivers) == 2 else "."))
    share = best.intercept_share
    if kind == IpeKind.RATIO:
        unit = driver_label(db, site, best.drivers[0])[1]
        a.step(f"Forme proposée : ratio kWh par {unit}. Le talon ({fr(share * 100)} % de la consommation moyenne) est "
               "faible : la consommation est presque proportionnelle au facteur.")
    else:
        a.step(f"Forme proposée : IPE modélisé, base 100 (ISO 50006). Le talon pèse {fr(share * 100)} % de la "
               "consommation moyenne : un ratio simple varierait avec l'activité même sans gain réel. "
               "IPE = consommation mesurée / consommation attendue par le modèle × 100 ; sous 100, la performance "
               "s'améliore.")
    a.step("Statut : proposition de l'IA de la plateforme, à valider avant de devenir un IPE suivi.")
    if best.r2 >= 0.9:
        a.factor(f"Relation très nette (R² = {fr(best.r2, 2)})", 0.2)
    elif best.r2 >= 0.75:
        a.factor(f"Relation nette (R² = {fr(best.r2, 2)})", 0.1)
    elif best.r2 < 0.6:
        a.factor(f"Relation modérée (R² = {fr(best.r2, 2)})", -0.1)
    if best.cv <= 0.05:
        a.factor(f"Erreur hors échantillon faible ({fr(best.cv * 100, 1)} %)", 0.1)
    elif best.cv > 0.15:
        a.factor(f"Erreur hors échantillon élevée ({fr(best.cv * 100, 1)} %)", -0.1)
    if len(best.months) >= 20:
        a.factor(f"{len(best.months)} mois d'historique", 0.05)
    elif len(best.months) < 15:
        a.factor(f"Seulement {len(best.months)} mois d'historique", -0.1)
    if any(d in ("DJU", "DJF") for d in best.drivers) and str(getattr(weather, "source", "")).startswith("MOCK"):
        a.factor("Degrés-jours issus de la météo simulée (démonstration)", -0.05)
    a.gain_basis = "Indicateur de suivi : pas de gain estimé."
    definition = IpeDefinition(organization_id=site.organization_id, site_id=site.id,
                               name=default_name(db, site, energy, kind, best.drivers)[:160], energy=energy, kind=kind,
                               drivers=list(best.drivers), model=model_json(best), origin="PLATFORM",
                               status=ReviewStatus.PROPOSED)
    apply_assessment(db, definition, a, Fluid.ELEC, today_local())
    return definition


def propose_all(db: Session, organization_id: int | None = None) -> list[IpeDefinition]:
    """Propositions de l'IA pour chaque site (sans doublon ni reprise d'un refus récent) ; valideurs prévenus."""
    from app.services import integrations

    weather = integrations.weather_provider(db)
    stmt = select(Site).order_by(Site.id)
    if organization_id is not None:
        stmt = stmt.where(Site.organization_id == organization_id)
    created = []
    for site in db.scalars(stmt).all():
        for definition in propose_for_site(db, site, weather):
            db.add(definition)
            created.append(definition)
    db.flush()
    for org_id in {d.organization_id for d in created}:
        count = sum(1 for d in created if d.organization_id == org_id)
        notify(db, org_id, f"À valider : {count} IPE proposé(s) par l'IA de la plateforme", validators_only=True)
    db.commit()
    return created
