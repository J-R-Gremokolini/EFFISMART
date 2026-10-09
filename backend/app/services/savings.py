"""F1 — Évolution avant / après recommandations : économies mesurées.

Méthode inspirée du protocole IPMVP (option C, au compteur) :
1. période de référence : depuis la première anomalie validée à l'origine de la recommandation (au moins
   60 jours, au plus 365) jusqu'à la veille de l'application ;
2. modèle de référence : le moteur de prévision v2 (`forecasting`), appris sur cette seule période ;
3. période de suivi : du lendemain de l'application à la dernière donnée (au moins 14 jours) ;
4. consommation attendue sans l'action = modèle de référence alimenté par la météo réelle du suivi ;
   économie = attendue − mesurée ;
5. contrôle IPMVP : si le suivi sort de la plage de températures de la référence, le modèle extrapole ;
   la plateforme le signale et baisse sa confiance.

La mesure est une sortie algorithmique (principe P1) : présentée avec son raisonnement et son niveau de
confiance, elle n'est montrée au client qu'une fois validée par un humain, figée telle que validée.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Drift, Fluid, Recommendation, RecommendationKind, ReviewStatus
from app.providers.weather import WeatherUnavailableError
from app.services import forecasting, memo
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of, emission_factors_at, estimated_price
from app.services.drift import daily_kwh
from app.services.validation import Assessment, fr
from app.timeutils import to_local

MIN_BASELINE_DAYS = 60
MAX_BASELINE_DAYS = 365
MIN_REPORTING_DAYS = 14
METHOD = "Mesure avant / après v1 (IPMVP option C ; référence : moteur de prévision v2)"


@dataclass
class SavingsMeasurement:
    recommendation_id: int
    applied_on: date
    baseline_start: date
    baseline_end: date
    reporting_start: date
    reporting_end: date
    reporting_days: int
    expected_kwh: float
    actual_kwh: float
    saved_kwh: float
    saved_pct: float
    saved_eur: float
    saved_kgco2e: float
    annualized_kwh: float
    annualized_eur: float
    uncertainty_kwh: float
    estimated_gain_kwh: float | None  # gain annuel estimé quand la recommandation a été proposée
    model: str
    cv_rmse: float
    confidence: float
    confidence_factors: list[dict]
    reasoning: list[str]
    weekly: list[dict] = field(default_factory=list)  # [{"week", "expected", "actual"}]
    method: str = METHOD

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("applied_on", "baseline_start", "baseline_end", "reporting_start", "reporting_end"):
            data[key] = data[key].isoformat()
        return data


def availability(rec: Recommendation) -> date | None:
    """Premier jour où une mesure est possible (14 jours de suivi après l'application)."""
    if rec.applied_at is None:
        return None
    return to_local(rec.applied_at).date() + timedelta(days=MIN_REPORTING_DAYS + 1)


def measure(db: Session, rec: Recommendation) -> SavingsMeasurement | None:
    """Mesure des économies d'une recommandation appliquée ; None si les données ne le permettent pas encore."""
    from app.services import integrations  # import local : évite un cycle

    dp = rec.delivery_point
    if rec.status != ReviewStatus.APPLIED or rec.applied_at is None or dp is None or not has_active_consent(db, dp.id):
        return None
    if rec.kind == RecommendationKind.LOAD_SHIFT:
        return None  # F12 : l'énergie ne change pas, le gain est financier ; pas de mesure avant / après en kWh
    applied_on = to_local(rec.applied_at).date()
    reporting_end = data_as_of(db, [dp.id])
    if reporting_end is None or (reporting_end - applied_on).days < MIN_REPORTING_DAYS:
        return None
    origin_days = [d.day for d in (db.get(Drift, i) for i in [rec.drift_id, *(rec.supporting_drift_ids or [])] if i)
                   if d is not None]
    first_anomaly = min(origin_days) if origin_days else applied_on - timedelta(days=MIN_BASELINE_DAYS)
    baseline_start = max(min(first_anomaly, applied_on - timedelta(days=MIN_BASELINE_DAYS)),
                         applied_on - timedelta(days=MAX_BASELINE_DAYS))
    baseline_end = applied_on - timedelta(days=1)
    reporting_start = applied_on + timedelta(days=1)  # le jour de l'intervention est ignoré
    baseline = daily_kwh(db, dp.id, baseline_start, baseline_end)
    reporting = daily_kwh(db, dp.id, reporting_start, reporting_end)
    if len(baseline) < MIN_BASELINE_DAYS or len(reporting) < MIN_REPORTING_DAYS:
        return None
    weather = integrations.weather_provider(db)
    weather_start = baseline_start - timedelta(days=max(forecasting.INERTIA_CHOICES))
    try:
        climate = forecasting.Climate(weather.daily_temperature(weather_start, reporting_end),
                                      weather.daily_solar(weather_start, reporting_end))
    except WeatherUnavailableError:
        return None
    # Le modèle de référence ne dépend que de la période de référence et de sa météo : mémorisé, il n'est réappris que
    # si une mesure de cette période change (sinon chaque affichage du tableau de bord le réapprendrait).
    key = (dp.id, dp.fluid, baseline_start, baseline_end, str(getattr(weather, "source", "")),
           memo.measurement_stamp(db, dp.id, baseline_start, baseline_end))
    forecast = memo.cached("forecast", key, lambda: forecasting.build_forecast(
        baseline, climate, base=settings.dju_base_temperature, cooling=dp.fluid == Fluid.ELEC), copy_result=False)
    if forecast is None:
        return None
    expected = actual = 0.0
    weekly: dict[date, list[float]] = {}
    for day, value in sorted(reporting.items()):
        estimate = forecast.predict(day, climate)
        if estimate is None:
            continue
        expected += estimate
        actual += value
        week = weekly.setdefault(day - timedelta(days=day.weekday()), [0.0, 0.0])
        week[0] += estimate
        week[1] += value
    if expected <= 0:
        return None
    saved = expected - actual
    days = (reporting_end - reporting_start).days + 1
    factor = emission_factors_at(db, reporting_end).get(dp.fluid)
    uncertainty = expected * max(forecast.chosen.mean_block_error, 0.02)
    # IPMVP : la référence doit couvrir les conditions du suivi ; sinon le modèle extrapole.
    reference_temps = [climate.temperature[d] for d in baseline if d in climate.temperature]
    low, high = min(reference_temps) - 1.0, max(reference_temps) + 1.0
    followed = [climate.temperature[d] for d in reporting if d in climate.temperature]
    outside = sum(1 for t in followed if not low <= t <= high) / len(followed) if followed else 0.0

    a = Assessment(algorithm=METHOD)
    origin = f"la première anomalie validée ({first_anomaly:%d/%m/%Y})" if origin_days else "l'application"
    a.step(f"Action : « {rec.title} », déclarée appliquée le {applied_on:%d/%m/%Y}.")
    a.step(f"Période de référence (avant) : du {baseline_start:%d/%m/%Y} au {baseline_end:%d/%m/%Y}, à partir de "
           f"{origin} ; {len(baseline)} jours de données du compteur {dp.external_ref}.")
    a.step(f"Modèle de référence : {forecast.chosen.config.describe()} ; erreur journalière hors échantillon "
           f"{fr(forecast.chosen.cv_rmse * 100, 1)} %.")
    a.step(f"Période de suivi (après) : du {reporting_start:%d/%m/%Y} au {reporting_end:%d/%m/%Y} ({days} jours) : "
           f"{fr(expected)} kWh attendus sans l'action (modèle de référence et météo réelle), {fr(actual)} kWh mesurés.")
    a.step(f"Économie : {fr(saved)} kWh ({fr(saved / expected * 100, 1)} %) ± {fr(uncertainty)} kWh, soit "
           f"{fr(saved * 365 / days)} kWh par an au même rythme"
           + (f" (gain estimé à la proposition : {fr(rec.gain_kwh)} kWh par an)." if rec.gain_kwh else "."))
    if outside > 0.2:
        a.step(f"Prudence : {fr(outside * 100)} % des jours de suivi ont une température hors de la plage de la "
               f"période de référence ({fr(low + 1, 1)} à {fr(high - 1, 1)} °C). Le modèle extrapole ; le protocole "
               "IPMVP recommande une référence couvrant les mêmes conditions. Refaire la mesure après une saison "
               "comparable la consolidera.")
        a.factor(f"Suivi hors des conditions de la référence ({fr(outside * 100)} % des jours) : extrapolation", -0.1)
    a.step("Statut : mesure de la plateforme, à valider avant d'être montrée au client.")
    cv = forecast.chosen.cv_rmse
    if cv <= 0.10:
        a.factor(f"Modèle de référence précis ({fr(cv * 100, 1)} % d'erreur journalière)", 0.15)
    elif cv <= 0.20:
        a.factor(f"Modèle de référence correct ({fr(cv * 100, 1)} % d'erreur journalière)", 0.05)
    elif cv > 0.30:
        a.factor(f"Modèle de référence imprécis ({fr(cv * 100, 1)} % d'erreur journalière)", -0.15)
    if days >= 60:
        a.factor(f"Suivi long ({days} jours)", 0.1)
    elif days < 30:
        a.factor(f"Suivi court ({days} jours) : effet saisonnier possible", -0.1)
    if abs(saved) > 2 * uncertainty:
        a.factor("Écart nettement supérieur à l'incertitude du modèle", 0.1)
    elif abs(saved) <= uncertainty:
        a.factor("Écart dans la marge d'incertitude : économie non démontrée", -0.15)
    if str(getattr(weather, "source", "")).startswith("MOCK"):
        a.factor("Météo simulée (démonstration)", -0.05)

    price = estimated_price(dp.fluid)
    return SavingsMeasurement(
        recommendation_id=rec.id, applied_on=applied_on, baseline_start=baseline_start, baseline_end=baseline_end,
        reporting_start=reporting_start, reporting_end=reporting_end, reporting_days=days,
        expected_kwh=round(expected, 1), actual_kwh=round(actual, 1), saved_kwh=round(saved, 1),
        saved_pct=round(saved / expected, 4), saved_eur=round(saved * price, 1),
        saved_kgco2e=round(saved * factor.factor_kgco2_per_kwh, 1) if factor else 0.0,
        annualized_kwh=round(saved * 365 / days, 1), annualized_eur=round(saved * 365 / days * price, 1),
        uncertainty_kwh=round(uncertainty, 1), estimated_gain_kwh=rec.gain_kwh,
        model=forecast.chosen.config.describe(), cv_rmse=round(cv, 4), confidence=a.confidence,
        confidence_factors=[{"label": label, "delta": round(delta, 2)} for label, delta in a.factors],
        reasoning=list(a.reasoning),
        weekly=[{"week": week.isoformat(), "expected": round(v[0], 1), "actual": round(v[1], 1)}
                for week, v in sorted(weekly.items())],
    )
