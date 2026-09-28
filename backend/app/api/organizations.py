"""Organisations, sites, portefeuille et dashboard (F1, F5)."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_repo, require_auditor, require_writer
from app.models import Organization, Site, User
from app.repositories import TenantRepository, sees_unvalidated
from app.schemas import (
    OrganizationIn,
    OrganizationOut,
    SiteDetailOut,
    SiteIn,
    SiteOut,
    UserOut,
    ViewerIn,
)
from app.services import dashboard, onboarding
from app.services.delivery_points import delivery_point_out

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
    return onboarding.create_organization(db, user, name=body.name, siren=body.siren, address=body.address)


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
    data = dashboard.organization_dashboard(db, org, period, start, end)
    if not sees_unvalidated(repo.user):
        data["open_drifts"] = 0  # principe P1 : les dérives non validées ne sont pas montrées à ce compte
    return data


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
    return onboarding.create_site(db, org, **body.model_dump())


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
    """Crée un compte « espace client » en lecture seule (F5), éventuellement responsable énergie (P1)."""
    org = repo.get_organization(org_id)
    return onboarding.create_viewer(db, org, email=body.email, password=body.password,
                                    energy_manager=body.is_energy_manager)
