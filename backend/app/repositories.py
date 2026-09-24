"""Accès aux données filtré par tenant (brief §3.1).

Règle d'isolation : toute lecture de données métier par l'API passe par
`TenantRepository`, qui applique le filtre auditeur / organisation dans la
requête SQL elle-même — jamais seulement dans l'UI. Une ressource hors
périmètre est signalée « introuvable » (404) pour ne rien divulguer.
"""
from __future__ import annotations

from sqlalchemy import ColumnElement, Select, false, or_, select, true
from sqlalchemy.orm import Session

from app.models import (
    ActionLog,
    AuditorClientLink,
    Consent,
    DeliveryPoint,
    Drift,
    DriftStatus,
    ExportJob,
    Organization,
    RegulatoryDeadline,
    Role,
    Site,
    User,
)
from app.timeutils import today_local


class ResourceNotFound(Exception):
    pass


def active_links_clause():
    """Liens auditeur ↔ client en vigueur (actifs et non terminés)."""
    return (
        AuditorClientLink.active.is_(True),
        or_(AuditorClientLink.end_date.is_(None), AuditorClientLink.end_date >= today_local()),
    )


class TenantRepository:
    def __init__(self, db: Session, user: User) -> None:
        self.db = db
        self.user = user

    # --- Filtre d'isolation -----------------------------------------------

    def org_clause(self, organization_id_column) -> ColumnElement[bool]:
        """Condition SQL restreignant `organization_id_column` au périmètre de l'utilisateur."""
        role = self.user.role
        if role == Role.ADMIN:
            return true()
        if role == Role.AUDITOR:
            if self.user.auditor_id is None:
                return false()
            linked_orgs = select(AuditorClientLink.organization_id).where(
                AuditorClientLink.auditor_id == self.user.auditor_id, *active_links_clause()
            )
            return organization_id_column.in_(linked_orgs)
        if role == Role.CLIENT_VIEWER:
            if self.user.organization_id is None:
                return false()
            return organization_id_column == self.user.organization_id
        return false()

    def _one(self, stmt: Select):
        obj = self.db.scalar(stmt)
        if obj is None:
            raise ResourceNotFound()
        return obj

    # --- Organisations, sites, points de livraison ---------------------------

    def list_organizations(self) -> list[Organization]:
        stmt = select(Organization).where(self.org_clause(Organization.id)).order_by(Organization.name)
        return list(self.db.scalars(stmt))

    def get_organization(self, organization_id: int) -> Organization:
        return self._one(
            select(Organization).where(Organization.id == organization_id, self.org_clause(Organization.id))
        )

    def list_sites(self, organization_id: int | None = None) -> list[Site]:
        stmt = select(Site).where(self.org_clause(Site.organization_id))
        if organization_id is not None:
            stmt = stmt.where(Site.organization_id == organization_id)
        return list(self.db.scalars(stmt.order_by(Site.id)))

    def get_site(self, site_id: int) -> Site:
        return self._one(select(Site).where(Site.id == site_id, self.org_clause(Site.organization_id)))

    def _delivery_points_stmt(self) -> Select:
        return (
            select(DeliveryPoint)
            .join(Site, DeliveryPoint.site_id == Site.id)
            .where(self.org_clause(Site.organization_id))
        )

    def list_delivery_points(self, organization_id: int | None = None) -> list[DeliveryPoint]:
        stmt = self._delivery_points_stmt()
        if organization_id is not None:
            stmt = stmt.where(Site.organization_id == organization_id)
        return list(self.db.scalars(stmt.order_by(DeliveryPoint.id)))

    def get_delivery_point(self, delivery_point_id: int) -> DeliveryPoint:
        return self._one(self._delivery_points_stmt().where(DeliveryPoint.id == delivery_point_id))

    def get_consent(self, consent_id: int) -> Consent:
        return self._one(
            select(Consent)
            .join(DeliveryPoint, Consent.delivery_point_id == DeliveryPoint.id)
            .join(Site, DeliveryPoint.site_id == Site.id)
            .where(Consent.id == consent_id, self.org_clause(Site.organization_id))
        )

    # --- Dérives --------------------------------------------------------------

    def _drifts_stmt(self) -> Select:
        return (
            select(Drift)
            .join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id)
            .join(Site, DeliveryPoint.site_id == Site.id)
            .where(self.org_clause(Site.organization_id))
        )

    def list_drifts(self, organization_id: int, status: DriftStatus | None = None) -> list[Drift]:
        stmt = self._drifts_stmt().where(Site.organization_id == organization_id)
        if status is not None:
            stmt = stmt.where(Drift.status == status)
        return list(self.db.scalars(stmt.order_by(Drift.day.desc(), Drift.id.desc())))

    def get_drift(self, drift_id: int) -> Drift:
        return self._one(self._drifts_stmt().where(Drift.id == drift_id))

    # --- Réglementaire --------------------------------------------------------

    def list_deadlines(self, organization_id: int | None = None) -> list[RegulatoryDeadline]:
        stmt = (
            select(RegulatoryDeadline)
            .join(Site, RegulatoryDeadline.site_id == Site.id)
            .where(self.org_clause(Site.organization_id))
        )
        if organization_id is not None:
            stmt = stmt.where(Site.organization_id == organization_id)
        return list(self.db.scalars(stmt.order_by(RegulatoryDeadline.due_date)))

    def get_deadline(self, deadline_id: int) -> RegulatoryDeadline:
        return self._one(
            select(RegulatoryDeadline)
            .join(Site, RegulatoryDeadline.site_id == Site.id)
            .where(RegulatoryDeadline.id == deadline_id, self.org_clause(Site.organization_id))
        )

    def list_action_logs(self, site_id: int) -> list[ActionLog]:
        site = self.get_site(site_id)
        stmt = select(ActionLog).where(ActionLog.site_id == site.id).order_by(ActionLog.performed_at.desc())
        return list(self.db.scalars(stmt))

    # --- Exports ----------------------------------------------------------------

    def list_export_jobs(self, organization_id: int) -> list[ExportJob]:
        stmt = select(ExportJob).where(
            ExportJob.organization_id == organization_id, self.org_clause(ExportJob.organization_id)
        )
        return list(self.db.scalars(stmt.order_by(ExportJob.created_at.desc(), ExportJob.id.desc())))

    def get_export_job(self, job_id: int) -> ExportJob:
        return self._one(
            select(ExportJob).where(ExportJob.id == job_id, self.org_clause(ExportJob.organization_id))
        )
