"""Dépôt de documents par les clients : périmètre, formats, droits et traçabilité."""
import io
import zipfile

import pytest

from app.config import settings
from app.models import DocumentKind, DocumentStatus, Notification
from app.repositories import ResourceNotFound, TenantRepository
from app.services import documents
from app.services.documents import DocumentError, DocumentPermissionError

PDF = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"


def _xlsx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
    return buffer.getvalue()


def _deposit(db, user, org, content=PDF, filename="facture.pdf", **kwargs):
    repo = TenantRepository(db, user)
    return documents.deposit(db, repo, user, org.id, filename=filename, content=content,
                             kind=kwargs.pop("kind", DocumentKind.INVOICE_ELEC), **kwargs)


def test_client_deposits_invoice_for_own_organization(db, world):
    doc = _deposit(db, world.client_a, world.org_a, site_id=world.site_a.id, comment="Facture d'août")
    assert doc.status == DocumentStatus.RECEIVED
    assert documents.read_content(doc) == PDF
    assert doc.stored_name != "facture.pdf" and doc.stored_name.endswith(".pdf")
    # L'auditeur du client est prévenu, pas celui d'un autre cabinet.
    recipients = {n.user_id for n in db.query(Notification).all()}
    assert world.auditor_a.id in recipients
    assert world.auditor_b.id not in recipients


def test_client_cannot_deposit_for_another_organization(db, world):
    with pytest.raises(ResourceNotFound):
        _deposit(db, world.client_a, world.org_b)


def test_site_must_belong_to_the_organization(db, world):
    with pytest.raises(ResourceNotFound):
        _deposit(db, world.client_a, world.org_a, site_id=world.site_b.id)


def test_other_auditor_cannot_see_the_document(db, world):
    doc = _deposit(db, world.client_a, world.org_a)
    with pytest.raises(ResourceNotFound):
        TenantRepository(db, world.auditor_b).get_document(doc.id)
    assert TenantRepository(db, world.auditor_a).get_document(doc.id).id == doc.id


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("programme.exe", b"MZ\x90\x00"),  # extension refusée
        ("facture.pdf", b"MZ\x90\x00 pas un PDF"),  # extension acceptée mais contenu falsifié
        ("releve.xlsx", b"PK\x03\x04 archive corrompue"),  # faux tableur
        ("releve.csv", b"\x00\x01\x02 binaire"),  # CSV binaire
        ("vide.pdf", b""),
    ],
)
def test_rejects_unsafe_or_invalid_files(db, world, filename, content):
    with pytest.raises(DocumentError):
        _deposit(db, world.client_a, world.org_a, content=content, filename=filename)


def test_accepts_readings_as_csv_and_excel(db, world):
    csv_doc = _deposit(db, world.client_a, world.org_a, content="date;index\n01/09/2026;12345\n".encode(),
                       filename="releve.csv", kind=DocumentKind.METER_READING)
    xlsx_doc = _deposit(db, world.client_a, world.org_a, content=_xlsx(), filename="releves.xlsx",
                        kind=DocumentKind.METER_READING)
    assert csv_doc.content_type == "text/csv"
    assert xlsx_doc.content_type.endswith("spreadsheetml.sheet")


def test_rejects_files_over_size_limit(db, world, monkeypatch):
    monkeypatch.setattr(settings, "document_max_mb", 0.00001)  # ≈ 10 octets
    with pytest.raises(DocumentError, match="taille maximale"):
        _deposit(db, world.client_a, world.org_a)


def test_rejects_duplicates_and_path_tricks(db, world):
    doc = _deposit(db, world.client_a, world.org_a, filename="..\\..\\windows\\facture.pdf")
    assert doc.original_name == "facture.pdf"
    with pytest.raises(DocumentError, match="déjà été déposé"):
        _deposit(db, world.client_a, world.org_a, filename="copie.pdf")


def test_rejects_inverted_period(db, world):
    from datetime import date

    with pytest.raises(DocumentError, match="période"):
        _deposit(db, world.client_a, world.org_a, period_start=date(2026, 9, 1), period_end=date(2026, 8, 1))


def test_only_auditor_reviews_and_rejection_needs_a_reason(db, world):
    doc = _deposit(db, world.client_a, world.org_a)
    with pytest.raises(DocumentPermissionError):
        documents.review(db, world.client_a, doc, DocumentStatus.PROCESSED)
    with pytest.raises(DocumentError):
        documents.review(db, world.auditor_a, doc, DocumentStatus.REJECTED, note="  ")
    documents.review(db, world.auditor_a, doc, DocumentStatus.REJECTED, note="Page 2 manquante")
    assert doc.status == DocumentStatus.REJECTED and doc.review_note == "Page 2 manquante"
    # Le client qui a déposé le document est prévenu, avec le motif.
    messages = [n.message for n in db.query(Notification).filter_by(user_id=world.client_a.id)]
    assert any("refusé" in m and "Page 2 manquante" in m for m in messages)


def test_client_withdraws_only_own_unprocessed_deposit(db, world):
    doc = _deposit(db, world.client_a, world.org_a)
    path_content = documents.read_content(doc)
    assert path_content == PDF
    # L'auditeur ne peut pas retirer le dépôt du client (il le refuse avec un motif à la place).
    with pytest.raises(DocumentPermissionError):
        documents.withdraw(db, world.auditor_a, doc)
    documents.withdraw(db, world.client_a, doc)
    with pytest.raises(FileNotFoundError):
        documents.read_content(doc)

    processed = _deposit(db, world.client_a, world.org_a, content=PDF + b"%2")
    documents.review(db, world.auditor_a, processed, DocumentStatus.PROCESSED)
    with pytest.raises(DocumentPermissionError):
        documents.withdraw(db, world.client_a, processed)
