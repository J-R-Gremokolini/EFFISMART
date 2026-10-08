"""Version 2 (F6 à F12) : suivi tri-axe, IPE, plan 2D, simulateur, rapport trimestriel, trajectoire, décalage de charge."""
from datetime import date, timedelta

import pytest

from app.models import (
    AdjustmentKind,
    AssetNodeKind,
    AssetRelationKind,
    RecommendationKind,
    ReportStatus,
    ReviewStatus,
    Role,
    TariffOption,
    User,
)
from app.repositories import ResourceNotFound, TenantRepository
from app.services import (
    assets,
    ipe,
    load_shift,
    quarterly_reports,
    savings,
    simulator,
    site_plan,
    tariffs,
    trajectory,
    validation,
)
from app.services.ingestion import ingest_delivery_point
from app.services.integrations import weather_provider
from app.timeutils import add_months, month_start, yesterday_local

K, R = AssetNodeKind, AssetRelationKind
HPHC = dict(option=TariffOption.HPHC, price_hp=0.25, price_hc=0.15, subscription_eur_month=30.0)


@pytest.fixture()
def history(db, world):
    """Treize mois de données sur le point du client A : de quoi apprendre un modèle de consommation (F11)."""
    end = yesterday_local()
    ingest_delivery_point(db, world.dp_a, end - timedelta(days=420), end)
    return end


@pytest.fixture()
def manager(db, world) -> User:
    user = User(email="energie-a@test.fr", password_hash="x", role=Role.CLIENT_VIEWER,
                organization_id=world.org_a.id, is_energy_manager=True)
    db.add(user)
    db.commit()
    return user


def _contract(db, world, **kw):
    params = {**HPHC, **kw}
    return tariffs.save_contract(db, TenantRepository(db, world.auditor_a), world.auditor_a, world.dp_a.id,
                                 supplier="Fournisseur test", valid_from=date(2020, 1, 1), **params)


def _graph(db, world):
    """Compteur → chargeurs (décalables) et éclairage → entrepôt ; puissance du profil simulé."""
    repo, user = TenantRepository(db, world.auditor_a), world.auditor_a
    meter = assets.ensure_meter_node(db, world.dp_a)
    db.commit()

    def node(kind, category, name, **kw):
        return assets.create_node(db, repo, user, world.site_a.id, kind=kind, category_code=category, name=name, **kw)

    chargers = node(K.EQUIPMENT, "CHARGING", "Chargeurs", power_kw=30)
    lighting = node(K.EQUIPMENT, "LIGHTING", "Éclairage", power_kw=20)
    zone = node(K.ZONE, "STORAGE", "Stockage")
    for usage_cat, usage_name in (("LIGHTING", "Éclairage"), ("PROCESS", "Recharge")):
        usage = node(K.USAGE, usage_cat, usage_name)
        assets.create_relation(db, repo, user, zone.id, R.HOSTS, usage.id)
    for source, kind, target in [(meter, R.SUPPLIES, chargers), (meter, R.SUPPLIES, lighting),
                                 (chargers, R.SERVES, zone), (lighting, R.SERVES, zone)]:
        assets.create_relation(db, repo, user, source.id, kind, target.id)
    return meter, chargers, lighting, zone


# --- F6 : suivi mensuel tri-axe -----------------------------------------------------------------------


def test_contract_prices_each_half_hour_and_prorates_the_subscription(db, world):
    end = yesterday_local()
    start = end - timedelta(days=29)
    indicative = tariffs.monthly_costing(db, world.dp_a, start, end)
    _contract(db, world)
    priced = tariffs.monthly_costing(db, world.dp_a, start, end)
    total = tariffs.Costing()
    for costing in priced.values():
        total.merge(costing)
    assert set(total.by_period) == {"HP", "HC"}  # chaque pas au prix de sa plage
    hp_kwh, hp_eur = total.by_period["HP"]
    assert hp_eur == pytest.approx(hp_kwh * 0.25)
    assert total.subscription_eur == pytest.approx(30.0 * 12 / 365 * 30)
    assert sum(c.kwh for c in indicative.values()) == pytest.approx(total.kwh)
    rows = tariffs.monthly_triaxis(db, world.org_a, start, end)
    assert rows and all(r.pricing == "contrat" and r.kgco2e > 0 for r in rows)


def test_tempo_season_has_22_red_and_43_white_days():
    season = tariffs._tempo_season(2025)
    colors = list(season.values())
    assert colors.count("ROUGE") == 22 and colors.count("BLANC") == 43
    assert all(day.weekday() < 5 for day, color in season.items() if color == "ROUGE")


