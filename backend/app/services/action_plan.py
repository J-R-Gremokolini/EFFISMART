"""R2 — Plan d'actions du référent énergie ; R4 — plan de mesure et vérification IPMVP de chaque action.

Formation PRO-REFEI (ATEE) :
- SP4 : chaque action est qualifiée et quantifiée (MWh/an, € HT/an, investissement, temps de retour) ; le plan se
  bâtit à court terme et à moyen terme (le moyen terme reprend le court terme), les actions écartées restent « pour
  mémoire » ; le ROI n'est pas le seul critère : niveau d'investissement, environnement, mix énergétique, sécurité,
  maintenance, réglementation, productivité, management ;
- SP3 : quatre natures d'action (conception, technique, pilotage-maintenance, organisationnelle) et la situation de
  référence (rentabilité du surinvestissement) ;
- SP5 : un responsable et un seul par action, une échéance datée (jamais « asap »), les moyens de vérification ;
  cotation simple (faisabilité économique, technique, risques, de 1 à 4), revue périodique du plan ;
- SP8 : fiche QQOQPCC (Qui ? Quoi ? Où ? Quand ? Pourquoi ? Combien ? Comment ?) et contacts à prendre ;
- énergieSIM : groupes « prioritaires, ambitieuses, très ambitieuses », indicateurs avec et sans CEE.

Les actions sont saisies par des humains (auditeur, responsable énergie) : la plateforme chiffre, rappelle les
règles et suggère (option IPMVP, note économique), elle ne décide pas.
"""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import (
    ActionStatus,
    ActionTemplate,
    AssetNode,
    EnergyAction,
    Fluid,
    IpeDefinition,
    Organization,
    Recommendation,
    ReviewStatus,
    Site,
    User,
)
from app.providers.weather import WeatherProvider
from app.repositories import TenantRepository
from app.services import economics, ipe, ipe_definitions, simulator, tariffs
from app.services.dashboard import data_as_of, emission_factors_at, estimated_price
from app.services.energy_data import latest_declared_day
from app.services.validation import fr
from app.timeutils import add_months, month_start, to_local, today_local, utcnow

CATEGORIES = {
    "LIGHTING": "Éclairage", "COMPRESSED_AIR": "Air comprimé", "HVAC": "Chauffage, climatisation, ventilation",
    "COLD": "Froid", "PROCESS": "Process, profil électrique", "MOTORS": "Moteurs, pompes, ventilateurs",
    "STEAM": "Chaufferie, vapeur, calorifugeage", "RENEWABLES": "Énergies renouvelables, récupération de chaleur",
    "METERING": "Comptage, pilotage, GTC", "BEHAVIOUR": "Organisation, sensibilisation",
    "PURCHASING": "Achats d'énergie, contrats", "OTHER": "Autre",
}
NATURES = {"DESIGN": "Conception", "TECHNICAL": "Technique", "OPERATION": "Pilotage, maintenance",
           "ORGANISATIONAL": "Organisationnelle"}
PRIORITIES = {"PRIORITY": "Prioritaire", "AMBITIOUS": "Ambitieuse", "VERY_AMBITIOUS": "Très ambitieuse",
              "UNRANKED": "Non priorisée"}
HORIZONS = {"SHORT": "Court terme", "MEDIUM": "Moyen terme", "MEMO": "Pour mémoire"}
STATUS_LABELS = {
    ActionStatus.IDENTIFIED: ("Identifiée", "neutral"), ActionStatus.PLANNED: ("Planifiée", "neutral"),
    ActionStatus.IN_PROGRESS: ("En cours", "warning"), ActionStatus.DONE: ("Réalisée", "success"),
    ActionStatus.VERIFIED: ("Vérifiée", "success"), ActionStatus.ABANDONED: ("Abandonnée", "danger"),
}
COBENEFITS = {
    "ENVIRONMENT": "Environnement : moins de CO₂, de rejets chauds",
    "ENERGY_MIX": "Mix énergétique : coûts sécurisés, renouvelables",
    "SAFETY": "Sécurité des personnes et des biens, fiabilité",
    "MAINTENANCE": "Maintenance préventive, équipement en fin de vie",
    "REGULATION": "Conformité réglementaire, actuelle ou à venir",
    "PRODUCTIVITY": "Productivité, qualité de production",
    "MANAGEMENT": "Conditions de travail, compétences, implication",
}
SCORE_LABELS = {4: "Forte", 3: "Moyenne", 2: "Faible", 1: "Nulle"}
SCORE_HELP = {
    "economic": "4 : attractivité économique forte ; 3 : acceptable ; 2 : pas acceptable ; 1 : clairement non "
                "acceptable.",
    "technical": "4 : technologie adaptée, existante et bien connue (meilleures techniques disponibles, BREF) ; "
                 "3 : connue mais en évolution rapide ou peu de retours d'expérience ; 2 : inadaptée ; 1 : rupture "
                 "technologique requise.",
    "risk": "4 : pas de risque identifié ; 3 : faibles risques sur la faisabilité ; 2 : doute sur la faisabilité ; "
            "1 : non faisable.",
}
OPEN_STATUSES = (ActionStatus.IDENTIFIED, ActionStatus.PLANNED, ActionStatus.IN_PROGRESS)
COMMITTED_STATUSES = (ActionStatus.PLANNED, ActionStatus.IN_PROGRESS, ActionStatus.DONE, ActionStatus.VERIFIED)


