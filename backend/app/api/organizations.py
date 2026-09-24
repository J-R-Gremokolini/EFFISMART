"""Organisations, sites, portefeuille et dashboard (F1, F5)."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_repo, require_auditor, require_writer
from app.models import AuditorClientLink, Organization, Role, Site, User
from app.repositories import TenantRepository
from app.schemas import (
    OrganizationIn,
    OrganizationOut,
    SiteDetailOut,
    SiteIn,
    SiteOut,
    UserOut,
    ViewerIn,
)
from app.security import hash_password
from app.services import dashboard, regulatory
from app.services.delivery_points import delivery_point_out
from app.timeutils import today_local

router = APIRouter(tags=["organisations"])


@router.get("/portfolio", dependencies=[Depends(require_auditor)])
def get_portfolio(repo: TenantRepository = Depends(get_repo), db: Session = Depends(get_db)) -> list[dict]:
    return dashboard.portfolio(db, repo)


@router.get("/organizations", response_model=list[OrganizationOut])
def list_organizations(repo: TenantRepository = Depends(get_repo)):
    return repo.list_organizations()


@router.post("/organizations", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
def create_organization(
    body: OrganizationIn, user: User = Depends(require_writer), db: Session = Depends(get_db)
) -> Organization:
    org = Organization(name=body.name, siren=body.siren, address=body.address)
    db.add(org)
    db.flush()
    if user.auditor_id is not None:
        db.add(
            AuditorClientLink(
                auditor_id=user.auditor_id, organization_id=org.id, start_date=today_local(), active=True
            )
        )
    db.commit()
    return org


@router.get("/organizations/{org_id}", response_model=OrganizationOut)
def get_organization(org_id: int, repo: TenantRepository = Depends(get_repo)):
    return repo.get_organization(org_id)


@router.get("/organizations/{org_id}/dashboard")
def get_dashboard(
    org_id: int,
    period: Literal["7d", "30d", "12m", "custom"] = "30d",
    start: date | None = None,
    end: date | None = None,
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> dict:
    org = repo.get_organization(org_id)
    return dashboard.organization_dashboard(db, org, period, start, end)


@router.get("/organizations/{org_id}/sites", response_model=list[SiteDetailOut])
def list_sites(org_id: int, repo: TenantRepository = Depends(get_repo), db: Session = Depends(get_db)):
    repo.get_organization(org_id)
    result = []
    for site in repo.list_sites(org_id):
        detail = SiteDetailOut.model_validate(site)
        detail.delivery_points = [delivery_point_out(db, dp) for dp in site.delivery_points]
        result.append(detail)
    return result


@router.post("/organizations/{org_id}/sites", response_model=SiteOut, status_code=status.HTTP_201_CREATED)
def create_site(
    org_id: int,
    body: SiteIn,
    _: User = Depends(require_writer),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> Site:
    org = repo.get_organization(org_id)
    site = Site(organization_id=org.id, **body.model_dump())
    db.add(site)
    db.flush()
    regulatory.ensure_operat_deadline(db, site)
    db.commit()
    return site


@router.post(
    "/organizations/{org_id}/viewers", response_model=UserOut, status_code=status.HTTP_201_CREATED
)
def create_viewer(
    org_id: int,
    body: ViewerIn,
    _: User = Depends(require_writer),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> User:
    """Crée un compte « espace client » en lecture seule (F5)."""
    org = repo.get_organization(org_id)
    email = body.email.strip().lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Un compte existe déjà avec cet e-mail")
    viewer = User(
        email=email, password_hash=hash_password(body.password), role=Role.CLIENT_VIEWER, organization_id=org.id
    )
    db.add(viewer)
    db.commit()
    return viewer
