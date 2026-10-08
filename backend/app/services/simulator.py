"""F9 — Simulateur d'économies : gains énergétiques et financiers des actions recommandées.

1. Consommation annuelle du site par énergie : 12 derniers mois, compteurs (N1) et factures (N0).
2. Répartition par usage :
   - chauffage et refroidissement : part liée au climat, d'après le modèle de consommation (F11) ;
   - autres usages : énergie estimée des équipements du graphe P2 qui les servent (puissance nominale × durée
     type de fonctionnement annuelle) ; le reste, non modélisé (bureautique, prises…), en « autres usages » ;
   - sans modèle ni graphe : « autres usages ». L'auditeur ajuste chaque part.
3. Actions : gestes types de la bibliothèque (économie relative sur un usage) et recommandations validées
   (leur gain estimé). Sur un même usage, les gestes se cumulent sans double compte : 1 − Π(1 − économie).
4. Gains : kWh, € au prix effectivement payé (contrat F6, à défaut prix indicatif), tCO₂e (facteurs ADEME),
   investissement indicatif et temps de retour simple.
5. Le scénario retenu alimente la trajectoire Décret Tertiaire « avec actions » (F11).

Les valeurs de la bibliothèque sont des ordres de grandeur indicatifs, à ajuster par l'auditeur. Une simulation
n'est pas une mesure : la mesure avant / après (F1) vient une fois les actions appliquées.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    ActionTemplate,
    Fluid,
    Recommendation,
    ReviewStatus,
    Role,
    SavingsScenario,
    Site,
    User,
)
from app.providers.weather import WeatherProvider
from app.repositories import TenantRepository
from app.services import assets, consumption_model, tariffs
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of, emission_factors_at, estimated_price
from app.services.energy_data import combined_daily, latest_declared_day

logger = logging.getLogger(__name__)

USAGE_LABELS = {
    "HEATING": "Chauffage", "COOLING": "Refroidissement", "DHW": "Eau chaude sanitaire",
    "VENTILATION": "Ventilation", "LIGHTING": "Éclairage", "IT": "Informatique", "PROCESS": "Process",
    "REFRIGERATION": "Froid alimentaire", "OTHER": "Autres usages",
}
CLIMATE_USAGES = ("HEATING", "COOLING")
# Durée type de fonctionnement à pleine puissance par an (heures), pour estimer l'énergie d'un usage.
USAGE_HOURS = {"LIGHTING": 2500, "VENTILATION": 3000, "IT": 5000, "PROCESS": 2000, "REFRIGERATION": 5000,
               "DHW": 1500, "OTHER": 2000}
# (code, titre, usage, économie relative, investissement €/m², investissement fixe €, note)
DEFAULT_LIBRARY = [
    ("CHAUF_CONSIGNE", "Baisser la consigne de chauffage de 1 °C", "HEATING", 0.07, 0, 0,
     "Ordre de grandeur couramment admis : environ 7 % du chauffage par degré de consigne."),
    ("CHAUF_REDUIT", "Réduit de nuit et de week-end du chauffage", "HEATING", 0.12, 2, 0, None),
    ("CHAUF_ROBINETS", "Robinets thermostatiques sur les émetteurs", "HEATING", 0.08, 4, 0, None),
    ("CHAUF_CALORIFUGE", "Calorifugeage des réseaux de distribution", "HEATING", 0.05, 2, 0, None),
    ("CLIM_CONSIGNE", "Consigne de climatisation relevée à 26 °C", "COOLING", 0.15, 0, 0, None),
    ("ECL_LED", "Éclairage LED", "LIGHTING", 0.50, 15, 0, None),
    ("ECL_DETECTION", "Détection de présence et gradation de l'éclairage", "LIGHTING", 0.25, 6, 0, None),
    ("VENT_PROG", "Programmation horaire de la ventilation (CTA)", "VENTILATION", 0.30, 1, 0, None),
    ("IT_VEILLE", "Extinction des postes et des veilles la nuit", "IT", 0.10, 0, 0, None),
    ("ECS_TEMP", "Température d'eau chaude sanitaire ajustée, dans le respect des règles sanitaires", "DHW", 0.08,
     0, 0, None),
    ("PROCESS_ARRET", "Arrêt des machines hors production", "PROCESS", 0.10, 0, 0, None),
    ("FROID_FERMETURES", "Rideaux et fermetures des zones froides", "REFRIGERATION", 0.10, 3, 0, None),
]
FLUID_NAMES = {Fluid.ELEC: "électricité", Fluid.GAS: "gaz"}


class SimulatorError(ValueError):
    pass


class SimulatorPermissionError(PermissionError):
    pass


# --- Bibliothèque de gestes types -------------------------------------------------------------------


def ensure_library(db: Session) -> int:
    """Crée les gestes de la bibliothèque commune qui manquent (idempotent)."""
    existing = set(db.scalars(select(ActionTemplate.code).where(ActionTemplate.auditor_id.is_(None))))
    created = 0
    for code, title, usage, pct, eur_m2, eur, notes in DEFAULT_LIBRARY:
        if code not in existing:
            db.add(ActionTemplate(code=code, title=title, usage=usage, savings_pct=pct, investment_eur_m2=eur_m2,
                                  investment_eur=eur, notes=notes))
            created += 1
    db.commit()
    return created


def library(db: Session, user: User) -> list[ActionTemplate]:
    """Gestes actifs : bibliothèque commune et gestes propres au cabinet de l'utilisateur."""
    own = ActionTemplate.auditor_id == user.auditor_id if user.auditor_id else ActionTemplate.auditor_id.is_(None)
    return list(db.scalars(select(ActionTemplate).where(
        ActionTemplate.active.is_(True), or_(ActionTemplate.auditor_id.is_(None), own))
        .order_by(ActionTemplate.usage, ActionTemplate.title)))


