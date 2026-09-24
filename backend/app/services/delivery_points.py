"""Présentation d'un point de livraison avec son état de consentement."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Consent, DeliveryPoint
from app.schemas import ConsentOut, DeliveryPointOut
from app.services.consent import has_active_consent
from app.timeutils import ensure_utc, utcnow


def active_consent(db: Session, delivery_point_id: int) -> Consent | None:
    now = utcnow()
    for consent in db.scalars(
        select(Consent)
        .where(Consent.delivery_point_id == delivery_point_id, Consent.revoked_at.is_(None))
        .order_by(Consent.granted_at.desc())
    ):
        if consent.expires_at is None or ensure_utc(consent.expires_at) > now:
            return consent
    return None


def delivery_point_out(db: Session, dp: DeliveryPoint) -> DeliveryPointOut:
    out = DeliveryPointOut.model_validate(dp)
    out.has_active_consent = has_active_consent(db, dp.id)
    consent = active_consent(db, dp.id) if out.has_active_consent else None
    out.active_consent = ConsentOut.model_validate(consent) if consent else None
    return out
