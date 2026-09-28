"""Ingestion des mesures depuis les fournisseurs.

Idempotente : les mesures de la plage sont remplacées. Aucune valeur de
consommation n'est journalisée (donnée commercialement sensible, brief §5).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import DeliveryPoint, Measurement
from app.providers.registry import get_energy_provider
from app.services.consent import active_consent_clause, require_active_consent
from app.timeutils import local_day_bounds, yesterday_local

logger = logging.getLogger(__name__)

_INSERT_CHUNK = 5000


def ingest_delivery_point(db: Session, delivery_point: DeliveryPoint, start: date, end: date) -> int:
    """Récupère et stocke les mesures [start, end] (jours locaux inclus). Renvoie le nombre de mesures."""
    require_active_consent(db, delivery_point)
    provider = get_energy_provider(delivery_point.provider, delivery_point.fluid, delivery_point)
    measurements = provider.fetch_load_curve(delivery_point.external_ref, start, end)

    t0, t1 = local_day_bounds(start, end)
    db.execute(
        delete(Measurement).where(
            Measurement.delivery_point_id == delivery_point.id,
            Measurement.time >= t0,
            Measurement.time < t1,
        )
    )
    rows = [
        {
            "delivery_point_id": delivery_point.id,
            "time": m.time,
            "value_kwh": m.value_kwh,
            "avg_power_kw": m.avg_power_kw,
            "step": m.step,
        }
        for m in measurements
    ]
    for i in range(0, len(rows), _INSERT_CHUNK):
        db.execute(insert(Measurement), rows[i : i + _INSERT_CHUNK])
    db.commit()
    logger.info("Ingestion point #%s du %s au %s : %d mesures", delivery_point.id, start, end, len(rows))
    return len(rows)


def ingest_all_for_day(db: Session, day: date) -> int:
    """Job quotidien : récupère la veille pour tous les points consentis."""
    points = db.scalars(select(DeliveryPoint).where(active_consent_clause())).all()
    total = 0
    for dp in points:
        try:
            total += ingest_delivery_point(db, dp, day, day)
        except Exception:  # un point en échec ne bloque pas les autres
            db.rollback()
            logger.exception("Échec d'ingestion pour le point #%s", dp.id)
    return total


def backfill_delivery_point(delivery_point_id: int, days: int | None = None) -> None:
    """Récupère l'historique d'un point nouvellement consenti, puis analyse les jours récents.

    Exécuté en tâche de fond : ouvre sa propre session.
    """
    from app.services.drift import run_detection  # import local : évite un cycle

    days = settings.backfill_days if days is None else days
    end = yesterday_local()
    start = end - timedelta(days=days - 1)
    with SessionLocal() as db:
        dp = db.get(DeliveryPoint, delivery_point_id)
        if dp is None:
            return
        ingest_delivery_point(db, dp, start, end)
        detection_start = max(start, end - timedelta(days=settings.detection_backfill_days - 1))
        day = detection_start
        while day <= end:
            run_detection(db, day, delivery_point_ids=[dp.id])
            day += timedelta(days=1)
