"""F2b — Anomalies contextualisées : qualification et propagation d'impact par le graphe physique (P2)."""
from datetime import date, timedelta

import pytest

from app.models import (
    AssetNode,
    AssetNodeKind,
    AssetRelation,
    AssetRelationKind,
    Consent,
    DeliveryPoint,
    Drift,
    DriftKind,
    DriftStatus,
    Fluid,
    Notification,
    ProviderKind,
)
from app.providers.mock import AnomalyKind, AnomalySpec, MockDataProvider, between, elec_profile
from app.providers.registry import set_provider_factory
from app.repositories import TenantRepository
from app.services import anomaly_context, assets, validation
from app.services.drift import run_detection
from app.services.ingestion import ingest_delivery_point
from app.timeutils import utcnow

K, R = AssetNodeKind, AssetRelationKind
ELEC_REF, GAS_REF = "30009000000555", "21009000000555"
SATURDAY, WINTER_DAY = date(2025, 6, 14), date(2025, 1, 15)
UNTIL = date(2025, 6, 30)


def _use_mock(anomalies: list[AnomalySpec]) -> None:
    set_provider_factory(lambda provider, fluid: MockDataProvider(fluid=fluid, anomalies=anomalies,
                                                                  available_until=UNTIL))


def _point(db, world, ref: str, fluid: Fluid, **kw) -> DeliveryPoint:
    dp = DeliveryPoint(site_id=world.site_a.id, fluid=fluid, external_ref=ref, provider=ProviderKind.MOCK, **kw)
    db.add(dp)
    db.flush()
    db.add(Consent(delivery_point_id=dp.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    return dp


class Builder:
    """Saisie du graphe par l'auditeur, comme dans la page « Équipements »."""

    def __init__(self, db, world):
        self.db, self.repo, self.user, self.site_id = db, TenantRepository(db, world.auditor_a), world.auditor_a, \
            world.site_a.id

    def node(self, kind, category, name, **kw) -> AssetNode:
        return assets.create_node(self.db, self.repo, self.user, self.site_id, kind=kind, category_code=category,
                                  name=name, **kw)

    def link(self, *triples) -> None:
        for source, kind, target in triples:
            assets.create_relation(self.db, self.repo, self.user, source.id, kind, target.id)


@pytest.fixture()
def boiler_site(db, world):
    """L'exemple de l'étude de marché : chaudière → eau chaude → CTA de l'atelier 2 et radiateurs des bureaux R+1."""
    _use_mock([AnomalySpec(GAS_REF, AnomalyKind.OVERCONSUMPTION, between(WINTER_DAY, WINTER_DAY), 0.5)])
    normal = MockDataProvider(fluid=Fluid.GAS, anomalies=[], available_until=UNTIL).fetch_load_curve(
        GAS_REF, WINTER_DAY, WINTER_DAY)[0].value_kwh
    gas = _point(db, world, GAS_REF, Fluid.GAS, daily_threshold_kwh=round(normal * 1.2))
    b = Builder(db, world)
    meter = assets.ensure_meter_node(db, gas)
    db.commit()
    boiler = b.node(K.EQUIPMENT, "BOILER", "Chaudière", power_kw=300)
    water = b.node(K.FLOW, "HOT_WATER", "Eau chaude")
    ahu = b.node(K.EQUIPMENT, "AHU", "CTA atelier")
    radiators = b.node(K.EQUIPMENT, "TERMINAL", "Radiateurs")
    workshop = b.node(K.ZONE, "PRODUCTION", "Atelier 2", surface_m2=900)
    offices = b.node(K.ZONE, "OFFICE", "Bureaux R+1", surface_m2=400)
    heating = b.node(K.USAGE, "HEATING", "Chauffage")
    b.link((meter, R.SUPPLIES, boiler), (boiler, R.PRODUCES, water), (water, R.SUPPLIES, ahu),
           (water, R.SUPPLIES, radiators), (ahu, R.SERVES, workshop), (radiators, R.SERVES, offices),
           (workshop, R.HOSTS, heating), (offices, R.HOSTS, heating))
    ingest_delivery_point(db, gas, WINTER_DAY - timedelta(days=10), WINTER_DAY + timedelta(days=2))
    drift = next(d for d in run_detection(db, WINTER_DAY, delivery_point_ids=[gas.id]) if d.kind == DriftKind.THRESHOLD)
    return drift, b, workshop


def test_boiler_drift_names_the_zones_it_may_impact(db, boiler_site):
    """« Dérive détectée sur la chaudière, zones potentiellement impactées : atelier 2, bureaux R+1 »."""
    drift, _, _ = boiler_site
    context = drift.context
    assert context["nature"] == "HEAT_REGULATION" and context["localisation"] == "precise"
    assert context["summary"] == ("Dérive détectée sur « Chaudière » ; zones potentiellement impactées : "
                                  "Atelier 2, Bureaux R+1.")
    assert {"CTA atelier", "Radiateurs"} <= set(context["impacted"]["equipment"])
    assert context["impacted"]["usages"] == ["Chauffage"]
    assert any("Chaudière → produit → Eau chaude → alimente → CTA atelier" in c for c in context["chains"])
    # Le contexte figure dans le raisonnement, avant le statut, et dans l'alerte envoyée aux valideurs.
    assert any(step.startswith(anomaly_context.CONTEXT_STEP_PREFIX) for step in drift.reasoning)
    assert drift.reasoning[-1].startswith("Statut :")
    alert = db.query(Notification).filter_by(drift_id=drift.id).first()
    assert "Équipement suspect : Chaudière ; zones potentiellement impactées : Atelier 2, Bureaux R+1" in alert.message


def test_off_hours_drift_is_located_on_the_equipment_the_recommendation_targets(db, world):
    """Groupe froid en marche un samedi : localisé par sa puissance, les serveurs (continus) sont écartés ;
    la recommandation vise le même équipement."""
    _use_mock([AnomalySpec(ELEC_REF, AnomalyKind.WEEKEND_ON, between(SATURDAY, SATURDAY), 0.35)])
    dp = _point(db, world, ELEC_REF, Fluid.ELEC)
    b = Builder(db, world)
    meter = assets.ensure_meter_node(db, dp)
    db.commit()
    excess = elec_profile(ELEC_REF).peak_kw * 0.35
    chiller = b.node(K.EQUIPMENT, "CHILLER", "Groupe froid", power_kw=round(excess))
    servers = b.node(K.EQUIPMENT, "IT", "Serveurs", power_kw=round(excess))
    lighting = b.node(K.EQUIPMENT, "LIGHTING", "Éclairage", power_kw=round(excess / 4))
    chilled = b.node(K.FLOW, "CHILLED_WATER", "Eau glacée")
    ahu = b.node(K.EQUIPMENT, "AHU", "CTA")
    offices = b.node(K.ZONE, "OFFICE", "Plateau Est")
    b.link((meter, R.SUPPLIES, chiller), (meter, R.SUPPLIES, servers), (meter, R.SUPPLIES, lighting),
           (chiller, R.PRODUCES, chilled), (chilled, R.SUPPLIES, ahu), (ahu, R.SERVES, offices),
           (lighting, R.SERVES, offices))
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    drift = next(d for d in run_detection(db, SATURDAY, delivery_point_ids=[dp.id]) if d.kind == DriftKind.OFF_HOURS)

    context = drift.context
    assert context["nature"] == "OFF_HOURS"
    assert context["suspects"][0]["name"] == "Groupe froid" and context["suspects"][0]["retained"]
    assert "Serveurs (fonctionnement continu attendu)" in context["excluded"]
    assert [z["name"] for z in context["impacted"]["zones"]] == ["Plateau Est"]
    assert {chiller.id, chilled.id, ahu.id, offices.id, meter.id} <= set(context["highlight_ids"])

    repo = TenantRepository(db, world.auditor_a)
    _, rec = validation.review_drift(db, repo, world.auditor_a, drift.id, DriftStatus.QUALIFIED)
    assert rec.equipment_id == context["suspects"][0]["id"]
    # Une fois validé, le contexte part avec l'anomalie (webhooks, API partenaires).
    assert validation.drift_payload(db, drift)["context"]["summary"].startswith("Dérive détectée sur « Groupe froid »")


def test_without_graph_the_anomaly_is_reported_but_not_located(db, world):
    _use_mock([AnomalySpec(ELEC_REF, AnomalyKind.NIGHT_ON, between(SATURDAY, SATURDAY), 0.2)])
    dp = _point(db, world, ELEC_REF, Fluid.ELEC)
    ingest_delivery_point(db, dp, date(2025, 5, 10), date(2025, 6, 20))
    drift = run_detection(db, SATURDAY, delivery_point_ids=[dp.id])[0]
    assert drift.kind == DriftKind.BASELOAD  # F2a fonctionne sans graphe
    assert drift.context["localisation"] == "absente" and drift.context["summary"].startswith("Dérive non localisée")
    assert anomaly_context.short_text(drift.context) == ""
    alert = db.query(Notification).filter_by(drift_id=drift.id).first()
    assert "suspect" not in alert.message


def test_context_follows_the_graph_until_the_anomaly_is_validated(db, world, boiler_site):
    drift, b, workshop = boiler_site
    meter = db.query(AssetNode).filter_by(delivery_point_id=drift.delivery_point_id).one()
    # Un second générateur apparaît : l'anomalie encore à valider suit le graphe.
    backup = b.node(K.EQUIPMENT, "BOILER", "Chaudière d'appoint", power_kw=80)
    b.link((meter, R.SUPPLIES, backup))
    db.refresh(drift)
    assert drift.context["localisation"] == "probable"  # deux générateurs : le plus puissant est retenu
    assert [s["name"] for s in drift.context["suspects"]] == ["Chaudière", "Chaudière d'appoint"]

    repo = TenantRepository(db, world.auditor_a)
    validation.review_drift(db, repo, world.auditor_a, drift.id, DriftStatus.QUALIFIED)
    frozen = dict(drift.context)
    relation = db.query(AssetRelation).filter_by(target_id=workshop.id).one()
    assets.delete_relation(db, repo, world.auditor_a, relation.id)
    db.refresh(drift)
    assert drift.context == frozen  # validé tel quel : figé


def test_related_anomalies_share_the_impacted_zones(db, world, boiler_site):
    """Un aérotherme électrique de secours chauffe l'atelier 2 le même jour : même zone, cause probablement commune."""
    drift, b, workshop = boiler_site
    elec = _point(db, world, ELEC_REF, Fluid.ELEC)
    meter = assets.ensure_meter_node(db, elec)
    db.commit()
    heater = b.node(K.EQUIPMENT, "TERMINAL", "Aérotherme de secours", power_kw=20)
    b.link((meter, R.SUPPLIES, heater), (heater, R.SERVES, workshop))
    other = Drift(delivery_point_id=elec.id, kind=DriftKind.OFF_HOURS, day=WINTER_DAY, measured_value=40,
                  reference_value=20, deviation_pct=100, unit="kW", details="test")
    db.add(other)
    db.flush()
    anomaly_context.contextualize(db, other)
    db.commit()
    assert anomaly_context.related(db, drift) == [other]
    assert anomaly_context.related(db, drift, statuses=(DriftStatus.QUALIFIED,)) == []
