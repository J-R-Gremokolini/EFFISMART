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
    AssetNode,
    AuditorClientLink,
    Consent,
    DeliveryPoint,
    Document,
    DocumentStatus,
    Drift,
    DriftStatus,
    ExportJob,
    Organization,
    Prediction,
    Recommendation,
    RegulatoryDeadline,
    ReviewStatus,
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


def sees_unvalidated(user: User) -> bool:
    """Principe P1 : une sortie algorithmique non validée n'est visible que de ceux qui la valident
    (auditeur, responsable énergie du client) et de l'administrateur de la plateforme."""
    if user.role in (Role.AUDITOR, Role.ADMIN):
        return True
    return user.role == Role.CLIENT_VIEWER and bool(user.is_energy_manager)


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

    # --- Dérives (anomalies) --------------------------------------------------------

    def _drifts_stmt(self) -> Select:
        stmt = (
            select(Drift)
            .join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id)
            .join(Site, DeliveryPoint.site_id == Site.id)
            .where(self.org_clause(Site.organization_id))
        )
        if not sees_unvalidated(self.user):
            stmt = stmt.where(Drift.status == DriftStatus.QUALIFIED)
        return stmt

    def list_drifts(self, organization_id: int, status: DriftStatus | None = None) -> list[Drift]:
        stmt = self._drifts_stmt().where(Site.organization_id == organization_id)
        if status is not None:
            stmt = stmt.where(Drift.status == status)
        return list(self.db.scalars(stmt.order_by(Drift.day.desc(), Drift.id.desc())))

    def get_drift(self, drift_id: int) -> Drift:
        return self._one(self._drifts_stmt().where(Drift.id == drift_id))

    # --- Recommandations et prévisions (principe P1 : non validées = réservées aux valideurs) --------

    def _recommendations_stmt(self) -> Select:
        stmt = select(Recommendation).where(self.org_clause(Recommendation.organization_id))
        if not sees_unvalidated(self.user):
            stmt = stmt.where(Recommendation.status.in_([ReviewStatus.VALIDATED, ReviewStatus.APPLIED]))
        return stmt

    def list_recommendations(
        self, organization_id: int, statuses: list[ReviewStatus] | None = None
    ) -> list[Recommendation]:
        stmt = self._recommendations_stmt().where(Recommendation.organization_id == organization_id)
        if statuses:
            stmt = stmt.where(Recommendation.status.in_(statuses))
        return list(self.db.scalars(stmt.order_by(Recommendation.created_at.desc(), Recommendation.id.desc())))

    def get_recommendation(self, recommendation_id: int) -> Recommendation:
        return self._one(self._recommendations_stmt().where(Recommendation.id == recommendation_id))

    def _predictions_stmt(self) -> Select:
        stmt = select(Prediction).where(self.org_clause(Prediction.organization_id))
        if not sees_unvalidated(self.user):
            stmt = stmt.where(Prediction.status == ReviewStatus.VALIDATED)
        return stmt

    def list_predictions(self, organization_id: int, statuses: list[ReviewStatus] | None = None) -> list[Prediction]:
        stmt = self._predictions_stmt().where(Prediction.organization_id == organization_id)
        if statuses:
            stmt = stmt.where(Prediction.status.in_(statuses))
        return list(self.db.scalars(stmt.order_by(Prediction.created_at.desc(), Prediction.id.desc())))

    def get_prediction(self, prediction_id: int) -> Prediction:
        return self._one(self._predictions_stmt().where(Prediction.id == prediction_id))

    # --- Graphe physique des équipements -----------------------------------------------------

    def list_asset_nodes(self, site_id: int) -> list[AssetNode]:
        site = self.get_site(site_id)
        stmt = select(AssetNode).where(AssetNode.site_id == site.id, self.org_clause(AssetNode.organization_id))
        return list(self.db.scalars(stmt.order_by(AssetNode.id)))

    def get_asset_node(self, node_id: int) -> AssetNode:
        return self._one(
            select(AssetNode).where(AssetNode.id == node_id, self.org_clause(AssetNode.organization_id))
        )

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

    # --- Documents déposés -----------------------------------------------------------

    def list_documents(self, organization_id: int, status: DocumentStatus | None = None) -> list[Document]:
        stmt = select(Document).where(
            Document.organization_id == organization_id, self.org_clause(Document.organization_id)
        )
        if status is not None:
            stmt = stmt.where(Document.status == status)
        return list(self.db.scalars(stmt.order_by(Document.uploaded_at.desc(), Document.id.desc())))

    def get_document(self, document_id: int) -> Document:
        return self._one(
            select(Document).where(Document.id == document_id, self.org_clause(Document.organization_id))
        )

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
