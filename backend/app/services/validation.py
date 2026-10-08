"""Principe P1 — toute sortie algorithmique est expliquée et attend une validation humaine.

Une sortie (anomalie, recommandation d'optimisation, prévision) est présentée avec :
- son raisonnement : les étapes du calcul, dans l'ordre, lisibles par un non-spécialiste ;
- son gain estimé : kWh, € et kgCO₂e par an, avec les hypothèses du calcul ;
- son niveau de confiance : un score borné (jamais 100 %) et les facteurs qui le composent.

Elle attend ensuite la décision d'un humain : l'auditeur partenaire ou le responsable énergie du client.
La plateforme ne valide jamais seule : ni traitement automatique, ni administrateur de la plateforme.
Tant qu'elle n'est pas validée, une sortie n'est vue que de ses valideurs : pas de notification au reste
du client, pas de webhook, pas d'API partenaires. La plateforme n'agit jamais sur les équipements :
« appliquée » est une déclaration humaine, faite après l'intervention réelle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AuditorClientLink,
    DeliveryPoint,
    Drift,
    DriftStatus,
    Fluid,
    Notification,
    Organization,
    Prediction,
    Recommendation,
    ReviewStatus,
    Role,
    Site,
    User,
)
from app.repositories import TenantRepository, active_links_clause
from app.services.dashboard import emission_factors_at, estimated_price
from app.timeutils import utcnow

CONFIDENCE_FLOOR = 0.05
CONFIDENCE_CEILING = 0.95  # jamais 100 % : la décision reste humaine
MAX_COMMENT_LENGTH = 2000

DRIFT_STATUS_LABELS = {DriftStatus.OPEN: "À valider", DriftStatus.QUALIFIED: "Validée", DriftStatus.IGNORED: "Écartée"}
REVIEW_STATUS_LABELS = {
    ReviewStatus.PROPOSED: "À valider",
    ReviewStatus.VALIDATED: "Validée",
    ReviewStatus.REJECTED: "Écartée",
    ReviewStatus.APPLIED: "Appliquée",
    ReviewStatus.SUPERSEDED: "Remplacée",
}
RECOMMENDATION_TRANSITIONS = {
    ReviewStatus.PROPOSED: {ReviewStatus.VALIDATED, ReviewStatus.REJECTED},
    ReviewStatus.VALIDATED: {ReviewStatus.APPLIED, ReviewStatus.REJECTED, ReviewStatus.PROPOSED},
    ReviewStatus.REJECTED: {ReviewStatus.PROPOSED},
    ReviewStatus.APPLIED: {ReviewStatus.VALIDATED},  # annulation d'une déclaration erronée
}
PREDICTION_TRANSITIONS = {
    ReviewStatus.PROPOSED: {ReviewStatus.VALIDATED, ReviewStatus.REJECTED},
    ReviewStatus.VALIDATED: {ReviewStatus.REJECTED, ReviewStatus.PROPOSED},
    ReviewStatus.REJECTED: {ReviewStatus.PROPOSED},
}


class ValidationError(ValueError):
    """Décision impossible : transition interdite, motif manquant…"""


class ValidationPermissionError(PermissionError):
    """L'utilisateur n'est pas un valideur (auditeur partenaire ou responsable énergie du client)."""


def fr(value: float, digits: int = 0) -> str:
    """Nombre au format français, pour les textes de raisonnement (1 234,5)."""
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def fr_kw(value: float) -> str:
    """Puissance : décimale seulement si nécessaire (7,5 kW ; 55 kW)."""
    return f"{fr(value, 0 if float(value).is_integer() else 1)} kW"


def fr_pct(value: float) -> str:
    """Variation signée au format français (+0,1 %)."""
    return f"{'+' if value > 0 else ''}{fr(value, 1)} %"


# --- Explication d'une sortie ----------------------------------------------------------------------


@dataclass
class Assessment:
    """Explication construite par l'algorithme qui produit la sortie."""

    algorithm: str
    reasoning: list[str] = field(default_factory=list)
    factors: list[tuple[str, float]] = field(default_factory=list)
    gain_kwh: float | None = None
    gain_basis: str | None = None
    base_confidence: float = 0.5

    def step(self, text: str) -> None:
        self.reasoning.append(text)

    def factor(self, label: str, delta: float) -> None:
        self.factors.append((label, delta))

    @property
    def confidence(self) -> float:
        score = self.base_confidence + sum(delta for _, delta in self.factors)
        return round(min(CONFIDENCE_CEILING, max(CONFIDENCE_FLOOR, score)), 2)


