"""Module référent énergie (formation PRO-REFEI) : rentabilité, plan d'actions, M&V, bilan, comptage,
management de l'énergie, sensibilisation, cibles d'IPE."""
from datetime import date, timedelta

import pytest

from app.models import (
    ActionStatus,
    AssetNodeKind,
    AssetRelationKind,
    CommunicationStatus,
    DeliveryPoint,
    Fluid,
    IpeDefinition,
    IpeKind,
    ProviderKind,
    ReviewStatus,
    Role,
    User,
)
from app.repositories import ResourceNotFound, TenantRepository
from app.services import (
    action_plan,
    assets,
    economics,
    energy_balance,
    energy_management,
    ipe_definitions,
    metering_plan,
    simulator,
)
from app.services.consent import grant_consent
from app.services.ingestion import ingest_delivery_point
from app.timeutils import today_local, yesterday_local

K, R = AssetNodeKind, AssetRelationKind
PARAMS = dict(economics.DEFAULTS)


@pytest.fixture()
def manager(db, world) -> User:
    user = User(email="energie-a@test.fr", password_hash="x", role=Role.CLIENT_VIEWER,
                organization_id=world.org_a.id, is_energy_manager=True)
    db.add(user)
    db.commit()
    return user


def _repo(db, user):
    return TenantRepository(db, user)


# --- Rentabilité (SP4, énergieSIM) ---------------------------------------------------------------------------


def test_npv_and_irr_match_the_energiesim_examples():
    """Fiche « indicateurs économiques » d'énergieSIM : deux projets de 2 000 €, taux de 10 %."""
    project_1 = [-2000, 1000, 1000, 1000, 0, 0]
    project_2 = [-2000, 800, 800, 800, 800, 800]
    assert economics.npv(project_1, 0.10) == pytest.approx(487, abs=1)
    assert economics.npv(project_2, 0.10) == pytest.approx(1033, abs=1)
    assert economics.irr(project_1) == pytest.approx(0.234, abs=0.002)
    assert economics.irr(project_2) == pytest.approx(0.286, abs=0.002)


def test_payback_and_lifetime():
    ind = economics.indicators(2000, 1000, 0, 3, PARAMS)
    assert ind.trb == 2.0
    assert len(ind.flows) == 4  # durée de vie de 3 ans < durée d'analyse de 10 ans
    assert ind.npv == pytest.approx(487, abs=1)
    assert ind.payback_label[0].startswith("ROI > 3 ans") is False
    free = economics.indicators(0, 500, 0, 5, PARAMS)
    assert free.immediate and free.trb == 0.0 and free.irr is None
    loss = economics.indicators(1000, -50, 0, 5, PARAMS)
    assert loss.trb is None and loss.irr is None
    assert economics.payback_class(0.5)[0].startswith("ROI < 1 an")
    assert economics.payback_class(5)[1] == "warning"


def test_cee_example_of_the_training():
    """SP4 : VEV sur pompe, 202,4 MWh cumac à 1,50 € HT : 303 € ; aide de 12 % sur 2 500 €."""
    params = {**PARAMS, "cee_price_eur_mwh": 1.5}
    cee = economics.cee_value_eur(202_400, params)
    assert cee == pytest.approx(303.6)
    assert cee / 2500 == pytest.approx(0.12, abs=0.005)
    with_cee = economics.indicators(2500, 2290, 0, 15, params, cee_eur=cee)
    assert with_cee.trb == pytest.approx(0.96, abs=0.01)  # ROI CEE ≈ 1,0 an


def test_economic_parameters_are_bounded(db, world):
    repo = _repo(db, world.auditor_a)
    org = action_plan.set_economics(db, repo, world.auditor_a, world.org_a.id, {"discount_rate": 0.08})
    assert economics.params_of(org)["discount_rate"] == 0.08
    assert economics.params_of(org)["horizon_years"] == 10
    with pytest.raises(action_plan.ActionPlanError):
        action_plan.set_economics(db, repo, world.auditor_a, world.org_a.id, {"discount_rate": 0.9})


# --- Plan d'actions (SP4, SP5, SP8) ----------------------------------------------------------------------------


def _action(db, world, user=None, **values):
    user = user or world.auditor_a
    base = dict(title="Réduction des fuites d'air comprimé", category="COMPRESSED_AIR", nature="TECHNICAL",
                savings={"ELEC": 165_000}, investment_eur=4000, lifetime_years=5)
    base.update(values)
    return action_plan.create_action(db, _repo(db, user), user, world.site_a.id, **base)


