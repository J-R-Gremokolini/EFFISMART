"""Principe P1 : toute sortie algorithmique (anomalie, recommandation, prévision) est présentée avec son
raisonnement, son gain estimé et son niveau de confiance, et attend la validation d'un humain :
l'auditeur partenaire ou le responsable énergie du client, jamais la plateforme seule."""
from datetime import date, timedelta

import httpx
import pytest

from app.models import (
    AssetNode,
    AssetNodeKind,
    AssetRelationKind,
    Consent,
    DeliveryPoint,
    Drift,
    DriftKind,
    DriftStatus,
    Fluid,
    ProviderKind,
    RecommendationKind,
    ReviewStatus,
    Role,
    User,
    WebhookDelivery,
)
from app.providers.mock import AnomalyKind, AnomalySpec, MockDataProvider, between, elec_profile
from app.providers.registry import set_provider_factory
from app.repositories import ResourceNotFound, TenantRepository
from app.security import hash_password
from app.services import assets, integrations, net, predictions, validation
from app.services.drift import run_detection
from app.services.ingestion import ingest_delivery_point
from app.timeutils import utcnow, yesterday_local
from tests.conftest import PASSWORD, login

K, R = AssetNodeKind, AssetRelationKind
REF = "30009000000888"
DAY = date(2025, 6, 14)  # samedi
UNTIL = date(2025, 6, 30)


@pytest.fixture()
def manager(db, world) -> User:
    """Responsable énergie du client A."""
    user = User(email="energie-a@test.fr", password_hash=hash_password(PASSWORD), role=Role.CLIENT_VIEWER,
                organization_id=world.org_a.id, is_energy_manager=True)
    db.add(user)
    db.commit()
    return user


def _graph(db, world, dp) -> AssetNode:
    """Compteur → groupe froid (puissance ≈ excès du week-end) → eau glacée → CTA → bureaux → climatisation."""
    repo, user = TenantRepository(db, world.auditor_a), world.auditor_a
    meter = assets.ensure_meter_node(db, dp)
    db.commit()
    excess = elec_profile(REF).peak_kw * 0.35

    def node(kind, category, name, **kw):
        return assets.create_node(db, repo, user, world.site_a.id, kind=kind, category_code=category, name=name, **kw)

    chiller = node(K.EQUIPMENT, "CHILLER", "Groupe froid", power_kw=round(excess))
    lighting = node(K.EQUIPMENT, "LIGHTING", "Éclairage", power_kw=round(excess / 4))
    servers = node(K.EQUIPMENT, "IT", "Serveurs", power_kw=round(excess))  # même puissance, mais fonctionnement continu
    ahu = node(K.EQUIPMENT, "AHU", "CTA bureaux", power_kw=5)
    chilled = node(K.FLOW, "CHILLED_WATER", "Eau glacée")
    offices = node(K.ZONE, "OFFICE", "Bureaux", surface_m2=800)
    cooling = node(K.USAGE, "COOLING", "Climatisation")
    for source, kind, target in [
        (meter, R.SUPPLIES, chiller), (meter, R.SUPPLIES, lighting), (meter, R.SUPPLIES, servers),
        (meter, R.SUPPLIES, ahu), (chiller, R.PRODUCES, chilled), (chilled, R.SUPPLIES, ahu),
        (ahu, R.SERVES, offices), (lighting, R.SERVES, offices), (offices, R.HOSTS, cooling),
    ]:
        assets.create_relation(db, repo, user, source.id, kind, target.id)
    return chiller


