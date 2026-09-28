"""Graphe physique des équipements : des relations réelles et typées, pas une arborescence de rangement."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import AssetNode, AssetNodeKind, AssetRelationKind, Fluid, Site
from app.repositories import ResourceNotFound, TenantRepository
from app.services import assets, onboarding
from tests.conftest import login

K, R = AssetNodeKind, AssetRelationKind


def _tools(db, world, user=None):
    user = user or world.auditor_a
    repo = TenantRepository(db, user)

    def node(kind, category, name, site_id=None, **kw):
        return assets.create_node(db, repo, user, site_id or world.site_a.id, kind=kind, category_code=category,
                                  name=name, **kw)

    def link(source, kind, target):
        return assets.create_relation(db, repo, user, source.id, kind, target.id)

    return node, link


@pytest.fixture()
def chain(db, world):
    """Compteur → alimente → Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage."""
    node, link = _tools(db, world)
    meter = assets.ensure_meter_node(db, world.dp_a)
    db.commit()
    boiler = node(K.EQUIPMENT, "BOILER", "Chaudière", power_kw=300)
    hot = node(K.FLOW, "HOT_WATER", "Eau chaude")
    ahu = node(K.EQUIPMENT, "AHU", "CTA")
    zone = node(K.ZONE, "OFFICE", "Zone", surface_m2=500)
    usage = node(K.USAGE, "HEATING", "Usage")
    link(meter, R.SUPPLIES, boiler)
    link(boiler, R.PRODUCES, hot)
    link(hot, R.SUPPLIES, ahu)
    link(ahu, R.SERVES, zone)
    link(zone, R.HOSTS, usage)
    return SimpleNamespace(meter=meter, boiler=boiler, hot=hot, ahu=ahu, zone=zone, usage=usage)


def test_physical_chain_reads_like_the_building(db, world, chain):
    graph = assets.load_site_graph(db, world.site_a.id)
    assert [assets.chain_text(p) for p in graph.paths_from(chain.boiler.id)] == [
        "Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage"
    ]
    assert [n.name for n in graph.direct_consumers(chain.meter.id)] == ["Chaudière"]
    assert graph.warnings() == []


def test_graph_is_not_a_tree(db, world, chain):
    """Plusieurs sources, plusieurs cibles, et même une boucle physique (récupération de chaleur)."""
    node, link = _tools(db, world)
    chiller = node(K.EQUIPMENT, "CHILLER", "Groupe froid", power_kw=40)
    chilled = node(K.FLOW, "CHILLED_WATER", "Eau glacée")
    zone2 = node(K.ZONE, "OFFICE", "Zone 2")
    exhaust = node(K.FLOW, "AIR", "Air extrait")
    link(chain.meter, R.SUPPLIES, chiller)
    link(chiller, R.PRODUCES, chilled)
    link(chilled, R.SUPPLIES, chain.ahu)  # une CTA alimentée par deux fluides
    link(chain.ahu, R.SERVES, zone2)
    link(zone2, R.HOSTS, chain.usage)  # un usage partagé par deux zones
    link(chain.ahu, R.PRODUCES, exhaust)
    link(exhaust, R.SUPPLIES, chain.ahu)  # boucle : récupération sur l'air extrait

    graph = assets.load_site_graph(db, world.site_a.id)
    assert {source.name for _, source in graph.predecessors(chain.ahu.id)} == {"Eau chaude", "Eau glacée", "Air extrait"}
    assert {z.name for z in graph.downstream(chain.meter.id, K.ZONE)} == {"Zone", "Zone 2"}
    assert {c.name for c in graph.direct_consumers(chain.meter.id)} == {"Chaudière", "Groupe froid"}
    assert len(graph.paths_from(chain.meter.id)) >= 3  # le parcours se termine malgré la boucle
    assert graph.to_dot().count(" -> ") == len(graph.relations)
    # Le groupe froid atteint une zone qui accueille le chauffage, mais il ne chauffe pas : chaîne écartée.
    assert any(p[-1][1].id == chain.usage.id for p in graph.paths_from(chiller.id))
    assert all(p[-1][1].id != chain.usage.id for p in graph.physical_paths(chiller.id))


def test_chains_are_grouped_and_physically_relevant(db, world, chain):
    node, link = _tools(db, world)
    ventilation = node(K.USAGE, "VENTILATION", "Ventilation")
    dhw = node(K.USAGE, "DHW", "Eau chaude sanitaire")
    lighting_usage = node(K.USAGE, "LIGHTING", "Éclairage")
    for usage in (ventilation, dhw, lighting_usage):
        link(chain.zone, R.HOSTS, usage)
    graph = assets.load_site_graph(db, world.site_a.id)
    # Chaque équipement de la chaîne doit pouvoir servir l'usage : la chaudière ne ventile pas, la CTA ne
    # produit pas d'eau chaude sanitaire, aucun des deux n'éclaire.
    assert assets.chain_summaries(graph.physical_paths(chain.boiler.id)) == [
        "Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage"
    ]
    assert assets.chain_summaries(graph.physical_paths(chain.ahu.id)) == [
        "CTA → dessert → Zone → accueille → Usage, Ventilation"
    ]
    assert {u.name for u in graph.usages_served(chain.boiler.id)} == {"Usage"}


def test_physically_absurd_relations_are_refused(db, world, chain):
    node, link = _tools(db, world)
    for source, kind, target in [
        (chain.zone, R.PRODUCES, chain.meter),  # une zone ne produit rien
        (chain.usage, R.SUPPLIES, chain.boiler),  # un usage n'alimente rien
        (chain.boiler, R.SERVES, chain.usage),  # un équipement dessert une zone, pas un usage
        (chain.boiler, R.PRODUCES, chain.boiler),  # pas de lien vers soi-même
        (chain.meter, R.SUPPLIES, chain.boiler),  # doublon
    ]:
        with pytest.raises(assets.AssetGraphError):
            link(source, kind, target)
    other_site = Site(organization_id=world.org_a.id, name="Autre bâtiment")
    db.add(other_site)
    db.commit()
    far_zone = node(K.ZONE, "OFFICE", "Zone éloignée", site_id=other_site.id)
    with pytest.raises(assets.AssetGraphError):
        link(chain.ahu, R.SERVES, far_zone)
    with pytest.raises(assets.AssetGraphError):
        node(K.METER, "ELEC", "Compteur fantôme")  # un compteur naît d'un point de livraison


def test_graph_isolation_and_roles(db, world, chain):
    with pytest.raises(ResourceNotFound):
        assets.site_graph(db, TenantRepository(db, world.auditor_b), world.site_a.id)
    with pytest.raises(ResourceNotFound):
        assets.delete_node(db, TenantRepository(db, world.auditor_b), world.auditor_b, chain.boiler.id)
    node_as_client, _ = _tools(db, world, world.client_a)
    with pytest.raises(assets.AssetPermissionError):
        node_as_client(K.ZONE, "OFFICE", "Zone du client")
    # L'espace client consulte le graphe de son organisation.
    assert assets.site_graph(db, TenantRepository(db, world.client_a), world.site_a.id).nodes


def test_missing_links_are_reported(db, world):
    node, _ = _tools(db, world)
    assets.ensure_meter_node(db, world.dp_a)
    node(K.FLOW, "HOT_WATER", "Eau orpheline")
    warnings = assets.load_site_graph(db, world.site_a.id).warnings()
    assert any("Eau orpheline" in w and "produit par aucun équipement" in w for w in warnings)
    assert any("n'alimente aucun équipement" in w for w in warnings)


def test_deleting_an_element_removes_its_relations(db, world, chain):
    assets.delete_node(db, TenantRepository(db, world.auditor_a), world.auditor_a, chain.hot.id)
    graph = assets.load_site_graph(db, world.site_a.id)
    assert chain.hot.id not in graph.nodes and graph.successors(chain.boiler.id) == []
    with pytest.raises(assets.AssetGraphError):
        assets.delete_node(db, TenantRepository(db, world.auditor_a), world.auditor_a, chain.meter.id)


def test_new_delivery_point_enters_the_graph(db, world):
    dp = onboarding.create_delivery_point(db, world.site_a, fluid=Fluid.GAS, external_ref="21000900000001")
    meter = db.scalar(select(AssetNode).where(AssetNode.delivery_point_id == dp.id))
    assert meter.kind == K.METER and meter.category == "GAS" and meter.site_id == world.site_a.id


def test_assets_api_returns_readable_chains(client, world, chain):
    response = client.get(f"/api/sites/{world.site_a.id}/assets", headers=login(client, world.client_a))
    assert response.status_code == 200
    assert response.json()["chains"] == [
        f"{chain.meter.name} → alimente → Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone "
        "→ accueille → Usage"
    ]
    assert client.get(f"/api/sites/{world.site_b.id}/assets", headers=login(client, world.client_a)).status_code == 404