def test_only_the_auditor_enters_a_contract(db, world, manager):
    with pytest.raises(tariffs.ContractPermissionError):
        tariffs.save_contract(db, TenantRepository(db, manager), manager, world.dp_a.id, supplier="X",
                              valid_from=date(2025, 1, 1), **HPHC)
    with pytest.raises(tariffs.ContractError, match="heures creuses"):
        _contract(db, world, price_hc=None)
    with pytest.raises(ResourceNotFound):  # point d'un autre client
        tariffs.save_contract(db, TenantRepository(db, world.auditor_a), world.auditor_a, world.dp_b.id,
                              supplier="X", valid_from=date(2025, 1, 1), **HPHC)


# --- F7 : IPE et passerelle ISO 50001 ------------------------------------------------------------------


def test_ipe_relates_energy_to_surface_production_and_headcount(db, world, manager):
    repo = TenantRepository(db, manager)
    last = month_start(yesterday_local())
    for k in range(2):
        month = add_months(last, -k)
        ipe.set_variable(db, repo, manager, world.site_a.id, AdjustmentKind.PRODUCTION, month, 100)
        ipe.set_variable(db, repo, manager, world.site_a.id, AdjustmentKind.HEADCOUNT, month, 20)
    report = ipe.site_report(db, world.site_a, last, weather_provider(db))
    values = report.current.values
    assert values["kwh_m2"] == pytest.approx(values["kwh"] / 1000)
    assert values["kwh_fte"] == pytest.approx(values["kwh"] / 20)
    assert any("Production renseignée pour 2 mois" in note for note in report.current.notes)
    with pytest.raises(ipe.IpePermissionError):  # un simple compte client consulte seulement
        ipe.set_variable(db, TenantRepository(db, world.client_a), world.client_a, world.site_a.id,
                         AdjustmentKind.HEADCOUNT, last, 10)


def test_iso50001_certification_exempts_from_the_energy_audit(db, world):
    certified, text = ipe.iso50001_status(world.org_a)
    assert not certified and "audit énergétique obligatoire reste dû" in text
    world.org_a.iso50001_certified_until = date(2030, 1, 1)
    assert ipe.iso50001_status(world.org_a)[0]


# --- F11 : trajectoire Décret Tertiaire -----------------------------------------------------------------


def test_trajectory_projects_2030_at_the_current_pace_then_needs_validation(db, world, history, manager):
    weather = weather_provider(db)
    current = sum(p.kwh for p in trajectory.normalized_consumption(db, world.site_a, history, weather))
    world.site_a.dt_reference_year, world.site_a.dt_reference_kwh = 2019, current / 0.8  # −20 % depuis 2019
    db.commit()
    created = trajectory.refresh_trajectories(db, world.org_a.id)
    assert len(created) == 1
    result = created[0]
    assert result.annual_rate < 0 and 0.2 < result.reduction_2030 < 0.4  # loin des −40 % exigés
    assert any("au lieu des −40 % exigés" in step for step in result.reasoning)
    assert result.gain_kwh > 0  # enjeu : économie supplémentaire nécessaire
    # Principe P1 : le client ne la voit qu'une fois validée par un humain.
    assert TenantRepository(db, world.client_a).list_trajectories(world.org_a.id) == []
    validation.review_trajectory(db, TenantRepository(db, manager), manager, result.id, ReviewStatus.VALIDATED)
    assert [t.id for t in TenantRepository(db, world.client_a).list_trajectories(world.org_a.id)] == [result.id]
    assert trajectory.refresh_trajectories(db, world.org_a.id) == []  # au plus une par mois de données


def test_first_year_has_no_trajectory(db, world):
    world.site_a.dt_reference_year, world.site_a.dt_reference_kwh = 2019, 100000
    db.commit()
    assert trajectory.refresh_trajectories(db, world.org_a.id) == []  # 40 jours d'historique : mode dégradé


# --- F9 : simulateur d'économies ------------------------------------------------------------------------


