"""API partenaires (lecture seule) : pour les logiciels des cabinets et de leurs clients (ERP, GMAO, BI…).

Authentification par clé d'API (créée dans la page « Intégrations ») :
    X-API-Key: esk_…        ou        Authorization: Bearer esk_…
Une clé voit les clients de son cabinet, ou un seul client si elle a été restreinte.
Mêmes règles d'isolation que l'application (TenantRepository) ; seuls les points consentis sont exposés.
Principe P1 : seules les sorties algorithmiques validées par un humain (auditeur ou responsable énergie)
sont exposées, avec leur raisonnement, leur gain estimé et leur niveau de confiance.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import ApiKey, DriftStatus, Fluid, ReviewStatus, Role, User
from app.repositories import ResourceNotFound, TenantRepository
from app.services import integrations, validation
from app.services.consent import has_active_consent
from app.services.dashboard import consented_delivery_points, data_as_of
from app.services.energy_data import combined_daily
from app.timeutils import month_start, yesterday_local

class UTF8JSONResponse(JSONResponse):
    """Encodage déclaré explicitement : certains clients (PowerShell 5, anciens ERP) lisent sinon en Latin-1."""

    media_type = "application/json; charset=utf-8"


router = APIRouter(prefix="/v1", tags=["API partenaires"], default_response_class=UTF8JSONResponse)

_hits: dict[int, deque] = defaultdict(deque)
_lock = threading.Lock()


class PartnerScope:
    def __init__(self, key: ApiKey, repo: TenantRepository) -> None:
        self.key, self.repo = key, repo

    def organization(self, organization_id: int):
        if self.key.organization_id not in (None, organization_id):
            raise ResourceNotFound()
        return self.repo.get_organization(organization_id)


def _rate_limit(key_id: int) -> None:
    now = time.monotonic()
    with _lock:
        hits = _hits[key_id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= settings.partner_api_rate_limit_per_minute:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Trop de requêtes : réessayez dans une minute.")
        hits.append(now)


def partner_scope(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> PartnerScope:
    raw = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    key = integrations.authenticate_api_key(db, raw)
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clé d'API absente, invalide ou révoquée.")
    _rate_limit(key.id)
    # Utilisateur technique (non enregistré) portant le périmètre du cabinet propriétaire de la clé.
    return PartnerScope(key, TenantRepository(db, User(role=Role.AUDITOR, auditor_id=key.auditor_id)))


@router.get("/organizations")
def list_organizations(scope: PartnerScope = Depends(partner_scope)) -> list[dict]:
    orgs = scope.repo.list_organizations()
    if scope.key.organization_id is not None:
        orgs = [o for o in orgs if o.id == scope.key.organization_id]
    return [{"id": o.id, "name": o.name, "siren": o.siren} for o in orgs]


@router.get("/organizations/{org_id}/sites")
def list_sites(org_id: int, scope: PartnerScope = Depends(partner_scope), db: Session = Depends(get_db)) -> list[dict]:
    org = scope.organization(org_id)
    return [
        {
            "id": site.id, "name": site.name, "surface_m2": site.surface_m2,
            "is_tertiary_decret": site.is_tertiary_decret,
            "delivery_points": [
                {"id": dp.id, "fluid": dp.fluid.value, "reference": dp.external_ref, "is_primary": dp.is_primary,
                 "data_available": has_active_consent(db, dp.id)}
                for dp in site.delivery_points
            ],
        }
        for site in scope.repo.list_sites(org.id)
    ]


@router.get("/organizations/{org_id}/consumption")
def consumption(
    org_id: int,
    start: date | None = None,
    end: date | None = None,
    granularity: Literal["day", "month"] = "day",
    scope: PartnerScope = Depends(partner_scope),
    db: Session = Depends(get_db),
) -> dict:
    """Consommation en kWh par jour ou par mois et par énergie : compteurs consentis (N1) et, les jours sans
    mesure, factures et relevés (N0) ; `n0_kwh` indique la part issue des factures."""
    org = scope.organization(org_id)
    points = scope.repo.list_delivery_points(org.id)
    end = end or data_as_of(db, [p.id for p in consented_delivery_points(db, org.id)]) or yesterday_local()
    start = start or end - timedelta(days=29)
    if start > end:
        raise HTTPException(422, "start doit précéder end.")
    if (end - start).days > 731:
        raise HTTPException(422, "Période limitée à 2 ans par requête.")
    totals: dict[str, dict[str, float]] = {}
    for dp in points:
        for day, (kwh, level) in combined_daily(db, dp, start, end).items():
            key = day.isoformat() if granularity == "day" else month_start(day).strftime("%Y-%m")
            bucket = totals.setdefault(key, {**{f.value: 0.0 for f in Fluid}, "N0": 0.0})
            bucket[dp.fluid.value] += kwh
            if level == "N0":
                bucket["N0"] += kwh
    rows = [{"period": k, **{f"{name.lower()}_kwh": round(v, 3) for name, v in values.items()}}
            for k, values in sorted(totals.items())]
    return {"organization_id": org.id, "start": start, "end": end, "granularity": granularity,
            "unit": "kWh", "data": rows}


@router.get("/organizations/{org_id}/drifts")
def drifts(
    org_id: int,
    since: date | None = None,
    scope: PartnerScope = Depends(partner_scope),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Anomalies validées par un humain (les anomalies à valider ou écartées ne sortent pas)."""
    org = scope.organization(org_id)
    items = scope.repo.list_drifts(org.id, DriftStatus.QUALIFIED)
    if since:
        items = [d for d in items if d.day >= since]
    return [validation.drift_payload(db, d) for d in items]


@router.get("/organizations/{org_id}/recommendations")
def recommendations(
    org_id: int, scope: PartnerScope = Depends(partner_scope), db: Session = Depends(get_db)
) -> list[dict]:
    """Recommandations validées ou déclarées appliquées par un humain."""
    org = scope.organization(org_id)
    items = scope.repo.list_recommendations(org.id, [ReviewStatus.VALIDATED, ReviewStatus.APPLIED])
    return [validation.recommendation_payload(db, r) for r in items]


@router.get("/organizations/{org_id}/predictions")
def predictions(
    org_id: int, scope: PartnerScope = Depends(partner_scope), db: Session = Depends(get_db)
) -> list[dict]:
    """Prévisions validées par un humain."""
    org = scope.organization(org_id)
    return [validation.prediction_payload(db, p)
            for p in scope.repo.list_predictions(org.id, [ReviewStatus.VALIDATED])]


@router.get("/organizations/{org_id}/trajectories")
def trajectories(
    org_id: int, scope: PartnerScope = Depends(partner_scope), db: Session = Depends(get_db)
) -> list[dict]:
    """F11 : trajectoires Décret Tertiaire validées par un humain."""
    org = scope.organization(org_id)
    return [validation.trajectory_payload(db, t)
            for t in scope.repo.list_trajectories(org.id, [ReviewStatus.VALIDATED])]



