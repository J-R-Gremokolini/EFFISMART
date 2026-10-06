"""Source des DJU (degrés-jours unifiés), abstraite comme les données énergie (brief §7).

V1 : `MockWeatherProvider`, source documentée ci-dessous. À remplacer par une
source réelle (Météo-France, DJU COSTIC…) en implémentant `WeatherProvider`.
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Protocol

from app.config import settings
from app.timeutils import daterange


class WeatherProvider(Protocol):
    source: str

    def daily_dju(self, start: date, end: date) -> dict[date, float]:
        """DJU chauffage par jour, jours inclus."""
        ...

    def daily_temperature(self, start: date, end: date) -> dict[date, float]:
        """Température extérieure moyenne journalière (°C), jours inclus."""
        ...

    def daily_solar(self, start: date, end: date) -> dict[date, float]:
        """Rayonnement solaire global horizontal, moyenne journalière (W/m²) ; vide si indisponible."""
        ...


NORMALS_SOURCE = ("normales simplifiées : sinusoïdes calées sur Paris (température 5 °C mi-janvier, 20,5 °C "
                  "mi-juillet ; rayonnement 30 W/m² fin décembre, 250 W/m² fin juin)")
MJ_PER_DAY_TO_W = 1e6 / 86400  # MJ/m² par jour → W/m² moyens
# Température sol-air (Catalina, Virgone, Blanco, CIFQ 2009) : T_sa = T_air + α·G / h_e.
SOLAR_ABSORPTION = 0.6  # α, coefficient d'absorption moyen de l'enveloppe
EXTERNAL_EXCHANGE = 23.0  # h_e, coefficient d'échange extérieur (W/m²·K), vent moyen


def sol_air(temperature: float, solar: float, factor: float = 1.0) -> float:
    """Température sol-air ; `factor` module l'exposition au soleil (part vitrée, orientation)."""
    return temperature + factor * SOLAR_ABSORPTION * solar / EXTERNAL_EXCHANGE


def normal_mean_temperature(day: date) -> float:
    """Température moyenne « normale » du jour, sans aléa (utilisée pour projeter l'avenir)."""
    day_of_year = day.timetuple().tm_yday
    return 12.75 - 7.75 * math.cos(2 * math.pi * (day_of_year - 15) / 365.25)


def normal_solar(day: date) -> float:
    """Rayonnement global horizontal « normal » du jour (W/m² moyens sur 24 h), sans aléa."""
    day_of_year = day.timetuple().tm_yday
    return 140.0 - 110.0 * math.cos(2 * math.pi * (day_of_year + 10) / 365.25)


def normal_dju(day: date, base_temperature: float | None = None) -> float:
    base = settings.dju_base_temperature if base_temperature is None else base_temperature
    return max(0.0, base - normal_mean_temperature(day))


class MockWeatherProvider:
    """Température moyenne journalière synthétique.

    Modèle : sinusoïde annuelle calée sur les normales de Paris-Montsouris
    (Tmoy ≈ 5 °C mi-janvier, ≈ 20,5 °C mi-juillet) + bruit déterministe ±3 °C
    (graine = date, donc reproductible entre processus).
    DJU méthode « météo » : max(0, T_base − Tmoy), T_base = 18 °C par défaut.
    """

    source = (
        "MOCK V1 — sinusoïde calée sur les normales Paris-Montsouris, bruit ±3 °C, "
        "DJU méthode météo base 18 °C"
    )

    def __init__(self, base_temperature: float | None = None) -> None:
        self.base_temperature = (
            settings.dju_base_temperature if base_temperature is None else base_temperature
        )

    @staticmethod
    def mean_temperature(day: date) -> float:
        noise = random.Random(f"temperature:{day.isoformat()}").uniform(-3.0, 3.0)
        return normal_mean_temperature(day) + noise

    @staticmethod
    def solar(day: date) -> float:
        """Rayonnement synthétique : normale saisonnière × nébulosité aléatoire (graine = date)."""
        return normal_solar(day) * random.Random(f"solar:{day.isoformat()}").uniform(0.4, 1.3)

    def dju(self, day: date) -> float:
        return max(0.0, self.base_temperature - self.mean_temperature(day))

    def daily_dju(self, start: date, end: date) -> dict[date, float]:
        return {day: self.dju(day) for day in daterange(start, end)}

    def daily_temperature(self, start: date, end: date) -> dict[date, float]:
        return {day: self.mean_temperature(day) for day in daterange(start, end)}

    def daily_solar(self, start: date, end: date) -> dict[date, float]:
        return {day: self.solar(day) for day in daterange(start, end)}