def test_simulator_combines_gestures_without_double_counting(db, world, history):
    simulator.ensure_library(db)
    _graph(db, world)
    breakdown = simulator.breakdown(db, world.site_a, weather_provider(db))
    elec = breakdown[0]
    assert elec.shares.get("HEATING", 0) > 0  # part liée au climat, d'après le modèle de consommation
    # Puissance × durée type : éclairage 20 kW × 2 500 h, chargeurs 30 kW × 2 000 h.
    assert elec.shares["LIGHTING"] == pytest.approx(elec.shares["PROCESS"] * (20 * 2500) / (30 * 2000))
    assert elec.shares["OTHER"] > 0  # le reste, non modélisé
    templates = {t.code: t for t in simulator.library(db, world.auditor_a)}
    led, detection = templates["ECL_LED"], templates["ECL_DETECTION"]
    one = simulator.simulate(world.site_a, breakdown, [led], [])
    both = simulator.simulate(world.site_a, breakdown, [led, detection], [])
    lighting = elec.usage_kwh("LIGHTING")
    assert one["saved_kwh"] == pytest.approx(lighting * 0.5, rel=0.01)
    assert both["saved_kwh"] == pytest.approx(lighting * (1 - 0.5 * 0.75), rel=0.01)  # pas 75 %
    assert both["investment_eur"] == pytest.approx((15 + 6) * 1000)
    assert both["payback_years"] == pytest.approx(both["investment_eur"] / both["saved_eur"], rel=0.05)


def test_retained_scenario_feeds_the_trajectory_with_actions(db, world, history):
    simulator.ensure_library(db)
    weather = weather_provider(db)
    breakdown = simulator.breakdown(db, world.site_a, weather)
    template = next(t for t in simulator.library(db, world.auditor_a) if t.code == "CHAUF_CONSIGNE")
    results = simulator.simulate(world.site_a, breakdown, [template], [])
    with pytest.raises(simulator.SimulatorPermissionError):
        simulator.save_scenario(db, TenantRepository(db, world.client_a), world.client_a, world.site_a.id,
                                name="Plan", template_ids=[template.id], recommendation_ids=[], results=results)
    simulator.save_scenario(db, TenantRepository(db, world.auditor_a), world.auditor_a, world.site_a.id,
                            name="Plan chauffage", template_ids=[template.id], recommendation_ids=[], results=results,
                            retained=True)
    current = sum(p.kwh for p in trajectory.normalized_consumption(db, world.site_a, history, weather))
    world.site_a.dt_reference_year, world.site_a.dt_reference_kwh = 2019, current / 0.9
    result = trajectory.compute(db, world.site_a, weather, history)
    assert result.reduction_2030_with_actions > result.reduction_2030
    assert any("Plan chauffage" in step for step in result.reasoning)


# --- F12 : décalage de charge, mode conseil ---------------------------------------------------------


def test_load_shift_is_advised_never_applied_by_the_platform(db, world, history):
    _contract(db, world)
    _, chargers, _, _ = _graph(db, world)
    created = load_shift.propose_load_shifts(db, world.org_a.id)
    rec = next(r for r in created if r.equipment_id == chargers.id)
    assert rec.kind == RecommendationKind.LOAD_SHIFT and rec.status == ReviewStatus.PROPOSED
    assert rec.gain_eur > 0 and rec.gain_kwh == 0  # gain financier, énergie inchangée
    text = " ".join([rec.title, rec.action, *rec.reasoning]).lower()
    assert "décalage de charge" in text and "pilotage" not in text and "automati" not in text
    assert "22 h" in rec.title  # heures creuses du contrat
    assert load_shift.propose_load_shifts(db, world.org_a.id) == []  # pas de doublon
    rec.status, rec.applied_at = ReviewStatus.APPLIED, rec.created_at
    assert savings.measure(db, rec) is None  # pas de mesure avant / après en kWh


def test_base_contract_has_no_load_shift(db, world, history):
    _contract(db, world, option=TariffOption.BASE, price_base=0.2, price_hp=None, price_hc=None)
    _graph(db, world)
    assert load_shift.propose_load_shifts(db, world.org_a.id) == []


# --- F8 : plan 2D ----------------------------------------------------------------------------------------


def test_plan_places_elements_and_situates_an_anomaly(db, world):
    _, chargers, _, zone = _graph(db, world)
    repo = TenantRepository(db, world.auditor_a)
    with pytest.raises(site_plan.PlanError):
        site_plan.set_position(db, repo, world.auditor_a, zone.id, 80, 10, 40, 30)  # déborde du plan
    site_plan.set_position(db, repo, world.auditor_a, zone.id, 10, 10, 60, 50)
    site_plan.set_position(db, repo, world.auditor_a, chargers.id, 30, 40)
    with pytest.raises(assets.AssetPermissionError):
        site_plan.set_position(db, TenantRepository(db, world.client_a), world.client_a, chargers.id, 1, 1)
    with pytest.raises(site_plan.PlanError, match="PNG ou JPEG"):
        site_plan.save_plan(db, repo, world.auditor_a, world.site_a.id, "plan.svg", b"<svg onload='x'/>")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big") + (400).to_bytes(4, "big") + b"\x00" * 20
    plan = site_plan.save_plan(db, repo, world.auditor_a, world.site_a.id, "plan.png", png)
    assert plan.aspect_ratio == pytest.approx(0.5)
    nodes = repo.list_asset_nodes(world.site_a.id)
    marks = site_plan.PlanMarks(suspects={chargers.id}, zones={zone.id}, notes={chargers.id: ["Suspect"]})
    svg = site_plan.render_svg(nodes, plan, marks, site_plan.image_data_uri(plan))
    assert "Zone potentiellement impactée" in svg and "animate" in svg and "data:image/png;base64" in svg
    assert {n.name for n in site_plan.unplaced(nodes)} >= {"Éclairage"}


