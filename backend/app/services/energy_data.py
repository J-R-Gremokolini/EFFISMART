"""Niveaux de données (F1, F4) et consommations déclarées.

- N1 : compteurs communicants (Linky, Gazpar, connecteurs) — courbe 30 min ou relevé journalier,
  consentement du client requis ;
- N0 : factures et relevés d'index — la consommation d'une période, saisie par l'auditeur ou importée
  d'un tableur déposé par le client.

Règle de combinaison, jour par jour et point par point : la mesure N1 prime ; un jour sans mesure N1
reçoit sa part des périodes N0 qui le couvrent, au prorata des jours. C'est le mode dégradé : totaux
et mois disponibles, mais ni courbe de charge, ni détection d'anomalies, ni prévision journalière.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DeclaredConsumption, DeclaredSource, DeliveryPoint, Role, Site, User
from app.repositories import TenantRepository
from app.services.consent import has_active_consent
from app.services.drift import daily_kwh
from app.timeutils import daterange, today_local

LEVEL_LABELS = {"N1": "compteurs communicants", "N0": "factures et relevés"}
SOURCE_LABELS = {DeclaredSource.INVOICE: "Facture", DeclaredSource.METER_READING: "Relevé d'index",
                 DeclaredSource.OTHER: "Autre justificatif"}
MAX_PERIOD_DAYS = 400


class DeclaredError(ValueError):
    """Saisie refusée ; message affichable."""


class DeclaredPermissionError(PermissionError):
    pass


def can_edit(user: User) -> bool:
    return user.role in (Role.AUDITOR, Role.ADMIN)


def _require_editor(user: User) -> None:
    if not can_edit(user):
        raise DeclaredPermissionError("Les consommations des factures sont saisies par l'auditeur : déposez vos "
                                      "factures et relevés dans « Documents ».")


# --- Saisie --------------------------------------------------------------------------------------


def list_declared(db: Session, repo: TenantRepository, organization_id: int) -> list[DeclaredConsumption]:
    repo.get_organization(organization_id)
    stmt = (select(DeclaredConsumption)
            .where(DeclaredConsumption.organization_id == organization_id,
                   repo.org_clause(DeclaredConsumption.organization_id))
            .order_by(DeclaredConsumption.period_start.desc(), DeclaredConsumption.id.desc()))
    return list(db.scalars(stmt))


def add_declared(
    db: Session, repo: TenantRepository, user: User, delivery_point_id: int, *, period_start: date,
    period_end: date, kwh: float, amount_eur: float | None = None, source: DeclaredSource = DeclaredSource.INVOICE,
    document_id: int | None = None, notes: str | None = None, commit: bool = True,
) -> DeclaredConsumption:
    _require_editor(user)
    dp = repo.get_delivery_point(delivery_point_id)
    organization_id = dp.site.organization_id
    if period_end < period_start:
        raise DeclaredError("La fin de période doit suivre le début.")
    if (period_end - period_start).days + 1 > MAX_PERIOD_DAYS:
        raise DeclaredError(f"Période trop longue : {MAX_PERIOD_DAYS} jours au plus par saisie.")
    if period_end > today_local():
        raise DeclaredError("La période ne peut pas se terminer dans le futur.")
    if kwh is None or kwh < 0:
        raise DeclaredError("La consommation doit être positive ou nulle.")
    if amount_eur is not None and amount_eur < 0:
        raise DeclaredError("Le montant doit être positif.")
    overlap = db.scalar(select(DeclaredConsumption).where(
        DeclaredConsumption.delivery_point_id == dp.id,
        DeclaredConsumption.period_start <= period_end, DeclaredConsumption.period_end >= period_start))
    if overlap is not None:
        raise DeclaredError(f"Période déjà couverte pour {dp.external_ref} par une saisie du "
                            f"{overlap.period_start:%d/%m/%Y} au {overlap.period_end:%d/%m/%Y}.")
    if document_id is not None and repo.get_document(document_id).organization_id != organization_id:
        raise DeclaredError("Ce document appartient à une autre organisation.")
    entry = DeclaredConsumption(
        organization_id=organization_id, delivery_point_id=dp.id, period_start=period_start, period_end=period_end,
        kwh=float(kwh), amount_eur=amount_eur, source=source, document_id=document_id,
        notes=(notes or "").strip() or None, created_by=user.id,
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    return entry


def delete_declared(db: Session, repo: TenantRepository, user: User, entry_id: int) -> None:
    _require_editor(user)
    entry = db.get(DeclaredConsumption, entry_id)
    if entry is None:
        raise DeclaredError("Saisie introuvable.")
    repo.get_organization(entry.organization_id)  # périmètre
    db.delete(entry)
    db.commit()


_HEADERS = {
    "point": ("point", "prm", "pce", "point de livraison", "reference", "référence"),
    "start": ("debut", "début", "date debut", "date de début", "du"),
    "end": ("fin", "date fin", "date de fin", "au"),
    "kwh": ("kwh", "consommation", "consommation kwh", "conso kwh"),
    "amount": ("montant", "montant ttc", "montant eur", "eur", "€"),
}


def _parse_date(value: str) -> date:
    value = value.strip()
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise DeclaredError(f"date illisible « {value} » (attendu JJ/MM/AAAA)")


def _parse_number(value: str) -> float | None:
    value = re.sub(r"[\s €]|kwh", "", value.strip(), flags=re.IGNORECASE).replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise DeclaredError(f"nombre illisible « {value} »") from exc


def import_csv(db: Session, repo: TenantRepository, user: User, organization_id: int, content: bytes,
               document_id: int | None = None) -> tuple[int, list[str]]:
    """Importe un tableur CSV (« ; » ou « , ») : colonnes point, début, fin, kWh et, si connu, montant.

    Chaque ligne est contrôlée comme une saisie manuelle ; les lignes refusées sont listées.
    """
    _require_editor(user)
    repo.get_organization(organization_id)
    text = content.decode("utf-8-sig", errors="replace")
    first_line = text.splitlines()[0] if text.strip() else ""
    delimiter = max(";,\t", key=first_line.count) if first_line else ";"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = [h.strip().lower() for h in next(reader, [])]
    columns = {}
    for key, aliases in _HEADERS.items():
        columns[key] = next((i for i, h in enumerate(header) if h in aliases), None)
    missing = [label for key, label in (("point", "point"), ("start", "début"), ("end", "fin"), ("kwh", "kWh"))
               if columns[key] is None]
    if missing:
        raise DeclaredError("Colonnes manquantes : " + ", ".join(missing) + ". En-tête attendu : "
                            "point;debut;fin;kwh;montant")
    points = {dp.external_ref: dp for dp in repo.list_delivery_points(organization_id)}
    created, errors = 0, []
    for line_number, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            ref = row[columns["point"]].strip()
            if ref not in points:
                raise DeclaredError(f"point de livraison « {ref} » inconnu pour ce client")
            amount = _parse_number(row[columns["amount"]]) if columns["amount"] is not None else None
            add_declared(db, repo, user, points[ref].id, period_start=_parse_date(row[columns["start"]]),
                         period_end=_parse_date(row[columns["end"]]), kwh=_parse_number(row[columns["kwh"]]),
                         amount_eur=amount, document_id=document_id, commit=False)
            created += 1
        except (DeclaredError, IndexError) as exc:
            errors.append(f"ligne {line_number} : {exc if str(exc) else 'colonnes incomplètes'}")
    db.commit()
    return created, errors


# --- Combinaison N1 + N0 ---------------------------------------------------------------------------


def _n1_daily(db: Session, dp: DeliveryPoint, start: date, end: date) -> dict[date, float]:
    return daily_kwh(db, dp.id, start, end) if has_active_consent(db, dp.id) else {}


def declared_daily(db: Session, dp: DeliveryPoint, start: date, end: date,
                   covered: set[date] | None = None) -> dict[date, float]:
    """Part journalière des consommations déclarées (N0), hors jours déjà couverts par une mesure N1."""
    covered = covered or set()
    result: dict[date, float] = defaultdict(float)
    for entry in db.scalars(select(DeclaredConsumption).where(
            DeclaredConsumption.delivery_point_id == dp.id,
            DeclaredConsumption.period_start <= end, DeclaredConsumption.period_end >= start)):
        per_day = entry.kwh / ((entry.period_end - entry.period_start).days + 1)
        for day in daterange(max(start, entry.period_start), min(end, entry.period_end)):
            if day not in covered:
                result[day] += per_day
    return dict(result)


def combined_daily(db: Session, dp: DeliveryPoint, start: date, end: date) -> dict[date, tuple[float, str]]:
    """Consommation journalière d'un point : {jour: (kWh, « N1 » ou « N0 »)}."""
    n1 = _n1_daily(db, dp, start, end)
    combined = {day: (value, "N1") for day, value in n1.items()}
    for day, value in declared_daily(db, dp, start, end, set(n1)).items():
        combined[day] = (value, "N0")
    return combined