class WeatherUnavailableError(RuntimeError):
    """Source météo injoignable : le détecteur climatique saute la journée plutôt que de conclure à tort."""


class OpenMeteoWeatherProvider:
    """Températures moyennes journalières réelles (Open-Meteo, sans clé d'API).

    - jours de plus de 5 jours : API d'archive (réanalyse ERA5, publiée avec quelques jours de décalage) ;
    - jours récents : API de prévision avec l'historique des derniers jours (`past_days`).
    DJU méthode « météo » : max(0, T_base − Tmoy). Résultats mis en cache pour la durée du processus.
    Usage commercial : prévoir l'offre payante Open-Meteo (licence de l'API gratuite non commerciale).
    """

    ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    ARCHIVE_LAG_DAYS = 5
    _cache: dict[tuple[float, float, date], float] = {}  # températures
    _solar_cache: dict[tuple[float, float, date], float] = {}  # rayonnement, W/m²

    def __init__(self, latitude: float, longitude: float, base_temperature: float | None = None) -> None:
        self.latitude, self.longitude = round(float(latitude), 4), round(float(longitude), 4)
        self.base_temperature = settings.dju_base_temperature if base_temperature is None else base_temperature
        self.source = f"Open-Meteo, point {self.latitude}, {self.longitude} ; DJU méthode météo base {self.base_temperature:g} °C"

    def _fetch(self, url: str, params: dict) -> dict[date, tuple[float, float | None]]:
        """(température moyenne, rayonnement moyen en W/m² ou None) par jour."""
        from app.services import net  # import local : évite un cycle providers ↔ services

        try:
            with net.client() as http:
                response = http.get(url, params={
                    "latitude": self.latitude, "longitude": self.longitude,
                    "daily": "temperature_2m_mean,shortwave_radiation_sum",
                    "timezone": settings.timezone, **params,
                })
            response.raise_for_status()
            daily = response.json()["daily"]
        except Exception as exc:  # réseau, format inattendu…
            raise WeatherUnavailableError(f"Open-Meteo indisponible : {exc.__class__.__name__}") from exc
        radiation = daily.get("shortwave_radiation_sum") or [None] * len(daily["time"])
        return {date.fromisoformat(d): (float(t), float(g) * MJ_PER_DAY_TO_W if g is not None else None)
                for d, t, g in zip(daily["time"], daily["temperature_2m_mean"], radiation) if t is not None}

    def mean_temperatures(self, start: date, end: date) -> dict[date, float]:
        key = (self.latitude, self.longitude)
        missing = [d for d in daterange(start, end) if (*key, d) not in self._cache]
        if missing:
            from app.timeutils import today_local

            today = today_local()
            limit = today - timedelta(days=self.ARCHIVE_LAG_DAYS)
            old = [d for d in missing if d < limit]
            recent = [d for d in missing if d >= limit]
            fetched: dict[date, tuple[float, float | None]] = {}
            if old:
                fetched |= self._fetch(self.ARCHIVE_URL, {"start_date": min(old).isoformat(),
                                                           "end_date": max(old).isoformat()})
            if recent:
                fetched |= self._fetch(self.FORECAST_URL, {"past_days": min(92, (today - min(recent)).days + 1),
                                                            "forecast_days": 1})
            for day, (temperature, solar) in fetched.items():
                self._cache[(*key, day)] = temperature
                if solar is not None:
                    self._solar_cache[(*key, day)] = solar
        return {d: self._cache[(*key, d)] for d in daterange(start, end) if (*key, d) in self._cache}

    def daily_dju(self, start: date, end: date) -> dict[date, float]:
        return {d: max(0.0, self.base_temperature - t) for d, t in self.mean_temperatures(start, end).items()}

    def daily_temperature(self, start: date, end: date) -> dict[date, float]:
        return self.mean_temperatures(start, end)

    def daily_solar(self, start: date, end: date) -> dict[date, float]:
        self.mean_temperatures(start, end)  # même appel : remplit aussi le cache du rayonnement
        key = (self.latitude, self.longitude)
        return {d: self._solar_cache[(*key, d)] for d in daterange(start, end) if (*key, d) in self._solar_cache}


def get_weather_provider() -> WeatherProvider:
    """Source simulée par défaut ; la source réelle se choisit dans les intégrations (`integrations.weather_provider`)."""
    return MockWeatherProvider()
