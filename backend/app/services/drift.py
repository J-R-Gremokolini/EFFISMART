"""F2a — Seuils et dérive : détection des anomalies de consommation (V1).

Quatre détecteurs purement statistiques, sans IA, entièrement faisables avec les données N1 (compteurs) :
1. THRESHOLD         : dépassement d'un seuil de puissance (élec.) ou de conso journalière (gaz) ;
2. CLIMATE_DEVIATION : écart à la même période N-1 corrigée des DJU ;
3. BASELOAD          : talon de nuit anormal (22 h – 6 h) ;
4. OFF_HOURS         : consommation en période d'inoccupation hors nuit (week-end, avant l'arrivée et après
                       le départ). L'attendu part du talon de nuit du jour : un même excès n'est signalé qu'une fois.

F2b (`anomaly_context`) replace ensuite chaque anomalie dans le graphe physique du site (principe P2) :
équipement suspect, zones et usages potentiellement impactés.

Principe P1 : une dérive est une anomalie *proposée*, expliquée (raisonnement, gain estimé,
niveau de confiance) et validée ou écartée par un humain ; jamais le déclencheur d'une action automatique.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    DeliveryPoint,
    Drift,
    DriftKind,
    Fluid,
    Measurement,
    MeasurementStep,
)
from app.providers.registry import get_energy_provider
from app.providers.weather import WeatherProvider, WeatherUnavailableError
from app.services.consent import active_consent_clause
from app.services.forecasting import learn_operation_classes
from app.timeutils import local_day_bounds, to_local

logger = logging.getLogger(__name__)

DRIFT_LABELS = {
    DriftKind.THRESHOLD: "Dépassement de seuil",
    DriftKind.CLIMATE_DEVIATION: "Écart à l'historique corrigé du climat",
    DriftKind.BASELOAD: "Talon de nuit anormal",
    DriftKind.OFF_HOURS: "Consommation en période d'inoccupation",
}
MIN_NIGHT_SLOTS = 8  # au moins la moitié des créneaux de nuit pour juger le talon
MIN_OFF_HOURS_SLOTS = 4
MIN_COMPARABLE_DAYS = 3


@dataclass(frozen=True)
class DriftCandidate:
    kind: DriftKind
    measured: float
    reference: float
    unit: str
    details: str
    # Éléments du calcul, repris dans l'explication de l'anomalie (principe P1).
    facts: dict = field(default_factory=dict, compare=False)

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


def slot_count(db: Session, delivery_point_id: int, day: date) -> int:
    t0, t1 = local_day_bounds(day, day)
    return int(db.scalar(select(func.count()).select_from(Measurement).where(
        Measurement.delivery_point_id == delivery_point_id, Measurement.time >= t0, Measurement.time < t1)) or 0)


def coverage(db: Session, dp: DeliveryPoint, day: date) -> float:
    """Part des mesures attendues effectivement reçues ce jour-là (46 à 50 pas les jours de changement d'heure)."""
    if dp.fluid == Fluid.GAS:
        return 1.0 if slot_count(db, dp.id, day) else 0.0
    t0, t1 = local_day_bounds(day, day)
    expected = (t1 - t0).total_seconds() / 1800
    return min(1.0, slot_count(db, dp.id, day) / expected) if expected else 0.0


def is_night_slot(local_dt: datetime) -> bool:
    """Nuit (22 h – 6 h par défaut), chaque jour de la semaine : le créneau du talon."""
    hour = local_dt.hour
    return hour >= settings.inactive_night_start_hour or hour < settings.inactive_night_end_hour


def is_off_hours_slot(local_dt: datetime) -> bool:
    """Inoccupation hors nuit : le week-end en journée ; les jours ouvrés, avant l'arrivée et après le départ."""
    if is_night_slot(local_dt):
        return False
    if local_dt.weekday() >= 5:
        return True
    return local_dt.hour < settings.occupancy_start_hour or local_dt.hour >= settings.occupancy_end_hour


def off_hours_period(day: date) -> str:
    night_end = settings.inactive_night_end_hour
    if day.weekday() >= 5:
        return f"le week-end, de {night_end} h à {settings.inactive_night_start_hour} h"
    return (f"avant l'arrivée ({night_end} h – {settings.occupancy_start_hour} h) et après le départ "
            f"({settings.occupancy_end_hour} h – {settings.inactive_night_start_hour} h)")


def night_period() -> str:
    return f"la nuit ({settings.inactive_night_start_hour} h – {settings.inactive_night_end_hour} h)"


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
            facts={"threshold_source": "custom", "excess_kwh": total - dp.daily_threshold_kwh,
                   "coverage": coverage(db, dp, day)},
        )

    threshold = dp.power_threshold_kw
    subscribed = None
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
    above = [kw for _, kw in slots if kw > threshold]
    return DriftCandidate(
        DriftKind.THRESHOLD, peak, threshold, "kW",
        f"Pic de {peak:.0f} kW à {peak_time:%H:%M} pour un seuil de {threshold:.0f} kW",
        facts={
            "threshold_source": "subscribed" if subscribed else "custom", "subscribed_kva": subscribed,
            "ratio": settings.threshold_ratio_of_subscribed_power, "peak_time": f"{peak_time:%H:%M}",
            "slots_above": len(above), "excess_kw": peak - threshold,
            "excess_kwh": sum((kw - threshold) * 0.5 for kw in above), "coverage": coverage(db, dp, day),
        },
    )


