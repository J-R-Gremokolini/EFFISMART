"""F11 — Modèle de consommation : le moteur de prévision v2 appris sur 12 à 24 mois d'historique.

Modèle mathématique (régressions sur la température, l'inertie, le soleil et les classes de fonctionnement)
renforcé par l'apprentissage : le meilleur modèle est choisi par validation hors échantillon, et la famille
« jours pertinents » apprend des jours analogues (voir `forecasting`). Trois usages :
1. consommation attendue d'un jour à la météo réelle : une anomalie devient un écart au modèle, pas un
   dépassement de seuil arbitraire (améliore F2) ;
2. consommation d'une année de météo normale, corrigée du climat : trajectoire Décret Tertiaire (F11) ;
3. part de la consommation liée au climat (chauffage, refroidissement) : simulateur d'économies (F9).

Moins de 12 mois d'historique : mode dégradé, le modèle n'est pas utilisé (première année d'un client).
Les modèles appris sont gardés en mémoire, repérés par les points, la fin d'apprentissage et une empreinte des
données : une nouvelle donnée dans la fenêtre provoque un nouvel apprentissage.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DeliveryPoint, Fluid, Measurement
from app.providers.weather import WeatherProvider, WeatherUnavailableError, normal_mean_temperature, normal_solar
from app.services import forecasting
from app.timeutils import daterange, local_day_bounds, to_local

logger = logging.getLogger(__name__)

MIN_HISTORY_DAYS = 365
MAX_HISTORY_DAYS = 730
MAX_RELIABLE_CV = 0.30  # au-delà, le modèle n'explique pas assez la consommation pour juger un écart
NEUTRAL_TEMPERATURE = 20.0  # ni chauffage (base 18 °C) ni refroidissement (base 22 °C)
_CACHE: dict[tuple, ConsumptionModel | None] = {}
_CACHE_SIZE = 256


@dataclass
class ConsumptionModel:
    forecast: forecasting.Forecast
    climate: forecasting.Climate  # météo observée jusqu'à la fin d'apprentissage, normale au-delà
    train_start: date
    train_end: date
    days: int
    fluid: Fluid

    @property
    def cv(self) -> float:
        return self.forecast.chosen.cv_rmse

    @property
    def reliable(self) -> bool:
        return self.cv <= MAX_RELIABLE_CV

    def describe(self) -> str:
        return self.forecast.chosen.config.describe()

    def predict(self, day: date, climate: forecasting.Climate | None = None) -> float | None:
        return self.forecast.predict(day, climate or self.climate)

    def normal_year(self, start: date) -> list[date]:
        return list(daterange(start, start + timedelta(days=364)))

    def normal_year_kwh(self, start: date) -> float:
        """Consommation d'une année de météo normale à partir de `start` (corrigée du climat)."""
        return sum(self.predict(day) or 0.0 for day in self.normal_year(start))

    def climate_parts(self, start: date) -> tuple[float, float, float]:
        """(chauffage, refroidissement, reste) sur une année normale : écart entre la météo normale et une
        météo neutre (20 °C, sans soleil), où ni chauffage ni refroidissement ne sont nécessaires."""
        days = self.normal_year(start)
        neutral = forecasting.Climate(
            {**self.climate.temperature, **{d - timedelta(days=k): NEUTRAL_TEMPERATURE
                                            for d in days for k in range(4)}},
            {**self.climate.solar, **{d - timedelta(days=k): 0.0 for d in days for k in range(4)}}
            if self.climate.solar else {},
        )
        heating = cooling = rest = 0.0
        for day in days:
            normal, base = self.predict(day) or 0.0, self.predict(day, neutral) or 0.0
            part = max(0.0, normal - base)
            if self.climate.temperature.get(day, NEUTRAL_TEMPERATURE) < NEUTRAL_TEMPERATURE:
                heating += part
            else:
                cooling += part
            rest += normal - part
        return heating, cooling, rest