@pytest.fixture()
def weekend_drift(db, world) -> Drift:
    """Talon anormal un samedi (groupe froid resté allumé), sur un site dont le graphe est modélisé."""
    set_provider_factory(lambda provider, fluid: MockDataProvider(
        fluid=fluid, anomalies=[AnomalySpec(REF, AnomalyKind.WEEKEND_ON, between(DAY, DAY), 0.35)],
        available_until=UNTIL))
    dp = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref=REF, provider=ProviderKind.MOCK)
    db.add(dp)
    db.flush()
    db.add(Consent(delivery_point_id=dp.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    _graph(db, world, dp)
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    drifts = [d for d in run_detection(db, DAY, delivery_point_ids=[dp.id]) if d.kind == DriftKind.BASELOAD]
    assert len(drifts) == 1
    return drifts[0]


@pytest.fixture()
def hooks():
    """Réseau simulé : les webhooks « partent » sans appel réel."""
    net.set_transport(httpx.MockTransport(lambda request: httpx.Response(204)))
    yield
    net.set_transport(None)


# --- Explication ---------------------------------------------------------------------------------------


def test_every_anomaly_comes_with_reasoning_gain_and_confidence(weekend_drift):
    drift = weekend_drift
    assert drift.status == DriftStatus.OPEN  # proposée, pas décidée
    assert len(drift.reasoning) >= 5
    assert any("talon médian" in step for step in drift.reasoning)
    assert any("Groupe froid" in step for step in drift.reasoning)  # le graphe physique est cité
    assert validation.CONFIDENCE_FLOOR <= drift.confidence <= validation.CONFIDENCE_CEILING < 1
    assert drift.confidence_factors and all({"label", "delta"} <= set(f) for f in drift.confidence_factors)
    assert drift.gain_kwh > 0 and drift.gain_eur > 0 and drift.gain_kgco2e > 0
    assert "occurrences par an" in drift.gain_basis
    assert drift.algorithm


# --- Qui voit, qui valide -----------------------------------------------------------------------------


def test_unvalidated_outputs_are_hidden_from_plain_client_accounts(client, world, weekend_drift, manager):
    url = f"/api/organizations/{world.org_a.id}/drifts"
    assert client.get(url, headers=login(client, world.client_a)).json() == []
    assert [d["id"] for d in client.get(url, headers=login(client, manager)).json()] == [weekend_drift.id]
    dashboard = client.get(f"/api/organizations/{world.org_a.id}/dashboard", headers=login(client, world.client_a))
    assert dashboard.json()["open_drifts"] == 0


def test_energy_manager_validates_then_every_client_account_sees_it(client, world, weekend_drift, manager):
    headers = login(client, manager)
    response = client.patch(f"/api/drifts/{weekend_drift.id}", headers=headers,
                            json={"status": "QUALIFIED", "comment": "Groupe froid oublié en marche"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validated_by_role"] == "ENERGY_MANAGER" and body["reasoning"] and body["confidence_level"]
    seen = client.get(f"/api/organizations/{world.org_a.id}/drifts", headers=login(client, world.client_a)).json()
    assert [d["id"] for d in seen] == [weekend_drift.id]
    # La validation reste la seule écriture ouverte au responsable énergie.
    assert client.post(f"/api/organizations/{world.org_a.id}/sites", headers=headers,
                       json={"name": "x"}).status_code == 403


def test_energy_manager_cannot_reach_other_organizations(client, db, world, manager):
    drift_b = Drift(delivery_point_id=world.dp_b.id, kind=DriftKind.BASELOAD, day=DAY, measured_value=10,
                    reference_value=5, deviation_pct=100, unit="kW", details="test")
    db.add(drift_b)
    db.commit()
    response = client.patch(f"/api/drifts/{drift_b.id}", headers=login(client, manager), json={"status": "QUALIFIED"})
    assert response.status_code == 404


def test_plain_client_account_cannot_validate(client, world, weekend_drift):
    response = client.patch(f"/api/drifts/{weekend_drift.id}", headers=login(client, world.client_a),
                            json={"status": "QUALIFIED"})
    assert response.status_code == 403


def test_the_platform_never_validates_alone(client, db, weekend_drift):
    """L'administrateur de la plateforme voit tout mais ne valide rien."""
    admin = User(email="admin@test.fr", password_hash=hash_password(PASSWORD), role=Role.ADMIN)
    db.add(admin)
    db.commit()
    response = client.patch(f"/api/drifts/{weekend_drift.id}", headers=login(client, admin), json={"status": "QUALIFIED"})
    assert response.status_code == 403
    db.refresh(weekend_drift)
    assert weekend_drift.status == DriftStatus.OPEN


def test_rejecting_requires_a_reason(client, world, weekend_drift):
    headers = login(client, world.auditor_a)
    url = f"/api/drifts/{weekend_drift.id}"
    assert client.patch(url, headers=headers, json={"status": "IGNORED"}).status_code == 422
    response = client.patch(url, headers=headers, json={"status": "IGNORED", "comment": "Maintenance programmée"})
    assert response.status_code == 200 and response.json()["status"] == "IGNORED"


# --- Recommandations ----------------------------------------------------------------------------------


def test_validated_anomaly_leads_to_a_targeted_recommendation(db, world, weekend_drift):
    repo = TenantRepository(db, world.auditor_a)
    _, rec = validation.review_drift(db, repo, world.auditor_a, weekend_drift.id, DriftStatus.QUALIFIED, "Confirmé")
    assert rec is not None and rec.status == ReviewStatus.PROPOSED
    assert rec.kind == RecommendationKind.SCHEDULE_OFF_HOURS
    # Le groupe froid a la bonne puissance ; les serveurs, de même puissance, fonctionnent en continu.
    assert db.get(AssetNode, rec.equipment_id).name == "Groupe froid"
    text = " ".join(rec.reasoning)
    assert "Serveurs (fonctionnement continu attendu)" in text
    assert "Groupe froid → produit → Eau glacée → alimente → CTA bureaux → dessert → Bureaux → accueille → Climatisation" in text
    assert rec.gain_kwh > 0 and rec.confidence >= 0.5


def test_power_peak_targets_the_equipment_matching_the_jump(db, world):
    """Le pic dépasse le seuil de peu, mais la hausse par rapport au pic habituel désigne les chargeurs."""
    ref, day = "30009000000999", date(2025, 6, 11)
    set_provider_factory(lambda provider, fluid: MockDataProvider(
        fluid=fluid, anomalies=[AnomalySpec(ref, AnomalyKind.POWER_SPIKE, between(day, day), 0.8)],
        available_until=UNTIL))
    normal_peak = max(m.avg_power_kw for m in MockDataProvider(anomalies=[], available_until=UNTIL)
                      .fetch_load_curve(ref, day, day))
    dp = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref=ref, provider=ProviderKind.MOCK,
                       power_threshold_kw=normal_peak * 1.2)
    db.add(dp)
    db.flush()
    db.add(Consent(delivery_point_id=dp.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    repo, user = TenantRepository(db, world.auditor_a), world.auditor_a
    meter = assets.ensure_meter_node(db, dp)
    db.commit()
    spike_kw = elec_profile(ref).peak_kw * 0.8
    for name, category, power in [("Chargeurs", "CHARGING", round(spike_kw)), ("Éclairage", "LIGHTING", 20),
                                  ("Compresseur", "PROCESS", round(normal_peak * 0.25))]:
        eq = assets.create_node(db, repo, user, world.site_a.id, kind=K.EQUIPMENT, category_code=category,
                                name=name, power_kw=power)
        assets.create_relation(db, repo, user, meter.id, R.SUPPLIES, eq.id)
    ingest_delivery_point(db, dp, date(2025, 5, 1), date(2025, 6, 20))
    drift = next(d for d in run_detection(db, day, delivery_point_ids=[dp.id]) if d.kind == DriftKind.THRESHOLD)

    _, rec = validation.review_drift(db, repo, user, drift.id, DriftStatus.QUALIFIED)
    assert rec.kind == RecommendationKind.PEAK_SHAVING
    assert db.get(AssetNode, rec.equipment_id).name == "Chargeurs"
    assert any(step.startswith("Pic habituel") for step in rec.reasoning)


def test_recommendation_lifecycle_stays_human(db, world, weekend_drift, manager):
    repo = TenantRepository(db, world.auditor_a)
    _, rec = validation.review_drift(db, repo, world.auditor_a, weekend_drift.id, DriftStatus.QUALIFIED)
    client_repo = TenantRepository(db, world.client_a)
    with pytest.raises(ResourceNotFound):  # proposée : invisible pour un simple compte client
        client_repo.get_recommendation(rec.id)
    with pytest.raises(validation.ValidationError):  # jamais « appliquée » sans validation
        validation.review_recommendation(db, repo, world.auditor_a, rec.id, ReviewStatus.APPLIED)
    manager_repo = TenantRepository(db, manager)
    validation.review_recommendation(db, manager_repo, manager, rec.id, ReviewStatus.VALIDATED)
    validation.review_recommendation(db, manager_repo, manager, rec.id, ReviewStatus.APPLIED, "Horloge reprogrammée")
    assert rec.status == ReviewStatus.APPLIED and rec.applied_by == manager.id
    assert client_repo.get_recommendation(rec.id).id == rec.id


def test_withdrawn_anomaly_withdraws_its_unvalidated_recommendation(db, world, weekend_drift):
    repo = TenantRepository(db, world.auditor_a)
    _, rec = validation.review_drift(db, repo, world.auditor_a, weekend_drift.id, DriftStatus.QUALIFIED)
    validation.review_drift(db, repo, world.auditor_a, weekend_drift.id, DriftStatus.IGNORED, "Erreur de compteur")
    db.refresh(rec)
    assert rec.status == ReviewStatus.SUPERSEDED


# --- Sorties vers l'extérieur -------------------------------------------------------------------------


def test_webhooks_only_carry_validated_outputs(db, world, weekend_drift, hooks):
    repo = TenantRepository(db, world.auditor_a)
    integrations.create_webhook(db, world.auditor_a, repo, name="GMAO", url="https://hooks.partenaire.example/x",
                                events=["drift.validated", "recommendation.validated"])
    assert db.query(WebhookDelivery).count() == 0  # la détection seule n'envoie rien
    validation.review_drift(db, repo, world.auditor_a, weekend_drift.id, DriftStatus.QUALIFIED)
    deliveries = db.query(WebhookDelivery).all()
    assert [d.event for d in deliveries] == ["drift.validated"]
    data = deliveries[0].payload["data"]
    assert data["validated_by_role"] == "AUDITOR" and data["reasoning"] and data["estimated_gain"]["kwh_per_year"] > 0
    assert "auditeur-a@test.fr" not in str(data)  # jamais l'identité du valideur


def test_partner_api_exposes_validated_outputs_only(client, db, world, weekend_drift):
    repo = TenantRepository(db, world.auditor_a)
    _, raw = integrations.create_api_key(db, world.auditor_a, repo, name="BI")
    url = f"/api/v1/organizations/{world.org_a.id}/drifts"
    assert client.get(url, headers={"X-API-Key": raw}).json() == []
    validation.review_drift(db, repo, world.auditor_a, weekend_drift.id, DriftStatus.QUALIFIED)
    data = client.get(url, headers={"X-API-Key": raw}).json()
    assert [d["drift_id"] for d in data] == [weekend_drift.id]
    assert data[0]["estimated_gain"]["eur_per_year"] > 0 and data[0]["confidence_level"]
    recs = client.get(f"/api/v1/organizations/{world.org_a.id}/recommendations", headers={"X-API-Key": raw}).json()
    assert recs == []  # proposée, pas encore validée


# --- Prévisions ----------------------------------------------------------------------------------------


def test_predictions_are_proposed_explained_and_validated_by_a_human(db, world, manager):
    end = yesterday_local()
    ingest_delivery_point(db, world.dp_a, end - timedelta(days=400), end)
    created = predictions.refresh_predictions(db, world.org_a.id)
    assert len(created) == 1
    prediction = created[0]
    assert prediction.status == ReviewStatus.PROPOSED
    assert prediction.reasoning and prediction.confidence_factors and prediction.algorithm
    assert prediction.low_kwh <= prediction.predicted_kwh <= prediction.high_kwh
    assert 0 < prediction.measured_kwh <= prediction.predicted_kwh
    with pytest.raises(ResourceNotFound):
        TenantRepository(db, world.client_a).get_prediction(prediction.id)

    assert predictions.refresh_predictions(db, world.org_a.id) == []  # au plus une par mois de données
    newer = predictions.refresh_predictions(db, world.org_a.id, force=True)[0]
    db.refresh(prediction)
    assert prediction.status == ReviewStatus.SUPERSEDED  # remplacée : elle n'avait pas été validée

    validation.review_prediction(db, TenantRepository(db, manager), manager, newer.id, ReviewStatus.VALIDATED)
    assert TenantRepository(db, world.client_a).get_prediction(newer.id).id == newer.id
    predictions.refresh_predictions(db, world.org_a.id, force=True)
    db.refresh(newer)
    assert newer.status == ReviewStatus.VALIDATED  # une décision humaine n'est jamais écrasée
