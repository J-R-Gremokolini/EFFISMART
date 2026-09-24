"""Isolation multi-tenant (brief §3.1, critère F1) : l'auditeur A ne voit rien de l'auditeur B."""
from datetime import date, timedelta

from app.models import AuditorClientLink
from app.repositories import ResourceNotFound, TenantRepository
from tests.conftest import login


def test_auditor_lists_only_own_organizations(client, world):
    headers = login(client, world.auditor_a)
    names = [o["name"] for o in client.get("/api/organizations", headers=headers).json()]
    assert names == [world.org_a.name]


def test_portfolio_contains_only_own_clients(client, world):
    headers = login(client, world.auditor_a)
    rows = client.get("/api/portfolio", headers=headers).json()
    assert [r["organization_id"] for r in rows] == [world.org_a.id]


def test_other_tenant_resources_are_not_found(client, world):
    headers = login(client, world.auditor_a)
    b = world
    end = date.today()
    forbidden_reads = [
        f"/api/organizations/{b.org_b.id}",
        f"/api/organizations/{b.org_b.id}/dashboard",
        f"/api/organizations/{b.org_b.id}/sites",
        f"/api/organizations/{b.org_b.id}/drifts",
        f"/api/organizations/{b.org_b.id}/exports",
        f"/api/delivery-points/{b.dp_b.id}/load-curve?start={end - timedelta(days=7)}&end={end}",
        f"/api/delivery-points/{b.dp_b.id}/consents",
        f"/api/sites/{b.site_b.id}/actions",
    ]
    for url in forbidden_reads:
        assert client.get(url, headers=headers).status_code == 404, url


def test_cannot_write_into_other_tenant(client, world):
    headers = login(client, world.auditor_a)
    response = client.post(
        f"/api/organizations/{world.org_b.id}/sites", headers=headers, json={"name": "Intrus"}
    )
    assert response.status_code == 404
    response = client.post(
        f"/api/sites/{world.site_b.id}/delivery-points", headers=headers,
        json={"fluid": "ELEC", "external_ref": "30009000000099"},
    )
    assert response.status_code == 404


def test_regulatory_deadlines_are_isolated(client, world, db):
    from app.services.regulatory import ensure_operat_deadline

    ensure_operat_deadline(db, world.site_a)
    ensure_operat_deadline(db, world.site_b)
    db.commit()
    headers = login(client, world.auditor_a)
    deadlines = client.get("/api/regulatory/deadlines", headers=headers).json()
    assert deadlines and {d["organization_id"] for d in deadlines} == {world.org_a.id}


def test_repository_filters_at_query_level(db, world):
    repo = TenantRepository(db, world.auditor_a)
    assert [dp.id for dp in repo.list_delivery_points()] == [world.dp_a.id]
    try:
        repo.get_delivery_point(world.dp_b.id)
    except ResourceNotFound:
        pass
    else:
        raise AssertionError("Le point de l'auditeur B ne doit pas être accessible")


def test_ended_link_revokes_access(client, world, db):
    link = db.query(AuditorClientLink).filter_by(organization_id=world.org_a.id).one()
    link.end_date = date.today() - timedelta(days=1)
    db.commit()
    headers = login(client, world.auditor_a)
    assert client.get(f"/api/organizations/{world.org_a.id}", headers=headers).status_code == 404
