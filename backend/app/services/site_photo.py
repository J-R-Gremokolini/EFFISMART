"""Photo d'un site, en tête du tableau de bord.

Facultative : sans photo, le tableau de bord montre une illustration de la courbe de charge. Comme le plan 2D, la
photo est déposée par l'auditeur (PNG ou JPEG, 10 Mo au plus, type vérifié sur le contenu et non sur le nom) ;
l'espace client la consulte. Le fichier est rangé avec les documents, sous un nom aléatoire.
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SitePhoto, User
from app.repositories import TenantRepository
from app.services import assets
from app.services.site_plan import ALLOWED_TYPES, MAX_PLAN_MB, PlanError, image_type
from app.timeutils import utcnow

MAX_PHOTO_MB = MAX_PLAN_MB
READ_ONLY = "La photo du site est déposée par l'auditeur ; l'espace client la consulte."


def photo_dir() -> Path:
    path = Path(settings.document_dir) / "photos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_photo(db: Session, site_id: int) -> SitePhoto | None:
    return db.scalar(select(SitePhoto).where(SitePhoto.site_id == site_id))


def save_photo(db: Session, repo: TenantRepository, user: User, site_id: int, file_name: str,
               content: bytes) -> SitePhoto:
    """Dépose (ou remplace) la photo d'un site : PNG ou JPEG, 10 Mo au plus."""
    if not assets.can_edit(user):
        raise assets.AssetPermissionError(READ_ONLY)
    site = repo.get_site(site_id)
    if len(content) > MAX_PHOTO_MB * 1024 * 1024:
        raise PlanError(f"Image trop lourde : {MAX_PHOTO_MB} Mo au plus.")
    content_type = image_type(content)
    if content_type is None:
        raise PlanError("Format refusé : image PNG ou JPEG uniquement.")
    photo = get_photo(db, site.id)
    if photo is not None:
        (photo_dir() / photo.stored_name).unlink(missing_ok=True)
    else:
        photo = SitePhoto(organization_id=site.organization_id, site_id=site.id)
        db.add(photo)
    stored = uuid.uuid4().hex + ALLOWED_TYPES[content_type]
    (photo_dir() / stored).write_bytes(content)
    photo.file_name, photo.content_type, photo.stored_name = Path(file_name).name[:255], content_type, stored
    photo.uploaded_by, photo.uploaded_at = user.id, utcnow()
    db.commit()
    return photo


def remove_photo(db: Session, repo: TenantRepository, user: User, site_id: int) -> None:
    if not assets.can_edit(user):
        raise assets.AssetPermissionError(READ_ONLY)
    photo = get_photo(db, repo.get_site(site_id).id)
    if photo is not None:
        (photo_dir() / photo.stored_name).unlink(missing_ok=True)
        db.delete(photo)
        db.commit()


def photo_data_uri(photo: SitePhoto | None) -> str | None:
    if photo is None:
        return None
    path = photo_dir() / photo.stored_name
    if not path.is_file():
        return None
    return f"data:{photo.content_type};base64,{base64.b64encode(path.read_bytes()).decode()}"
