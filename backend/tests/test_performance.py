"""Fluidité : calculs mémorisés jamais périmés, prédiction groupée identique, préchauffage sans erreur."""
from datetime import date, timedelta

import pytest

from app.models import DeclaredConsumption, DeclaredSource, Measurement, MeasurementStep, TariffOption
from app.repositories import TenantRepository
from app.services import forecasting, memo, tariffs, warmup
from app.services.drift import daily_kwh
from app.timeutils import local_midnight_utc, yesterday_local


def _total(db, dp, start, end) -> tuple[float, float]:
    months = tariffs.monthly_costing(db, dp, start, end)
    return sum(c.kwh for c in months.values()), sum(c.eur for c in months.values())


def test_memoized_costing_follows_the_data(db, world):
    end = yesterday_local()
    start = end - timedelta(days=30)
    kwh, eur = _total(db, world.dp_a, start, end)
    assert _total(db, world.dp_a, start, end) == (kwh, eur)
    assert memo.stats()["hits"] >= 1  # second appel servi par la mémoire
    # Une grille tarifaire : le coût change, la mémoire ne sert pas l'ancien chiffre.
    tariffs.save_contract(db, TenantRepository(db, world.auditor_a), world.auditor_a, world.dp_a.id,
                          supplier="Test", option=TariffOption.BASE, price_base=1.0, valid_from=date(2020, 1, 1))
    assert _total(db, world.dp_a, start, end)[1] == pytest.approx(kwh * 1.0)  # 1 €/kWh, sans abonnement
    # Une correction de mesure (même nombre de pas, autre valeur) : refait.
    row = db.query(Measurement).filter(Measurement.delivery_point_id == world.dp_a.id,
                                       Measurement.time >= local_midnight_utc(end)).first()
    row.value_kwh += 100
    db.commit()
    assert _total(db, world.dp_a, start, end)[0] == kwh + 100
    # Une facture saisie hors de la période couverte par la courbe : refait aussi.
    db.add(DeclaredConsumption(organization_id=world.org_a.id, delivery_point_id=world.dp_a.id,
                               period_start=start - timedelta(days=60), period_end=start - timedelta(days=31),
                               kwh=3000, source=DeclaredSource.INVOICE))
    db.commit()
    early = _total(db, world.dp_a, start - timedelta(days=60), end)[0]
    assert early >= kwh + 100 + 3000 - 1


def test_memoized_daily_series_returns_independent_copies(db, world):
    end = yesterday_local()
    first = daily_kwh(db, world.dp_a.id, end - timedelta(days=20), end)
    first[end] = -1.0  # un appelant qui modifie son résultat n'altère pas la mémoire
    second = daily_kwh(db, world.dp_a.id, end - timedelta(days=20), end)
    assert second[end] != -1.0
    db.add(Measurement(delivery_point_id=world.dp_a.id, time=local_midnight_utc(end) + timedelta(minutes=5),
                       value_kwh=50.0, step=MeasurementStep.PT30M))
    db.commit()
    assert daily_kwh(db, world.dp_a.id, end - timedelta(days=20), end)[end] == pytest.approx(second[end] + 50.0)


def test_grouped_relevant_days_prediction_is_identical():
    start = date(2025, 1, 1)
    days = [start + timedelta(days=k) for k in range(240)]
    temperature = {start - timedelta(days=5) + timedelta(days=k): 10 + 8 * ((k * 37) % 23) / 23 for k in range(260)}
    climate = forecasting.Climate(temperature)
    consumption = {d: 500 + 12 * max(0.0, 18 - temperature[d]) + (60 if d.weekday() < 5 else 0) for d in days}
    config = forecasting.Config("relevant", 2, False, 10)
    model = forecasting.fit(config, forecasting.DEFAULT_CLASSES, days[:180], consumption, climate, base=18,
                            cooling=False)
    targets = days[180:] + [start - timedelta(days=30)]  # le dernier jour n'a pas de météo : None
    assert model.predict_many(targets, climate) == [model.predict(d, climate) for d in targets]


def test_operation_class_lookup_matches_groups():
    classes = forecasting.OperationClasses(((0, 2, 4), (1, 3), (5, 6)))
    for offset in range(7):
        day = date(2026, 10, 5) + timedelta(days=offset)  # lundi 5 octobre 2026
        assert classes.of(day) == next(i for i, g in enumerate(classes.groups) if day.weekday() in g)


def test_warmup_prepares_without_error(db, world):
    durations = warmup.warm(db)
    assert set(durations) == {"chiffrages", "modèles"}
    assert memo.stats()["costing"] > 0
