"""Intégrations d'API : sources réelles, connecteur générique, API partenaires, webhooks, sécurité.

Le réseau est remplacé par des API simulées (httpx.MockTransport) : aucun appel sortant réel.
"""
import json
from datetime import date, timedelta

import httpx
import pytest

from app.models import (
    AuditorClientLink,
    Consent,
    ConnectorAuth,
    DeliveryPoint,
    DeliveryStatus,
    DocumentKind,
    Fluid,
    IntegrationKind,
    MeasurementStep,
    Organization,
    ProviderKind,
    Role,
    User,
    ValueUnit,
    WebhookDelivery,
)
from app.providers.base import ProviderNotConfiguredError
from app.providers.enedis import EnedisDataConnectProvider
from app.providers.grdf import GrdfAdictProvider
from app.providers.registry import get_energy_provider
from app.providers.weather import OpenMeteoWeatherProvider
from app.repositories import ResourceNotFound, TenantRepository
from app.security import hash_password
from app.services import documents, integrations, net
from app.services import secrets as secret_store
from app.services.ingestion import ingest_delivery_point
from app.timeutils import ensure_utc, utcnow, yesterday_local
from tests.conftest import PASSWORD


@pytest.fixture()
def fake_api():
    """Installe une API simulée ; `routes` associe (méthode, chemin) → fonction(request) → Response."""
    calls: list[httpx.Request] = []
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for (method, prefix), responder in routes.items():
            if request.method == method and str(request.url).startswith(prefix):
                return responder(request)
        return httpx.Response(404)

    net.set_transport(httpx.MockTransport(handler))
    yield routes, calls
    net.set_transport(None)


@pytest.fixture()
def admin(db):
    user = User(email="admin@test.fr", password_hash=hash_password(PASSWORD), role=Role.ADMIN)
    db.add(user)
    db.commit()
    return user


# --- Sécurité -----------------------------------------------------------------------------


def test_secrets_are_encrypted():
    token = secret_store.encrypt({"client_secret": "super-secret"})
    assert "super-secret" not in token
    assert secret_store.decrypt(token) == {"client_secret": "super-secret"}


@pytest.mark.parametrize("url", [
    "http://api.example.com/data",  # HTTP non chiffré
    "https://127.0.0.1/data",  # boucle locale
    "https://10.0.0.5/data",  # réseau privé
    "https://169.254.169.254/latest/meta-data",  # métadonnées cloud
    "https://user:pass@93.184.215.14/data",  # identifiants dans l'URL
])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(net.UnsafeUrlError):
        net.check_public_url(url)


def test_public_ip_url_is_accepted():
    assert net.check_public_url("https://93.184.215.14/data") == "https://93.184.215.14/data"


def test_only_admin_configures_platform_sources(db, world, admin):
    with pytest.raises(integrations.IntegrationPermissionError):
        integrations.save_platform(db, world.auditor_a, IntegrationKind.ENEDIS_DATACONNECT, enabled=False,
                                   settings_values={}, secrets_values={})
    with pytest.raises(integrations.IntegrationError, match="identifiants"):
        integrations.save_platform(db, admin, IntegrationKind.ENEDIS_DATACONNECT, enabled=True,
                                   settings_values={"environment": "sandbox"}, secrets_values={})
    row = integrations.save_platform(db, admin, IntegrationKind.ENEDIS_DATACONNECT, enabled=True,
                                     settings_values={"environment": "sandbox"},
                                     secrets_values={"client_id": "cid", "client_secret": "csecret"})
    assert "csecret" not in (row.secrets_encrypted or "")
    assert integrations.secret_status(row) == {"client_id": True, "client_secret": True}
    # Champ secret laissé vide : la valeur enregistrée est conservée.
    integrations.save_platform(db, admin, IntegrationKind.ENEDIS_DATACONNECT, enabled=True,
                               settings_values={"environment": "production"}, secrets_values={"client_secret": ""})
    assert secret_store.decrypt(row.secrets_encrypted)["client_secret"] == "csecret"


