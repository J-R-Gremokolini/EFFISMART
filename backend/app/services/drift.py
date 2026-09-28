"""F2a — Détection de dérives de consommation.

Trois détecteurs purement statistiques (pas d'IA) :
1. THRESHOLD         : dépassement d'un seuil de puissance (élec.) ou de conso journalière (gaz) ;
2. CLIMATE_DEVIATION : écart à la même période N-1 corrigée des DJU ;
3. BASELOAD          : talon anormal en période théorique d'inoccupation (nuit, week-end).

Principe P1 : une dérive est une alerte à qualifier par un humain,
jamais le déclencheur d'une action automatique.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AuditorClientLink,
    DeliveryPoint,
    Drift,
    DriftKind,
    DriftStatus,
    Fluid,
    Measurement,
    MeasurementStep,
    Notification,
    Role,
    User,
)
from app.providers.registry import get_energy_provider
from app.providers.weather import WeatherProvider, WeatherUnavailableError
from app.repositories import active_links_clause
from app.services.consent import active_consent_clause
from app.timeutils import local_day_bounds, to_local

logger = logging.getLogger(__name__)

DRIFT_LABELS = {
    DriftKind.THRESHOLD: "Dépassement de seuil",
    DriftKind.CLIMATE_DEVIATION: "Écart à l'historique corrigé du climat",
    DriftKind.BASELOAD: "Talon anormal",
}


@dataclass(frozen=True)
class DriftCandidate:
    kind: DriftKind
    measured: float
    reference: float
    unit: str
    details: str

    @property
    def deviation_pct(self) -> float:
        return (self.measured - self.reference) / self.reference * 100 if self.reference else 0.0


# --- Lecture des séries ------------------------------------------------------


def half_hour_powers(db: Session, delivery_point_id: int, start: date, end: date) -> list[tuple[datetime, float]]:
    """Puissances moyennes 30 min (kW), horodatées en heure locale."""
    t0, t1 = local_day_bounds(start, end)
    rows = db.execute(
        select(Measurement.time, Measurement.value_kwh, Measurement.avg_power_kw).where(
            Measurement.delivery_point_id == delivery_point_id,
            Measurement.time >= t0,
            Measurement.time < t1,
            Measurement.step == MeasurementStep.PT30M,
        )
    ).all()
    return [(to_local(t), kw if kw is not None else kwh * 2) for t, kwh, kw in rows]


def daily_kwh(db: Session, delivery_point_id: int, start: date, end: date) -> dict[date, float]:
    t0, t1 = local_day_bounds(start, end)
    rows = db.execute(
        select(Measurement.time, Measurement.value_kwh).where(
            Measurement.delivery_point_id == delivery_point_id,
            Measurement.time >= t0,
            Measurement.time < t1,
        )
    ).all()
    totals: dict[date, float] = defaultdict(float)
    for t, kwh in rows:
        totals[to_local(t).date()] += kwh
    return dict(totals)


def is_inactive_slot(local_dt: datetime) -> bool:
    """Période théorique d'inoccupation : week-end entier, et chaque nuit."""
    hour = local_dt.hour
    return (
        local_dt.weekday() >= 5
        or hour >= settings.inactive_night_start_hour
        or hour < settings.inactive_night_end_hour
    )


# --- Détecteurs ------------------------------------------------------------------


def detect_threshold(db: Session, dp: DeliveryPoint, day: date, weather: WeatherProvider) -> DriftCandidate | None:
    if dp.fluid == Fluid.GAS:
        if dp.daily_threshold_kwh is None:
            return None
        total = daily_kwh(db, dp.id, day, day).get(day)
        if total is None or total <= dp.daily_threshold_kwh:
            return None
        return DriftCandidate(
            DriftKind.THRESHOLD, total, dp.daily_threshold_kwh, "kWh",
            f"Consommation journalière de {total:.0f} kWh pour un seuil de {dp.daily_threshold_kwh:.0f} kWh",
        )

    threshold = dp.power_threshold_kw
    if threshold is None:
        subscribed = dp.subscribed_power_kva
        if subscribed is None:
            provider = get_energy_provider(dp.provider, dp.fluid, dp)
            subscribed = provider.fetch_contract_info(dp.external_ref).subscribed_power_kva
        if not subscribed:
            return None
        # Hypothèse V1 : cos φ ≈ 1, donc kVA ≈ kW.
        threshold = subscribed * settings.threshold_ratio_of_subscribed_power

    slots = half_hour_powers(db, dp.id, day, day)
    if not slots:
        return None
    peak_time, peak = max(slots, key=lambda slot: slot[1])
    if peak <= threshold:
        return None
    return DriftCandidate(
        DriftKind.THRESHOLD, peak, threshold, "kW",
        f"Pic de {peak:.0f} kW à {peak_time:%H:%M} pour un seuil de {threshold:.0f} kW",
    )