def apply_assessment(db: Session, output, assessment: Assessment, fluid: Fluid, at: date) -> None:
    """Recopie l'explication sur la sortie ; convertit le gain en € (prix indicatifs) et en kgCO₂e."""
    output.algorithm = assessment.algorithm
    output.reasoning = list(assessment.reasoning)
    output.confidence = assessment.confidence
    output.confidence_factors = [{"label": label, "delta": round(delta, 2)} for label, delta in assessment.factors]
    output.gain_basis = assessment.gain_basis
    if assessment.gain_kwh is None:
        output.gain_kwh = output.gain_eur = output.gain_kgco2e = None
        return
    kwh = assessment.gain_kwh
    factor = emission_factors_at(db, at).get(fluid)
    output.gain_kwh = round(kwh)
    output.gain_eur = round(kwh * estimated_price(fluid))
    output.gain_kgco2e = round(kwh * factor.factor_kgco2_per_kwh) if factor else None


def confidence_level(score: float | None) -> tuple[str, str]:
    """(libellé, ton d'affichage) du niveau de confiance."""
    if score is None:
        return "Non évaluée", "neutral"
    if score >= 0.75:
        return "Élevée", "success"
    if score >= 0.5:
        return "Moyenne", "warning"
    return "Faible", "danger"


def explanation_dict(output) -> dict:
    """Explication exposée par l'API et les webhooks."""
    return {
        "reasoning": output.reasoning or [],
        "confidence": output.confidence,
        "confidence_level": confidence_level(output.confidence)[0],
        "confidence_factors": output.confidence_factors or [],
        "estimated_gain": {
            "kwh_per_year": output.gain_kwh,
            "eur_per_year": output.gain_eur,
            "kgco2e_per_year": output.gain_kgco2e,
            "basis": output.gain_basis,
        },
        "algorithm": output.algorithm,
    }


# --- Qui valide ------------------------------------------------------------------------------------


def can_validate(user: User) -> bool:
    """Valideurs : l'auditeur partenaire et le responsable énergie du client. Jamais l'administrateur."""
    if user.role == Role.AUDITOR:
        return user.auditor_id is not None
    return user.role == Role.CLIENT_VIEWER and bool(user.is_energy_manager)


def validator_label(user: User | None) -> str:
    if user is None:
        return "—"
    return "l'auditeur" if user.role == Role.AUDITOR else "le responsable énergie"


def validator_role(user: User | None) -> str | None:
    """Rôle exposé à l'extérieur (webhooks, API) : jamais l'identité de la personne."""
    if user is None:
        return None
    return "AUDITOR" if user.role == Role.AUDITOR else "ENERGY_MANAGER"


def _require_validator(user: User) -> None:
    if not can_validate(user):
        raise ValidationPermissionError(
            "Seuls l'auditeur partenaire et le responsable énergie du client peuvent valider ou écarter "
            "une sortie de la plateforme."
        )


def _clean_comment(comment: str | None, *, required: bool, what: str = "écartez cette sortie") -> str | None:
    comment = (comment or "").strip() or None
    if required and (comment is None or len(comment) < 3):
        raise ValidationError(f"Indiquez pourquoi vous {what} : ce motif est conservé et aide à fiabiliser la plateforme.")
    if comment and len(comment) > MAX_COMMENT_LENGTH:
        raise ValidationError("Commentaire limité à 2 000 caractères.")
    return comment


# --- Notifications ---------------------------------------------------------------------------------


def _recipients(db: Session, organization_id: int, *, validators_only: bool) -> list[User]:
    linked = select(AuditorClientLink.auditor_id).where(
        AuditorClientLink.organization_id == organization_id, *active_links_clause()
    )
    conditions = [and_(User.role == Role.AUDITOR, User.auditor_id.in_(linked))]
    client = and_(User.role == Role.CLIENT_VIEWER, User.organization_id == organization_id)
    if validators_only:
        conditions.append(and_(client, User.is_energy_manager.is_(True)))
    elif settings.notify_clients_on_drift:
        conditions.append(client)
    return list(db.scalars(select(User).where(or_(*conditions))))