def test_an_action_is_quantified_and_scored(db, world):
    action = _action(db, world, cobenefits=["SAFETY", "ENVIRONMENT"])
    prices = action_plan.site_prices(db, world.site_a)
    econ = action_plan.evaluate(action, prices, PARAMS)
    assert econ.saved_kwh == 165_000
    assert econ.energy_gain_eur == pytest.approx(165_000 * prices.price[Fluid.ELEC])
    assert econ.suggested_economic_score == 4  # retour < 1 an
    assert econ.score == action_plan.priority_score(4, None, None, ["SAFETY", "ENVIRONMENT"])
    assert 0 < econ.score <= 100


def test_reference_situation_counts_only_the_extra_investment(db, world):
    """SP3 : moteur neuf (160 k€) plutôt qu'un rebobinage (50 k€) : la rentabilité porte sur 110 k€."""
    action = _action(db, world, title="Moteur haut rendement", category="MOTORS", savings={"ELEC": 838_000},
                     investment_eur=160_000, reference_investment_eur=50_000, lifetime_years=20)
    prices = action_plan.SitePrices({Fluid.ELEC: 0.07, Fluid.GAS: 0.06}, {}, {Fluid.ELEC: 0.05, Fluid.GAS: 0.2})
    econ = action_plan.evaluate(action, prices, PARAMS)
    assert econ.investment == 110_000
    assert econ.without_cee.trb == pytest.approx(1.9, abs=0.05)


def test_funding_hints_follow_the_training(db, world):
    action = _action(db, world, title="Récupération de chaleur sur les fumées", category="RENEWABLES",
                     savings={"GAS": 60_000}, investment_eur=65_000)
    econ = action_plan.evaluate(action, action_plan.site_prices(db, world.site_a), PARAMS)
    hints = action_plan.funding_hints(action, econ)
    assert any("Fonds Chaleur" in h and "non cumulable" in h for h in hints)
    assert any("tiers-financement" in h for h in hints)
    assert any("avant toute commande" in h for h in hints)


def test_only_the_energy_manager_or_the_auditor_edit_the_plan(db, world, manager):
    action = _action(db, world, user=manager)
    assert action.created_by == manager.id
    with pytest.raises(action_plan.ActionPlanPermissionError):
        _action(db, world, user=world.client_a)
    with pytest.raises(ResourceNotFound):
        _repo(db, world.auditor_b).get_action(action.id)


def test_a_planned_action_has_one_owner_and_a_date(db, world):
    action = _action(db, world)
    repo = _repo(db, world.auditor_a)
    with pytest.raises(action_plan.ActionPlanError, match="asap"):
        action_plan.set_status(db, repo, world.auditor_a, action.id, ActionStatus.PLANNED)
    action_plan.update_action(db, repo, world.auditor_a, action.id, owner="R.E.",
                              due_date=today_local() + timedelta(days=30))
    action_plan.set_status(db, repo, world.auditor_a, action.id, ActionStatus.PLANNED)
    with pytest.raises(action_plan.ActionPlanError):
        action_plan.set_status(db, repo, world.auditor_a, action.id, ActionStatus.VERIFIED)
    action_plan.set_status(db, repo, world.auditor_a, action.id, ActionStatus.DONE)
    assert action.done_on == today_local()
    with pytest.raises(action_plan.ActionPlanError, match="mesure et vérification"):
        action_plan.set_status(db, repo, world.auditor_a, action.id, ActionStatus.VERIFIED)
    action_plan.save_mv_plan(db, repo, world.auditor_a, action.id, {"option": "A — mesure isolée"})
    action_plan.set_status(db, repo, world.auditor_a, action.id, ActionStatus.VERIFIED)
    with pytest.raises(action_plan.ActionPlanError):
        action_plan.delete_action(db, repo, world.auditor_a, action.id)