def detect_climate_deviation(
    db: Session, dp: DeliveryPoint, day: date, weather: WeatherProvider
) -> DriftCandidate | None:
    """Compare la conso du jour à l'attendu issu de N-1 corrigé des DJU.

    Sur une fenêtre de ±W jours autour de J-364, on ajuste par moindres carrés
    conso = a + b × DJU sur les jours de la même classe de fonctionnement que le
    jour analysé, puis attendu = a + b × DJU(jour). Les classes (jours de la
    semaine qui se ressemblent) sont déduites des données (Paudel, 2016) : un
    samedi de production n'est pas comparé à un dimanche de fermeture. Si la
    classe compte trop peu de jours dans la fenêtre, celle-ci est doublée.
    """
    actual = daily_kwh(db, dp.id, day, day).get(day)
    if actual is None:
        return None
    window = settings.climate_regression_window_days
    center = day - timedelta(days=364)
    history = daily_kwh(db, dp.id, center - timedelta(days=2 * window), center + timedelta(days=2 * window))
    if not history:
        return None
    recent = daily_kwh(db, dp.id, day - timedelta(days=56), day - timedelta(days=1))
    classes = learn_operation_classes({**history, **recent}, min_class_days=8)
    try:
        dju = weather.daily_dju(min(history), max(max(history), day))
    except WeatherUnavailableError:
        logger.warning("Météo indisponible : détecteur climatique ignoré pour le %s", day)
        return None
    if day not in dju:
        return None
    day_class = classes.of(day)
    for span in (window, 2 * window):
        points = [(dju[d], kwh) for d, kwh in history.items()
                  if d in dju and abs((d - center).days) <= span and classes.of(d) == day_class]
        if len(points) >= 8:
            window = span
            break
    else:
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
    residuals = [y - (mean_y + slope * (x - mean_x)) for x, y in points]
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    return DriftCandidate(
        DriftKind.CLIMATE_DEVIATION, actual, expected, "kWh",
        f"Consommation de {actual:.0f} kWh pour {expected:.0f} kWh attendus "
        f"(N-1 corrigé des DJU : {dju[day]:.1f} DJU ce jour)".replace(".", ","),
        facts={
            "n_points": len(points), "slope": slope, "dju": dju[day], "window": window,
            "center": center.isoformat(), "day_class": classes.label_of(day), "classes_learned": classes.learned,
            "r2": 1 - sum(r * r for r in residuals) / ss_tot if ss_tot > 0 and slope > 0 else None,
            "residual_cv": (statistics.fmean(r * r for r in residuals) ** 0.5) / mean_y if mean_y > 0 else None,
            "tolerance": settings.climate_deviation_tolerance, "excess_kwh": actual - expected,
            "weather_source": weather.source, "coverage": coverage(db, dp, day),
        },
    )


def detect_baseload(db: Session, dp: DeliveryPoint, day: date, weather: WeatherProvider) -> DriftCandidate | None:
    """Talon de nuit : puissance moyenne la nuit vs médiane des créneaux de nuit des N jours précédents (élec.)."""
    if dp.fluid != Fluid.ELEC:
        return None
    today_slots = [kw for t, kw in half_hour_powers(db, dp.id, day, day) if is_night_slot(t)]
    reference_slots = [
        kw
        for t, kw in half_hour_powers(
            db, dp.id, day - timedelta(days=settings.baseload_reference_days), day - timedelta(days=1)
        )
        if is_night_slot(t)
    ]
    if len(today_slots) < MIN_NIGHT_SLOTS or len(reference_slots) < 48:
        return None
    measured = statistics.fmean(today_slots)
    reference = statistics.median(reference_slots)
    if reference <= 0 or measured <= reference * (1 + settings.baseload_tolerance):
        return None
    reference_mean = statistics.fmean(reference_slots)
    return DriftCandidate(
        DriftKind.BASELOAD, measured, reference, "kW",
        f"Puissance moyenne de {measured:.0f} kW la nuit pour un talon de référence de {reference:.0f} kW",
        facts={
            "period": night_period(), "night_slots": len(today_slots), "reference_slots": len(reference_slots),
            "reference_days": settings.baseload_reference_days,
            "reference_cv": statistics.pstdev(reference_slots) / reference_mean if reference_mean > 0 else None,
            "tolerance": settings.baseload_tolerance, "excess_kw": measured - reference,
            "excess_kwh": (measured - reference) * len(today_slots) * 0.5, "coverage": coverage(db, dp, day),
        },
    )