def can_edit(user: User) -> bool:
    return user.role == Role.AUDITOR and user.auditor_id is not None


def add_template(db: Session, user: User, *, title: str, usage: str, savings_pct: float,
                 investment_eur_m2: float = 0.0, investment_eur: float = 0.0, notes: str | None = None) -> ActionTemplate:
    """Geste propre au cabinet de l'auditeur (la bibliothèque commune reste inchangée)."""
    if not can_edit(user):
        raise SimulatorPermissionError("La bibliothèque de gestes est enrichie par l'auditeur.")
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise SimulatorError("Intitulé obligatoire (200 caractères au plus).")
    if usage not in USAGE_LABELS:
        raise SimulatorError("Usage inconnu.")
    if not 0 < savings_pct < 1:
        raise SimulatorError("L'économie est une part de la consommation de l'usage, entre 0 et 100 %.")
    if investment_eur_m2 < 0 or investment_eur < 0:
        raise SimulatorError("L'investissement doit être positif ou nul.")
    template = ActionTemplate(auditor_id=user.auditor_id, code=f"CABINET_{user.auditor_id}", title=title, usage=usage,
                              savings_pct=savings_pct, investment_eur_m2=investment_eur_m2,
                              investment_eur=investment_eur, notes=(notes or "").strip() or None)
    db.add(template)
    db.commit()
    return template


# --- Répartition par usage --------------------------------------------------------------------------


@dataclass
class UsageBreakdown:
    fluid: Fluid
    total_kwh: float
    shares: dict[str, float]  # part de la consommation de l'énergie, par usage
    price: float  # €/kWh effectivement payé (hors abonnement)
    price_source: str
    kgco2e_per_kwh: float
    notes: list[str] = field(default_factory=list)

    def usage_kwh(self, usage: str) -> float:
        return self.total_kwh * self.shares.get(usage, 0.0)


def _graph_energy(site: Site, points: list, db: Session) -> dict[str, float]:
    """Énergie annuelle estimée par usage hors climat : puissance nominale × durée type de fonctionnement."""
    graph = assets.load_site_graph(db, site.id)
    weights: dict[str, float] = defaultdict(float)
    for dp in points:
        meter = graph.meter_for(dp.id)
        for eq in graph.direct_consumers(meter.id) if meter else []:
            usages = [u.category for u in graph.usages_served(eq.id) if u.category not in CLIMATE_USAGES]
            if eq.power_kw and usages:
                for usage in usages:
                    key = usage if usage in USAGE_LABELS else "OTHER"
                    weights[key] += eq.power_kw / len(usages) * USAGE_HOURS.get(key, USAGE_HOURS["OTHER"])
    return weights