def test_real_provider_refused_until_integration_enabled(db, world):
    dp = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref="30009000000555",
                       provider=ProviderKind.ENEDIS_DATACONNECT)
    with pytest.raises(ProviderNotConfiguredError, match="n'est pas activée"):
        get_energy_provider(dp.provider, dp.fluid, dp)


# --- Sources de données -------------------------------------------------------------------


def test_enedis_load_curve_chunks_and_converts(fake_api):
    routes, calls = fake_api
    routes[("POST", "https://gw.ext.prod-sandbox.api.enedis.fr/oauth2/v3/token")] = \
        lambda r: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    def curve(request):
        assert request.headers["Authorization"] == "Bearer tok"
        start = request.url.params["start"]
        # Puissance moyenne en W, horodatée à la FIN de l'intervalle (heure locale).
        return httpx.Response(200, json={"meter_reading": {"interval_reading": [
            {"value": "2000", "date": f"{start} 00:30:00", "interval_length": "PT30M"},
            {"value": "4000", "date": f"{start} 01:00:00", "interval_length": "PT30M"},
        ]}})

    routes[("GET", "https://gw.ext.prod-sandbox.api.enedis.fr/metering_data_clc")] = curve
    provider = EnedisDataConnectProvider("cid", "secret", "sandbox")
    start = date(2025, 1, 1)
    points = provider.fetch_load_curve("30001000000001", start, start + timedelta(days=9))
    curve_calls = [c for c in calls if "metering_data_clc" in str(c.url)]
    assert len(curve_calls) == 2  # 10 jours → 2 fenêtres de 7 jours maximum
    assert len([c for c in calls if "token" in str(c.url)]) == 1  # jeton réutilisé
    first = points[0]
    assert first.step == MeasurementStep.PT30M
    assert first.value_kwh == pytest.approx(1.0)  # 2 000 W pendant 30 min = 1 kWh
    assert first.avg_power_kw == pytest.approx(2.0)
    assert first.time.hour == 23 and first.time.minute == 0  # 00:00 heure de Paris (hiver) = 23:00 UTC


def test_grdf_parses_daily_ndjson(fake_api):
    routes, _ = fake_api
    routes[("POST", "https://sofit-sso-oidc.grdf.fr")] = \
        lambda r: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    lines = "\n".join(json.dumps({"pce": "21000000000001", "consommation": {
        "date_debut_consommation": f"2025-01-0{d}", "date_fin_consommation": f"2025-01-0{d + 1}",
        "energie": 100.0 + d}}) for d in (1, 2, 3))
    routes[("GET", "https://api.grdf.fr/adict/v2/pce/21000000000001/donnees_consos_informatives")] = \
        lambda r: httpx.Response(200, text=lines + '\n{"pce": "21000000000001", "consommation": {"energie": null}}')
    points = GrdfAdictProvider("cid", "secret").fetch_load_curve("21000000000001", date(2025, 1, 1),
                                                                 date(2025, 1, 3))
    assert [p.value_kwh for p in points] == [101.0, 102.0, 103.0]
    assert all(p.step == MeasurementStep.P1D for p in points)


def test_open_meteo_computes_dju_and_caches(fake_api):
    routes, calls = fake_api
    OpenMeteoWeatherProvider._cache.clear()

    def archive(request):
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(3)]
        return httpx.Response(200, json={"daily": {"time": [d.isoformat() for d in days],
                                                   "temperature_2m_mean": [5.0, 18.5, None]}})

    routes[("GET", OpenMeteoWeatherProvider.ARCHIVE_URL)] = archive
    weather = OpenMeteoWeatherProvider(48.85, 2.35)
    dju = weather.daily_dju(date(2025, 1, 1), date(2025, 1, 3))
    assert dju == {date(2025, 1, 1): 13.0, date(2025, 1, 2): 0.0}  # jour sans température : absent
    weather.daily_dju(date(2025, 1, 1), date(2025, 1, 2))
    assert len(calls) == 1  # second appel servi par le cache