def notify(db: Session, organization_id: int, message: str, *, validators_only: bool,
           exclude_user_id: int | None = None, drift_id: int | None = None, email: bool = True) -> None:
    """Alerte dans l'application et, si l'utilisateur l'accepte, e-mail immédiat (F2). Pas de commit ici.

    `validators_only` : sortie non validée, seuls ses valideurs sont prévenus.
    """
    from app.services import mailer  # import local : évite un cycle

    organization = db.get(Organization, organization_id)
    for user in _recipients(db, organization_id, validators_only=validators_only):
        if user.id != exclude_user_id:
            db.add(Notification(user_id=user.id, drift_id=drift_id, organization_id=organization_id, message=message))
            if email:
                mailer.enqueue(db, user, message, organization.name if organization else None)


# --- Décisions -----------------------------------------------------------------------------------------


def drift_payload(db: Session, drift: Drift) -> dict:
    dp = drift.delivery_point
    return {
        "drift_id": drift.id, "kind": drift.kind.value, "day": drift.day.isoformat(),
        "site": dp.site.name, "delivery_point": dp.external_ref, "fluid": dp.fluid.value,
        "measured": drift.measured_value, "reference": drift.reference_value,
        "deviation_pct": drift.deviation_pct, "unit": drift.unit, "details": drift.details,
        "status": drift.status.value, "comment": drift.comment,
        "validated_by_role": validator_role(db.get(User, drift.qualified_by) if drift.qualified_by else None),
        "validated_at": drift.qualified_at.isoformat() if drift.qualified_at else None,
        "context": drift.context,  # F2b : équipement suspect, zones et usages potentiellement impactés
        # F2 : alertes regroupées (une cause, une alerte).
        "grouped_with_id": drift.grouped_with_id, "grouping_reason": drift.grouping_reason,
        "group_drift_ids": list(db.scalars(select(Drift.id).where(Drift.grouped_with_id == drift.id)
                                           .order_by(Drift.day, Drift.id))),
        **explanation_dict(drift),
    }


def recommendation_payload(db: Session, rec: Recommendation) -> dict:
    return {
        "recommendation_id": rec.id, "kind": rec.kind.value, "title": rec.title, "action": rec.action,
        "site": rec.site.name, "delivery_point": rec.delivery_point.external_ref if rec.delivery_point else None,
        "drift_id": rec.drift_id, "status": rec.status.value,
        "validated_by_role": validator_role(db.get(User, rec.reviewed_by) if rec.reviewed_by else None),
        "validated_at": rec.reviewed_at.isoformat() if rec.reviewed_at else None,
        "applied_at": rec.applied_at.isoformat() if rec.applied_at else None,
        **explanation_dict(rec),
    }


def prediction_payload(db: Session, prediction: Prediction) -> dict:
    return {
        "prediction_id": prediction.id, "kind": prediction.kind.value, "site": prediction.site.name,
        "fluid": prediction.fluid.value, "year": prediction.year, "data_as_of": prediction.data_as_of.isoformat(),
        "measured_kwh": prediction.measured_kwh, "predicted_kwh": prediction.predicted_kwh,
        "low_kwh": prediction.low_kwh, "high_kwh": prediction.high_kwh, "reference_kwh": prediction.reference_kwh,
        "models_compared": prediction.model_comparison or [],
        "status": prediction.status.value,
        "validated_by_role": validator_role(db.get(User, prediction.reviewed_by) if prediction.reviewed_by else None),
        "validated_at": prediction.reviewed_at.isoformat() if prediction.reviewed_at else None,
        **explanation_dict(prediction),
    }