def test_medium_term_plan_includes_short_term_and_memo_is_kept_aside(db, world):
    short = _action(db, world, horizon="SHORT", priority="PRIORITY")
    medium = _action(db, world, title="Free-cooling", category="HVAC", horizon="MEDIUM", priority="AMBITIOUS",
                     savings={"ELEC": 304_600}, investment_eur=53_160, lifetime_years=20)
    _action(db, world, title="Chaudière à granulés", horizon="MEMO", savings={"GAS": 1000}, investment_eur=50_000)
    prices = action_plan.site_prices(db, world.site_a)
    evaluated = [(a, action_plan.evaluate(a, prices, PARAMS)) for a in _repo(db, world.auditor_a).list_actions(world.org_a.id)]
    summary = action_plan.plan_summary(evaluated, PARAMS, bill_eur=100_000)
    assert summary.short.count == 1 and summary.medium.count == 2 and summary.memo == 1
    assert summary.medium.investment == short.investment_eur + medium.investment_eur
    assert summary.medium.bill_share is not None
    assert [g.label for g in summary.by_priority] == ["Prioritaire", "Ambitieuse"]
    note = action_plan.director_note(world.org_a, summary, evaluated, PARAMS, 100_000, None, "Cabinet A")
    assert "Free-cooling" in note and "Chaudière à granulés" not in note and "obligé" in note


def test_planning_alerts_follow_the_periodic_review(db, world):
    action = _action(db, world, owner="O.L.", due_date=today_local() - timedelta(days=60))
    action.status = ActionStatus.PLANNED
    _action(db, world, title="Arrêt des compresseurs le week-end")
    alerts = action_plan.planning_alerts(_repo(db, world.auditor_a).list_actions(world.org_a.id))
    assert any("dépassée" in a for a in alerts)
    assert any("à planifier" in a for a in alerts)


def test_a_gesture_enters_the_plan_with_its_estimated_savings(db, world):
    simulator.ensure_library(db)
    template = next(t for t in simulator.library(db, world.auditor_a) if t.code == "MOT_VEV_POMPES")
    assert template.cee_sheet == "IND-UT-102" and template.category == "MOTORS"
    breakdown = simulator.UsageBreakdown(Fluid.ELEC, 1_000_000, {"PROCESS": 0.4, "OTHER": 0.6}, 0.15, "test", 0.05)
    action = action_plan.from_template(db, _repo(db, world.auditor_a), world.auditor_a, world.site_a.id, template,
                                       [breakdown])
    assert action.savings == {"ELEC": 100_000}  # 25 % de 400 MWh de process
    assert action.origin == "GESTURE" and action.cee_sheet == "IND-UT-102" and action.investment_eur == 2500


def test_the_mv_plan_suggests_option_c_when_savings_are_visible_at_the_meter(db, world):
    action = _action(db, world)
    prices = action_plan.site_prices(db, world.site_a)
    econ = action_plan.evaluate(action, prices, PARAMS)
    big = action_plan.suggest_mv(db, action, econ, {"ELEC": 1_000_000}, prices)
    assert big["option"] == "C"
    small = action_plan.suggest_mv(db, action, econ, {"ELEC": 10_000_000}, prices)
    assert small["option"] == "A"
    assert "10 %" in small["reasoning"][-1]
    assert action_plan.mv_completeness(None) == (1, 13)
    action_plan.save_mv_plan(db, _repo(db, world.auditor_a), world.auditor_a, action.id, small["values"])
    assert action_plan.mv_completeness(action.mv_plan)[0] >= 12


# --- Bilan énergétique, Pareto, puissance souscrite (R1) ------------------------------------------------------


def test_site_balance_and_financial_weight(db, world):
    balance = energy_balance.site_balance(db, world.site_a)
    assert balance is not None and balance.total_kwh > 0 and balance.total_eur > 0
    assert balance.kwh_m2 == pytest.approx(balance.total_kwh / 1000)
    assert balance.lines[0].pricing == "indicatif"
    assert energy_balance.financial_weight(world.org_a, 50_000) is None
    energy_balance.set_finances(db, _repo(db, world.auditor_a), world.auditor_a, world.org_a.id,
                                revenue_eur=10_000_000, ebitda_eur=400_000, year=2025)
    weight = energy_balance.financial_weight(world.org_a, 200_000)
    assert weight.share_revenue == pytest.approx(0.02) and weight.share_ebitda == pytest.approx(0.5)
    assert weight.ebitda_gain(20_000) == pytest.approx(0.05)  # guide ComptIAA : 10 % de gain, +5 % d'EBE
    with pytest.raises(PermissionError):
        energy_balance.set_finances(db, _repo(db, world.client_a), world.client_a, world.org_a.id,
                                    revenue_eur=1, ebitda_eur=1, year=2025)


