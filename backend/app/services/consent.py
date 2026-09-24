"""Consentement RGPD (brief §5) — vérifié au niveau service, pas seulement dans l'UI.

Aucun appel fournisseur ni affichage de donnée pour un point de livraison
sans consentement actif (non expiré, non révoqué).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Consent, DeliveryPoint
from app.providers.registry import get_energy_provider
from app.timeutils import ensure_utc, utcnow


class ConsentRequiredError(Exception):
    pass


def _active_conditions(now: datetime):
    return (
        Consent.revoked_at.is_(None),
        Consent.granted_at <= now,
        or_(Consent.expires_at.is_(None), Consent.expires_at > now),
    )


def active_consent_clause(now: datetime | None = None):
    """Condition EXISTS corrélée à `DeliveryPoint`, à utiliser dans une requête sur les points."""
    now = now or utcnow()
    return (
        select(Consent.id)
        .where(Consent.delivery_point_id == DeliveryPoint.id, *_active_conditions(now))
        .exists()
    )


def has_active_consent(db: Session, delivery_point_id: int) -> bool:
    count = db.scalar(
        select(func.count(Consent.id)).where(
            Consent.delivery_point_id == delivery_point_id, *_active_conditions(utcnow())
        )
    )
    return bool(count)


def require_active_consent(db: Session, delivery_point: DeliveryPoint) -> None:
    if not has_active_consent(db, delivery_point.id):
        raise ConsentRequiredError(delivery_point.id)


def grant_consent(
    db: Session,
    delivery_point: DeliveryPoint,
    *,
    scope: str,
    proof_ref: str,
    expires_at: datetime | None = None,
    granted_by: int | None = None,
) -> Consent:
    """Enregistre le consentement puis récupère les infos contrat (désormais autorisé)."""
    consent = Consent(
        delivery_point_id=delivery_point.id,
        granted_at=utcnow(),
        expires_at=ensure_utc(expires_at) if expires_at else None,
        scope=scope,
        proof_ref=proof_ref,
        granted_by=granted_by,
    )
    db.add(consent)
    db.flush()
    contract = get_energy_provider(delivery_point.provider, delivery_point.fluid).fetch_contract_info(
        delivery_point.external_ref
    )
    delivery_point.subscribed_power_kva = contract.subscribed_power_kva
    db.commit()
    return consent


def revoke_consent(db: Session, consent: Consent) -> Consent:
    if consent.revoked_at is None:
        consent.revoked_at = utcnow()
        db.commit()
    return consent
