"""F3 — Suivi réglementaire : échéances par site et journal d'actions."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ActionLog,
    AuditorClientLink,
    DeadlineStatus,
    Notification,
    Obligation,
    RegulatoryDeadline,
    Role,
    Site,
    User,
)
from app.repositories import active_links_clause
from app.timeutils import today_local, utcnow

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


def log_action(
    db: Session,
    site: Site,
    *,
    obligation: Obligation,
    description: str,
    user_id: int,
    performed_at: datetime | None = None,
) -> ActionLog:
    description = description.strip()
    if not description:
        raise ValueError("La description de l'action est obligatoire")
    action = ActionLog(
        site_id=site.id,
        obligation=obligation,
        description=description,
        performed_at=performed_at or utcnow(),
        performed_by=user_id,
    )
    db.add(action)
    db.commit()
    return action


def _reminder_level(days_left: int) -> int | None:
    """Seuil de rappel atteint : le plus petit seuil ≥ jours restants ; -1 = en retard."""
    if days_left < 0:
        return -1
    reached = [t for t in settings.reminder_days if days_left <= t]
    return min(reached) if reached else None


def send_reminders(db: Session, today: date | None = None) -> int:
    """F3 — rappels d'échéance aux auditeurs du client (application et e-mail), à J-60, J-30, J-7, le jour J,
    puis une fois en retard. Un seul rappel par seuil, même après une longue absence. Renvoie le nombre envoyé."""
    from app.services.mailer import enqueue  # import local : le service d'e-mail charge la session de base

    today = today or today_local()
    sent = 0
    for deadline in db.scalars(select(RegulatoryDeadline).where(RegulatoryDeadline.status != DeadlineStatus.DONE)):
        days_left = (deadline.due_date - today).days
        level = _reminder_level(days_left)
        if level is None or (deadline.reminder_level is not None and deadline.reminder_level <= level):
            continue
        deadline.reminder_level = level
        site = deadline.site
        when = ("aujourd'hui" if days_left == 0 else f"en retard de {-days_left} jour(s)" if days_left < 0
                else f"dans {days_left} jour(s)")
        message = (f"Rappel d'échéance : {OBLIGATION_LABELS[deadline.obligation]}, {site.name}, le "
                   f"{deadline.due_date:%d/%m/%Y} ({when})")
        auditors = db.scalars(select(User).where(
            User.role == Role.AUDITOR,
            User.auditor_id.in_(select(AuditorClientLink.auditor_id).where(
                AuditorClientLink.organization_id == site.organization_id, *active_links_clause()))))
        for user in auditors:
            db.add(Notification(user_id=user.id, organization_id=site.organization_id, message=message))
            enqueue(db, user, message, site.organization.name)
        sent += 1
    db.commit()
    return sent


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
        if due_date != deadline.due_date:
            deadline.reminder_level = None  # nouvelle date : le cycle de rappels repart
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
