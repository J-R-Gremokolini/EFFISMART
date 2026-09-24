"""F3 — Suivi réglementaire : échéances par site et journal d'actions."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ActionLog, DeadlineStatus, Obligation, RegulatoryDeadline, Site
from app.timeutils import today_local

OBLIGATION_LABELS = {
    Obligation.DECRET_TERTIAIRE_OPERAT: "Déclaration OPERAT (Décret Tertiaire)",
    Obligation.AUDIT_EED: "Audit énergétique (EED)",
    Obligation.VSME: "Rapport VSME",
}


def operat_due_date(year: int) -> date:
    return date(year, settings.operat_due_month, settings.operat_due_day)


def next_operat_due_date(today: date) -> date:
    due = operat_due_date(today.year)
    return due if due >= today else operat_due_date(today.year + 1)


def compute_status(due_date: date, current: DeadlineStatus | None = None, today: date | None = None) -> DeadlineStatus:
    """UPCOMING → DUE_SOON à J-`due_soon_days` (paramétrable). DONE est définitif."""
    if current == DeadlineStatus.DONE:
        return DeadlineStatus.DONE
    today = today or today_local()
    if (due_date - today).days <= settings.due_soon_days:
        return DeadlineStatus.DUE_SOON
    return DeadlineStatus.UPCOMING


def create_deadline(
    db: Session, site: Site, obligation: Obligation, due_date: date, notes: str | None = None
) -> RegulatoryDeadline:
    deadline = RegulatoryDeadline(
        site_id=site.id,
        obligation=obligation,
        due_date=due_date,
        status=compute_status(due_date),
        notes=notes,
    )
    db.add(deadline)
    db.flush()
    return deadline


def ensure_operat_deadline(db: Session, site: Site, today: date | None = None) -> RegulatoryDeadline | None:
    """Un site assujetti au Décret Tertiaire a toujours une échéance OPERAT annuelle ouverte."""
    if not site.is_tertiary_decret:
        return None
    existing = db.scalar(
        select(RegulatoryDeadline).where(
            RegulatoryDeadline.site_id == site.id,
            RegulatoryDeadline.obligation == Obligation.DECRET_TERTIAIRE_OPERAT,
            RegulatoryDeadline.status != DeadlineStatus.DONE,
        )
    )
    if existing:
        return existing
    return create_deadline(
        db, site, Obligation.DECRET_TERTIAIRE_OPERAT, next_operat_due_date(today or today_local()),
        "Déclaration annuelle des consommations N-1 sur la plateforme OPERAT (ADEME)",
    )


def refresh_statuses(db: Session, today: date | None = None) -> int:
    """Job quotidien : fait passer les échéances en DUE_SOON. Renvoie le nombre de changements."""
    changed = 0
    for deadline in db.scalars(select(RegulatoryDeadline).where(RegulatoryDeadline.status != DeadlineStatus.DONE)):
        new_status = compute_status(deadline.due_date, deadline.status, today)
        if new_status != deadline.status:
            deadline.status = new_status
            changed += 1
    db.commit()
    return changed


def update_deadline(
    db: Session,
    deadline: RegulatoryDeadline,
    *,
    user_id: int,
    status: DeadlineStatus | None = None,
    due_date: date | None = None,
    notes: str | None = None,
) -> RegulatoryDeadline:
    if due_date is not None:
        deadline.due_date = due_date
    if notes is not None:
        deadline.notes = notes
    becomes_done = status == DeadlineStatus.DONE and deadline.status != DeadlineStatus.DONE
    if status == DeadlineStatus.DONE:
        deadline.status = DeadlineStatus.DONE
    elif status is not None or deadline.status != DeadlineStatus.DONE:
        # Réouverture explicite ou changement de date : statut recalculé.
        deadline.status = compute_status(deadline.due_date)

    if becomes_done:
        label = OBLIGATION_LABELS[deadline.obligation]
        db.add(
            ActionLog(
                site_id=deadline.site_id,
                obligation=deadline.obligation,
                description=f"Échéance « {label} » du {deadline.due_date:%d/%m/%Y} marquée comme réalisée",
                performed_by=user_id,
            )
        )
        if deadline.obligation == Obligation.DECRET_TERTIAIRE_OPERAT:
            site = db.get(Site, deadline.site_id)
            if site is not None and site.is_tertiary_decret:
                create_deadline(
                    db, site, Obligation.DECRET_TERTIAIRE_OPERAT, operat_due_date(deadline.due_date.year + 1),
                    "Déclaration annuelle des consommations N-1 sur la plateforme OPERAT (ADEME)",
                )
    db.commit()
    return deadline
