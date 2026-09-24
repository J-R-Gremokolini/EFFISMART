"""MockDataProvider : données plausibles, déterministes, jamais au-delà de J-1."""
from datetime import date

from app.models import Fluid, MeasurementStep
from app.providers.mock import MockDataProvider
from app.timeutils import to_local, today_local

REF = "30001000000042"
UNTIL = date(2025, 12, 31)


def _provider(fluid=Fluid.ELEC):
    return MockDataProvider(fluid=fluid, anomalies=[], available_until=UNTIL)


def _daily_total(day: date) -> float:
    return sum(m.value_kwh for m in _provider().fetch_load_curve(REF, day, day))


def test_elec_curve_has_48_half_hours_per_day():
    points = _provider().fetch_load_curve(REF, date(2025, 6, 10), date(2025, 6, 10))
    assert len(points) == 48
    assert all(p.step == MeasurementStep.PT30M for p in points)


def test_dst_days_have_46_or_50_steps():
    assert len(_provider().fetch_load_curve(REF, date(2025, 3, 30), date(2025, 3, 30))) == 46
    assert len(_provider().fetch_load_curve(REF, date(2025, 10, 26), date(2025, 10, 26))) == 50


def test_is_deterministic():
    a = _provider().fetch_load_curve(REF, date(2025, 2, 1), date(2025, 2, 3))
    b = _provider().fetch_load_curve(REF, date(2025, 2, 1), date(2025, 2, 3))
    assert a == b


def test_day_night_profile():
    points = _provider().fetch_load_curve(REF, date(2025, 6, 11), date(2025, 6, 11))  # mercredi
    by_hour = {to_local(p.time).hour: p.avg_power_kw for p in points}
    assert by_hour[11] > by_hour[3] * 2


def test_weekend_trough():
    assert _daily_total(date(2025, 6, 15)) < _daily_total(date(2025, 6, 11)) * 0.7  # dimanche vs mercredi


def test_winter_consumes_more_than_summer():
    assert _daily_total(date(2025, 1, 15)) > _daily_total(date(2025, 7, 16))  # deux mercredis


def test_gas_is_daily():
    points = _provider(Fluid.GAS).fetch_load_curve("21000000000042", date(2025, 1, 1), date(2025, 1, 7))
    assert len(points) == 7
    assert all(p.step == MeasurementStep.P1D and p.avg_power_kw is None for p in points)


def test_never_returns_today_or_future():
    today = today_local()
    points = MockDataProvider(anomalies=[]).fetch_load_curve(REF, today, today)
    assert points == []
