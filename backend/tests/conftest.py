"""Fixtures de test : base SQLite en mémoire, deux auditeurs isolés (A et B)."""
import os
import tempfile

# Configuration AVANT l'import de l'application.
os.environ["EFFISMART_DATABASE_URL"] = "sqlite://"
os.environ["EFFISMART_ENABLE_SCHEDULER"] = "false"
os.environ["EFFISMART_BACKFILL_DAYS"] = "10"
os.environ["EFFISMART_DETECTION_BACKFILL_DAYS"] = "2"
os.environ["EFFISMART_EXPORT_DIR"] = tempfile.mkdtemp(prefix="effismart-exports-")
os.environ["EFFISMART_DOCUMENT_DIR"] = tempfile.mkdtemp(prefix="effismart-documents-")
from cryptography.fernet import Fernet  # noqa: E402

os.environ["EFFISMART_SECRET_KEY"] = Fernet.generate_key().decode()

from dataclasses import dataclass  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Auditor,
    AuditorClientLink,
    Base,
    Consent,
    DeliveryPoint,
    EmissionFactor,
    Fluid,
    Organization,
    ProviderKind,
    Role,
    Site,
    User,
)
from app.providers.registry import set_provider_factory  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.ingestion import ingest_delivery_point  # noqa: E402
from app.timeutils import utcnow, yesterday_local  # noqa: E402

PASSWORD = "secret-123"


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        set_provider_factory(None)
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db):
    return TestClient(app)


@dataclass
class World:
    auditor_a: User
    auditor_b: User
    client_a: User
    org_a: Organization
    org_b: Organization
    site_a: Site
    site_b: Site
    dp_a: DeliveryPoint
    dp_b: DeliveryPoint


def _tenant(db, label: str, ref: str):
    auditor = Auditor(name=f"Cabinet {label}", email=f"cabinet-{label}@test.fr")
    org = Organization(name=f"Client {label}")
    db.add_all([auditor, org])
    db.flush()
    db.add(AuditorClientLink(auditor_id=auditor.id, organization_id=org.id, start_date=date(2024, 1, 1), active=True))
    site = Site(organization_id=org.id, name=f"Site {label}", surface_m2=1000, is_tertiary_decret=True)
    db.add(site)
    db.flush()
    dp = DeliveryPoint(site_id=site.id, fluid=Fluid.ELEC, external_ref=ref, provider=ProviderKind.MOCK,
                       is_primary=True)
    db.add(dp)
    db.flush()
    db.add(Consent(delivery_point_id=dp.id, granted_at=utcnow() - timedelta(days=1), scope="test",
                   proof_ref="test"))
    user = User(email=f"auditeur-{label}@test.fr", password_hash=hash_password(PASSWORD), role=Role.AUDITOR,
                auditor_id=auditor.id)
    db.add(user)
    return user, org, site, dp


@pytest.fixture()
def world(db) -> World:
    auditor_a, org_a, site_a, dp_a = _tenant(db, "a", "30009000000001")
    auditor_b, org_b, site_b, dp_b = _tenant(db, "b", "30009000000002")
    client_a = User(email="client-a@test.fr", password_hash=hash_password(PASSWORD), role=Role.CLIENT_VIEWER,
                    organization_id=org_a.id)
    db.add(client_a)
    db.add(EmissionFactor(fluid=Fluid.ELEC, factor_kgco2_per_kwh=0.052, valid_from=date(2023, 1, 1),
                          source="ADEME test", version="test-v1"))
    db.add(EmissionFactor(fluid=Fluid.GAS, factor_kgco2_per_kwh=0.227, valid_from=date(2023, 1, 1),
                          source="ADEME test", version="test-v1"))
    db.commit()
    end = yesterday_local()
    for dp in (dp_a, dp_b):
        ingest_delivery_point(db, dp, end - timedelta(days=40), end)
    return World(auditor_a, auditor_b, client_a, org_a, org_b, site_a, site_b, dp_a, dp_b)


def login(client: TestClient, user: User) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
