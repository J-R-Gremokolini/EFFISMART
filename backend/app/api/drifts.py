"""F2a — Consultation et validation humaine des dérives (principe P1), notifications."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, get_repo, require_validator
from app.models import Drift, DriftStatus, Notification, User
from app.repositories import TenantRepository
from app.schemas import DriftOut, DriftUpdate, NotificationOut
from app.services import validation

router = APIRouter(tags=["dérives"])


def explanation_fields(db: Session, output, validator_id: int | None, validated_at) -> dict:
    return {
        "reasoning": output.reasoning or [],
        "confidence": output.confidence,
        "confidence_level": validation.confidence_level(output.confidence)[0],
        "confidence_factors": output.confidence_factors or [],
        "gain_kwh": output.gain_kwh,
        "gain_eur": output.gain_eur,
        "gain_kgco2e": output.gain_kgco2e,
        "gain_basis": output.gain_basis,
        "algorithm": output.algorithm,
        "validated_by_role": validation.validator_role(db.get(User, validator_id) if validator_id else None),
        "validated_at": validated_at,
    }


def drift_out(db: Session, drift: Drift) -> DriftOut:
    dp = drift.delivery_point
    return DriftOut(
        id=drift.id,
        delivery_point_id=dp.id,
        external_ref=dp.external_ref,
        fluid=dp.fluid,
        site_name=dp.site.name,
        kind=drift.kind,
        day=drift.day,
        measured_value=drift.measured_value,
        reference_value=drift.reference_value,
        deviation_pct=drift.deviation_pct,
        unit=drift.unit,
        details=drift.details,
        status=drift.status,
        comment=drift.comment,
        detected_at=drift.detected_at,
        context=drift.context,
        **explanation_fields(db, drift, drift.qualified_by, drift.qualified_at),
    )


@router.get("/organizations/{org_id}/drifts", response_model=list[DriftOut])
def list_drifts(
    org_id: int, status: DriftStatus | None = None, repo: TenantRepository = Depends(get_repo)
) -> list[DriftOut]:
    """Un compte client (hors responsable énergie) ne voit que les dérives validées par un humain."""
    repo.get_organization(org_id)
    return [drift_out(repo.db, d) for d in repo.list_drifts(org_id, status)]


@router.patch("/drifts/{drift_id}", response_model=DriftOut)
def review_drift(
    drift_id: int,
    body: DriftUpdate,
    user: User = Depends(require_validator),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> DriftOut:
    """Décision humaine (principe P1) : valider (QUALIFIED), écarter (IGNORED, motif obligatoire) ou rouvrir (OPEN).

    Réservée à l'auditeur partenaire et au responsable énergie du client.
    """
    try:
        drift, _ = validation.review_drift(db, repo, user, drift_id, body.status, body.comment)
    except validation.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return drift_out(db, drift)


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    limit: int = 20, user: User = Depends(get_current_user), repo: TenantRepository = Depends(get_repo)
):
    stmt = (
        select(Notification)
        .where(Notification.user_id == user.id, repo.org_clause(Notification.organization_id))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(min(max(limit, 1), 100))
    )
    return list(repo.db.scalars(stmt))