def test_pareto_proposes_the_significant_uses():
    b = simulator.UsageBreakdown(Fluid.ELEC, 1000, {"PROCESS": 0.5, "COMPRESSED_AIR": 0.3, "LIGHTING": 0.15,
                                                    "OTHER": 0.05}, 0.2, "test", 0.05,
                                 sources={"PROCESS": "estimé", "COMPRESSED_AIR": "estimé", "LIGHTING": "estimé",
                                          "OTHER": "par différence"})
    rows, notes = energy_balance.usage_pareto([b])
    assert [r.usage for r in rows] == ["PROCESS", "COMPRESSED_AIR", "LIGHTING", "OTHER"]
    assert [r.significant for r in rows] == [True, True, False, False]
    assert rows[-1].cumulative == pytest.approx(1.0)
    assert "Pareto" in notes[0]


def test_subscribed_power_check(db, world):
    world.dp_a.subscribed_power_kva = 500
    db.commit()
    checks = energy_balance.power_checks(db, world.site_a)
    assert len(checks) == 1
    check = checks[0]
    assert check.status == "OVERSIZED" and check.suggested_kva < 500
    assert check.yearly_gain(10) > 0
    world.dp_a.subscribed_power_kva = check.max_kva * 0.8
    db.commit()
    assert energy_balance.power_checks(db, world.site_a)[0].status == "OVER"


# --- Plan de comptage (R5) ------------------------------------------------------------------------------------


def test_metering_plan_reconciles_sub_meters(db, world):
    sub = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref="30009000000099",
                        provider=ProviderKind.MOCK, is_primary=False)
    db.add(sub)
    db.commit()
    grant_consent(db, sub, scope="test", proof_ref="test")
    end = yesterday_local()
    ingest_delivery_point(db, sub, end - timedelta(days=40), end)
    main_node = assets.ensure_meter_node(db, world.dp_a)
    sub_node = assets.ensure_meter_node(db, sub)
    repo = _repo(db, world.auditor_a)
    assets.create_relation(db, repo, world.auditor_a, main_node.id, R.SUPPLIES, sub_node.id)
    db.refresh(world.site_a)
    assessment = metering_plan.assess(db, world.site_a, end)
    assert assessment.level == 2
    assert len(assessment.reconciliations) == 1
    rec = assessment.reconciliations[0]
    # Deux profils simulés indépendants : la somme des sous-compteurs peut dépasser le général, ce qui est signalé.
    assert rec.status in ("OK", "INCOHERENT")
    assert rec.unmetered_kwh == pytest.approx(rec.main.kwh - rec.subs_kwh)
    assert [s["done"] for s in assessment.steps][0] is False  # aucun équipement décrit


# --- Management de l'énergie (R3) ---------------------------------------------------------------------------


def test_maturity_scores_exclude_not_applicable_answers():
    answers = {"P1": 2, "P2": 1, "P3": -1}
    scores = energy_management.compute_scores(answers)
    assert scores["themes"]["POLICY"] == pytest.approx(3 / 8)  # P4, P5 sans réponse comptent 0 ; P3 exclue
    assert scores["axes"]["STRATEGY"] == pytest.approx((3 / 8 + 0) / 2)
    assert len(energy_management.QUESTIONS) == 38 and len(energy_management.THEMES) == 12
    assert energy_management.level(0.7)[0] == "Structurée"


def test_assessment_is_saved_by_a_validator_and_shows_platform_evidence(db, world, manager):
    repo = _repo(db, manager)
    assessment = energy_management.save_assessment(db, repo, manager, world.org_a.id, {"P1": 2, "N1": 1})
    assert assessment.scores["answered"] == 2
    with pytest.raises(PermissionError):
        energy_management.save_assessment(db, _repo(db, world.client_a), world.client_a, world.org_a.id, {"P1": 2})
    with pytest.raises(energy_management.ManagementError):
        energy_management.save_assessment(db, repo, manager, world.org_a.id, {"ZZ": 2})
    energy_management.save_management(
        db, repo, manager, world.org_a.id, policy="Réduire de 15 % nos consommations d'ici 2028.",
        policy_approved_on=date(2026, 1, 15), scope="Site A, électricité", team=[{"name": "R.E.", "role": "Référent"}],
        objectives=[{"label": "−15 % kWh/m²", "indicator": "kWh/m²", "target": 200}], review_months=12,
        last_review_on=date(2025, 6, 1))
    hints = energy_management.evidence(db, world.org_a)
    assert "validée le 15/01/2026" in hints["P1"] and "1 membre" in hints["O2"]
    assert "Détection quotidienne" in hints["N3"]
    record = repo.get_management(world.org_a.id)
    assert energy_management.smart_gaps(record.objectives[0]) == ["échéance", "responsable"]
    due, late = energy_management.next_review(record)
    assert due == date(2026, 6, 1) and late is (due < today_local())