def breakdown(db: Session, site: Site, weather: WeatherProvider, as_of: date | None = None) -> list[UsageBreakdown]:
    """Consommation des 12 derniers mois du site par énergie, et sa répartition estimée par usage."""
    results = []
    for fluid in Fluid:
        points = [dp for dp in site.delivery_points if dp.fluid == fluid]
        if not points:
            continue
        end = as_of or data_as_of(db, [dp.id for dp in points]) or latest_declared_day(db, [dp.id for dp in points])
        if end is None:
            continue
        start = end - timedelta(days=364)
        total = sum(kwh for dp in points for kwh, _ in combined_daily(db, dp, start, end).values())
        if total <= 0:
            continue
        notes = []
        shares: dict[str, float] = {}
        consented = [dp for dp in points if has_active_consent(db, dp.id)]
        model = consumption_model.train(db, consented, end, weather) if consented else None
        if model is not None:
            heating, cooling, rest = model.climate_parts(end + timedelta(days=1))
            normal = heating + cooling + rest
            if normal > 0:
                shares["HEATING"], shares["COOLING"] = heating / normal, cooling / normal
                notes.append(f"Chauffage {round(shares['HEATING'] * 100)} %, refroidissement "
                             f"{round(shares['COOLING'] * 100)} % : part liée au climat d'après le modèle de "
                             "consommation (année de météo normale comparée à une météo neutre).")
        else:
            notes.append("Moins de 12 mois d'historique : part liée au climat non estimée (mode dégradé), à saisir.")
        rest_share = max(0.0, 1 - sum(shares.values()))
        estimated = _graph_energy(site, points, db)
        if estimated:
            # Au plus le reste de la consommation : au-delà, les estimations sont réduites en proportion.
            scale = min(1.0, rest_share * total / sum(estimated.values()))
            for usage, kwh in estimated.items():
                shares[usage] = shares.get(usage, 0.0) + kwh * scale / total
            shares["OTHER"] = shares.get("OTHER", 0.0) + max(0.0, 1 - sum(shares.values()))
            notes.append("Autres usages : puissance nominale des équipements du graphe × durée type de fonctionnement "
                         "(éclairage 2 500 h, ventilation 3 000 h, informatique 5 000 h par an…) ; le reste, non "
                         "modélisé (bureautique, prises…), en « autres usages ». À ajuster.")
        else:
            shares["OTHER"] = shares.get("OTHER", 0.0) + rest_share
            notes.append("Aucun équipement à puissance connue dans le graphe : reste en « autres usages ».")
        price, source = tariffs.effective_price(db, points, start, end, fluid)
        factor = emission_factors_at(db, end).get(fluid)
        results.append(UsageBreakdown(fluid, total, {u: s for u, s in shares.items() if s > 0}, price, source,
                                      factor.factor_kgco2_per_kwh if factor else 0.0, notes))
    return results


# --- Simulation ------------------------------------------------------------------------------------


