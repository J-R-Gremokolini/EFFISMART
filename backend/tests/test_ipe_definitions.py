"""F7 — IPE personnalisés (créés par un humain) et proposés par l'IA (validés par un humain, principe P1)."""
from datetime import timedelta

import pytest

from app.models import IpeKind, ReviewStatus, Role, User
from app.repositories import TenantRepository
from app.services import ipe, ipe_definitions as idf, validation
from app.services.ingestion import ingest_delivery_point
from app.services.integrations import weather_provider
from app.timeutils import add_months, yesterday_local


@pytest.fixture()
def history(db, world):
    """Un peu plus de deux ans de données : 24 mois complets pour l'IA, une année de référence entière."""
    end = yesterday_local()
    ingest_delivery_point(db, world.dp_a, end - timedelta(days=760), end)
    return end


@pytest.fixture()
def manager(db, world) -> User:
    user = User(email="energie-a@test.fr", password_hash="x", role=Role.CLIENT_VIEWER,
                organization_id=world.org_a.id, is_energy_manager=True)
    db.add(user)
    db.commit()
    return user


def _months(n: int = 24):
    last = idf.last_complete_month()
    return [add_months(last, -k) for k in range(n - 1, -1, -1)]


def test_a_user_creates_a_variable_and_a_ratio_ipe(db, world, history, manager):
    repo = TenantRepository(db, manager)
    meals = idf.create_variable(db, repo, manager, world.site_a.id, "Repas servis", "repas")
    months = _months(12)
    for month in months:
        idf.set_variable_value(db, repo, manager, meals.id, month, 1000)
    with pytest.raises(ipe.IpePermissionError):  # un simple compte client consulte seulement
        idf.create_variable(db, TenantRepository(db, world.client_a), world.client_a, world.site_a.id, "X", "x")
    definition = idf.create_definition(db, repo, manager, world.site_a.id, energy="ELEC", kind=IpeKind.RATIO,
                                       drivers=[f"VAR:{meals.id}"], weather=weather_provider(db))
    assert definition.status == ReviewStatus.VALIDATED and definition.origin == "USER"
    assert definition.name == "kWh électricité par repas"
    value = idf.evaluate(db, definition, weather_provider(db))
    energy = idf.monthly_energy(db, world.site_a, "ELEC", months)
    assert value.current == pytest.approx(sum(energy.values()) / (1000 * len(energy)))
    with pytest.raises(idf.IpeDefinitionError, match="un seul facteur"):
        idf.create_definition(db, repo, manager, world.site_a.id, energy="ELEC", kind=IpeKind.RATIO,
                              drivers=["DJU", "WORKDAYS"], weather=weather_provider(db))
    with pytest.raises(idf.IpeDefinitionError, match="aucun point de livraison en gaz"):
        idf.create_definition(db, repo, manager, world.site_a.id, energy="GAS", kind=IpeKind.RATIO,
                              drivers=["DJU"], weather=weather_provider(db))


def test_a_modelled_ipe_learns_its_reference_on_the_baseline_year(db, world, history):
    repo = TenantRepository(db, world.auditor_a)
    world.site_a.energy_baseline_year = idf.last_complete_month().year - 1
    definition = idf.create_definition(db, repo, world.auditor_a, world.site_a.id, energy="ELEC",
                                       kind=IpeKind.MODEL, drivers=["DJU"], weather=weather_provider(db))
    assert definition.model["months"] >= 10 and "kWh/mois =" in idf.formula(db, world.site_a, definition)
    value = idf.evaluate(db, definition, weather_provider(db))
    assert value.baseline == pytest.approx(100, abs=1)  # base 100 sur l'année de référence
    assert value.unit.startswith("indice")


