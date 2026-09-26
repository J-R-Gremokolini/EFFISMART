"""F3 — Échéances réglementaires et journal d'actions (réservé à l'auditeur)."""
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_repo, require_auditor
from app.models import ActionLog, RegulatoryDeadline, User
from app.repositories import TenantRepository
from app.schemas import ActionLogIn, ActionLogOut, DeadlineIn, DeadlineOut, DeadlineUpdate
from app.services import regulatory
from app.timeutils import today_local

router = APIRouter(tags=["réglementaire"], dependencies=[Depends(require_auditor)])


def deadline_out(deadline: RegulatoryDeadline) -> DeadlineOut:
    site = deadline.site
    return DeadlineOut(
        id=deadline.id,
        site_id=site.id,
        site_name=site.name,
        organization_id=site.organization_id,
        organization_name=site.organization.name,
        obligation=deadline.obligation,
        due_date=deadline.due_date,
        status=deadline.status,
        days_left=(deadline.due_date - today_local()).days,
        notes=deadline.notes,
    )


@router.get("/regulatory/deadlines", response_model=list[DeadlineOut])
def list_deadlines(organization_id: int | None = None, repo: TenantRepository = Depends(get_repo)):
    return [deadline_out(d) for d in repo.list_deadlines(organization_id)]


@router.post("/sites/{site_id}/deadlines", response_model=DeadlineOut, status_code=status.HTTP_201_CREATED)
def create_deadline(
    site_id: int, body: DeadlineIn, repo: TenantRepository = Depends(get_repo), db: Session = Depends(get_db)
):
    site = repo.get_site(site_id)
    deadline = regulatory.create_deadline(db, site, body.obligation, body.due_date, body.notes)
    db.commit()
    return deadline_out(deadline)


@router.patch("/deadlines/{deadline_id}", response_model=DeadlineOut)
def update_deadline(
    deadline_id: int,
    body: DeadlineUpdate,
    user: User = Depends(require_auditor),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
):
    deadline = repo.get_deadline(deadline_id)
    regulatory.update_deadline(
        db, deadline, user_id=user.id, status=body.status, due_date=body.due_date, notes=body.notes
    )
    return deadline_out(deadline)


@router.get("/sites/{site_id}/actions", response_model=list[ActionLogOut])
def list_actions(site_id: int, repo: TenantRepository = Depends(get_repo)):
    return repo.list_action_logs(site_id)


@router.post("/sites/{site_id}/actions", response_model=ActionLogOut, status_code=status.HTTP_201_CREATED)
def log_action(
    site_id: int,
    body: ActionLogIn,
    user: User = Depends(require_auditor),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> ActionLog:
    site = repo.get_site(site_id)
    return regulatory.log_action(
        db, site, obligation=body.obligation, description=body.description,
        user_id=user.id, performed_at=body.performed_at,
    )