def review_drift(
    db: Session, repo: TenantRepository, user: User, drift_id: int, status: DriftStatus, comment: str | None = None
) -> tuple[Drift, Recommendation | None]:
    """Valide, écarte ou rouvre une anomalie. Une anomalie validée déclenche une recommandation *proposée*
    (elle-même à valider) ; renvoie cette recommandation, nouvelle ou existante."""
    # Imports locaux : évitent un cycle.
    from app.services import alert_groups, anomaly_context, integrations, recommendations
    from app.services.drift import DRIFT_LABELS

    _require_validator(user)
    drift = repo.get_drift(drift_id)
    if drift.grouped_with_id is not None:
        lead = db.get(Drift, drift.grouped_with_id)
        raise ValidationError(f"Cette anomalie est regroupée avec l'alerte du {lead.day:%d/%m/%Y} : la décision se "
                              "prend sur cette alerte, ou détachez d'abord l'anomalie du groupe.")
    comment = _clean_comment(comment, required=status == DriftStatus.IGNORED, what="écartez cette anomalie")
    previous = drift.status
    # La décision vaut pour tout le groupe d'alertes de même cause.
    group = alert_groups.members(db, drift)
    decided = status != DriftStatus.OPEN
    for output in (drift, *group):
        output.status, output.comment = status, comment
        output.qualified_by = user.id if decided else None
        output.qualified_at = utcnow() if decided else None
    dp = drift.delivery_point
    organization_id = dp.site.organization_id

    recommendation = None
    if status == DriftStatus.QUALIFIED and previous != DriftStatus.QUALIFIED:
        context = anomaly_context.short_text(drift.context)
        notify(db, organization_id,
               f"Anomalie validée par {validator_label(user)} : {DRIFT_LABELS[drift.kind].lower()} le "
               f"{drift.day:%d/%m/%Y}, {dp.site.name} ({dp.external_ref})"
               + (f", avec {len(group)} occurrence(s) regroupée(s) de même cause" if group else "")
               + (f". {context[0].upper()}{context[1:]}" if context else ""),
               validators_only=False, exclude_user_id=user.id, drift_id=drift.id)
        integrations.enqueue_event(db, "drift.validated", organization_id, drift_payload(db, drift))
        recommendation = recommendations.propose_for_drift(db, drift)
        if recommendation is not None and group:
            support = list(recommendation.supporting_drift_ids or [])
            recommendation.supporting_drift_ids = support + [
                d.id for d in group if d.id not in support and d.id != recommendation.drift_id]
    elif previous == DriftStatus.QUALIFIED and status != DriftStatus.QUALIFIED:
        recommendations.withdraw_for_drift(db, drift)
    db.commit()
    return drift, recommendation


def review_recommendation(
    db: Session, repo: TenantRepository, user: User, recommendation_id: int, status: ReviewStatus,
    comment: str | None = None,
) -> Recommendation:
    """Valide, écarte, rouvre, ou déclare appliquée (après l'intervention réelle) une recommandation."""
    from app.services import integrations

    _require_validator(user)
    rec = repo.get_recommendation(recommendation_id)
    if status not in RECOMMENDATION_TRANSITIONS.get(rec.status, set()):
        raise ValidationError(
            f"Passage de « {REVIEW_STATUS_LABELS[rec.status]} » à « {REVIEW_STATUS_LABELS[status]} » impossible."
        )
    if status == ReviewStatus.APPLIED:
        rec.applied_by, rec.applied_at = user.id, utcnow()
        rec.applied_comment = _clean_comment(comment, required=False)
    elif rec.status == ReviewStatus.APPLIED:  # retour à « validée » : la déclaration était erronée
        rec.applied_by = rec.applied_at = rec.applied_comment = None
        rec.savings_snapshot = rec.savings_validated_by = rec.savings_validated_at = None
    else:
        comment = _clean_comment(comment, required=status == ReviewStatus.REJECTED,
                                 what="écartez cette recommandation")
        decided = status != ReviewStatus.PROPOSED
        rec.reviewed_by = user.id if decided else None
        rec.reviewed_at = utcnow() if decided else None
        rec.review_comment = comment
    previous, rec.status = rec.status, status

    if status == ReviewStatus.VALIDATED and previous == ReviewStatus.PROPOSED:
        notify(db, rec.organization_id, f"Recommandation validée par {validator_label(user)} : {rec.title}",
               validators_only=False, exclude_user_id=user.id)
        integrations.enqueue_event(db, "recommendation.validated", rec.organization_id,
                                   recommendation_payload(db, rec))
    elif status == ReviewStatus.APPLIED:
        notify(db, rec.organization_id, f"Recommandation appliquée (déclarée par {validator_label(user)}) : {rec.title}",
               validators_only=False, exclude_user_id=user.id)
        integrations.enqueue_event(db, "recommendation.applied", rec.organization_id, recommendation_payload(db, rec))
    db.commit()
    return rec


