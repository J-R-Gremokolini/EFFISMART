"""Source des DJU (degrés-jours unifiés), abstraite comme les données énergie (brief §7).

V1 : `MockWeatherProvider`, source documentée ci-dessous. À remplacer par une
source réelle (Météo-France, DJU COSTIC…) en implémentant `WeatherProvider`.
"""
from __future__ import annotations

import math
import random
from datetime import date
from typing import Protocol

from app.config import settings
from app.timeutils import daterange


class WeatherProvider(Protocol):
    source: str

    def daily_dju(self, start: date, end: date) -> dict[date, float]:
        """DJU chauffage par jour, jours inclus."""
        ...


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
        day_of_year = day.timetuple().tm_yday
        seasonal = 12.75 - 7.75 * math.cos(2 * math.pi * (day_of_year - 15) / 365.25)
        noise = random.Random(f"temperature:{day.isoformat()}").uniform(-3.0, 3.0)
        return seasonal + noise

    def dju(self, day: date) -> float:
        return max(0.0, self.base_temperature - self.mean_temperature(day))

    def daily_dju(self, start: date, end: date) -> dict[date, float]:
        return {day: self.dju(day) for day in daterange(start, end)}


def get_weather_provider() -> WeatherProvider:
    return MockWeatherProvider()
