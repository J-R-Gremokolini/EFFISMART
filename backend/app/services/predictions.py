"""Prévisions (principe P1) : projection de la consommation de l'année civile, par site et par énergie.

Méthode — signature énergétique, courante en audit énergétique :
1. sur les 365 derniers jours, conso journalière = a + b × DJU, ajustée séparément pour les jours
   ouvrés et le week-end (b ≥ 0 : le chauffage ne baisse pas quand il fait plus froid) ;
2. du 1er janvier à la dernière donnée : consommation mesurée ;
3. reste de l'année : a + b × DJU normaux ;
4. intervalle : dispersion journalière du modèle (1,96 σ √n) + 10 % de la part climatique projetée
   + 5 % du reste de l'année (changements d'usage que l'historique ne peut pas prévoir).

Une prévision est *proposée* : seul un humain la valide, et le client ne la voit qu'une fois validée.
Elle est recalculée au plus une fois par mois (ou à la demande de l'auditeur) ; une projection plus
récente remplace une projection encore non validée, jamais une projection validée.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DeliveryPoint, Fluid, Prediction, PredictionKind, ReviewStatus, Site
from app.providers.weather import NORMALS_SOURCE, WeatherProvider, WeatherUnavailableError, normal_dju
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of
from app.services.drift import daily_kwh
from app.services.validation import Assessment, apply_assessment, fr, fr_pct, notify
from app.timeutils import daterange

logger = logging.getLogger(__name__)

ALGORITHM = "Projection annuelle v1 (signature énergétique ouvrés / week-end × DJU normaux)"
FIT_DAYS = 365
MIN_FIT_DAYS = 60
CLIMATE_UNCERTAINTY = 0.10
USAGE_UNCERTAINTY = 0.05  # occupation, horaires, équipements : non prévisibles par la signature
FLUID_LABELS = {Fluid.ELEC: "électricité", Fluid.GAS: "gaz"}


@dataclass(frozen=True)
class Signature:
    intercept: float
    slope: float
    sigma: float
    n: int

    def predict(self, dju: float) -> float:
        return max(0.0, self.intercept + self.slope * dju)


def fit_signature(points: list[tuple[float, float]]) -> Signature | None:
    """Moindres carrés conso = a + b × DJU, avec b ≥ 0 (moyenne seule si les DJU varient trop peu)."""
    if len(points) < 5:
        return None
    xs, ys = [x for x, _ in points], [y for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    slope = max(0.0, sum((x - mean_x) * (y - mean_y) for x, y in points) / var_x) if var_x >= 1.0 else 0.0
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in points]
    sigma = math.sqrt(sum(r * r for r in residuals) / max(1, len(points) - 2))
    return Signature(intercept, slope, sigma, len(points))


def _site_daily(db: Session, points: list[DeliveryPoint], start: date, end: date) -> dict[date, float]:
    """Consommation journalière du site : seuls les jours où chaque compteur a une donnée sont retenus."""
    series = [daily_kwh(db, dp.id, start, end) for dp in points]
    days = set(series[0]).intersection(*series[1:]) if series else set()
    return {day: sum(s[day] for s in series) for day in days}


def project(db: Session, site: Site, fluid: Fluid, points: list[DeliveryPoint], as_of: date,
            weather: WeatherProvider) -> Prediction | None:
    """Construit une projection (non enregistrée) ; None si l'historique est insuffisant."""
    fit_start = as_of - timedelta(days=FIT_DAYS - 1)
    year = as_of.year
    jan1, dec31 = date(year, 1, 1), date(year, 12, 31)
    history = _site_daily(db, points, min(fit_start, jan1), as_of)
    fit = {d: v for d, v in history.items() if d >= fit_start}
    if len(fit) < MIN_FIT_DAYS:
        return None
    try:
        dju = weather.daily_dju(min(fit_start, jan1), as_of)
    except WeatherUnavailableError:
        logger.warning("Météo indisponible : projection %s ignorée pour le site #%s", year, site.id)
        return None
    workdays = fit_signature([(dju[d], v) for d, v in fit.items() if d in dju and d.weekday() < 5])
    weekends = fit_signature([(dju[d], v) for d, v in fit.items() if d in dju and d.weekday() >= 5])
    if workdays is None or weekends is None:
        return None

    def model(day: date, degree_days: float) -> float:
        return (workdays if day.weekday() < 5 else weekends).predict(degree_days)

    # Qualité du modèle sur l'historique (R²).
    fitted = [(v, model(d, dju[d])) for d, v in fit.items() if d in dju]
    mean_actual = statistics.fmean(v for v, _ in fitted)
    ss_tot = sum((v - mean_actual) ** 2 for v, _ in fitted)
    r2 = 1 - sum((v - p) ** 2 for v, p in fitted) / ss_tot if ss_tot > 0 else 0.0

    # Année en cours : mesuré, jours manquants reconstitués, reste projeté aux DJU normaux.
    monthly: dict[int, dict[str, float]] = {m: {"measured": 0.0, "predicted": 0.0} for m in range(1, 13)}
    measured = reconstituted_kwh = 0.0
    reconstituted_days = 0
    for day in daterange(jan1, as_of):
        if day in history:
            measured += history[day]
            monthly[day.month]["measured"] += history[day]
        else:
            value = model(day, dju.get(day, normal_dju(day)))
            reconstituted_kwh += value
            reconstituted_days += 1
            monthly[day.month]["predicted"] += value
    rest = climate_part = variance = 0.0
    rest_dju = 0.0
    remaining = list(daterange(as_of + timedelta(days=1), dec31)) if as_of < dec31 else []
    for day in remaining:
        degree_days = normal_dju(day)
        signature = workdays if day.weekday() < 5 else weekends
        value = signature.predict(degree_days)
        rest += value
        rest_dju += degree_days
        climate_part += signature.slope * degree_days
        variance += signature.sigma ** 2
        monthly[day.month]["predicted"] += value
    predicted = measured + reconstituted_kwh + rest
    margin = 1.96 * math.sqrt(variance) + CLIMATE_UNCERTAINTY * climate_part + USAGE_UNCERTAINTY * rest

    previous = _site_daily(db, points, date(year - 1, 1, 1), date(year - 1, 12, 31))
    reference = sum(previous.values()) if len(previous) >= 360 else None
    reference_monthly = {m: 0.0 for m in range(1, 13)}
    for day, value in previous.items():
        reference_monthly[day.month] += value

    a = Assessment(algorithm=ALGORITHM)
    label = FLUID_LABELS[fluid]
    a.step(f"Données : consommation journalière d'{label} du site {site.name} ({len(points)} compteur(s)), "
           f"{len(fit)} jours sur les {FIT_DAYS} derniers, jusqu'au {as_of:%d/%m/%Y}.")
    a.step(f"Modèle : signature énergétique conso = a + b × DJU. Jours ouvrés : a = {fr(workdays.intercept)} kWh, "
           f"b = {fr(workdays.slope, 1)} kWh par DJU ; week-end : a = {fr(weekends.intercept)} kWh, "
           f"b = {fr(weekends.slope, 1)} kWh par DJU. R² = {fr(r2, 2)} sur l'historique.")
    a.step(f"Consommé du 1er janvier au {as_of:%d/%m/%Y} : {fr(measured)} kWh mesurés"
           + (f", plus {fr(reconstituted_kwh)} kWh reconstitués pour {reconstituted_days} jour(s) sans donnée."
              if reconstituted_days else "."))
    if remaining:
        a.step(f"Reste de l'année ({len(remaining)} jours) : {fr(rest)} kWh projetés avec les DJU normaux "
               f"({fr(rest_dju)} DJU ; {NORMALS_SOURCE}).")
    a.step(f"Projection {year} : {fr(predicted)} kWh, intervalle {fr(max(0.0, predicted - margin))} – "
           f"{fr(predicted + margin)} kWh (dispersion journalière du modèle, ± 10 % sur la part liée au climat, "
           "± 5 % sur le reste de l'année pour les changements d'usage).")
    if reference is not None:
        a.step(f"Comparaison : {year - 1} = {fr(reference)} kWh, soit {fr_pct((predicted - reference) / reference * 100)} "
               "(écart brut, non corrigé du climat).")
    else:
        a.step(f"Comparaison : année {year - 1} incomplète, pas de référence.")
    a.step("Statut : proposition de la plateforme, à valider avant d'être montrée au client ou utilisée dans un rapport.")

    if r2 >= 0.8:
        a.factor(f"Le modèle explique bien la consommation passée (R² {fr(r2, 2)})", 0.1)
    elif r2 >= 0.5:
        a.factor(f"Le modèle explique correctement la consommation passée (R² {fr(r2, 2)})", 0.05)
    else:
        a.factor(f"Consommation passée mal expliquée par le modèle (R² {fr(r2, 2)})", -0.1)
    share = (as_of - jan1).days / ((dec31 - jan1).days + 1)
    if share >= 0.75:
        a.factor(f"{fr(share * 100)} % de l'année déjà mesurés", 0.15)
    elif share >= 0.5:
        a.factor(f"{fr(share * 100)} % de l'année déjà mesurés", 0.05)
    elif share < 0.25:
        a.factor("Début d'année : l'essentiel reste à projeter", -0.15)
    completeness = len(fit) / FIT_DAYS
    if completeness >= 0.95:
        a.factor("Historique complet sur 12 mois", 0.05)
    elif completeness < 0.8:
        a.factor(f"Historique partiel ({fr(completeness * 100)} % des jours sur 12 mois)", -0.1)
    if reconstituted_days:
        a.factor(f"{reconstituted_days} jour(s) sans donnée depuis le 1er janvier, reconstitués par le modèle", -0.05)
    if remaining:
        a.factor("DJU normaux simplifiés, non spécifiques au site", -0.05)
    if str(getattr(weather, "source", "")).startswith("MOCK"):
        a.factor("Historique météo simulé (démonstration)", -0.05)

    if reference is not None:
        a.gain_kwh = reference - predicted
        a.gain_basis = (f"Écart entre la consommation {year - 1} ({fr(reference)} kWh) et la projection {year} "
                        f"({fr(predicted)} kWh) : positif = économie projetée, négatif = surconsommation projetée. "
                        "Écart brut, non corrigé du climat ; prix et facteurs d'émission indicatifs.")
    else:
        a.gain_basis = f"Pas d'année {year - 1} complète : écart non calculable."

    prediction = Prediction(
        organization_id=site.organization_id, site_id=site.id, fluid=fluid,
        kind=PredictionKind.ANNUAL_CONSUMPTION, year=year, data_as_of=as_of,
        measured_kwh=round(measured, 1), predicted_kwh=round(predicted, 1),
        low_kwh=round(max(0.0, predicted - margin), 1), high_kwh=round(predicted + margin, 1),
        reference_kwh=round(reference, 1) if reference is not None else None,
        monthly=[{"month": f"{year}-{m:02d}", "measured": round(v["measured"], 1),
                  "predicted": round(v["predicted"], 1),
                  "reference": round(reference_monthly[m], 1) if reference is not None else None}
                 for m, v in monthly.items()],
    )
    apply_assessment(db, prediction, a, fluid, as_of)
    return prediction


