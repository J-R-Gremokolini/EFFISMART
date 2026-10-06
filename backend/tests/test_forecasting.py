"""Moteur de prévision v2 (Catalina et al., 2009 ; Paudel, 2016) : sur des données synthétiques dont on
connaît le comportement réel, le moteur doit le retrouver par validation hors échantillon."""
import math
import random
from datetime import date, timedelta

import pytest

from app.services import forecasting
from app.services.forecasting import Climate, Config

START = date(2024, 1, 1)
DAYS = [START + timedelta(days=i) for i in range(365)]


def _climate(seed: int = 1) -> Climate:
    """Température saisonnière avec des épisodes de plusieurs jours, rayonnement indépendant."""
    rng = random.Random(seed)
    temperature, solar, anomaly = {}, {}, 0.0
    for i in range(-5, 365):  # quelques jours avant le début, pour l'inertie
        day = START + timedelta(days=i)
        angle = 2 * math.pi * day.timetuple().tm_yday / 365
        anomaly = 0.7 * anomaly + rng.gauss(0, 2.5)  # persistance d'un jour sur l'autre
        temperature[day] = 12 - 8 * math.cos(angle - 2 * math.pi * 15 / 365) + anomaly
        solar[day] = max(10.0, 140 - 110 * math.cos(angle + 2 * math.pi * 10 / 365) + rng.gauss(0, 50))
    return Climate(temperature, solar)


def _noise(rng: random.Random, value: float) -> float:
    return value * (1 + rng.gauss(0, 0.01))


def test_sol_air_and_inertia_formulas():
    climate = Climate({date(2025, 1, 2): 5.0, date(2025, 1, 1): 3.0, date(2024, 12, 31): 1.0},
                      {date(2025, 1, 2): 115.0})
    assert climate.air(date(2025, 1, 2), solar=True) == pytest.approx(5.0 + 0.6 * 115 / 23)  # +3 °C
    assert climate.effective(date(2025, 1, 2), inertia=2, solar=False) == pytest.approx(3.0)
    assert climate.effective(date(2025, 1, 2), inertia=1, solar=True) is None  # rayonnement manquant


def test_inertia_of_the_building_is_recovered():
    """Le besoin suit la température moyenne des 3 derniers jours : le moteur retient 2 jours d'inertie."""
    climate, rng = _climate(), random.Random(2)
    consumption = {d: _noise(rng, 400 + 30 * max(0.0, 18 - climate.effective(d, 2, False))) for d in DAYS}
    forecast = forecasting.build_forecast(consumption, climate, base=18, cooling=False)
    assert forecast.chosen.config.family == "poly2"
    assert forecast.chosen.config.inertia == 2 and not forecast.chosen.config.solar
    assert forecast.chosen.cv_rmse < forecast.by_family["v1"].cv_rmse / 2  # bien meilleur que la v1


def test_solar_gains_are_recovered():
    """Le besoin dépend de la température sol-air : le moteur retient le rayonnement."""
    climate, rng = _climate(), random.Random(3)
    consumption = {d: _noise(rng, 400 + 30 * max(0.0, 18 - climate.air(d, True))) for d in DAYS}
    forecast = forecasting.build_forecast(consumption, climate, base=18, cooling=False)
    assert forecast.solar_available and forecast.chosen.config.solar
    assert forecast.chosen.config.inertia == 0


def test_operation_classes_are_learned_from_the_data():
    """Lundi à 80 %, mardi–vendredi à 100 %, week-end à 50 % : trois classes."""
    climate, rng = _climate(), random.Random(4)
    level = {0: 0.8, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 0.5, 6: 0.5}
    consumption = {d: _noise(rng, level[d.weekday()] * (400 + 30 * max(0.0, 18 - climate.temperature[d])))
                   for d in DAYS}
    classes = forecasting.learn_operation_classes(consumption)
    assert classes.learned and classes.groups == ((0,), (1, 2, 3, 4), (5, 6))
    assert "mardi–vendredi" in classes.describe()


def test_relevant_days_win_on_a_non_linear_regulation():
    """Chauffage déclenché en tout ou rien sous 12 °C : aucune signature ne le décrit, les jours analogues oui."""
    climate, rng = _climate(), random.Random(5)
    consumption = {d: _noise(rng, 900 if climate.temperature[d] < 12 else 300) for d in DAYS}
    forecast = forecasting.build_forecast(consumption, climate, base=18, cooling=False)
    assert forecast.chosen.config.family == "relevant"
    assert forecast.chosen.cv_rmse < forecast.by_family["poly2"].cv_rmse


def test_simplest_model_is_kept_when_nothing_is_better():
    """Données conformes à la v1 : les variantes n'apportent rien, la v1 est conservée (parcimonie)."""
    climate, rng = _climate(), random.Random(6)
    consumption = {d: _noise(rng, (300 if d.weekday() >= 5 else 500) + 25 * max(0.0, 18 - climate.temperature[d]))
                   for d in DAYS}
    forecast = forecasting.build_forecast(consumption, climate, base=18, cooling=False)
    assert forecast.chosen.config == Config("v1")


def test_validation_is_out_of_sample():
    """Chaque période est prédite par un modèle qui ne l'a pas vue : un modèle qui « apprend par cœur »
    (un seul jour analogue) n'est pas favorisé."""
    climate, rng = _climate(), random.Random(7)
    consumption = {d: rng.uniform(300, 900) for d in DAYS}  # pur bruit : rien à apprendre
    forecast = forecasting.build_forecast(consumption, climate, base=18, cooling=False)
    assert forecast.chosen.cv_rmse > 0.2  # l'erreur hors échantillon reste élevée, honnêtement


def test_not_enough_history():
    climate = _climate()
    assert forecasting.build_forecast({d: 100.0 for d in DAYS[:30]}, climate, base=18, cooling=False) is None
