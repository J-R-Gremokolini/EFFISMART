"""Équipements cliquables : consommation d'un équipement ou d'un compteur, contenu d'une zone, disposition du graphe."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models import AssetNodeKind, AssetRelationKind, Consent, DeliveryPoint, Fluid, ProviderKind
from app.repositories import TenantRepository
from app.services import assets, equipment_view, site_plan
from app.services.energy_data import combined_daily
from app.services.ingestion import ingest_delivery_point
from app.timeutils import utcnow, yesterday_local

K, R = AssetNodeKind, AssetRelationKind


@pytest.fixture()
def site(db, world):
    """Compteur → chargeurs (30 kW) et éclairage (20 kW) → entrepôt ; chaudière sur le même compteur → eau chaude
    → CTA, qui dessert l'entrepôt et les bureaux."""
    repo, user = TenantRepository(db, world.auditor_a), world.auditor_a
    meter = assets.ensure_meter_node(db, world.dp_a)
    db.commit()

    def node(kind, category, name, **kw):
        return assets.create_node(db, repo, user, world.site_a.id, kind=kind, category_code=category, name=name, **kw)

    def link(source, kind, target):
        assets.create_relation(db, repo, user, source.id, kind, target.id)

    chargers = node(K.EQUIPMENT, "CHARGING", "Chargeurs", power_kw=30)
    lighting = node(K.EQUIPMENT, "LIGHTING", "Éclairage", power_kw=20)
    hot = node(K.FLOW, "HOT_WATER", "Eau chaude")
    ahu = node(K.EQUIPMENT, "AHU", "CTA", power_kw=15)
    store = node(K.ZONE, "STORAGE", "Entrepôt")
    offices = node(K.ZONE, "OFFICE", "Bureaux")
    light_use = node(K.USAGE, "LIGHTING", "Éclairage")
    for source, kind, target in [(meter, R.SUPPLIES, chargers), (meter, R.SUPPLIES, lighting),
                                 (chargers, R.SERVES, store), (lighting, R.SERVES, store),
                                 (hot, R.SUPPLIES, ahu), (ahu, R.SERVES, store), (ahu, R.SERVES, offices),
                                 (store, R.HOSTS, light_use)]:
        link(source, kind, target)
    return SimpleNamespace(repo=repo, user=user, node=node, link=link, meter=meter, chargers=chargers,
                           lighting=lighting, hot=hot, ahu=ahu, store=store, offices=offices, light_use=light_use)


def _graph(db, world):
    return assets.load_site_graph(db, world.site_a.id)


def _consumption(db, world, node):
    return equipment_view.node_consumption(db, world.site_a, _graph(db, world), node.id)


def _measured(db, dp, result):
    return sum(kwh for kwh, _ in combined_daily(db, dp, result.start, result.end).values())


def test_shared_meter_is_split_by_power_times_typical_hours(db, world, site):
    chargers, lighting = _consumption(db, world, site.chargers), _consumption(db, world, site.lighting)
    assert chargers.method == lighting.method == "ESTIMATED"
    # 30 kW × 2 000 h contre 20 kW × 2 500 h : 60 000 / 110 000.
    assert chargers.sources[0].share == pytest.approx(60 / 110)
    assert lighting.sources[0].share == pytest.approx(50 / 110)
    assert chargers.kwh + lighting.kwh == pytest.approx(_measured(db, world.dp_a, chargers))
    assert chargers.sources[0].co_consumers == ["Éclairage"]
    assert "55 %" in chargers.explanation[0]
    assert set(chargers.by_fluid) == {Fluid.ELEC}
    assert sum(chargers.monthly().values()) == pytest.approx(chargers.kwh)
    assert chargers.eur > 0
    assert chargers.kgco2e == pytest.approx(chargers.kwh * 0.052)


def test_sole_consumer_is_measured_and_the_meter_shows_its_whole_reading(db, world, site):
    assets.delete_relation(db, site.repo, site.user, _relation(db, world, site.meter, site.lighting))
    chargers = _consumption(db, world, site.chargers)
    assert chargers.method == "MEASURED"
    assert chargers.sources[0].share == 1.0
    assert chargers.kwh == pytest.approx(_measured(db, world.dp_a, chargers))
    assert chargers.explanation[0].startswith("Seul équipement de son compteur")

    meter = _consumption(db, world, site.meter)
    assert meter.method == "MEASURED"
    assert meter.kwh == pytest.approx(chargers.kwh)
    assert meter.sources[0].co_consumers == ["Chargeurs"]


def _relation(db, world, source, target):
    return next(r.id for r in _graph(db, world).relations if r.source_id == source.id and r.target_id == target.id)


def test_equipment_without_power_is_not_estimated_and_the_others_are_flagged(db, world, site):
    pump = site.node(K.EQUIPMENT, "PUMP_FAN", "Pompe")
    site.link(site.meter, R.SUPPLIES, pump)
    result = _consumption(db, world, pump)
    assert result.method == "UNKNOWN"
    assert result.kwh == 0
    assert "sans puissance connue pour celui-ci" in result.explanation[0]
    chargers = _consumption(db, world, site.chargers)
    assert chargers.method == "ESTIMATED"
    assert "Puissance inconnue pour Pompe" in chargers.explanation[0]


