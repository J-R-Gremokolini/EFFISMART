"""F2a — Détection de dérives sur données mock avec anomalies injectées."""
from datetime import date, timedelta

import pytest

from app.models import (
    Consent,
    DeliveryPoint,
    Drift,
    DriftKind,
    DriftStatus,
    Fluid,
    Notification,
    ProviderKind,
    Role,
    User,
)
from app.providers.mock import AnomalyKind, AnomalySpec, MockDataProvider, between
from app.providers.registry import set_provider_factory
from app.services.drift import run_detection
from app.services.ingestion import ingest_delivery_point
from app.timeutils import utcnow
from tests.conftest import login

REF = "30009000000777"
UNTIL = date(2025, 6, 30)


def _use_mock(anomalies: list[AnomalySpec]) -> None:
    set_provider_factory(
        lambda provider, fluid: MockDataProvider(fluid=fluid, anomalies=anomalies, available_until=UNTIL)
    )


@pytest.fixture()
def dp(db, world) -> DeliveryPoint:
    point = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref=REF, provider=ProviderKind.MOCK)
    db.add(point)
    db.flush()
    db.add(Consent(delivery_point_id=point.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    return point


def _kinds(drifts: list[Drift]) -> set[DriftKind]:
    return {d.kind for d in drifts}


def test_weekend_equipment_left_on_raises_baseload_drift(db, dp):
    """Critère d'acceptation : équipement allumé un week-end → dérive « talon anormal »."""
    anomaly_day = date(2025, 6, 14)  # samedi
    _use_mock([AnomalySpec(REF, AnomalyKind.WEEKEND_ON, between(anomaly_day, anomaly_day), 0.35)])
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))

    drifts = run_detection(db, anomaly_day, delivery_point_ids=[dp.id])
    baseload = [d for d in drifts if d.kind == DriftKind.BASELOAD]
    assert len(baseload) == 1
    drift = baseload[0]
    # Une dérive porte : type, date, point de livraison, écart chiffré, statut.
    assert drift.day == anomaly_day
    assert drift.delivery_point_id == dp.id
    assert drift.deviation_pct > 40
    assert drift.status == DriftStatus.OPEN


def test_normal_weekend_raises_no_baseload_drift(db, dp):
    _use_mock([])
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    assert DriftKind.BASELOAD not in _kinds(run_detection(db, date(2025, 6, 7), delivery_point_ids=[dp.id]))


def test_power_spike_raises_threshold_drift(db, dp):
    spike_day = date(2025, 6, 11)
    _use_mock([AnomalySpec(REF, AnomalyKind.POWER_SPIKE, between(spike_day, spike_day), 0.8)])
    ingest_delivery_point(db, dp, date(2025, 6, 1), date(2025, 6, 20))
    normal_peak = max(
        m.avg_power_kw for m in MockDataProvider(anomalies=[], available_until=UNTIL).fetch_load_curve(
            REF, spike_day, spike_day
        )
    )
    dp.power_threshold_kw = normal_peak * 1.2  # seuil paramétrable par point
    db.commit()

    assert DriftKind.THRESHOLD in _kinds(run_detection(db, spike_day, delivery_point_ids=[dp.id]))
    assert DriftKind.THRESHOLD not in _kinds(run_detection(db, date(2025, 6, 12), delivery_point_ids=[dp.id]))


def test_overconsumption_raises_climate_deviation_drift(db, dp):
    start, end = date(2025, 6, 10), date(2025, 6, 13)
    _use_mock([AnomalySpec(REF, AnomalyKind.OVERCONSUMPTION, between(start, end), 0.45)])
    ingest_delivery_point(db, dp, date(2024, 5, 1), date(2025, 6, 20))  # N-1 nécessaire

    assert DriftKind.CLIMATE_DEVIATION in _kinds(run_detection(db, date(2025, 6, 11), delivery_point_ids=[dp.id]))
    assert DriftKind.CLIMATE_DEVIATION not in _kinds(
        run_detection(db, date(2025, 6, 18), delivery_point_ids=[dp.id])
    )


def test_saturday_production_is_not_compared_with_closed_sunday(db, world):
    """Atelier de démonstration : production gaz le samedi, fermé le dimanche. Le détecteur compare le samedi
    aux samedis (classe déduite des données), et non à un « week-end » moyen : pas de fausse alerte."""
    _use_mock([])
    atelier = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.GAS, external_ref="21000000000007",
                            provider=ProviderKind.MOCK)
    db.add(atelier)
    db.flush()
    db.add(Consent(delivery_point_id=atelier.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    ingest_delivery_point(db, atelier, date(2024, 4, 1), date(2025, 6, 20))
    saturday = date(2025, 6, 14)
    drifts = run_detection(db, saturday, delivery_point_ids=[atelier.id])
    assert DriftKind.CLIMATE_DEVIATION not in _kinds(drifts)


def test_detection_is_idempotent_and_notifies_validators_only(db, dp, world):
    """Principe P1 : une anomalie non validée n'est signalée qu'à ceux qui peuvent la valider."""
    manager = User(email="energie-a@test.fr", password_hash="x", role=Role.CLIENT_VIEWER,
                   organization_id=world.org_a.id, is_energy_manager=True)
    db.add(manager)
    db.commit()
    day = date(2025, 6, 14)
    _use_mock([AnomalySpec(REF, AnomalyKind.WEEKEND_ON, between(day, day), 0.35)])
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    first = run_detection(db, day, delivery_point_ids=[dp.id])
    second = run_detection(db, day, delivery_point_ids=[dp.id])
    assert first and not second
    recipients = {n.user_id for n in db.query(Notification).all()}
    assert world.auditor_a.id in recipients
    assert manager.id in recipients
    assert world.client_a.id not in recipients  # simple compte client : rien avant validation
    assert world.auditor_b.id not in recipients


def test_auditor_qualifies_drift(client, db, dp, world):
    day = date(2025, 6, 14)
    _use_mock([AnomalySpec(REF, AnomalyKind.WEEKEND_ON, between(day, day), 0.35)])
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    drift = run_detection(db, day, delivery_point_ids=[dp.id])[0]

    headers = login(client, world.auditor_a)
    response = client.patch(
        f"/api/drifts/{drift.id}", headers=headers, json={"status": "QUALIFIED", "comment": "Groupe froid"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "QUALIFIED"


def test_no_detection_without_consent(db, dp):
    day = date(2025, 6, 14)
    _use_mock([AnomalySpec(REF, AnomalyKind.WEEKEND_ON, between(day, day), 0.35)])
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    for consent in db.query(Consent).filter_by(delivery_point_id=dp.id):
        consent.revoked_at = utcnow()
    db.commit()
    assert run_detection(db, day, delivery_point_ids=[dp.id]) == []
