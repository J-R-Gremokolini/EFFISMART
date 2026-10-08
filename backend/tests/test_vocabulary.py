"""Vocabulaire produit : la donnée la plus fraîche est celle de la veille (données J+1, voir `app/vocabulary.py`).

Garde-fou : aucun texte de l'application (services, interface, front) ne promet un suivi plus rapide que J+1,
ni un pilotage des équipements par la plateforme (décision D3).
"""
import re
from datetime import date, timedelta
from pathlib import Path

from app.models import Consent, DeliveryPoint, Fluid, IntegrationKind, Measurement, ProviderKind, Role, User
from app.providers.mock import MockDataProvider
from app.providers.registry import set_provider_factory
from app.services import integrations
from app.services.ingestion import ingest_recent
from app.timeutils import local_day_bounds, utcnow

BACKEND = Path(__file__).resolve().parents[1]
SOURCES = [*BACKEND.glob("app/**/*.py"), BACKEND / "effismart_ui.py", BACKEND / "ui_theme.py",
           *(BACKEND.parent / "frontend" / "src").glob("**/*.ts*")]
BANNED = {
    r"temps[\s-]+r[ée]el": "« suivi quotidien » ou « données J+1 »",
    r"\breal[\s-]?time\b": "« données J+1 »",
    r"instantan[ée]": "« analyse de la courbe de charge au pas 30 min »",
    r"pilotage automatique": "« recommandation de décalage de charge » (décision D3)",
}


def test_no_text_promises_more_than_next_day_data():
    found = []
    for path in SOURCES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern, instead in BANNED.items():
                if re.search(pattern, line, re.IGNORECASE):
                    found.append(f"{path.relative_to(BACKEND.parent)}:{number} : employer {instead}")
    assert not found, "\n".join(found)


def test_daily_run_rereads_gas_published_at_day_plus_two(db, world):
    """Gaz publié à J+1 ou J+2 : le traitement quotidien relit la veille et l'avant-veille."""
    today = date(2025, 6, 20)
    set_provider_factory(lambda provider, fluid: MockDataProvider(fluid=fluid, anomalies=[],
                                                                  available_until=today - timedelta(days=1)))
    gas = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.GAS, external_ref="21009000000111",
                        provider=ProviderKind.MOCK)
    db.add(gas)
    db.flush()
    db.add(Consent(delivery_point_id=gas.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    ingest_recent(db, today)
    assert db.query(Measurement).filter_by(delivery_point_id=gas.id).count() == 2  # avant-veille et veille
    t0, t1 = local_day_bounds(today - timedelta(days=1), today - timedelta(days=1))
    elec = db.query(Measurement).filter(Measurement.delivery_point_id == world.dp_a.id, Measurement.time >= t0,
                                        Measurement.time < t1).count()
    assert elec == 48  # électricité : la veille, au pas de 30 min


def test_sge_referencing_is_tracked_not_activated(db, world):
    """Décision D10 : le référencement SGE-Tiers se suit dès la semaine 1, sans collecte à activer."""
    admin = User(email="admin@test.fr", password_hash="x", role=Role.ADMIN)
    db.add(admin)
    db.commit()
    row = integrations.save_platform(db, admin, IntegrationKind.ENEDIS_SGE, enabled=True,
                                     settings_values={"referencing": "dossier déposé", "filed_on": "05/10/2026",
                                                      "notes": ""}, secrets_values={})
    assert not row.enabled and integrations.platform_settings(row)["referencing"] == "dossier déposé"
    ok, message = integrations.test_platform(db, admin, IntegrationKind.ENEDIS_SGE)
    assert not ok and "référencement" in message