class ActionPlanError(ValueError):
    pass


class ActionPlanPermissionError(PermissionError):
    pass


def can_edit(user: User) -> bool:
    """Le plan d'actions est tenu par le responsable énergie du client ou par l'auditeur."""
    return ipe.can_enter_variables(user)


def _require_editor(user: User) -> None:
    if not can_edit(user):
        raise ActionPlanPermissionError("Le plan d'actions est tenu par le responsable énergie ou l'auditeur.")


def set_economics(db: Session, repo: TenantRepository, user: User, organization_id: int, values: dict) -> Organization:
    """Paramètres de rentabilité propres à l'organisation (taux d'actualisation, durée, prix, CEE)."""
    _require_editor(user)
    org = repo.get_organization(organization_id)
    try:
        clean = economics.check_params(values)
    except ValueError as exc:
        raise ActionPlanError(str(exc)) from exc
    org.economics = {**(org.economics or {}), **clean}
    db.commit()
    return org


# --- Chiffrage ---------------------------------------------------------------------------------------------


@dataclass
class SitePrices:
    """Prix effectivement payés (12 derniers mois) et facteurs d'émission, par énergie."""

    price: dict[Fluid, float]
    source: dict[Fluid, str]
    kgco2e: dict[Fluid, float]


def site_prices(db: Session, site: Site) -> SitePrices:
    ids = [dp.id for dp in site.delivery_points]
    end = data_as_of(db, ids) or latest_declared_day(db, ids) or today_local()
    start = end - timedelta(days=364)
    factors = emission_factors_at(db, end)
    price, source, kgco2e = {}, {}, {}
    for fluid in Fluid:
        points = [dp for dp in site.delivery_points if dp.fluid == fluid]
        if points:
            price[fluid], source[fluid] = tariffs.effective_price(db, points, start, end, fluid)
        else:
            price[fluid], source[fluid] = estimated_price(fluid), "prix indicatif"
        factor = factors.get(fluid)
        kgco2e[fluid] = factor.factor_kgco2_per_kwh if factor else 0.0
    return SitePrices(price, source, kgco2e)


@dataclass
class ActionEconomics:
    saved_kwh: float  # toutes énergies (une surconsommation se déduit)
    energy_gain_eur: float  # RAE
    saved_kgco2e: float
    investment: float  # investissement retenu : surinvestissement si une situation de référence est saisie
    cee_eur: float
    without_cee: economics.Indicators
    with_cee: economics.Indicators
    suggested_economic_score: int
    score: int  # cotation sur 100
    lines: dict[str, float] = field(default_factory=dict)  # kWh/an par énergie

    @property
    def cee_aid_share(self) -> float | None:
        return self.cee_eur / self.investment if self.investment > 0 and self.cee_eur else None


def economic_score(indicators: economics.Indicators) -> int:
    """Note de faisabilité économique suggérée d'après le temps de retour brut (SP4, SP5)."""
    if indicators.immediate or (indicators.trb is not None and indicators.trb < 1):
        return 4
    if indicators.trb is not None and indicators.trb <= 3:
        return 3
    if indicators.trb is not None and indicators.trb <= 7:
        return 2
    return 1


def priority_score(economic: int, technical: int | None, risk: int | None, cobenefits: list | None) -> int:
    """Cotation sur 100 (inspirée de l'exemple WinErgia de la formation) : 85 points pour la faisabilité économique
    (40 %), technique (30 %) et les risques (30 %), notées de 1 à 4 ; 15 points pour les co-bénéfices (SP4).
    Une note technique ou de risque non saisie compte pour 3 (« moyenne »)."""
    base = 0.4 * economic + 0.3 * (technical or 3) + 0.3 * (risk or 3)
    bonus = 15 * min(len(cobenefits or []), len(COBENEFITS)) / len(COBENEFITS)
    return round(85 * base / 4 + bonus)


