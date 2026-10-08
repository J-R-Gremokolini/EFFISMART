"""F4 — Données énergie pour les rapports RSE (OPERAT / VSME).

Choix V1 (ambiguïté signalée) : la génération crée un ExportJob, c'est donc une
écriture réservée à l'auditeur ; le client (lecture seule, F5) télécharge ses exports.
"""
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_repo, require_writer
from app.models import ExportJob, User
from app.repositories import TenantRepository
from app.schemas import ExportIn, ExportJobOut
from app.services.exports import ExportEngine, archive_name, build_zip

router = APIRouter(tags=["exports"])


@router.get("/organizations/{org_id}/exports", response_model=list[ExportJobOut])
def list_exports(org_id: int, repo: TenantRepository = Depends(get_repo)):
    repo.get_organization(org_id)
    return repo.list_export_jobs(org_id)


@router.post(
    "/organizations/{org_id}/exports", response_model=list[ExportJobOut], status_code=status.HTTP_201_CREATED
)
def create_exports(
    org_id: int,
    body: ExportIn,
    user: User = Depends(require_writer),
    repo: TenantRepository = Depends(get_repo),
    db: Session = Depends(get_db),
) -> list[ExportJob]:
    org = repo.get_organization(org_id)
    return ExportEngine().run(db, org, body.period_start, body.period_end, body.formats, user.id)


@router.get("/exports/{job_id}/download")
def download_export(job_id: int, repo: TenantRepository = Depends(get_repo)) -> Response:
    job = repo.get_export_job(job_id)
    try:
        content = build_zip(job)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fichier d'export indisponible") from None
    filename = archive_name(job)
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