def test_the_ai_proposes_the_factor_that_explains_consumption(db, world, history, manager):
    """Une variable qui suit la consommation : l'IA la trouve et propose un ratio, à valider par un humain."""
    repo = TenantRepository(db, world.auditor_a)
    batches = idf.create_variable(db, repo, world.auditor_a, world.site_a.id, "Fournées", "fournées")
    months = _months(24)
    energy = idf.monthly_energy(db, world.site_a, "ELEC", months)
    for k, (month, kwh) in enumerate(sorted(energy.items())):
        idf.set_variable_value(db, repo, world.auditor_a, batches.id, month, kwh / 400 * (1 + 0.02 * (-1) ** k))
    created = idf.propose_all(db, world.org_a.id)
    proposal = next(d for d in created if d.drivers == [f"VAR:{batches.id}"])
    assert proposal.kind == IpeKind.RATIO and proposal.origin == "PLATFORM"
    assert proposal.status == ReviewStatus.PROPOSED and proposal.name == "kWh électricité par fournées"
    assert proposal.model["r2"] > 0.9 and any("Facteurs testés sans lien" in s or "R²" in s for s in proposal.reasoning)
    # Principe P1 : invisible pour un simple compte client avant validation ; puis suivi.
    assert TenantRepository(db, world.client_a).list_ipe_definitions(world.org_a.id) == []
    assert any(i.kind == "ipe" for i in validation.pending_outputs(TenantRepository(db, manager), world.org_a.id))
    validation.review_ipe(db, TenantRepository(db, manager), manager, proposal.id, ReviewStatus.VALIDATED)
    assert [d.id for d in TenantRepository(db, world.client_a).list_ipe_definitions(world.org_a.id)] == [proposal.id]
    assert idf.propose_all(db, world.org_a.id) == []  # déjà suivi : pas de nouvelle proposition


def test_a_rejected_proposal_is_not_proposed_again(db, world, history, manager):
    repo = TenantRepository(db, world.auditor_a)
    batches = idf.create_variable(db, repo, world.auditor_a, world.site_a.id, "Fournées", "fournées")
    for month, kwh in idf.monthly_energy(db, world.site_a, "ELEC", _months(24)).items():
        idf.set_variable_value(db, repo, world.auditor_a, batches.id, month, kwh / 400)
    proposal = next(d for d in idf.propose_all(db, world.org_a.id) if d.drivers == [f"VAR:{batches.id}"])
    validation.review_ipe(db, TenantRepository(db, manager), manager, proposal.id, ReviewStatus.REJECTED,
                          "Les fournées ne sont pas suivies de façon fiable.")
    assert all(d.drivers != [f"VAR:{batches.id}"] for d in idf.propose_all(db, world.org_a.id))


def test_a_factor_explaining_too_little_is_not_proposed(db, world, history):
    """Une variable parfaitement corrélée mais qui n'explique qu'une petite part de la consommation (le talon fait
    presque tout) : l'IPE resterait plat, l'IA ne le propose pas."""
    repo = TenantRepository(db, world.auditor_a)
    badges = idf.create_variable(db, repo, world.auditor_a, world.site_a.id, "Badges", "badges")
    energy = idf.monthly_energy(db, world.site_a, "ELEC", _months(24))
    floor = 0.95 * min(energy.values())
    for month, kwh in energy.items():
        idf.set_variable_value(db, repo, world.auditor_a, badges.id, month, kwh - floor)
    fitted = idf.fit(energy, [idf.driver_series(db, world.site_a, f"VAR:{badges.id}", _months(24), None)],
                     [f"VAR:{badges.id}"])
    assert fitted.r2 > 0.99 and 1 - fitted.intercept_share < idf.MIN_VARIABLE_SHARE
    assert all(d.drivers != [f"VAR:{badges.id}"] for d in idf.propose_all(db, world.org_a.id))


def test_a_second_analysis_does_not_add_an_all_energies_ipe(db, world, history):
    """Site à deux énergies : l'IPE par énergie suffit ; une nouvelle analyse n'ajoute pas « toutes énergies »."""
    from app.models import Consent, DeliveryPoint, Fluid, ProviderKind
    from app.timeutils import utcnow

    gas = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.GAS, external_ref="21009000000222",
                        provider=ProviderKind.MOCK)
    db.add(gas)
    db.flush()
    db.add(Consent(delivery_point_id=gas.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    ingest_delivery_point(db, gas, history - timedelta(days=760), history)
    first = idf.propose_all(db, world.org_a.id)
    assert first and all(d.energy != "ALL" for d in first)
    assert all(d.energy != "ALL" for d in idf.propose_all(db, world.org_a.id))
