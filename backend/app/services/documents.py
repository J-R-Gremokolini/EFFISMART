"""Dépôt de documents (factures d'énergie, relevés de compteur).

Seule écriture ouverte à l'espace client (exception documentée à la règle F5) :
un client dépose des documents pour SA propre organisation et peut retirer un dépôt
tant qu'il n'a pas été traité. L'auditeur consulte, télécharge et qualifie les dépôts.

Les contrôles sont faits ici, au niveau service (pas seulement dans l'interface) :
- périmètre : l'organisation et le site passent par `TenantRepository` ;
- format : liste blanche d'extensions ET vérification de la signature du contenu ;
- taille : `settings.document_max_mb` par fichier ;
- stockage : nom aléatoire, jamais le nom fourni par l'utilisateur (pas de traversée de chemin) ;
- doublons : empreinte SHA-256 par organisation.
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AuditorClientLink,
    Document,
    DocumentKind,
    DocumentStatus,
    Notification,
    Role,
    User,
)
from app.repositories import TenantRepository, active_links_clause
from app.timeutils import to_local, utcnow

KIND_LABELS = {
    DocumentKind.INVOICE_ELEC: "Facture d'électricité",
    DocumentKind.INVOICE_GAS: "Facture de gaz",
    DocumentKind.INVOICE_OTHER: "Autre facture d'énergie (fioul, chaleur, eau…)",
    DocumentKind.METER_READING: "Relevé de compteur",
    DocumentKind.OTHER: "Autre document",
}


class DocumentError(ValueError):
    """Dépôt refusé (format, taille, doublon, période incohérente…) ; message affichable."""


class DocumentPermissionError(PermissionError):
    """Action non autorisée pour ce compte sur ce document."""


def _is_text(content: bytes) -> bool:
    sample = content[:65536]
    if b"\x00" in sample:
        return False
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            sample.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _is_xlsx(content: bytes) -> bool:
    if not content.startswith(b"PK\x03\x04"):
        return False
    try:
        # Lecture de la table des matières uniquement : aucune décompression du contenu.
        names = zipfile.ZipFile(io.BytesIO(content)).namelist()
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names)


# Extension → (type MIME, contrôle de la signature du contenu).
ALLOWED_TYPES = {
    ".pdf": ("application/pdf", lambda c: c.startswith(b"%PDF-")),
    ".png": ("image/png", lambda c: c.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda c: c.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda c: c.startswith(b"\xff\xd8\xff")),
    ".csv": ("text/csv", _is_text),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _is_xlsx),
}
ALLOWED_EXTENSIONS = [ext.lstrip(".") for ext in ALLOWED_TYPES]


def clean_filename(name: str) -> str:
    """Nom d'affichage sûr : dernier segment du chemin, caractères simples, 120 caractères au plus."""
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r"[^\w.\- ()]", "_", base).strip(" .")[:120]
    return base or "document"


def _storage_path(doc: Document) -> Path:
    return Path(settings.document_dir) / str(doc.organization_id) / doc.stored_name


def can_deposit(user: User, organization_id: int) -> bool:
    if user.role == Role.CLIENT_VIEWER:
        return user.organization_id == organization_id
    return user.role in (Role.AUDITOR, Role.ADMIN)