# --- Connecteur générique -----------------------------------------------------------------


def _connector(db, world, **overrides):
    values = dict(name="Capteurs usine", url_template="https://api.capteurs.example/v1/{ref}?from={start}&to={end}",
                  auth_type=ConnectorAuth.API_KEY_HEADER, auth_header="X-API-Key", secret="cle-capteurs",
                  records_path="data.readings", time_field="ts", value_field="power",
                  value_unit=ValueUnit.W, step=MeasurementStep.PT30M)
    values.update(overrides)
    return integrations.save_connector(db, world.auditor_a, **values)


def test_generic_connector_feeds_a_delivery_point(db, world, fake_api):
    routes, calls = fake_api
    day = yesterday_local() - timedelta(days=1)

    def readings(request):
        assert request.headers["X-API-Key"] == "cle-capteurs"
        return httpx.Response(200, json={"data": {"readings": [
            {"ts": f"{day.isoformat()}T10:00:00", "power": 3000},
            {"ts": f"{day.isoformat()}T10:30:00", "power": 1000},
        ]}})

    routes[("GET", "https://api.capteurs.example/v1/")] = readings
    connector = _connector(db, world)
    assert "cle-capteurs" not in (connector.secret_encrypted or "")

    from app.services import onboarding

    dp = onboarding.create_delivery_point(db, world.site_a, fluid=Fluid.ELEC, external_ref="capteur-07",
                                          provider=ProviderKind.GENERIC_API, connector=connector)
    db.add(Consent(delivery_point_id=dp.id, granted_at=utcnow() - timedelta(minutes=1), scope="t", proof_ref="t"))
    db.commit()
    assert ingest_delivery_point(db, dp, day, day) == 2
    assert "capteur-07" in str(calls[-1].url) and day.isoformat() in str(calls[-1].url)
    preview = integrations.test_connector(db, world.auditor_a, connector.id, "capteur-07", day, day)
    assert [round(p.value_kwh, 2) for p in preview] == [1.5, 0.5]  # 3 kW et 1 kW pendant 30 min


def test_generic_connector_validation(db, world, fake_api):
    with pytest.raises(integrations.IntegrationError):
        _connector(db, world, url_template="http://api.capteurs.example/{ref}")
    with pytest.raises(integrations.IntegrationError, match="nom d'hôte"):
        _connector(db, world, url_template="https://{ref}.example.com/data")
    with pytest.raises(integrations.IntegrationError, match="clé, le jeton"):
        _connector(db, world, secret="", auth_type=ConnectorAuth.BEARER)


def test_connectors_are_private_to_each_auditor(db, world, fake_api):
    connector = _connector(db, world)
    with pytest.raises(ResourceNotFound):
        integrations.get_connector(db, world.auditor_b, connector.id)
    with pytest.raises(integrations.IntegrationPermissionError):
        integrations.list_connectors(db, world.client_a)


# --- API partenaires ----------------------------------------------------------------------


def test_partner_api_key_scope(client, db, world):
    extra = Organization(name="Autre client A")
    db.add(extra)
    db.flush()
    db.add(AuditorClientLink(auditor_id=world.auditor_a.auditor_id, organization_id=extra.id,
                             start_date=date(2024, 1, 1), active=True))
    db.commit()
    repo = TenantRepository(db, world.auditor_a)
    key, raw = integrations.create_api_key(db, world.auditor_a, repo, name="ERP")
    assert raw.startswith("esk_") and raw not in key.key_hash
    restricted, raw_restricted = integrations.create_api_key(db, world.auditor_a, repo, name="Client A seul",
                                                             organization_id=world.org_a.id)

    assert client.get("/api/v1/organizations").status_code == 401
    names = {o["name"] for o in client.get("/api/v1/organizations", headers={"X-API-Key": raw}).json()}
    assert names == {world.org_a.name, "Autre client A"}
    assert client.get(f"/api/v1/organizations/{world.org_b.id}/drifts", headers={"X-API-Key": raw}).status_code == 404

    only = client.get("/api/v1/organizations", headers={"Authorization": f"Bearer {raw_restricted}"}).json()
    assert [o["id"] for o in only] == [world.org_a.id]
    assert client.get(f"/api/v1/organizations/{extra.id}/sites",
                      headers={"X-API-Key": raw_restricted}).status_code == 404

    consumption = client.get(f"/api/v1/organizations/{world.org_a.id}/consumption?granularity=month",
                             headers={"X-API-Key": raw}).json()
    assert consumption["unit"] == "kWh" and consumption["data"][0]["elec_kwh"] > 0

    integrations.revoke_api_key(db, world.auditor_a, key.id)
    assert client.get("/api/v1/organizations", headers={"X-API-Key": raw}).status_code == 401