def _daily_series(db: Session, points: list[DeliveryPoint], start: date, end: date) -> dict[date, float]:
    """Consommation journalière cumulée des points ; seuls les jours où chaque point a une donnée."""
    t0, t1 = local_day_bounds(start, end)
    per_point: dict[int, dict[date, float]] = {dp.id: defaultdict(float) for dp in points}
    rows = db.execute(select(Measurement.delivery_point_id, Measurement.time, Measurement.value_kwh).where(
        Measurement.delivery_point_id.in_([dp.id for dp in points]), Measurement.time >= t0, Measurement.time < t1))
    for dp_id, time, kwh in rows:
        per_point[dp_id][to_local(time).date()] += kwh
    series = list(per_point.values())
    days = set(series[0]).intersection(*series[1:]) if series else set()
    return {day: sum(s[day] for s in series) for day in days}


def _fingerprint(db: Session, points: list[DeliveryPoint], start: date, end: date) -> tuple:
    t0, t1 = local_day_bounds(start, end)
    count, total = db.execute(select(func.count(), func.sum(Measurement.value_kwh)).where(
        Measurement.delivery_point_id.in_([dp.id for dp in points]), Measurement.time >= t0,
        Measurement.time < t1)).one()
    return int(count or 0), round(float(total or 0.0), 1)


def climate_between(weather: WeatherProvider, start: date, end: date, horizon: date) -> forecasting.Climate:
    """Météo observée de `start` à `end`, normale ensuite jusqu'à `horizon` (jours manquants : normale)."""
    temperature = weather.daily_temperature(start, end)
    try:
        solar = weather.daily_solar(start, end)
    except WeatherUnavailableError:
        solar = {}
    return forecasting.Climate(
        {d: temperature.get(d, normal_mean_temperature(d)) for d in daterange(start, horizon)},
        {d: solar.get(d, normal_solar(d)) for d in daterange(start, horizon)} if solar else {},
    )


def train(db: Session, points: list[DeliveryPoint], train_end: date, weather: WeatherProvider) -> ConsumptionModel | None:
    """Modèle appris sur les 12 à 24 mois qui précèdent `train_end` (inclus) ; None en mode dégradé."""
    if not points:
        return None
    fluid = points[0].fluid
    train_start = train_end - timedelta(days=MAX_HISTORY_DAYS - 1)
    key = (tuple(sorted(dp.id for dp in points)), train_end, str(getattr(weather, "source", "")),
           _fingerprint(db, points, train_start, train_end))
    if key in _CACHE:
        return _CACHE[key]
    model = None
    history = _daily_series(db, points, train_start, train_end)
    if len(history) >= MIN_HISTORY_DAYS:
        first = min(history)
        try:
            climate = climate_between(weather, first - timedelta(days=max(forecasting.INERTIA_CHOICES)), train_end,
                                      train_end + timedelta(days=400))
        except WeatherUnavailableError:
            logger.warning("Météo indisponible : modèle de consommation non appris (points %s)", key[0])
            return None
        forecast = forecasting.build_forecast(history, climate, base=settings.dju_base_temperature,
                                              cooling=fluid == Fluid.ELEC)
        if forecast is not None:
            model = ConsumptionModel(forecast, climate, first, train_end, len(history), fluid)
    if len(_CACHE) >= _CACHE_SIZE:
        _CACHE.clear()
    _CACHE[key] = model
    return model


def model_for_day(db: Session, points: list[DeliveryPoint], day: date, weather: WeatherProvider) -> ConsumptionModel | None:
    """Modèle de référence pour juger un jour : appris jusqu'à la fin du mois précédent (un apprentissage par
    mois et par compteur), sans les jours à juger."""
    return train(db, points, day.replace(day=1) - timedelta(days=1), weather)


def observed(model: ConsumptionModel, weather: WeatherProvider, start: date, end: date) -> forecasting.Climate:
    """Climat du modèle, complété de la météo réellement observée de `start` à `end`."""
    temperature, solar = dict(model.climate.temperature), dict(model.climate.solar)
    begin = start - timedelta(days=max(forecasting.INERTIA_CHOICES))
    temperature.update(weather.daily_temperature(begin, end))
    if solar:
        try:
            solar.update(weather.daily_solar(begin, end))
        except WeatherUnavailableError:
            pass
    return forecasting.Climate(temperature, solar)
