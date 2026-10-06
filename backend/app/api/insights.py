"""Principe P1 — recommandations et prévisions (lecture, décision humaine) ; graphe physique (lecture)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.drifts import explanation_fields
from app.db import get_db
from app.deps import get_repo, require_validator
from app.models import Prediction, Recommendation, ReviewStatus, User
from app.repositories import TenantRepository
from app.schemas import AssetGraphOut, PredictionOut, RecommendationOut, ReviewIn
from app.services import assets, validation

router = APIRouter(tags=["recommandations et prévisions"])


def recommendation_out(db: Session, rec: Recommendation) -> RecommendationOut:
    return RecommendationOut(
        id=rec.id, organization_id=rec.organization_id, site_id=rec.site_id, site_name=rec.site.name,
        delivery_point_id=rec.delivery_point_id, drift_id=rec.drift_id, equipment_id=rec.equipment_id,
        kind=rec.kind, title=rec.title, action=rec.action, status=rec.status, review_comment=rec.review_comment,
        applied_at=rec.applied_at, applied_comment=rec.applied_comment, created_at=rec.created_at,
        **explanation_fields(db, rec, rec.reviewed_by, rec.reviewed_at),
    )


def prediction_out(db: Session, prediction: Prediction) -> PredictionOut:
    return PredictionOut(
        id=prediction.id, organization_id=prediction.organization_id, site_id=prediction.site_id,
        site_name=prediction.site.name, fluid=prediction.fluid, kind=prediction.kind, year=prediction.year,
        data_as_of=prediction.data_as_of, measured_kwh=prediction.measured_kwh,
        predicted_kwh=prediction.predicted_kwh, low_kwh=prediction.low_kwh, high_kwh=prediction.high_kwh,
        reference_kwh=prediction.reference_kwh, monthly=prediction.monthly or [],
        model_comparison=prediction.model_comparison or [], status=prediction.status,
        review_comment=prediction.review_comment, created_at=prediction.created_at,
        **explanation_fields(db, prediction, prediction.reviewed_by, prediction.reviewed_at),
    )


def _decide(call):
    try:
        return call()
    except validation.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/organizations/{org_id}/recommendations", response_model=list[RecommendationOut])
def list_recommendations(
    org_id: int, status: ReviewStatus | None = None, repo: TenantRepository = Depends(get_repo)
) -> list[RecommendationOut]:
    repo.get_organization(org_id)
    return [recommendation_out(repo.db, r) for r in repo.list_recommendations(org_id, [status] if status else None)]


@router.patch("/recommendations/{recommendation_id}", response_model=RecommendationOut)
def review_recommendation(
    recommendation_id: int, body: ReviewIn, user: User = Depends(require_validator),
    repo: TenantRepository = Depends(get_repo), db: Session = Depends(get_db),
) -> RecommendationOut:
    """Valider, écarter (motif obligatoire), rouvrir ou déclarer appliquée (APPLIED) après l'intervention réelle."""
    rec = _decide(lambda: validation.review_recommendation(db, repo, user, recommendation_id, body.status, body.comment))
    return recommendation_out(db, rec)


@router.get("/organizations/{org_id}/predictions", response_model=list[PredictionOut])
def list_predictions(
    org_id: int, status: ReviewStatus | None = None, repo: TenantRepository = Depends(get_repo)
) -> list[PredictionOut]:
    repo.get_organization(org_id)
    return [prediction_out(repo.db, p) for p in repo.list_predictions(org_id, [status] if status else None)]


@router.patch("/predictions/{prediction_id}", response_model=PredictionOut)
def review_prediction(
    prediction_id: int, body: ReviewIn, user: User = Depends(require_validator),
    repo: TenantRepository = Depends(get_repo), db: Session = Depends(get_db),
) -> PredictionOut:
    prediction = _decide(lambda: validation.review_prediction(db, repo, user, prediction_id, body.status, body.comment))
    return prediction_out(db, prediction)


@router.get("/sites/{site_id}/assets", response_model=AssetGraphOut)
def site_assets(site_id: int, repo: TenantRepository = Depends(get_repo)) -> AssetGraphOut:
    """Graphe physique du site : éléments, relations, chaînes compteur → usage, maillons manquants."""
    graph = assets.site_graph(repo.db, repo, site_id)
    chains = [text for node in graph.nodes.values() if node.kind.value == "METER"
              for text in assets.chain_summaries(graph.physical_paths(node.id))]
    return AssetGraphOut(site_id=site_id, nodes=list(graph.nodes.values()), relations=graph.relations,
                         chains=chains, warnings=graph.warnings())
