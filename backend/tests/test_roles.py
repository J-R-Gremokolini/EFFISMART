"""Garde-fous de rôle (F5) : l'espace client est strictement en lecture seule, vérifié côté API."""
import re

from app.main import app
from tests.conftest import login

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_WRITE_ROUTES = {"/api/auth/login"}


def test_client_reads_own_dashboard(client, world):
    headers = login(client, world.client_a)
    response = client.get(f"/api/organizations/{world.org_a.id}/dashboard", headers=headers)
    assert response.status_code == 200
    assert response.json()["totals"]["elec_kwh"] > 0


def test_client_sees_neither_portfolio_nor_other_clients(client, world):
    headers = login(client, world.client_a)
    assert client.get("/api/portfolio", headers=headers).status_code == 403
    assert client.get(f"/api/organizations/{world.org_b.id}", headers=headers).status_code == 404
    orgs = client.get("/api/organizations", headers=headers).json()
    assert [o["id"] for o in orgs] == [world.org_a.id]


def test_every_write_route_is_forbidden_to_client(client, world):
    """Parcourt TOUTES les routes d'écriture : une nouvelle route non protégée fera échouer ce test."""
    headers = login(client, world.client_a)
    checked = 0
    # Le schéma OpenAPI (API publique) liste toutes les routes, préfixes inclus.
    for path, operations in app.openapi()["paths"].items():
        if path in PUBLIC_WRITE_ROUTES:
            continue
        for method in {m.upper() for m in operations} & WRITE_METHODS:
            # Identifiants pris dans le périmètre du client : seul le rôle peut justifier le refus.
            url = re.sub(r"\{[^}]+\}", str(world.org_a.id), path)
            response = client.request(method, url, headers=headers, json={})
            assert response.status_code == 403, f"{method} {path} → {response.status_code}"
            checked += 1
    assert checked >= 10


def test_client_cannot_qualify_drift(client, world, db):
    from app.models import Drift, DriftKind

    drift = Drift(delivery_point_id=world.dp_a.id, kind=DriftKind.BASELOAD, day=world.dp_a.created_at.date(),
                  measured_value=10, reference_value=5, deviation_pct=100, unit="kW", details="test")
    db.add(drift)
    db.commit()
    headers = login(client, world.client_a)
    response = client.patch(f"/api/drifts/{drift.id}", headers=headers, json={"status": "IGNORED"})
    assert response.status_code == 403
    db.refresh(drift)
    assert drift.status.value == "OPEN"


def test_regulatory_is_auditor_only(client, world):
    headers = login(client, world.client_a)
    assert client.get("/api/regulatory/deadlines", headers=headers).status_code == 403


def test_unauthenticated_requests_are_rejected(client, world):
    assert client.get("/api/organizations").status_code == 401
