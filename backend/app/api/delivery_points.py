"""Points de livraison, consentement (onboarding) et courbe de charge."""
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_repo, require_writer
from app.models import Consent, DeliveryPoint, User
from app.repositories import TenantRepository
from app.schemas import ConsentIn, ConsentOut, DeliveryPointIn, DeliveryPointOut
from app.services import consent as consent_service
from app.services import dashboard
from app.services.delivery_points import delivery_point_out
from app.services.ingestion import backfill_delivery_point

router = APIRouter(tags=["points de livraison"])


@router.post(
    "/sites/{site_id}/delivery-points", response_model=DeliveryPointOut, status_code=status.HTTP_201_CREATED
)
def create_delivery_point(
    site_id: int,
    body: DeliveryPointIn,
    _: User = Depends(require_writer),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> DeliveryPointOut:
    site = repo.get_site(site_id)
    if db.scalar(select(DeliveryPoint.id).where(DeliveryPoint.external_ref == body.external_ref)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce point de livraison est déjà enregistré")
    dp = DeliveryPoint(site_id=site.id, **body.model_dump())
    db.add(dp)
    db.commit()
    return delivery_point_out(db, dp)


@router.get("/delivery-points/{dp_id}/load-curve")
def get_load_curve(
    dp_id: int,
    start: date,
    end: date,
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> dict:
    if start > end:
        start, end = end, start
    dp = repo.get_delivery_point(dp_id)
    return dashboard.load_curve(db, dp, start, end)


@router.get("/delivery-points/{dp_id}/consents", response_model=list[ConsentOut])
def list_consents(dp_id: int, repo: TenantRepository = Depends(get_repo), db: Session = Depends(get_db)):
    dp = repo.get_delivery_point(dp_id)
    return list(
        db.scalars(select(Consent).where(Consent.delivery_point_id == dp.id).order_by(Consent.granted_at.desc()))
    )


@router.post(
    "/delivery-points/{dp_id}/consents", response_model=ConsentOut, status_code=status.HTTP_201_CREATED
)
def grant_consent(
    dp_id: int,
    body: ConsentIn,
    background: BackgroundTasks,
    user: User = Depends(require_writer),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> Consent:
    """Étape de consentement de l'onboarding, puis récupération de l'historique en tâche de fond."""
    dp = repo.get_delivery_point(dp_id)
    consent = consent_service.grant_consent(
        db, dp, scope=body.scope, proof_ref=body.proof_ref, expires_at=body.expires_at, granted_by=user.id
    )
    background.add_task(backfill_delivery_point, dp.id)
    return consent


@router.post("/consents/{consent_id}/revoke", response_model=ConsentOut)
def revoke_consent(
    consent_id: int,
    _: User = Depends(require_writer),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> Consent:
    return consent_service.revoke_consent(db, repo.get_consent(consent_id))