def simulate(site: Site, breakdowns: list[UsageBreakdown], templates: list[ActionTemplate],
             recommendations: list[Recommendation], shares_override: dict | None = None) -> dict:
    """Gains annuels d'un ensemble d'actions. `shares_override` : {énergie: {usage: part}} ajustés à la main."""
    by_fluid = {b.fluid: b for b in breakdowns}
    if shares_override:
        for fluid_value, shares in shares_override.items():
            fluid = Fluid(fluid_value)
            if fluid in by_fluid:
                total = sum(shares.values())
                by_fluid[fluid].shares = {u: s / total for u, s in shares.items() if s > 0} if total > 0 else {}
    lines, invested = [], 0.0
    combined: dict[tuple[Fluid, str], float] = defaultdict(float)  # Σ −ln(1 − économie) par usage
    for template in templates:
        concerned = [b for b in by_fluid.values() if b.usage_kwh(template.usage) > 0]
        # Sans l'usage visé sur le site, le geste est sans objet : ni gain, ni investissement.
        investment = (template.investment_eur_m2 * (site.surface_m2 or 0.0) + template.investment_eur) if concerned else 0.0
        invested += investment
        standalone = sum(b.usage_kwh(template.usage) * template.savings_pct for b in concerned)
        lines.append({"kind": "geste", "id": template.id, "title": template.title, "usage": template.usage,
                      "saved_kwh": round(standalone), "saved_eur": round(sum(
                          b.usage_kwh(template.usage) * template.savings_pct * b.price for b in concerned)),
                      "investment_eur": round(investment),
                      "note": None if concerned else "usage absent de la répartition : sans effet"})
        for b in concerned:
            combined[(b.fluid, template.usage)] += -math.log(1 - template.savings_pct)
    saved = defaultdict(float)
    for (fluid, usage), weight in combined.items():
        saved[fluid] += by_fluid[fluid].usage_kwh(usage) * (1 - math.exp(-weight))
    for rec in recommendations:
        fluid = rec.delivery_point.fluid if rec.delivery_point else Fluid.ELEC
        gain = rec.gain_kwh or 0.0
        saved[fluid] += gain
        price = by_fluid[fluid].price if fluid in by_fluid else estimated_price(fluid)
        lines.append({"kind": "recommandation", "id": rec.id, "title": rec.title, "usage": None,
                      "saved_kwh": round(gain), "saved_eur": round(gain * price if gain else rec.gain_eur or 0.0),
                      "investment_eur": 0, "note": "gain estimé de la recommandation validée"})
    saved_kwh = sum(saved.values())
    saved_eur = sum(kwh * (by_fluid[f].price if f in by_fluid else estimated_price(f)) for f, kwh in saved.items())
    saved_eur += sum(rec.gain_eur or 0.0 for rec in recommendations if not rec.gain_kwh)  # décalage de charge : en €
    saved_co2 = sum(kwh * by_fluid[f].kgco2e_per_kwh for f, kwh in saved.items() if f in by_fluid)
    total_kwh = sum(b.total_kwh for b in by_fluid.values())
    return {
        "lines": lines,
        "by_fluid": {f.value: round(kwh) for f, kwh in saved.items()},
        "saved_kwh": round(saved_kwh), "saved_eur": round(saved_eur), "saved_kgco2e": round(saved_co2),
        "investment_eur": round(invested),
        "payback_years": round(invested / saved_eur, 1) if saved_eur > 0 and invested > 0 else None,
        "share_of_consumption": round(saved_kwh / total_kwh, 4) if total_kwh else 0.0,
        "consumption_kwh": round(total_kwh),
    }


def validated_recommendations(db: Session, site_id: int) -> list[Recommendation]:
    """Recommandations validées et pas encore appliquées : les actions décidées qui restent à mener."""
    return list(db.scalars(select(Recommendation).where(Recommendation.site_id == site_id,
                                                        Recommendation.status == ReviewStatus.VALIDATED)
                           .order_by(Recommendation.id)))


def save_scenario(db: Session, repo: TenantRepository, user: User, site_id: int, *, name: str,
                  template_ids: list[int], recommendation_ids: list[int], results: dict,
                  usage_shares: dict | None = None, retained: bool = False) -> SavingsScenario:
    """Enregistre un scénario ; le scénario retenu (un par site) alimente la trajectoire avec actions."""
    if not can_edit(user):
        raise SimulatorPermissionError("Les scénarios sont enregistrés par l'auditeur.")
    site = repo.get_site(site_id)
    name = (name or "").strip()
    if not name or len(name) > 200:
        raise SimulatorError("Nom du scénario obligatoire (200 caractères au plus).")
    if not template_ids and not recommendation_ids:
        raise SimulatorError("Choisissez au moins une action.")
    if retained:
        for other in db.scalars(select(SavingsScenario).where(SavingsScenario.site_id == site.id,
                                                              SavingsScenario.retained.is_(True))):
            other.retained = False
    scenario = SavingsScenario(organization_id=site.organization_id, site_id=site.id, name=name,
                               template_ids=list(template_ids), recommendation_ids=list(recommendation_ids),
                               usage_shares=usage_shares, results=results, retained=retained, created_by=user.id)
    db.add(scenario)
    db.commit()
    return scenario