def test_equipment_fed_by_a_flow_points_to_its_producer(db, world, site):
    boiler = site.node(K.EQUIPMENT, "BOILER", "Chaudière", power_kw=100)
    site.link(boiler, R.PRODUCES, site.hot)
    result = _consumption(db, world, site.ahu)
    assert result.method == "NO_METER"
    assert result.kwh == 0
    assert "« Eau chaude », produit par Chaudière" in result.explanation[0]
    # Ni compteur ni fluide : rien à attribuer, et l'interface le dit.
    lonely = site.node(K.EQUIPMENT, "OTHER", "Isolé", power_kw=1)
    assert _consumption(db, world, lonely).explanation == ["Aucun compteur ni fluide n'alimente cet équipement dans "
                                                           "le graphe."]


def test_sub_meter_is_deducted_from_its_parent(db, world, site):
    sub_dp = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref="30009000000003",
                           provider=ProviderKind.MOCK, is_primary=False)
    db.add(sub_dp)
    db.flush()
    db.add(Consent(delivery_point_id=sub_dp.id, granted_at=utcnow() - timedelta(days=1), scope="test",
                   proof_ref="test"))
    db.commit()
    end = yesterday_local()
    ingest_delivery_point(db, sub_dp, end - timedelta(days=40), end)
    sub = assets.ensure_meter_node(db, sub_dp)
    db.commit()
    site.link(site.meter, R.SUPPLIES, sub)
    assets.delete_relation(db, site.repo, site.user, _relation(db, world, site.meter, site.lighting))

    result = _consumption(db, world, site.chargers)
    assert result.method == "DIFFERENCE"
    main = combined_daily(db, world.dp_a, result.start, result.end)
    child = combined_daily(db, sub_dp, result.start, result.end)
    expected = sum(max(0.0, kwh - child.get(day, (0.0, ""))[0]) for day, (kwh, _) in main.items())
    assert 0 < expected < sum(kwh for kwh, _ in main.values())
    assert result.kwh == pytest.approx(expected)
    assert result.sources[0].sub_meters == [sub.name]
    assert any("Sous-compteurs déduits" in line for line in result.explanation)


def test_meter_without_data_says_so(db, world, site):
    silent = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.GAS, external_ref="21009000000009",
                           provider=ProviderKind.MOCK, is_primary=False)
    db.add(silent)
    db.commit()
    meter = assets.ensure_meter_node(db, silent)
    db.commit()
    boiler = site.node(K.EQUIPMENT, "BOILER", "Chaudière", power_kw=100)
    site.link(meter, R.SUPPLIES, boiler)
    result = _consumption(db, world, boiler)
    assert (result.method, result.start, result.kwh) == ("UNKNOWN", None, 0)
    assert result.explanation[0].startswith("Pas de consommation connue")


def test_only_meters_and_equipment_have_a_consumption(db, world, site):
    graph = _graph(db, world)
    for node in (site.hot, site.store, site.light_use):
        assert equipment_view.node_consumption(db, world.site_a, graph, node.id) is None
    assert equipment_view.node_consumption(db, world.site_a, graph, 10**6) is None


def test_zone_lists_the_equipment_serving_it_and_what_sits_inside(db, world, site):
    site_plan.set_position(db, site.repo, site.user, site.store.id, 10, 10, 40, 40)
    site_plan.set_position(db, site.repo, site.user, site.meter.id, 20, 20)  # dans l'entrepôt, sans le desservir
    site_plan.set_position(db, site.repo, site.user, site.chargers.id, 80, 80)  # dessert, placé ailleurs
    content = equipment_view.zone_content(_graph(db, world), site.store.id)
    assert [n.name for n in content.serving] == ["Chargeurs", "CTA", "Éclairage"]
    assert [n.name for n in content.placed] == [site.meter.name]
    assert [n.name for n in content.usages] == ["Éclairage"]
    assert content.shared == {site.ahu.id: ["Bureaux"]}  # la CTA dessert aussi les bureaux
    assert equipment_view.zone_content(_graph(db, world), site.chargers.id) is None


def test_layout_reads_left_to_right_in_the_direction_of_energy(db, world, site):
    graph = _graph(db, world)
    result = equipment_view.layout(graph)
    rank = {i: r for i, (r, _) in result.positions.items()}
    assert set(result.positions) == set(graph.nodes)
    assert rank[site.meter.id] < rank[site.chargers.id] < rank[site.store.id] < rank[site.light_use.id]
    assert rank[site.hot.id] < rank[site.ahu.id] < rank[site.store.id]
    assert result.ranks == len(set(rank.values()))
    for r in set(rank.values()):  # pas deux éléments au même endroit
        heights = [y for i, (rr, y) in result.positions.items() if rr == r]
        assert len(heights) == len(set(heights)) <= result.widest


def test_layout_terminates_on_a_physical_loop(db, world, site):
    exhaust = site.node(K.FLOW, "AIR", "Air extrait")
    site.link(site.ahu, R.PRODUCES, exhaust)
    site.link(exhaust, R.SUPPLIES, site.ahu)  # récupération de chaleur sur l'air extrait
    result = equipment_view.layout(_graph(db, world))
    assert len(result.positions) == len(_graph(db, world).nodes)
    assert max(r for r, _ in result.positions.values()) < 2 * len(result.positions)