# --- F10 : rapport trimestriel assisté -------------------------------------------------------------------


def test_quarterly_report_is_a_draft_until_the_auditor_signs_and_delivers_it(db, world, manager):
    end = yesterday_local()
    year, quarter = end.year, (end.month - 1) // 3 + 1
    report = quarterly_reports.generate(db, world.org_a, year, quarter)
    assert [s["key"] for s in report.sections] == [key for key, _ in quarterly_reports.SECTIONS]
    assert report.status == ReportStatus.DRAFT and "MWh" in report.sections[0]["platform_text"]
    auditor_repo = TenantRepository(db, world.auditor_a)
    client_repo = TenantRepository(db, world.client_a)
    assert client_repo.list_reports(world.org_a.id) == []  # le client ne voit pas le projet
    with pytest.raises(quarterly_reports.ReportPermissionError):  # ni le client ni son responsable énergie
        quarterly_reports.validate_report(db, TenantRepository(db, manager), manager, report.id)
    with pytest.raises(quarterly_reports.ReportError, match="synthèse"):
        quarterly_reports.validate_report(db, auditor_repo, world.auditor_a, report.id)
    quarterly_reports.save_texts(db, auditor_repo, world.auditor_a, report.id, introduction="Bon trimestre.",
                                 conclusion=None, section_texts={"drifts": "Visite prévue sur site."})
    quarterly_reports.validate_report(db, auditor_repo, world.auditor_a, report.id)
    # Régénérer ne touche plus un rapport validé.
    assert quarterly_reports.generate(db, world.org_a, year, quarter).status == ReportStatus.VALIDATED
    quarterly_reports.deliver(db, auditor_repo, world.auditor_a, report.id)
    assert [r.id for r in client_repo.list_reports(world.org_a.id)] == [report.id]
    document = quarterly_reports.render_html(db, report)
    assert "Cabinet a" in document and "Visite prévue sur site." in document
    assert "EffiSmart" not in document  # délivré sous l'identité du cabinet
    with pytest.raises(quarterly_reports.ReportError):
        quarterly_reports.save_texts(db, auditor_repo, world.auditor_a, report.id, introduction="x",
                                     conclusion=None, section_texts={})


def test_quarterly_drafts_are_generated_after_the_quarter_ends(db, world):
    created = quarterly_reports.generate_due(db, date(2026, 10, 2))
    assert {(r.organization_id, r.year, r.quarter) for r in created} >= {(world.org_a.id, 2026, 3)}
    assert quarterly_reports.generate_due(db, date(2026, 10, 3)) == []
    other = TenantRepository(db, world.auditor_b)
    report = next(r for r in created if r.organization_id == world.org_a.id)
    with pytest.raises(ResourceNotFound):  # isolation entre cabinets
        other.get_report(report.id)


def test_plan_shows_a_group_whose_last_occurrence_is_recent(db, world):
    """Alerte principale ancienne, occurrence regroupée récente : l'anomalie est toujours située sur le plan."""
    from app.models import Drift, DriftKind

    today = yesterday_local()
    lead = Drift(delivery_point_id=world.dp_a.id, kind=DriftKind.BASELOAD, day=today - timedelta(days=60),
                 measured_value=40, reference_value=20, deviation_pct=100, unit="kW", details="t")
    db.add(lead)
    db.flush()
    db.add(Drift(delivery_point_id=world.dp_a.id, kind=DriftKind.BASELOAD, day=today - timedelta(days=5),
                 measured_value=40, reference_value=20, deviation_pct=100, unit="kW", details="t",
                 grouped_with_id=lead.id, grouping_rule="REPEAT"))
    db.commit()
    assert [d.id for d in site_plan.recent_anomalies(db, world.site_a.id, today)] == [lead.id]
