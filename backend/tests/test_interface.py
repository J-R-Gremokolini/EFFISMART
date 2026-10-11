"""Interface claire ou sombre : photo du site en tête du tableau de bord, consommation par site, préférence de mode."""
from datetime import timedelta

import pytest

from app.models import Consent, DeliveryPoint, Fluid, ProviderKind, Site
from app.repositories import ResourceNotFound, TenantRepository
from app.services import assets, dashboard, site_photo, site_plan
from app.services.ingestion import ingest_delivery_point
from app.timeutils import utcnow, yesterday_local

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big") + (400).to_bytes(4, "big") + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40


def test_auditor_sets_replaces_and_removes_the_site_photo(db, world):
    repo = TenantRepository(db, world.auditor_a)
    assert site_photo.photo_data_uri(site_photo.get_photo(db, world.site_a.id)) is None
    first = site_photo.save_photo(db, repo, world.auditor_a, world.site_a.id, "C:/photos/façade.png", PNG)
    assert first.file_name == "façade.png" and first.content_type == "image/png"
    assert site_photo.photo_data_uri(first).startswith("data:image/png;base64,")
    old_file = site_photo.photo_dir() / first.stored_name

    second = site_photo.save_photo(db, repo, world.auditor_a, world.site_a.id, "hall.jpg", JPEG)
    assert second.id == first.id and second.content_type == "image/jpeg"
    assert not old_file.exists()  # l'ancienne photo ne reste pas sur le disque

    site_photo.remove_photo(db, repo, world.auditor_a, world.site_a.id)
    assert site_photo.get_photo(db, world.site_a.id) is None
    assert not (site_photo.photo_dir() / second.stored_name).exists()


def test_site_photo_is_checked_on_its_content(db, world):
    repo = TenantRepository(db, world.auditor_a)
    with pytest.raises(site_plan.PlanError, match="PNG ou JPEG"):
        site_photo.save_photo(db, repo, world.auditor_a, world.site_a.id, "photo.png", b"<svg onload='x'/>")
    with pytest.raises(site_plan.PlanError, match="trop lourde"):
        site_photo.save_photo(db, repo, world.auditor_a, world.site_a.id, "photo.png",
                              PNG + b"\x00" * (site_photo.MAX_PHOTO_MB * 1024 * 1024))
    assert site_photo.get_photo(db, world.site_a.id) is None


def test_client_reads_but_never_sets_the_photo_nor_reaches_another_tenant(db, world):
    with pytest.raises(assets.AssetPermissionError):
        site_photo.save_photo(db, TenantRepository(db, world.client_a), world.client_a, world.site_a.id, "p.png", PNG)
    with pytest.raises(ResourceNotFound):
        site_photo.save_photo(db, TenantRepository(db, world.auditor_b), world.auditor_b, world.site_a.id, "p.png",
                              PNG)


def test_sites_add_up_to_the_dashboard_totals(db, world):
    annex = Site(organization_id=world.org_a.id, name="Annexe", surface_m2=300)
    db.add(annex)
    db.flush()
    gas = DeliveryPoint(site_id=annex.id, fluid=Fluid.GAS, external_ref="21009000000011", provider=ProviderKind.MOCK)
    db.add(gas)
    db.flush()
    db.add(Consent(delivery_point_id=gas.id, granted_at=utcnow() - timedelta(days=1), scope="test", proof_ref="test"))
    db.commit()
    end = yesterday_local()
    ingest_delivery_point(db, gas, end - timedelta(days=40), end)
    start = end - timedelta(days=29)

    rows = dashboard.site_breakdown(db, world.org_a, start, end)
    totals = dashboard.organization_dashboard(db, world.org_a, "custom", start, end)["totals"]
    assert [r["name"] for r in rows] == [world.site_a.name, "Annexe"]
    assert rows[1]["elec_kwh"] == 0 and rows[1]["gas_kwh"] > 0 and rows[1]["points"] == 1
    for key in ("elec_kwh", "gas_kwh", "total_kwh", "cost_eur", "emissions_kgco2e"):
        assert sum(r[key] for r in rows) == pytest.approx(totals[key], abs=0.2)


def test_theme_preference_is_kept_on_the_account(db, world):
    assert world.client_a.ui_theme is None  # mode par défaut tant que l'utilisateur n'a rien choisi
    world.client_a.ui_theme = "sombre"
    db.commit()
    db.expire_all()
    assert db.get(type(world.client_a), world.client_a.id).ui_theme == "sombre"
