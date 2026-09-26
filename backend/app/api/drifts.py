"""F2a — Consultation et qualification des dérives, notifications."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, get_repo, require_writer
from app.models import Drift, DriftStatus, Notification, User
from app.repositories import TenantRepository
from app.schemas import DriftOut, DriftUpdate, NotificationOut
from app.services import drift as drift_service

router = APIRouter(tags=["dérives"])


def drift_out(drift: Drift) -> DriftOut:
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
    )


@router.get("/organizations/{org_id}/drifts", response_model=list[DriftOut])
def list_drifts(
    org_id: int, status: DriftStatus | None = None, repo: TenantRepository = Depends(get_repo)
) -> list[DriftOut]:
    repo.get_organization(org_id)
    return [drift_out(d) for d in repo.list_drifts(org_id, status)]


@router.patch("/drifts/{drift_id}", response_model=DriftOut)
def qualify_drift(
    drift_id: int,
    body: DriftUpdate,
    user: User = Depends(require_writer),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> DriftOut:
    """Qualification humaine (principe P1) : ouverte → qualifiée / ignorée, ou réouverture."""
    drift = repo.get_drift(drift_id)
    drift_service.qualify_drift(db, drift, status=body.status, comment=body.comment, user_id=user.id)
    return drift_out(drift)


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