def refresh_predictions(db: Session, organization_id: int | None = None, *, force: bool = False) -> list[Prediction]:
    """Projette l'année en cours pour chaque site et énergie. Au plus une par mois de données, sauf `force`."""
    from app.services import integrations

    weather = integrations.weather_provider(db)
    stmt = select(Site).order_by(Site.id)
    if organization_id is not None:
        stmt = stmt.where(Site.organization_id == organization_id)
    created: list[Prediction] = []
    for site in db.scalars(stmt).all():
        for fluid in Fluid:
            points = [dp for dp in site.delivery_points if dp.fluid == fluid and has_active_consent(db, dp.id)]
            as_of = data_as_of(db, [dp.id for dp in points]) if points else None
            if as_of is None:
                continue
            previous = list(db.scalars(select(Prediction).where(
                Prediction.site_id == site.id, Prediction.fluid == fluid, Prediction.year == as_of.year,
                Prediction.status != ReviewStatus.SUPERSEDED,
            ).order_by(Prediction.created_at.desc(), Prediction.id.desc())))
            latest = previous[0] if previous else None
            if not force and latest is not None and (latest.data_as_of.year, latest.data_as_of.month) == (
                    as_of.year, as_of.month):
                continue
            prediction = project(db, site, fluid, points, as_of, weather)
            if prediction is None:
                continue
            for old in previous:
                if old.status == ReviewStatus.PROPOSED:
                    old.status = ReviewStatus.SUPERSEDED
                    old.review_comment = (f"Remplacée par une projection plus récente (données au "
                                          f"{as_of:%d/%m/%Y}).")
            db.add(prediction)
            created.append(prediction)
    db.flush()
    for org_id in {p.organization_id for p in created}:
        count = sum(1 for p in created if p.organization_id == org_id)
        notify(db, org_id, f"À valider : {count} projection(s) de consommation annuelle", validators_only=True)
    db.commit()
    return created