def detect_climate_deviation(
    db: Session, dp: DeliveryPoint, day: date, weather: WeatherProvider
) -> DriftCandidate | None:
    """Compare la conso du jour à l'attendu issu de N-1 corrigé des DJU.

    Sur une fenêtre de ±W jours autour de J-364 (même jour de semaine), on ajuste
    par moindres carrés conso = a + b × DJU sur les jours de même type (ouvré /
    week-end), puis attendu = a + b × DJU(jour).
    """
    actual = daily_kwh(db, dp.id, day, day).get(day)
    if actual is None:
        return None
    window = settings.climate_regression_window_days
    center = day - timedelta(days=364)
    history = daily_kwh(db, dp.id, center - timedelta(days=window), center + timedelta(days=window))
    if not history:
        return None
    try:
        dju = weather.daily_dju(min(history), max(max(history), day))
    except WeatherUnavailableError:
        logger.warning("Météo indisponible : détecteur climatique ignoré pour le %s", day)
        return None
    if day not in dju:
        return None
    is_weekend = day.weekday() >= 5
    points = [(dju[d], kwh) for d, kwh in history.items()
              if d in dju and (d.weekday() >= 5) == is_weekend]
    if len(points) < 8:
        return None

    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    # Pente de chauffage physiquement ≥ 0 ; si les DJU varient trop peu (été), on garde la moyenne.
    slope = 0.0
    if var_x >= 1.0:
        slope = max(0.0, sum((x - mean_x) * (y - mean_y) for x, y in points) / var_x)
    expected = mean_y + slope * (dju[day] - mean_x)
    if expected <= 0 or actual <= expected * (1 + settings.climate_deviation_tolerance):
        return None
    return DriftCandidate(
        DriftKind.CLIMATE_DEVIATION, actual, expected, "kWh",
        f"Consommation de {actual:.0f} kWh pour {expected:.0f} kWh attendus "
        f"(N-1 corrigé des DJU : {dju[day]:.1f} DJU ce jour)",
    )


def detect_baseload(db: Session, dp: DeliveryPoint, day: date, weather: WeatherProvider) -> DriftCandidate | None:
    """Puissance moyenne en inoccupation du jour vs médiane des N jours précédents (élec. uniquement)."""
    if dp.fluid != Fluid.ELEC:
        return None
    today_slots = [kw for t, kw in half_hour_powers(db, dp.id, day, day) if is_inactive_slot(t)]
    reference_slots = [
        kw
        for t, kw in half_hour_powers(
            db, dp.id, day - timedelta(days=settings.baseload_reference_days), day - timedelta(days=1)
        )
        if is_inactive_slot(t)
    ]
    if not today_slots or len(reference_slots) < 48:
        return None
    measured = statistics.fmean(today_slots)
    reference = statistics.median(reference_slots)
    if reference <= 0 or measured <= reference * (1 + settings.baseload_tolerance):
        return None
    period = "le week-end" if day.weekday() >= 5 else "la nuit"
    return DriftCandidate(
        DriftKind.BASELOAD, measured, reference, "kW",
        f"Puissance moyenne de {measured:.0f} kW {period} pour un talon de référence de {reference:.0f} kW",
    )


DETECTORS = (detect_threshold, detect_climate_deviation, detect_baseload)


# --- Orchestration ---------------------------------------------------------------


def _notify(db: Session, dp: DeliveryPoint, drift: Drift) -> None:
    organization_id = dp.site.organization_id
    linked_auditors = select(AuditorClientLink.auditor_id).where(
        AuditorClientLink.organization_id == organization_id, *active_links_clause()
    )
    conditions = [and_(User.role == Role.AUDITOR, User.auditor_id.in_(linked_auditors))]
    if settings.notify_clients_on_drift:
        conditions.append(and_(User.role == Role.CLIENT_VIEWER, User.organization_id == organization_id))
    message = (
        f"{DRIFT_LABELS[drift.kind]} le {drift.day:%d/%m/%Y} — {dp.site.name} "
        f"({dp.external_ref}) : écart de {drift.deviation_pct:+.0f} %"
    )
    for user in db.scalars(select(User).where(or_(*conditions))):
        db.add(Notification(user_id=user.id, drift_id=drift.id, organization_id=organization_id, message=message))


def qualify_drift(
    db: Session, drift: Drift, *, status: DriftStatus, comment: str | None, user_id: int
) -> Drift:
    """Qualification humaine (principe P1) : ouverte → qualifiée / ignorée, ou réouverture."""
    drift.status = status
    drift.comment = comment
    drift.qualified_by = user_id
    db.commit()
    return drift


def run_detection(db: Session, day: date, delivery_point_ids: list[int] | None = None) -> list[Drift]:
    """Analyse un jour pour les points consentis. Idempotent : une dérive (point, type, jour) n'est créée qu'une fois."""
    stmt = select(DeliveryPoint).where(active_consent_clause())
    if delivery_point_ids is not None:
        stmt = stmt.where(DeliveryPoint.id.in_(delivery_point_ids))
    from app.services import integrations  # import local : évite un cycle drift ↔ intégrations

    weather = integrations.weather_provider(db)
    created: list[Drift] = []
    for dp in db.scalars(stmt).all():
        for detector in DETECTORS:
            candidate = detector(db, dp, day, weather)
            if candidate is None:
                continue
            already = db.scalar(
                select(Drift.id).where(
                    Drift.delivery_point_id == dp.id, Drift.kind == candidate.kind, Drift.day == day
                )
            )
            if already:
                continue
            drift = Drift(
                delivery_point_id=dp.id,
                kind=candidate.kind,
                day=day,
                measured_value=round(candidate.measured, 2),
                reference_value=round(candidate.reference, 2),
                deviation_pct=round(candidate.deviation_pct, 1),
                unit=candidate.unit,
                details=candidate.details,
            )
            db.add(drift)
            db.flush()
            _notify(db, dp, drift)
            integrations.enqueue_event(db, "drift.created", dp.site.organization_id, {
                "drift_id": drift.id, "kind": drift.kind.value, "day": drift.day.isoformat(),
                "site": dp.site.name, "delivery_point": dp.external_ref, "fluid": dp.fluid.value,
                "measured": drift.measured_value, "reference": drift.reference_value,
                "deviation_pct": drift.deviation_pct, "unit": drift.unit, "details": drift.details,
            })
            created.append(drift)
    db.commit()
    logger.info("Détection du %s : %d dérive(s) créée(s)", day, len(created))
    return created
