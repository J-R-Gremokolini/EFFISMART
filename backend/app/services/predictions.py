"""Prévisions (principe P1) : projection de la consommation de l'année civile, par site et par énergie.

Méthode (v2, voir `app/services/forecasting.py` et ses références) :
1. sur les 365 derniers jours, plusieurs modèles de consommation journalière sont mis en concurrence
   (signature linéaire v1, signature d'ordre 2, jours pertinents ; avec ou sans inertie et sol-air,
   classes de fonctionnement déduites des données) et validés hors échantillon ; le meilleur est retenu,
   le plus simple à précision équivalente ;
2. du 1er janvier à la dernière donnée : consommation mesurée ;
3. reste de l'année : le modèle retenu, alimenté par la météo normale ;
4. intervalle : erreur de validation (au plus forte sur une période de deux mois, plancher 3 %) et
   sensibilité au climat (normales ± 1,5 °C), combinées quadratiquement.

Une prévision est *proposée* : seul un humain la valide, et le client ne la voit qu'une fois validée.
Elle est recalculée au plus une fois par mois (ou à la demande de l'auditeur) ; une projection plus
récente remplace une projection encore non validée, jamais une projection validée.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DeliveryPoint, Fluid, Prediction, PredictionKind, ReviewStatus, Site
from app.providers.weather import (
    NORMALS_SOURCE,
    WeatherProvider,
    WeatherUnavailableError,
    normal_mean_temperature,
    normal_solar,
)
from app.services import forecasting
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of
from app.services.drift import daily_kwh
from app.services.validation import Assessment, apply_assessment, fr, fr_pct, notify
from app.timeutils import daterange

logger = logging.getLogger(__name__)

ALGORITHM = ("Projection annuelle v2 (classes de fonctionnement, inertie, sol-air, jours pertinents ; "
             "sélection par validation hors échantillon)")
FIT_DAYS = 365
MIN_FIT_DAYS = 60
CLIMATE_SHIFT = 1.5  # °C : écart plausible d'une saison à la normale
MIN_MODEL_ERROR = 0.03  # changements d'usage que l'historique ne peut pas révéler
FLUID_LABELS = {Fluid.ELEC: "électricité", Fluid.GAS: "gaz"}


def _site_daily(db: Session, points: list[DeliveryPoint], start: date, end: date) -> dict[date, float]:
    """Consommation journalière du site : seuls les jours où chaque compteur a une donnée sont retenus."""
    series = [daily_kwh(db, dp.id, start, end) for dp in points]
    days = set(series[0]).intersection(*series[1:]) if series else set()
    return {day: sum(s[day] for s in series) for day in days}


def _pct(value: float, digits: int = 1) -> str:
    return f"{fr(value * 100, digits)} %"


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
    weather_start = min(fit_start, jan1) - timedelta(days=max(forecasting.INERTIA_CHOICES))
    try:
        temperature = weather.daily_temperature(weather_start, as_of)
        solar = weather.daily_solar(weather_start, as_of)
    except WeatherUnavailableError:
        logger.warning("Météo indisponible : projection %s ignorée pour le site #%s", year, site.id)
        return None
    remaining = list(daterange(as_of + timedelta(days=1), dec31)) if as_of < dec31 else []
    # Passé : météo observée (normale si un jour manque) ; avenir : météo normale.
    climate = forecasting.Climate(
        {d: temperature.get(d, normal_mean_temperature(d)) for d in daterange(weather_start, dec31)},
        {d: solar.get(d, normal_solar(d)) for d in daterange(weather_start, dec31)} if solar else {},
    )
    forecast = forecasting.build_forecast(fit, climate, base=settings.dju_base_temperature,
                                          cooling=fluid == Fluid.ELEC)
    if forecast is None:
        return None

    # Année en cours : mesuré, jours manquants reconstitués, reste projeté aux normales.
    monthly: dict[int, dict[str, float]] = {m: {"measured": 0.0, "predicted": 0.0} for m in range(1, 13)}
    measured = reconstituted_kwh = 0.0
    reconstituted_days = 0
    for day in daterange(jan1, as_of):
        if day in history:
            measured += history[day]
            monthly[day.month]["measured"] += history[day]
        else:
            value = forecast.predict(day, climate) or 0.0
            reconstituted_kwh += value
            reconstituted_days += 1
            monthly[day.month]["predicted"] += value
    rest = 0.0
    for day in remaining:
        value = forecast.predict(day, climate) or 0.0
        rest += value
        monthly[day.month]["predicted"] += value
    predicted = measured + reconstituted_kwh + rest

    # Intervalle : erreur du modèle (validation) et sensibilité au climat, indépendantes.
    climate_margin = 0.0
    for delta in (-CLIMATE_SHIFT, CLIMATE_SHIFT):
        shifted = climate.shifted(remaining, delta)
        climate_margin = max(climate_margin, abs(sum(forecast.predict(d, shifted) or 0.0 for d in remaining) - rest))
    model_error = max(forecast.chosen.max_block_error, MIN_MODEL_ERROR)
    margin = math.hypot(model_error * rest, climate_margin)

    previous = _site_daily(db, points, date(year - 1, 1, 1), date(year - 1, 12, 31))
    reference = sum(previous.values()) if len(previous) >= 360 else None
    reference_monthly = {m: 0.0 for m in range(1, 13)}
    for day, value in previous.items():
        reference_monthly[day.month] += value

    chosen = forecast.chosen
    config = chosen.config
    a = Assessment(algorithm=ALGORITHM)
    a.step(f"Données : consommation journalière d'{FLUID_LABELS[fluid]} du site {site.name} "
           f"({len(points)} compteur(s)), {len(fit)} jours sur les {FIT_DAYS} derniers, jusqu'au {as_of:%d/%m/%Y}.")
    if forecast.classes.learned:
        a.step(f"Classes de fonctionnement déduites des données (Paudel, 2016) : {forecast.classes.describe()}.")
    else:
        a.step("Classes de fonctionnement : jours ouvrés et week-end (historique trop court pour les déduire).")
    a.step(f"Validation hors échantillon (Catalina et al., 2009) : {forecast.validation_blocks} périodes "
           f"d'environ {forecast.block_days} jours, chacune retirée de l'apprentissage puis prédite. Meilleure erreur "
           "journalière (CV(RMSE)) par famille : "
           + " ; ".join(f"{forecasting.FAMILY_LABELS[family]} {_pct(e.cv_rmse)}"
                        for family, e in sorted(forecast.by_family.items(),
                                                key=lambda item: forecasting.FAMILY_RANK[item[0]])) + ".")
    best = min(e.cv_rmse for e in forecast.by_family.values())
    simpler = " Les variantes plus complexes n'améliorent pas la prévision de plus de 2 % : le modèle le plus " \
              "simple est conservé." if chosen.cv_rmse > best else ""
    a.step(f"Modèle retenu : {config.describe()}. Erreur journalière {_pct(chosen.cv_rmse)} ; écart sur le total "
           f"d'une période : {_pct(chosen.mean_block_error)} en moyenne, {_pct(chosen.max_block_error)} au plus."
           + simpler)
    if config.family != "v1":
        if config.inertia:
            a.step(f"Inertie : la consommation répond à la température moyenne des {config.inertia + 1} derniers jours "
                   "(Paudel, 2016 : 1 à 2 jours pour un bâtiment conventionnel, 3 pour un bâtiment basse "
                   "consommation).")
        else:
            a.step("Inertie : tenir compte des jours précédents n'améliore pas la prévision ; la consommation suit "
                   "la température du jour.")
    if not forecast.solar_available:
        a.step("Apports solaires : rayonnement indisponible pour cet historique ; température sol-air non testée.")
    elif config.solar:
        a.step("Apports solaires : la température sol-air (température + 0,6 × rayonnement / 23, Catalina et al., "
               "2009) améliore la prévision ; elle est retenue.")
    else:
        a.step("Apports solaires : la température sol-air a été testée sans améliorer la prévision ; non retenue.")
    a.step(f"Consommé du 1er janvier au {as_of:%d/%m/%Y} : {fr(measured)} kWh mesurés"
           + (f", plus {fr(reconstituted_kwh)} kWh reconstitués pour {reconstituted_days} jour(s) sans donnée."
              if reconstituted_days else "."))
    if remaining:
        a.step(f"Reste de l'année ({len(remaining)} jours) : {fr(rest)} kWh projetés avec la météo normale "
               f"({NORMALS_SOURCE}).")
    a.step(f"Projection {year} : {fr(predicted)} kWh, intervalle {fr(max(0.0, predicted - margin))} – "
           f"{fr(predicted + margin)} kWh : erreur du modèle ({_pct(model_error)} du reste de l'année, au plus forte "
           f"en validation, plancher 3 %) et sensibilité au climat (normales ± {fr(CLIMATE_SHIFT, 1)} °C : "
           f"± {fr(climate_margin)} kWh), combinées quadratiquement.")
    if reference is not None:
        a.step(f"Comparaison : {year - 1} = {fr(reference)} kWh, soit {fr_pct((predicted - reference) / reference * 100)} "
               "(écart brut, non corrigé du climat).")
    else:
        a.step(f"Comparaison : année {year - 1} incomplète, pas de référence.")
    a.step("Statut : proposition de la plateforme, à valider avant d'être montrée au client ou utilisée dans un rapport.")

    if chosen.cv_rmse <= 0.10:
        a.factor(f"Erreur de validation faible ({_pct(chosen.cv_rmse)} par jour, sur des périodes non apprises)", 0.15)
    elif chosen.cv_rmse <= 0.20:
        a.factor(f"Erreur de validation modérée ({_pct(chosen.cv_rmse)} par jour)", 0.05)
    elif chosen.cv_rmse <= 0.30:
        a.factor(f"Erreur de validation notable ({_pct(chosen.cv_rmse)} par jour)", -0.05)
    else:
        a.factor(f"Erreur de validation élevée ({_pct(chosen.cv_rmse)} par jour)", -0.15)
    if chosen.max_block_error <= 0.05:
        a.factor(f"Écart maximal sur le total d'une période : {_pct(chosen.max_block_error)} (≤ 5 %)", 0.05)
    elif chosen.max_block_error > 0.15:
        a.factor(f"Écart maximal sur le total d'une période : {_pct(chosen.max_block_error)}", -0.1)
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
        a.factor("Météo normale simplifiée, non spécifique au site", -0.05)
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
        # Pour la famille retenue, la variante retenue ; pour les autres, leur meilleure variante.
        model_comparison=[
            {"family": family, "label": forecasting.FAMILY_LABELS[family], "config": e.config.describe(),
             "cv_rmse": round(e.cv_rmse, 4), "max_block_error": round(e.max_block_error, 4),
             "chosen": e is chosen}
            for family, e in sorted(((f, chosen if f == config.family else best_of_family)
                                     for f, best_of_family in forecast.by_family.items()),
                                    key=lambda item: forecasting.FAMILY_RANK[item[0]])
        ],
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