def deposit(
    db: Session,
    repo: TenantRepository,
    user: User,
    organization_id: int,
    *,
    filename: str,
    content: bytes,
    kind: DocumentKind,
    site_id: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    comment: str | None = None,
) -> Document:
    org = repo.get_organization(organization_id)  # 404 si hors périmètre
    if not can_deposit(user, org.id):
        raise DocumentPermissionError("Ce compte ne peut pas déposer de document pour cette organisation.")
    if site_id is not None and repo.get_site(site_id).organization_id != org.id:
        raise DocumentError("Ce site n'appartient pas à cette organisation.")

    display_name = clean_filename(filename)
    extension = Path(display_name).suffix.lower()
    if extension not in ALLOWED_TYPES:
        raise DocumentError(
            f"« {display_name} » : format non accepté. Formats possibles : PDF, PNG, JPEG, CSV ou Excel (.xlsx)."
        )
    if not content:
        raise DocumentError(f"« {display_name} » est vide.")
    max_bytes = int(settings.document_max_mb * 1024 * 1024)
    if len(content) > max_bytes:
        raise DocumentError(
            f"« {display_name} » dépasse la taille maximale de {settings.document_max_mb:g} Mo."
        )
    content_type, signature_ok = ALLOWED_TYPES[extension]
    if not signature_ok(content):
        raise DocumentError(
            f"« {display_name} » ne correspond pas à un fichier {extension.lstrip('.').upper()} valide."
        )
    if period_start and period_end and period_start > period_end:
        raise DocumentError("La date de début de période doit précéder la date de fin.")

    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(
        select(Document).where(Document.organization_id == org.id, Document.sha256 == digest)
    )
    if existing is not None:
        raise DocumentError(
            f"« {display_name} » a déjà été déposé le {to_local(existing.uploaded_at):%d/%m/%Y} "
            f"(sous le nom « {existing.original_name} »)."
        )

    doc = Document(
        organization_id=org.id,
        site_id=site_id,
        kind=kind,
        status=DocumentStatus.RECEIVED,
        original_name=display_name,
        stored_name=f"{uuid.uuid4().hex}{extension}",
        content_type=content_type,
        size_bytes=len(content),
        sha256=digest,
        period_start=period_start,
        period_end=period_end,
        comment=(comment or "").strip() or None,
        uploaded_by=user.id,
    )
    path = _storage_path(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:  # « x » : n'écrase jamais un fichier existant
        handle.write(content)
    db.add(doc)
    try:
        db.flush()
        if user.role == Role.CLIENT_VIEWER:
            _notify_auditors(db, doc, user)
        from app.services import integrations  # import local : évite un cycle documents ↔ intégrations

        integrations.enqueue_event(db, "document.deposited", org.id, {
            "document_id": doc.id, "kind": doc.kind.value, "name": doc.original_name,
            "site_id": doc.site_id, "size_bytes": doc.size_bytes, "content_type": doc.content_type,
            "period_start": doc.period_start.isoformat() if doc.period_start else None,
            "period_end": doc.period_end.isoformat() if doc.period_end else None,
            "deposited_by_role": user.role.value,
        })
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return doc


def _notify_auditors(db: Session, doc: Document, uploader: User) -> None:
    linked = select(AuditorClientLink.auditor_id).where(
        AuditorClientLink.organization_id == doc.organization_id, *active_links_clause()
    )
    message = f"Nouveau document déposé par {uploader.email} : {KIND_LABELS[doc.kind]}, « {doc.original_name} »."
    for auditor in db.scalars(select(User).where(User.role == Role.AUDITOR, User.auditor_id.in_(linked))):
        db.add(Notification(user_id=auditor.id, organization_id=doc.organization_id, message=message))


def read_content(doc: Document) -> bytes:
    path = _storage_path(doc)
    if not path.is_file():
        raise FileNotFoundError(doc.id)
    return path.read_bytes()


def review(db: Session, user: User, doc: Document, status: DocumentStatus, note: str | None = None) -> Document:
    """Qualification par l'auditeur : traité, ou refusé avec un motif (visible par le client)."""
    if user.role not in (Role.AUDITOR, Role.ADMIN):
        raise DocumentPermissionError("Seul l'auditeur peut traiter un document.")
    note = (note or "").strip() or None
    if status == DocumentStatus.REJECTED and not note:
        raise DocumentError("Indiquez au client pourquoi le document est refusé.")
    doc.status = status
    doc.review_note = note
    doc.reviewed_by = user.id if status != DocumentStatus.RECEIVED else None
    doc.reviewed_at = utcnow() if status != DocumentStatus.RECEIVED else None
    _notify_uploader(db, doc)
    db.commit()
    return doc


def _notify_uploader(db: Session, doc: Document) -> None:
    """Prévient le client qui a déposé le document qu'il a été traité ou refusé."""
    uploader = db.get(User, doc.uploaded_by) if doc.uploaded_by else None
    if uploader is None or uploader.role != Role.CLIENT_VIEWER or doc.status == DocumentStatus.RECEIVED:
        return
    if doc.status == DocumentStatus.PROCESSED:
        message = f"Votre document « {doc.original_name} » a été traité."
    else:
        message = f"Votre document « {doc.original_name} » a été refusé : {doc.review_note}"
    db.add(Notification(user_id=uploader.id, organization_id=doc.organization_id, message=message))


def can_withdraw(user: User, doc: Document) -> bool:
    """Un dépôt peut être retiré par son auteur tant qu'il n'a pas été traité."""
    return doc.uploaded_by == user.id and doc.status == DocumentStatus.RECEIVED


def withdraw(db: Session, user: User, doc: Document) -> None:
    if not can_withdraw(user, doc):
        raise DocumentPermissionError("Seul l'auteur d'un dépôt non encore traité peut le retirer.")
    path = _storage_path(doc)
    db.delete(doc)
    db.commit()
    path.unlink(missing_ok=True)