def evaluate(action: EnergyAction, prices: SitePrices, params: dict) -> ActionEconomics:
    lines = {k: float(v or 0.0) for k, v in (action.savings or {}).items()}
    energy_eur = sum(kwh * prices.price.get(Fluid(k), estimated_price(Fluid(k))) for k, kwh in lines.items())
    co2 = sum(kwh * prices.kgco2e.get(Fluid(k), 0.0) for k, kwh in lines.items())
    investment = max(0.0, (action.investment_eur or 0.0) - (action.reference_investment_eur or 0.0))
    cee = economics.cee_value_eur(action.cee_kwh_cumac, params)
    without = economics.indicators(investment, energy_eur, action.recurring_eur or 0.0, action.lifetime_years, params)
    with_cee = economics.indicators(investment, energy_eur, action.recurring_eur or 0.0, action.lifetime_years, params,
                                    cee_eur=cee)
    suggested = economic_score(with_cee)
    score = priority_score(action.score_economic or suggested, action.score_technical, action.score_risk,
                           action.cobenefits)
    return ActionEconomics(sum(lines.values()), energy_eur, co2, investment, cee, without, with_cee, suggested, score,
                           lines)


# --- Saisie --------------------------------------------------------------------------------------------------

EDITABLE = ("title", "description", "category", "nature", "priority", "horizon", "savings", "recurring_eur",
            "investment_eur", "reference_investment_eur", "lifetime_years", "cee_kwh_cumac", "cee_sheet",
            "score_economic", "score_technical", "score_risk", "cobenefits", "owner", "location", "equipment_id",
            "due_date", "why", "how_much", "how", "contacts", "needs", "progress_note", "ipe_definition_id")