def validate_savings(db: Session, repo: TenantRepository, user: User, recommendation_id: int) -> Recommendation:
    """F1 « avant / après » : un humain valide la mesure des économies ; elle est figée telle que validée et
    devient visible du client. Une nouvelle validation remplace la précédente (suivi plus long)."""
    from app.services import integrations, savings

    _require_validator(user)
    rec = repo.get_recommendation(recommendation_id)
    if rec.status != ReviewStatus.APPLIED:
        raise ValidationError("Seule une recommandation déclarée appliquée a des économies à mesurer.")
    measurement = savings.measure(db, rec)
    if measurement is None:
        raise ValidationError("Mesure impossible pour l'instant : il faut au moins 14 jours de suivi après "
                              "l'application et les données du compteur concerné.")
    rec.savings_snapshot = measurement.as_dict()
    rec.savings_validated_by, rec.savings_validated_at = user.id, utcnow()
    notify(db, rec.organization_id,
           f"Économies mesurées validées par {validator_label(user)} : {fr(measurement.annualized_kwh)} kWh par an "
           f"({rec.title})", validators_only=False, exclude_user_id=user.id)
    integrations.enqueue_event(db, "savings.validated", rec.organization_id, {
        **recommendation_payload(db, rec), "savings": rec.savings_snapshot,
        "savings_validated_by_role": validator_role(user), "savings_validated_at": rec.savings_validated_at.isoformat(),
    })
    db.commit()
    return rec


def review_prediction(
    db: Session, repo: TenantRepository, user: User, prediction_id: int, status: ReviewStatus,
    comment: str | None = None,
) -> Prediction:
    from app.services import integrations

    _require_validator(user)
    prediction = repo.get_prediction(prediction_id)
    if status not in PREDICTION_TRANSITIONS.get(prediction.status, set()):
        raise ValidationError(
            f"Passage de « {REVIEW_STATUS_LABELS[prediction.status]} » à « {REVIEW_STATUS_LABELS[status]} » impossible."
        )
    comment = _clean_comment(comment, required=status == ReviewStatus.REJECTED, what="écartez cette prévision")
    decided = status != ReviewStatus.PROPOSED
    previous = prediction.status
    prediction.status, prediction.review_comment = status, comment
    prediction.reviewed_by = user.id if decided else None
    prediction.reviewed_at = utcnow() if decided else None
    if status == ReviewStatus.VALIDATED and previous == ReviewStatus.PROPOSED:
        notify(db, prediction.organization_id,
               f"Prévision {prediction.year} validée par {validator_label(user)} : {prediction.site.name}, "
               f"{'électricité' if prediction.fluid == Fluid.ELEC else 'gaz'}",
               validators_only=False, exclude_user_id=user.id)
        integrations.enqueue_event(db, "prediction.validated", prediction.organization_id,
                                   prediction_payload(db, prediction))
    db.commit()
    return prediction


# --- File de validation ---------------------------------------------------------------------------


@dataclass
class PendingItem:
    kind: str  # "drift" | "recommendation" | "prediction"
    output: Drift | Recommendation | Prediction

    @property
    def priority(self) -> float:
        """Enjeu × confiance : les sorties les plus utiles et les plus sûres d'abord."""
        return abs(self.output.gain_eur or 0) * (self.output.confidence or 0)


def pending_outputs(repo: TenantRepository, organization_id: int) -> list[PendingItem]:
    items = (
        [PendingItem("recommendation", r) for r in repo.list_recommendations(organization_id, [ReviewStatus.PROPOSED])]
        + [PendingItem("drift", d) for d in repo.list_drifts(organization_id, DriftStatus.OPEN)
           if d.grouped_with_id is None]  # une alerte par groupe de même cause
        + [PendingItem("prediction", p) for p in repo.list_predictions(organization_id, [ReviewStatus.PROPOSED])]
    )
    return sorted(items, key=lambda item: item.priority, reverse=True)


def count_pending(db: Session, organization_id: int) -> int:
    """Sorties en attente de validation (portefeuille de l'auditeur)."""
    drifts = db.scalar(
        select(func.count(Drift.id))
        .join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id)
        .join(Site, DeliveryPoint.site_id == Site.id)
        .where(Site.organization_id == organization_id, Drift.status == DriftStatus.OPEN,
               Drift.grouped_with_id.is_(None))
    ) or 0
    recs = db.scalar(select(func.count(Recommendation.id)).where(
        Recommendation.organization_id == organization_id, Recommendation.status == ReviewStatus.PROPOSED)) or 0
    preds = db.scalar(select(func.count(Prediction.id)).where(
        Prediction.organization_id == organization_id, Prediction.status == ReviewStatus.PROPOSED)) or 0
    return int(drifts + recs + preds)
