"""Utilitaires de dates.

Règle : les instants sont stockés en UTC ; le raisonnement métier (jour,
mois, nuit, week-end) se fait en heure locale (Europe/Paris par défaut).
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings

UTC = timezone.utc
LOCAL_TZ = ZoneInfo(settings.timezone)


def utcnow() -> datetime:
    return datetime.now(UTC)


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def yesterday_local() -> date:
    """Donnée la plus fraîche disponible : la veille (données J+1)."""
    return today_local() - timedelta(days=1)


def ensure_utc(dt: datetime) -> datetime:
    """Certains moteurs (SQLite en test) renvoient des datetimes naïfs : ils sont en UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_local(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(LOCAL_TZ)


def local_midnight_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=LOCAL_TZ).astimezone(UTC)


def local_day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Bornes UTC [début de `start`, début du lendemain de `end`[ en heure locale."""
    return local_midnight_utc(start), local_midnight_utc(end + timedelta(days=1))


def daterange(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, months: int) -> date:
    """Premier jour du mois décalé de `months` mois."""
    years, month_index = divmod(day.month - 1 + months, 12)
    return date(day.year + years, month_index + 1, 1)