def has_declared(db: Session, dp_ids: list[int]) -> set[int]:
    if not dp_ids:
        return set()
    return set(db.scalars(select(DeclaredConsumption.delivery_point_id)
                          .where(DeclaredConsumption.delivery_point_id.in_(dp_ids)).distinct()))


def organization_n0(db: Session, organization_id: int, start: date, end: date) -> dict[int, dict[date, float]]:
    """Part N0 par point et par jour, pour les points de l'organisation qui ont des saisies déclarées."""
    points = list(db.scalars(select(DeliveryPoint).join(Site, DeliveryPoint.site_id == Site.id)
                             .where(Site.organization_id == organization_id)))
    with_declared = has_declared(db, [dp.id for dp in points])
    result = {}
    for dp in points:
        if dp.id in with_declared:
            share = declared_daily(db, dp, start, end, set(_n1_daily(db, dp, start, end)))
            if share:
                result[dp.id] = share
    return result


def data_level(db: Session, dp: DeliveryPoint, start: date | None = None, end: date | None = None) -> str | None:
    """« N1 » si le point a des mesures de compteur consenties, « N0 » s'il n'a que des saisies, sinon None."""
    end = end or today_local()
    start = start or end - timedelta(days=365)
    if _n1_daily(db, dp, start, end):
        return "N1"
    return "N0" if dp.id in has_declared(db, [dp.id]) else None


def latest_declared_day(db: Session, dp_ids: list[int]) -> date | None:
    if not dp_ids:
        return None
    days = list(db.scalars(select(DeclaredConsumption.period_end)
                           .where(DeclaredConsumption.delivery_point_id.in_(dp_ids))))
    return max(days) if days else None