def _clean(db: Session, site: Site, values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key not in EDITABLE:
            raise ActionPlanError(f"Champ inconnu : {key}.")
        if isinstance(value, str):
            value = value.strip() or None
        out[key] = value
    if "title" in out:
        if not out["title"] or len(out["title"]) > 200:
            raise ActionPlanError("Intitulé obligatoire (200 caractères au plus) : c'est le « Quoi ? » de l'action.")
    for key, allowed in (("category", CATEGORIES), ("nature", NATURES), ("priority", PRIORITIES),
                         ("horizon", HORIZONS)):
        if key in out and out[key] not in allowed:
            raise ActionPlanError(f"Valeur inconnue pour « {key} ».")
    if "savings" in out:
        savings = {}
        for fluid, kwh in (out["savings"] or {}).items():
            if fluid not in (f.value for f in Fluid):
                raise ActionPlanError("Énergie inconnue.")
            if kwh:
                savings[fluid] = float(kwh)
        out["savings"] = savings
    for key in ("investment_eur", "reference_investment_eur", "cee_kwh_cumac"):
        if out.get(key) is not None and out[key] < 0:
            raise ActionPlanError("Investissement et CEE sont positifs ou nuls.")
    if out.get("lifetime_years") is not None and not 1 <= int(out["lifetime_years"]) <= 50:
        raise ActionPlanError("Durée de vie comprise entre 1 et 50 ans.")
    for key in ("score_economic", "score_technical", "score_risk"):
        if out.get(key) is not None and out[key] not in (1, 2, 3, 4):
            raise ActionPlanError("Les notes de cotation vont de 1 (nulle) à 4 (forte).")
    if out.get("cobenefits") is not None:
        unknown = set(out["cobenefits"]) - set(COBENEFITS)
        if unknown:
            raise ActionPlanError("Co-bénéfice inconnu.")
    if out.get("owner") and len(out["owner"]) > 120:
        raise ActionPlanError("Responsable : 120 caractères au plus.")
    if out.get("contacts") is not None:
        out["contacts"] = [c for c in out["contacts"] if (c.get("who") or "").strip()] or None
    if out.get("equipment_id") is not None:
        node = db.get(AssetNode, out["equipment_id"])
        if node is None or node.site_id != site.id:
            raise ActionPlanError("Équipement inconnu sur ce site.")
    if out.get("ipe_definition_id") is not None:
        definition = db.get(IpeDefinition, out["ipe_definition_id"])
        if definition is None or definition.site_id != site.id:
            raise ActionPlanError("IPE inconnu sur ce site.")
    return out


def create_action(db: Session, repo: TenantRepository, user: User, site_id: int, *, title: str,
                  category: str = "OTHER", nature: str = "TECHNICAL", origin: str = "USER",
                  template_id: int | None = None, recommendation_id: int | None = None, **values) -> EnergyAction:
    _require_editor(user)
    site = repo.get_site(site_id)
    fields = _clean(db, site, {"title": title, "category": category, "nature": nature, **values})
    action = EnergyAction(organization_id=site.organization_id, site_id=site.id, origin=origin,
                          template_id=template_id, recommendation_id=recommendation_id, created_by=user.id,
                          savings=fields.pop("savings", None) or {}, recurring_eur=fields.pop("recurring_eur", 0.0) or 0.0,
                          investment_eur=fields.pop("investment_eur", 0.0) or 0.0,
                          reference_investment_eur=fields.pop("reference_investment_eur", 0.0) or 0.0,
                          lifetime_years=int(fields.pop("lifetime_years", None) or 10), **fields)
    db.add(action)
    db.commit()
    return action


def update_action(db: Session, repo: TenantRepository, user: User, action_id: int, **values) -> EnergyAction:
    _require_editor(user)
    action = repo.get_action(action_id)
    fields = _clean(db, action.site, values)
    for key, value in fields.items():
        if key in ("recurring_eur", "investment_eur", "reference_investment_eur"):
            value = value or 0.0
        if key == "lifetime_years":
            value = int(value or 10)
        if key == "savings":
            value = value or {}
        setattr(action, key, value)
    if action.status in (ActionStatus.PLANNED, ActionStatus.IN_PROGRESS):
        _check_commitment(action)
    action.updated_by, action.updated_at = user.id, utcnow()
    db.commit()
    return action


def delete_action(db: Session, repo: TenantRepository, user: User, action_id: int) -> None:
    _require_editor(user)
    action = repo.get_action(action_id)
    if action.status in (ActionStatus.DONE, ActionStatus.VERIFIED):
        raise ActionPlanError("Une action réalisée reste au plan : c'est la trace des économies obtenues.")
    db.delete(action)
    db.commit()


def _check_commitment(action: EnergyAction) -> None:
    """SP5 : un responsable et un seul, une échéance datée ; SP8 : « Qui ? » et « Quand ? »."""
    missing = []
    if not action.owner:
        missing.append("un responsable (Qui ?)")
    if action.due_date is None:
        missing.append("une échéance datée (Quand ?)")
    if missing:
        raise ActionPlanError("Une action planifiée a " + " et ".join(missing) + " : bannir les « asap ».")


def set_status(db: Session, repo: TenantRepository, user: User, action_id: int, status: ActionStatus,
               note: str | None = None, done_on: date | None = None) -> EnergyAction:
    _require_editor(user)
    action = repo.get_action(action_id)
    if status in (ActionStatus.PLANNED, ActionStatus.IN_PROGRESS):
        _check_commitment(action)
    if status == ActionStatus.DONE:
        action.done_on = done_on or action.done_on or today_local()
        if action.done_on > today_local():
            raise ActionPlanError("Date de réalisation dans le futur.")
    if status == ActionStatus.VERIFIED:
        if action.status not in (ActionStatus.DONE, ActionStatus.VERIFIED):
            raise ActionPlanError("Une action se vérifie une fois réalisée.")
        if not (action.mv_plan or {}).get("option"):
            raise ActionPlanError("Renseignez d'abord le plan de mesure et vérification (option IPMVP).")
    if status in OPEN_STATUSES:
        action.done_on = None
    action.status = status
    if note:
        action.progress_note = note.strip()
    action.updated_by, action.updated_at = user.id, utcnow()
    db.commit()
    return action


def from_template(db: Session, repo: TenantRepository, user: User, site_id: int, template: ActionTemplate,
                  breakdowns: list[simulator.UsageBreakdown]) -> EnergyAction:
    """Inscrit un geste type au plan : économies estimées sur la répartition par usage du site (simulateur F9)."""
    site = repo.get_site(site_id)
    savings = {b.fluid.value: round(b.usage_kwh(template.usage) * template.savings_pct)
               for b in breakdowns if b.usage_kwh(template.usage) > 0}
    if not savings:
        raise ActionPlanError("Ce geste porte sur un usage absent de la répartition du site.")
    investment = template.investment_eur_m2 * (site.surface_m2 or 0.0) + template.investment_eur
    nature = "ORGANISATIONAL" if template.category == "BEHAVIOUR" else (
        "OPERATION" if investment == 0 else "TECHNICAL")
    return create_action(db, repo, user, site.id, title=template.title, category=template.category or "OTHER",
                         nature=nature, origin="GESTURE", template_id=template.id, savings=savings,
                         investment_eur=round(investment), lifetime_years=template.lifetime_years or 10,
                         cee_sheet=template.cee_sheet,
                         description=(template.notes or None))


def from_recommendation(db: Session, repo: TenantRepository, user: User, recommendation: Recommendation) -> EnergyAction:
    """Inscrit au plan une recommandation validée par un humain (principe P1)."""
    if recommendation.status not in (ReviewStatus.VALIDATED, ReviewStatus.APPLIED):
        raise ActionPlanError("Seule une recommandation validée s'inscrit au plan d'actions.")
    fluid = recommendation.delivery_point.fluid if recommendation.delivery_point else Fluid.ELEC
    savings = {fluid.value: round(recommendation.gain_kwh)} if recommendation.gain_kwh else {}
    category = {"SCHEDULE_OFF_HOURS": "BEHAVIOUR", "HEATING_CONTROL": "HVAC", "PEAK_SHAVING": "PURCHASING",
                "LOAD_SHIFT": "PURCHASING"}.get(recommendation.kind.value, "OTHER")
    action = create_action(db, repo, user, recommendation.site_id, title=recommendation.title, category=category,
                           nature="OPERATION", origin="RECOMMENDATION", recommendation_id=recommendation.id,
                           savings=savings, description=recommendation.action, equipment_id=recommendation.equipment_id,
                           recurring_eur=(recommendation.gain_eur or 0.0) if not recommendation.gain_kwh else 0.0)
    if recommendation.status == ReviewStatus.APPLIED and recommendation.applied_at:
        action.status, action.done_on = ActionStatus.DONE, to_local(recommendation.applied_at).date()
        db.commit()
    return action


# --- Financements (SP4 : « financer avec un apport externe mes projets d'économies ») -------------------------

CEE_FAMILIES = {"LIGHTING", "COMPRESSED_AIR", "HVAC", "COLD", "MOTORS", "STEAM", "METERING", "RENEWABLES"}
LARGE_INVESTMENT_EUR = 50_000


def funding_hints(action: EnergyAction, econ: ActionEconomics) -> list[str]:
    """Pistes de financement à étudier : des rappels, pas une éligibilité vérifiée."""
    hints = []
    if action.cee_sheet or action.category in CEE_FAMILIES:
        hints.append("Certificats d'économies d'énergie (CEE) : vérifier l'éligibilité" +
                     (f" (fiche {action.cee_sheet})" if action.cee_sheet else " (fiches d'opérations standardisées)") +
                     " et signer l'accord avec l'obligé avant toute commande.")
    if action.category == "RENEWABLES":
        hints.append("Fonds Chaleur de l'ADEME pour la chaleur renouvelable ou de récupération : aide non cumulable "
                     "avec les CEE, à comparer.")
    if econ.investment >= LARGE_INVESTMENT_EUR:
        hints.append("Investissement important : tiers-financement ou contrat de performance énergétique (CPE), prêts "
                     "verts (Bpifrance), appels à projets régionaux ; contacter la direction régionale de l'ADEME.")
    return hints


# --- Synthèse du plan (canevas de la formation, énergieSIM) --------------------------------------------------


@dataclass
class GroupSummary:
    label: str
    count: int
    saved_kwh: float
    gain_eur: float  # gain annuel net (énergie + récurrent)
    saved_kgco2e: float
    investment: float
    cee_eur: float
    without_cee: economics.Indicators
    with_cee: economics.Indicators
    bill_share: float | None = None  # part de la facture énergétique économisée


def summarize(label: str, items: list[tuple[EnergyAction, ActionEconomics]], params: dict,
              bill_eur: float | None) -> GroupSummary:
    investment = sum(e.investment for _, e in items)
    cee = sum(e.cee_eur for _, e in items)
    annual = sum(e.without_cee.annual_net for _, e in items)
    without = economics.group_indicators([e.without_cee.flows for _, e in items], investment, annual, params)
    with_cee = economics.group_indicators([e.with_cee.flows for _, e in items], max(0.0, investment - cee), annual,
                                          params)
    energy_eur = sum(e.energy_gain_eur for _, e in items)
    return GroupSummary(label, len(items), sum(e.saved_kwh for _, e in items), annual,
                        sum(e.saved_kgco2e for _, e in items), investment, cee, without, with_cee,
                        energy_eur / bill_eur if bill_eur else None)


@dataclass
class PlanSummary:
    short: GroupSummary  # court terme
    medium: GroupSummary  # moyen terme : reprend le court terme (canevas de la formation)
    by_priority: list[GroupSummary]
    memo: int  # actions pour mémoire, hors plan


def plan_summary(evaluated: list[tuple[EnergyAction, ActionEconomics]], params: dict,
                 bill_eur: float | None) -> PlanSummary:
    kept = [(a, e) for a, e in evaluated if a.status != ActionStatus.ABANDONED and a.horizon != "MEMO"]
    short = [(a, e) for a, e in kept if a.horizon == "SHORT"]
    groups = []
    for code, label in PRIORITIES.items():
        items = [(a, e) for a, e in kept if a.priority == code]
        if items:
            groups.append(summarize(label, items, params, bill_eur))
    return PlanSummary(summarize("Court terme", short, params, bill_eur),
                       summarize("Moyen terme (court terme compris)", kept, params, bill_eur), groups,
                       sum(1 for a, _ in evaluated if a.horizon == "MEMO"))


def planning_alerts(actions: list[EnergyAction], today: date | None = None) -> list[str]:
    """Revue périodique (SP5) : échéances dépassées, actions sans responsable ou sans échéance."""
    today = today or today_local()
    alerts = []
    for action in actions:
        if action.status in (ActionStatus.PLANNED, ActionStatus.IN_PROGRESS) and action.due_date and action.due_date < today:
            alerts.append(f"« {action.title} » : échéance du {action.due_date:%d/%m/%Y} dépassée ; mettre la date à "
                          "jour ou clôturer.")
        elif action.status == ActionStatus.IDENTIFIED and action.horizon == "SHORT" and not (action.owner and action.due_date):
            alerts.append(f"« {action.title} » (court terme) : à planifier, avec un responsable et une échéance.")
        if action.status == ActionStatus.DONE and not (action.mv_plan or {}).get("option"):
            alerts.append(f"« {action.title} » est réalisée : décrire comment ses économies seront vérifiées "
                          "(plan de mesure et vérification).")
    return alerts


# --- R4 : plan de mesure et vérification IPMVP (13 points, SP5) -----------------------------------------------

MV_POINTS = [
    ("option", "Option IPMVP retenue et justification"),
    ("baseline", "Situation de référence : période et données"),
    ("method", "Méthode d'analyse, algorithmes, hypothèses"),
    ("instrumentation", "Instrumentation, exploitation et maintenance"),
    ("valuation", "Valorisation financière des économies"),
    ("reporting_period", "Période de suivi"),
    ("adjustments", "Ajustements : facteurs périodiques et statiques"),
    ("responsibilities", "Responsabilités des opérations de M&V"),
    ("precision", "Précision attendue"),
    ("budget", "Budget de M&V et ressources"),
    ("reports", "Rapports de suivi : contenu et format"),
    ("quality", "Assurance qualité"),
]
MV_OPTIONS = {
    "A": "A — mesure isolée des paramètres clés (les autres sont estimés)",
    "B": "B — mesure isolée de tous les paramètres",
    "C": "C — site entier, au compteur général",
    "D": "D — simulation calibrée",
}
OPTION_C_MIN_SHARE = 0.10  # IPMVP : économies d'au moins 10 % de la consommation du compteur pour l'option C
MV_BUDGET_SHARE = 0.10  # IPMVP : coût annuel de M&V en général inférieur à 10 % des économies annuelles


def mv_completeness(mv_plan: dict | None) -> tuple[int, int]:
    """Points du plan renseignés, sur 13 (l'action elle-même est le point 1)."""
    filled = sum(1 for key, _ in MV_POINTS if (mv_plan or {}).get(key))
    return filled + 1, len(MV_POINTS) + 1


def suggest_mv(db: Session, action: EnergyAction, economics_: ActionEconomics,
               consumption_kwh: dict[str, float], prices: SitePrices) -> dict:
    """Plan de M&V suggéré, avec son raisonnement : à relire, ajuster et enregistrer par un humain (P1)."""
    reasoning = []
    shares = {k: abs(v) / consumption_kwh[k] for k, v in economics_.lines.items() if consumption_kwh.get(k)}
    share = max(shares.values()) if shares else 0.0
    from app.services import assets  # import local : le graphe n'est utile qu'ici

    graph = assets.load_site_graph(db, action.site_id)
    own_meter = None
    if action.equipment_id:
        for _, source in graph.predecessors(action.equipment_id):
            if source.kind.value == "METER" and graph.predecessors(source.id):
                own_meter = source
    if share >= OPTION_C_MIN_SHARE:
        option = "C"
        reasoning.append(f"Économies attendues : {fr(share * 100, 1)} % de la consommation du compteur, au-delà des "
                         "10 % qui les rendent visibles au compteur général (IPMVP) : option C.")
    elif own_meter is not None:
        option = "B"
        reasoning.append(f"Économies attendues : {fr(share * 100, 1)} % de la consommation, trop peu pour le compteur "
                         "général ; l'équipement a son sous-compteur : option B (mesure isolée de tous les "
                         "paramètres).")
    else:
        option = "A"
        reasoning.append(f"Économies attendues : {fr(share * 100, 1)} % de la consommation, trop peu pour le compteur "
                         "général (seuil de 10 %, IPMVP) : isoler l'action. Sans sous-compteur, option A : mesurer "
                         "les paramètres clés (puissance, durée de fonctionnement), estimer les autres.")
    gain = economics_.without_cee.annual_net
    budget = gain * MV_BUDGET_SHARE
    reasoning.append(f"Budget de M&V : en général moins de 10 % des économies annuelles, soit moins de {fr(budget)} € "
                     "par an ici.")
    definition = db.get(IpeDefinition, action.ipe_definition_id) if action.ipe_definition_id else None
    adjustments = "Facteurs périodiques : degrés-jours (DJU) pour le chauffage, production et jours ouvrés pour le " \
                  "process. Facteurs statiques : surfaces, horaires, équipements raccordés."
    if definition is not None:
        adjustments = (f"Facteurs de l'IPE « {definition.name} » : " + ", ".join(definition.drivers)
                       + ". Facteurs statiques : surfaces, horaires, équipements raccordés.")
    prices_text = ", ".join(f"{'électricité' if f == Fluid.ELEC else 'gaz'} {fr(prices.price[f], 3)} €/kWh "
                            f"({prices.source[f]})" for f in Fluid if f.value in economics_.lines)
    done = action.done_on
    suggestion = {
        "option": f"{MV_OPTIONS[option]}. " + reasoning[0],
        "baseline": ("Les 12 mois précédant la mise en œuvre" + (f", jusqu'au {done - timedelta(days=1):%d/%m/%Y}"
                                                                 if done else "") +
                     " : consommations EffiSmart (courbe 30 min ou factures) et facteurs d'influence du mois."),
        "method": "Consommation de référence ajustée aux conditions de la période de suivi (modèle de l'IPE ou "
                  "moteur de prévision), moins la consommation mesurée ; IPMVP.",
        "instrumentation": ("Sous-compteur de l'équipement" if option == "B" else
                            "Compteur général (courbe de charge)" if option == "C" else
                            "Mesure ponctuelle de puissance et compteur horaire de fonctionnement") +
                           " ; vérifier l'étalonnage et la continuité des données.",
        "valuation": f"Prix effectivement payés : {prices_text}." if prices_text else "",
        "reporting_period": "12 mois après la mise en œuvre, puis suivi annuel.",
        "adjustments": adjustments,
        "responsibilities": f"Responsable de l'action : {action.owner}." if action.owner else "",
        "precision": "±10 % sur les économies, à un niveau de confiance de 90 % (critère « 90/10 » de l'IPMVP).",
        "budget": f"Moins de {fr(budget)} € par an (10 % des économies annuelles attendues).",
        "reports": "Rapport annuel : référence ajustée, consommation mesurée, économies (kWh, €, CO₂), écarts "
                   "expliqués ; joint au rapport trimestriel du cabinet.",
        "quality": "Contrôle de cohérence des données (recoupement des compteurs), relecture par un second "
                   "intervenant, traçabilité des ajustements.",
    }
    return {"values": suggestion, "reasoning": reasoning, "option": option}


def save_mv_plan(db: Session, repo: TenantRepository, user: User, action_id: int, values: dict) -> EnergyAction:
    _require_editor(user)
    action = repo.get_action(action_id)
    allowed = {key for key, _ in MV_POINTS}
    if set(values) - allowed:
        raise ActionPlanError("Point du plan de M&V inconnu.")
    action.mv_plan = {k: (v or "").strip() for k, v in values.items() if (v or "").strip()} or None
    action.updated_by, action.updated_at = user.id, utcnow()
    db.commit()
    return action


@dataclass
class Verification:
    kind: str  # MEASURED (mesure avant / après validée), IPE (IPE avant / après), NONE
    text: str
    before: float | None = None
    after: float | None = None
    unit: str | None = None


def verification(db: Session, action: EnergyAction, weather: WeatherProvider | None) -> Verification:
    """Vérification des économies d'une action réalisée : mesure avant / après validée (F1) ou IPE de vérification."""
    if action.recommendation_id:
        rec = db.get(Recommendation, action.recommendation_id)
        snapshot = rec.savings_snapshot if rec is not None and rec.savings_validated_at else None
        if snapshot:
            return Verification("MEASURED", f"Économies mesurées et validées (IPMVP option C) : {fr(snapshot['annualized_kwh'])} "
                                            f"kWh/an, {fr(snapshot['annualized_eur'])} €/an.",
                                after=snapshot["annualized_kwh"], unit="kWh/an")
    definition = db.get(IpeDefinition, action.ipe_definition_id) if action.ipe_definition_id else None
    if definition is None or action.done_on is None:
        return Verification("NONE", "Reliez l'action à un IPE (ou à une recommandation mesurée) pour vérifier ses "
                                    "économies.")
    done = month_start(action.done_on)
    before_months = [add_months(done, -k) for k in range(12, 0, -1)]
    last = ipe_definitions.last_complete_month()
    after_months = []
    month = add_months(done, 1)
    while month <= last and len(after_months) < 12:
        after_months.append(month)
        month = add_months(month, 1)
    if len(after_months) < 2:
        return Verification("NONE", "Moins de 2 mois complets depuis la réalisation : vérification à venir.")
    before, _ = ipe_definitions.period_value(db, definition, before_months, weather)
    after, used = ipe_definitions.period_value(db, definition, after_months, weather)
    if before is None or after is None:
        return Verification("NONE", "Données insuffisantes pour comparer l'IPE avant et après.")
    change = (after - before) / before if before else 0.0
    return Verification("IPE", f"IPE « {definition.name} » : {fr(before, 2)} avant, {fr(after, 2)} sur les {used} mois "
                               f"suivant la réalisation ({'+' if change > 0 else ''}{fr(change * 100, 1)} %). "
                               "Un IPE intègre aussi les autres évolutions du site.", before, after)


# --- Note de synthèse pour la direction (SP6 ; énergieSIM : porter son plan auprès de la direction) ----------


def _e(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _signed(value: float) -> str:
    """Montant au format français, signe moins typographique (−2 707)."""
    return fr(value).replace("-", "−")


def director_note(org: Organization, summary: PlanSummary, evaluated: list[tuple[EnergyAction, ActionEconomics]],
                  params: dict, bill_eur: float | None, weight, signer: str) -> str:
    """Note autonome (HTML imprimable) : les chiffres du plan et les arguments par enjeu."""
    medium = summary.medium
    rows = []
    for action, econ in sorted(evaluated, key=lambda item: -item[1].score):
        if action.horizon == "MEMO" or action.status == ActionStatus.ABANDONED:
            continue
        trb = "immédiat" if econ.with_cee.immediate else (f"{fr(econ.with_cee.trb, 1)} an(s)"
                                                           if econ.with_cee.trb is not None else "—")
        rows.append(f"<tr><td>{_e(action.title)}<br><small>{_e(action.site.name)} · {_e(HORIZONS[action.horizon])} · "
                    f"{_e(PRIORITIES[action.priority])}</small></td><td>{fr(econ.saved_kwh / 1000, 1)}</td>"
                    f"<td>{fr(econ.without_cee.annual_net)}</td><td>{fr(econ.investment)}</td><td>{trb}</td>"
                    f"<td>{_signed(econ.with_cee.npv)}</td><td>{econ.score}</td></tr>")
    irr = medium.with_cee.irr
    args = [
        ("Économie", f"{fr(medium.gain_eur)} € HT économisés chaque année"
                     + (f", soit {fr(medium.bill_share * 100, 1)} % de la facture énergétique" if medium.bill_share else "")
                     + (f" et environ {fr(weight.ebitda_gain(medium.gain_eur) * 100, 1)} % d'EBE en plus"
                        if weight is not None and weight.ebitda_gain(medium.gain_eur) else "") + "."),
        ("Rentabilité", f"Investissement de {fr(medium.investment)} € HT, dont {fr(medium.cee_eur)} € de CEE attendus ; "
                        f"temps de retour brut {fr(medium.with_cee.trb, 1) + ' an(s)' if medium.with_cee.trb is not None else '—'}, "
                        f"VAN sur {params['horizon_years']} ans de {_signed(medium.with_cee.npv)} € au taux de "
                        f"{fr(params['discount_rate'] * 100, 1)} %"
                        + (f", TRI de {fr(irr * 100, 1)} %" if irr is not None else "") + "."),
        ("Stratégie", "Moins d'énergie consommée, c'est moins d'exposition aux hausses de prix de l'énergie et du CO₂."),
        ("Environnement", f"{fr(medium.saved_kgco2e / 1000, 1)} tCO₂e évitées par an."),
    ]
    cobenefits = defaultdict(int)
    for action, _ in evaluated:
        for code in action.cobenefits or []:
            cobenefits[code] += 1
    for code, count in sorted(cobenefits.items(), key=lambda item: -item[1]):
        args.append(("Au-delà de l'énergie", f"{COBENEFITS[code]} ({count} action(s))."))
    args_html = "".join(f"<li><b>{_e(title)}</b> : {_e(text)}</li>" for title, text in args)
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Plan d'actions énergie — {_e(org.name)}</title>
<style>body{{font-family:Arial,sans-serif;margin:32px;color:#111;max-width:960px}}h1{{font-size:22px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:right}}
td:first-child,th:first-child{{text-align:left}}.kpi{{display:flex;gap:16px;margin:16px 0}}
.kpi div{{border:1px solid #ccc;border-radius:6px;padding:10px 14px}}.kpi b{{display:block;font-size:20px}}
small{{color:#666}}</style></head><body>
<h1>Plan d'actions de performance énergétique — {_e(org.name)}</h1>
<p>Note pour la direction, préparée par {_e(signer)} le {today_local():%d/%m/%Y}. Chiffres EffiSmart : consommations des
12 derniers mois au prix payé ; investissements et économies estimés, à confirmer par devis.</p>
<div class="kpi"><div><b>{fr(medium.saved_kwh / 1000, 1)} MWh/an</b>économisés</div><div><b>{fr(medium.gain_eur)} € HT/an</b>de gains</div>
<div><b>{fr(medium.investment)} € HT</b>investis</div><div><b>{fr(medium.saved_kgco2e / 1000, 1)} tCO₂e/an</b>évitées</div></div>
<h2>Pourquoi agir</h2><ul>{args_html}</ul>
<h2>Actions retenues</h2><table><tr><th>Action</th><th>MWh/an</th><th>€ HT/an</th><th>Investissement €</th><th>Retour (CEE inclus)</th><th>VAN €</th><th>Note /100</th></tr>
{''.join(rows)}</table>
<p><small>Temps de retour brut = investissement / gain annuel net. VAN et TRI sur {params['horizon_years']} ans (au plus la durée
de vie de chaque équipement), taux d'actualisation {fr(params['discount_rate'] * 100, 1)} %, évolution du prix de l'énergie
{fr(params['price_escalation'] * 100, 1)} %/an. CEE valorisés {fr(params['cee_price_eur_mwh'], 2)} € par MWh cumac : l'accord avec
l'obligé (rôle actif et incitatif) doit être signé avant toute commande.</small></p></body></html>"""
