"""F2 — Une cause, une alerte : les anomalies de même cause se regroupent sous la plus ancienne."""
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
    OutgoingEmail,
    ProviderKind,
)
from app.providers.mock import AnomalyKind, AnomalySpec, MockDataProvider, between
from app.providers.registry import set_provider_factory
from app.repositories import TenantRepository
from app.services import alert_groups, validation
from app.services.drift import run_detection
from app.services.ingestion import ingest_delivery_point
from app.timeutils import utcnow
from tests.conftest import login

REF = "30009000000444"
MONDAY, FRIDAY = date(2025, 6, 9), date(2025, 6, 13)
UNTIL = date(2025, 6, 30)


@pytest.fixture()
def dp(db, world) -> DeliveryPoint:
    point = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref=REF, provider=ProviderKind.MOCK)
    db.add(point)
    db.flush()
    db.add(Consent(delivery_point_id=point.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    return point


@pytest.fixture()
def night_group(db, dp):
    """Éclairage resté allumé cinq nuits de suite : cinq anomalies, une seule alerte."""
    set_provider_factory(lambda provider, fluid: MockDataProvider(
        fluid=fluid, anomalies=[AnomalySpec(REF, AnomalyKind.NIGHT_ON, between(MONDAY, FRIDAY), 0.2)],
        available_until=UNTIL))
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    day = MONDAY
    while day <= FRIDAY:
        run_detection(db, day, delivery_point_ids=[dp.id])
        day += timedelta(days=1)
    drifts = db.query(Drift).filter_by(delivery_point_id=dp.id).order_by(Drift.day).all()
    assert [d.kind for d in drifts] == [DriftKind.BASELOAD] * 5
    return drifts[0], drifts[1:]


def _manual(db, dp, kind, day, measured, reference, unit) -> Drift:
    drift = Drift(delivery_point_id=dp.id, kind=kind, day=day, measured_value=measured, reference_value=reference,
                  deviation_pct=(measured - reference) / reference * 100, unit=unit, details="test")
    db.add(drift)
    db.flush()
    return drift


def test_repeated_alerts_are_grouped_under_the_oldest(db, world, night_group):
    lead, others = night_group
    assert lead.grouped_with_id is None and lead.day == MONDAY  # la moins récente est signalée
    assert all(d.grouped_with_id == lead.id and d.grouping_rule == alert_groups.RULE_REPEAT for d in others)
    assert "le problème se répète" in others[0].grouping_reason
    # Une seule alerte et un seul e-mail par valideur, l'alerte expliquant le regroupement.
    alerts = db.query(Notification).filter_by(user_id=world.auditor_a.id).all()
    assert len(alerts) == 1 and alerts[0].drift_id == lead.id
    assert "5 alertes regroupées en une seule, du 09/06 au 13/06/2025" in alerts[0].message
    assert "Pourquoi ce regroupement : le problème se répète (même compteur, même type d'anomalie)" in alerts[0].message
    assert db.query(OutgoingEmail).filter_by(user_id=world.auditor_a.id).count() == 1
    # Une seule entrée dans la file de validation.
    pending = validation.pending_outputs(TenantRepository(db, world.auditor_a), world.org_a.id)
    assert [i.output.id for i in pending if i.kind == "drift"] == [lead.id]
    assert validation.count_pending(db, world.org_a.id) == 1


def test_one_decision_covers_the_whole_group(db, world, night_group):
    lead, others = night_group
    repo = TenantRepository(db, world.auditor_a)
    with pytest.raises(validation.ValidationError, match="regroupée avec l'alerte du 09/06/2025"):
        validation.review_drift(db, repo, world.auditor_a, others[0].id, DriftStatus.QUALIFIED)
    _, rec = validation.review_drift(db, repo, world.auditor_a, lead.id, DriftStatus.QUALIFIED, "Éclairage")
    assert all(d.status == DriftStatus.QUALIFIED and d.qualified_by == world.auditor_a.id for d in others)
    assert set(rec.supporting_drift_ids) == {d.id for d in others}
    assert validation.drift_payload(db, lead)["group_drift_ids"] == [d.id for d in others]


def test_a_decided_alert_takes_no_new_occurrence(db, world, night_group, dp):
    """Le groupe est décidé : l'occurrence suivante ouvre une nouvelle alerte, à décider à son tour."""
    lead, _ = night_group
    validation.review_drift(db, TenantRepository(db, world.auditor_a), world.auditor_a, lead.id, DriftStatus.QUALIFIED)
    later = _manual(db, dp, DriftKind.BASELOAD, FRIDAY + timedelta(days=1), 60, 30, "kW")
    assert alert_groups.attach(db, later) is None


def test_a_validator_can_detach_an_anomaly_from_its_group(client, db, world, night_group):
    lead, others = night_group
    response = client.post(f"/api/drifts/{others[-1].id}/detach", headers=login(client, world.auditor_a))
    assert response.status_code == 200 and response.json()["grouped_with_id"] is None
    db.expire_all()
    detached = db.get(Drift, others[-1].id)
    assert detached.grouping_rule == alert_groups.RULE_DETACHED and detached.status == DriftStatus.OPEN
    assert "4 alertes regroupées" in db.query(Notification).filter_by(drift_id=lead.id).first().message
    # À la mise à niveau suivante, la décision humaine est respectée : pas de nouveau rattachement.
    assert alert_groups.regroup_existing(db) == 0 and db.get(Drift, detached.id).grouped_with_id is None
    # Un simple compte client ne modifie pas un regroupement.
    response = client.post(f"/api/drifts/{others[0].id}/detach", headers=login(client, world.client_a))
    assert response.status_code == 403


def test_daily_excess_explained_by_an_off_hours_anomaly_joins_it(db, dp):
    """Samedi : l'équipement en marche explique l'écart de la journée ; une seule alerte, la plus précise."""
    saturday = date(2025, 6, 14)
    off_hours = _manual(db, dp, DriftKind.OFF_HOURS, saturday, 80, 30, "kW")  # 50 kW × 16 h = 800 kWh
    climate = _manual(db, dp, DriftKind.CLIMATE_DEVIATION, saturday, 2400, 1700, "kWh")  # 700 kWh
    assert alert_groups.attach(db, climate) == off_hours
    assert climate.grouping_rule == alert_groups.RULE_EXPLAINED
    assert "s'explique à 100 %" in climate.grouping_reason


def test_unrelated_causes_on_the_same_meter_stay_separate(db, dp):
    """Bureaux : éclairage la nuit (160 kWh) et pompe à chaleur déréglée le jour (800 kWh) : deux causes."""
    monday = date(2025, 6, 9)
    night = _manual(db, dp, DriftKind.BASELOAD, monday, 50, 30, "kW")
    climate = _manual(db, dp, DriftKind.CLIMATE_DEVIATION, monday, 2400, 1600, "kWh")
    assert alert_groups.excess_kwh(night) == 160
    assert alert_groups.attach(db, climate) is None


def test_same_day_the_first_detector_leads(db, dp):
    """Anomalies enregistrées avant le regroupement, l'écart climatique en premier : à la mise à niveau, le samedi
    est signalé par l'anomalie « inoccupation », dont le détecteur passe en premier ce jour-là."""
    saturday = date(2025, 6, 14)
    climate = _manual(db, dp, DriftKind.CLIMATE_DEVIATION, saturday, 2400, 1700, "kWh")
    off_hours = _manual(db, dp, DriftKind.OFF_HOURS, saturday, 80, 30, "kW")
    db.commit()
    assert alert_groups.regroup_existing(db) == 1
    assert climate.grouped_with_id == off_hours.id and off_hours.grouped_with_id is None