def detect_off_hours(db: Session, dp: DeliveryPoint, day: date, weather: WeatherProvider) -> DriftCandidate | None:
    """Consommation en période d'inoccupation, hors nuit (élec.).

    Bâtiment inoccupé, la puissance devrait rester proche du talon de nuit. Attendu = talon de nuit *du jour
    même* + écart habituel entre inoccupation et nuit, médiane des jours comparables des 28 jours précédents
    (56 s'il y en a moins de 3) : même type de jour (ouvré ou week-end) et même classe de fonctionnement,
    déduite des données (un samedi de production n'est pas comparé à un dimanche fermé). Partir du talon du
    jour évite de signaler deux fois le même excès : un talon trop haut relève du détecteur précédent.
    """
    if dp.fluid != Fluid.ELEC:
        return None
    span = 2 * settings.baseload_reference_days
    slots: dict[date, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for t, kw in half_hour_powers(db, dp.id, day - timedelta(days=span), day):
        if is_night_slot(t):
            slots[t.date()][0].append(kw)
        elif is_off_hours_slot(t):
            slots[t.date()][1].append(kw)
    usable = {d: (statistics.fmean(night), statistics.fmean(off)) for d, (night, off) in slots.items()
              if len(night) >= MIN_NIGHT_SLOTS and len(off) >= MIN_OFF_HOURS_SLOTS}
    if day not in usable:
        return None
    talon, measured = usable[day]
    classes = learn_operation_classes(daily_kwh(db, dp.id, day - timedelta(days=span), day), min_class_days=8)
    weekend, day_class = day.weekday() >= 5, classes.of(day)
    for window in (settings.baseload_reference_days, span):
        gaps = [off - night for d, (night, off) in usable.items()
                if d < day and (day - d).days <= window and (d.weekday() >= 5) == weekend
                and classes.of(d) == day_class]
        if len(gaps) >= MIN_COMPARABLE_DAYS:
            break
    else:
        return None
    usual_gap = statistics.median(gaps)
    expected = talon + usual_gap
    if expected <= 0 or measured <= expected * (1 + settings.off_hours_tolerance):
        return None
    off_slots = len(slots[day][1])
    period = off_hours_period(day)
    return DriftCandidate(
        DriftKind.OFF_HOURS, measured, expected, "kW",
        f"Puissance moyenne de {measured:.0f} kW en inoccupation ({period}) pour {expected:.0f} kW attendus",
        facts={
            "period": period, "off_slots": off_slots, "talon": talon, "usual_gap": usual_gap,
            "comparable_days": len(gaps), "window": window, "day_class": classes.label_of(day),
            "classes_learned": classes.learned, "weekend": weekend, "tolerance": settings.off_hours_tolerance,
            "excess_kw": measured - expected, "excess_kwh": (measured - expected) * off_slots * 0.5,
            "coverage": coverage(db, dp, day),
        },
    )


DETECTORS = (detect_threshold, detect_climate_deviation, detect_baseload, detect_off_hours)
DETECTOR_BY_KIND = {
    DriftKind.THRESHOLD: detect_threshold,
    DriftKind.CLIMATE_DEVIATION: detect_climate_deviation,
    DriftKind.BASELOAD: detect_baseload,
    DriftKind.OFF_HOURS: detect_off_hours,
}


# --- Orchestration ---------------------------------------------------------------


def run_detection(db: Session, day: date, delivery_point_ids: list[int] | None = None) -> list[Drift]:
    """Analyse un jour pour les points consentis. Idempotent : une dérive (point, type, jour) n'est créée qu'une fois.

    Chaque dérive est créée « à valider », avec son explication ; seuls ses valideurs (auditeur, responsable
    énergie) sont prévenus. Rien ne part vers l'extérieur (webhooks, API partenaires) avant validation humaine.
    """
    stmt = select(DeliveryPoint).where(active_consent_clause())
    if delivery_point_ids is not None:
        stmt = stmt.where(DeliveryPoint.id.in_(delivery_point_ids))
    # Imports locaux : évitent un cycle drift ↔ intégrations / explications.
    from app.services import anomaly_context, drift_explanations, integrations, validation

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
            drift_explanations.explain_drift(db, dp, drift, candidate)  # F2b compris : contexte physique
            level = validation.confidence_level(drift.confidence)[0].lower()
            context = anomaly_context.short_text(drift.context)
            validation.notify(
                db, dp.site.organization_id,
                f"À valider : {DRIFT_LABELS[drift.kind].lower()} le {drift.day:%d/%m/%Y}, {dp.site.name} "
                f"({dp.external_ref}), écart de {drift.deviation_pct:+.0f} %, confiance {level}"
                + (f". {context[0].upper()}{context[1:]}" if context else ""),
                validators_only=True, drift_id=drift.id,
            )
            created.append(drift)
    db.commit()
    logger.info("Détection du %s : %d dérive(s) créée(s)", day, len(created))
    return created