def test_client_cannot_create_api_keys(db, world):
    with pytest.raises(integrations.IntegrationPermissionError):
        integrations.create_api_key(db, world.client_a, TenantRepository(db, world.client_a), name="x")


# --- Webhooks -----------------------------------------------------------------------------


def test_webhook_signed_delivery_on_document_deposit(db, world, fake_api):
    routes, calls = fake_api
    received = []

    def receiver(request):
        received.append(request)
        return httpx.Response(204)

    routes[("POST", "https://hooks.partenaire.example/effismart")] = receiver
    repo_a = TenantRepository(db, world.auditor_a)
    webhook, secret = integrations.create_webhook(db, world.auditor_a, repo_a, name="GMAO",
                                                  url="https://hooks.partenaire.example/effismart",
                                                  events=["document.deposited"])
    # Un webhook d'un autre cabinet ne reçoit rien.
    integrations.create_webhook(db, world.auditor_b, TenantRepository(db, world.auditor_b), name="Autre",
                                url="https://hooks.partenaire.example/autre", events=["document.deposited"])

    documents.deposit(db, TenantRepository(db, world.client_a), world.client_a, world.org_a.id,
                      filename="facture.pdf", content=b"%PDF-1.7\n%%EOF", kind=DocumentKind.INVOICE_ELEC)
    assert integrations.dispatch_pending(db) == (1, 0)
    request = received[0]
    body = request.content
    expected = integrations.signature(secret, request.headers["X-EffiSmart-Timestamp"], body)
    assert request.headers["X-EffiSmart-Signature"] == f"sha256={expected}"
    payload = json.loads(body)
    assert payload["event"] == "document.deposited" and payload["data"]["name"] == "facture.pdf"
    assert "%PDF" not in body.decode()  # le contenu du fichier n'est jamais envoyé


def test_webhook_failure_is_retried_then_abandoned(db, world, fake_api, monkeypatch):
    routes, _ = fake_api
    routes[("POST", "https://hooks.partenaire.example/panne")] = lambda r: httpx.Response(500)
    webhook, _ = integrations.create_webhook(db, world.auditor_a, TenantRepository(db, world.auditor_a),
                                             name="Panne", url="https://hooks.partenaire.example/panne",
                                             events=["drift.created"])
    integrations.enqueue_event(db, "drift.created", world.org_a.id, {"drift_id": 1})
    db.commit()
    assert integrations.dispatch_pending(db) == (0, 0)
    delivery = db.query(WebhookDelivery).one()
    assert delivery.status == DeliveryStatus.PENDING and delivery.attempts == 1
    assert ensure_utc(delivery.next_attempt_at) > utcnow()  # réessai différé
    monkeypatch.setattr("app.services.integrations.settings.webhook_max_attempts", 2)
    delivery.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert integrations.dispatch_pending(db) == (0, 1)
    assert delivery.status == DeliveryStatus.FAILED and delivery.last_status_code == 500


def test_webhook_rejects_internal_urls(db, world):
    with pytest.raises(integrations.IntegrationError):
        integrations.create_webhook(db, world.auditor_a, TenantRepository(db, world.auditor_a), name="x",
                                    url="https://192.168.1.10/hook", events=["drift.created"])