# --- Sensibilisation (R7) -------------------------------------------------------------------------------------


def test_awareness_messages_are_drafts_until_a_human_approves_them(db, world, manager):
    repo = _repo(db, world.auditor_a)
    assets.create_node(db, repo, world.auditor_a, world.site_a.id, kind=K.EQUIPMENT, category_code="COMPRESSOR",
                       name="Compresseur 1", power_kw=37)
    drafts = energy_management.generate_drafts(db, repo, world.auditor_a, world.site_a.id)
    air = next(m for m in drafts if m.theme == "COMPRESSED_AIR")
    assert "4,3 m³" in air.body and air.status == CommunicationStatus.DRAFT
    assert _repo(db, world.client_a).list_messages(world.org_a.id) == []  # P1 : brouillons invisibles du client
    energy_management.review_message(db, _repo(db, manager), manager, air.id, title=air.title, body=air.body,
                                     call_to_action=air.call_to_action, status=CommunicationStatus.APPROVED)
    assert [m.id for m in _repo(db, world.client_a).list_messages(world.org_a.id)] == [air.id]
    regenerated = energy_management.generate_drafts(db, repo, world.auditor_a, world.site_a.id)
    assert air.id not in [m.id for m in regenerated]  # l'approuvé est conservé, les brouillons sont refaits
    assert "<h1>" in energy_management.poster_html(air)


# --- Cible et seuil d'alerte d'un IPE (R6) ------------------------------------------------------------------


def test_ipe_target_and_alert_threshold(db, world):
    definition = IpeDefinition(organization_id=world.org_a.id, site_id=world.site_a.id, name="kWh/m²", energy="ALL",
                               kind=IpeKind.RATIO, drivers=["SURFACE"], origin="USER", status=ReviewStatus.PROPOSED)
    db.add(definition)
    db.commit()
    repo = _repo(db, world.auditor_a)
    with pytest.raises(ipe_definitions.IpeDefinitionError):
        ipe_definitions.set_target(db, repo, world.auditor_a, definition.id, 100)
    definition.status = ReviewStatus.VALIDATED
    db.commit()
    ipe_definitions.set_target(db, repo, world.auditor_a, definition.id, 100, 0.10)
    value = ipe_definitions.IpeValue("kWh/m²/an", 115, 120, 2025)
    status = ipe_definitions.target_status(definition, value)
    assert status.status == "ALERT" and status.gap == pytest.approx(0.15)
    value.current = 105
    assert ipe_definitions.target_status(definition, value).status == "WATCH"
    value.current = 95
    assert ipe_definitions.target_status(definition, value).status == "ON_TARGET"


def test_compressed_air_benchmark_is_shown_for_a_nm3_variable(db, world):
    variable = ipe_definitions.create_variable(db, _repo(db, world.auditor_a), world.auditor_a, world.site_a.id,
                                               "Air produit", "Nm³")
    definition = IpeDefinition(organization_id=world.org_a.id, site_id=world.site_a.id, name="kWh/Nm³", energy="ELEC",
                               kind=IpeKind.RATIO, drivers=[f"VAR:{variable.id}"], origin="USER",
                               status=ReviewStatus.VALIDATED)
    db.add(definition)
    db.commit()
    label, bounds = ipe_definitions.benchmark_for(db, definition)
    assert "110 à 125 Wh" in label and bounds == (0.110, 0.125)


def test_new_utility_categories_and_usage_sources(db, world):
    assert "COMPRESSOR" in assets.CATEGORIES[K.EQUIPMENT]
    assert "COMPRESSED_AIR" in assets.CATEGORIES[K.USAGE]
    assert simulator.USAGE_LABELS["COMPRESSED_AIR"] == "Air comprimé"
    simulator.ensure_library(db)
    codes = {t.code for t in simulator.library(db, world.auditor_a)}
    assert {"AIR_FUITES", "AIR_PRESSION", "CHAUF_POINTS_SINGULIERS", "FROID_HP_FLOTTANTE"} <= codes
