"""R1 — Bilan énergétique d'un site ou d'une organisation (formation PRO-REFEI, SP2 et SP5 ; guide ComptIAA).

1. Bilan par énergie sur les 12 derniers mois : consommation (kWh), coût (€ HT, abonnement compris, au contrat de
   fourniture F6, à défaut au prix indicatif), émissions (facteurs ADEME), parts de chaque énergie dans la
   consommation et dans la facture ; ratios kWh/m² et par unité produite.
2. Poids économique de l'énergie (SP5) : facture / chiffre d'affaires (indicateur à suivre à chaque exercice
   quand il dépasse 2 %) et facture / excédent brut d'exploitation, qui traduit l'impact opérationnel direct.
3. Répartition par usage et diagramme de Pareto : chaque part dit sa provenance (calculée par le modèle de
   consommation, estimée par puissance × durée, ou obtenue par différence), comme l'exige un bilan au sens de la
   norme NF EN 16247. Les usages qui cumulent 80 % de la consommation sont proposés comme usages énergétiques
   significatifs (UES, ISO 50001) : une proposition à confirmer par le référent énergie.
4. Achats d'énergie : la puissance souscrite est comparée à la puissance maximale atteinte (moyenne 30 min) ; une
   puissance trop élevée se paie toute l'année, des dépassements se paient cher (ADEME).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdjustmentKind, AdjustmentVariable, DeliveryPoint, Fluid, Organization, Site, User
from app.repositories import TenantRepository
from app.services import ipe, simulator, tariffs
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of, emission_factors_at
from app.services.drift import half_hour_powers
from app.services.energy_data import latest_declared_day
from app.services.validation import fr
from app.timeutils import add_months, month_start

UES_CUMULATIVE_SHARE = 0.80  # règle de Pareto : les usages qui cumulent 80 % de la consommation
UES_MIN_SHARE = 0.10  # à défaut (grande part non répartie) : tout usage d'au moins 10 %
MIN_USAGE_SHARE = 0.005  # en dessous de 0,5 % de la consommation, un usage rejoint les « autres usages »
REVENUE_WATCH_SHARE = 0.02  # SP5 : au-delà de 2 % du chiffre d'affaires, suivre l'indicateur à chaque exercice
COS_PHI = 0.93  # tan φ = 0,4 : seuil de facturation de l'énergie réactive ; kVA ≈ kW / 0,93
POWER_MARGIN = 1.15  # marge pour les pointes au pas 10 min, invisibles dans les moyennes 30 min
C5_STEPS = (3, 6, 9, 12, 15, 18, 24, 30, 36)  # puissances souscrites normalisées jusqu'à 36 kVA


class BalanceError(ValueError):
    pass


# --- 1. Bilan par énergie ---------------------------------------------------------------------------------


@dataclass
class EnergyLine:
    fluid: Fluid
    kwh: float
    eur: float
    kgco2e: float
    n0_kwh: float
    pricing: str  # contrat, partiel ou indicatif

    @property
    def price(self) -> float:
        return self.eur / self.kwh if self.kwh else 0.0


@dataclass
class Balance:
    start: date
    end: date
    lines: list[EnergyLine]
    surface_m2: float | None = None
    production: float | None = None
    production_unit: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def total_kwh(self) -> float:
        return sum(line.kwh for line in self.lines)

    @property
    def total_eur(self) -> float:
        return sum(line.eur for line in self.lines)

    @property
    def total_kgco2e(self) -> float:
        return sum(line.kgco2e for line in self.lines)

    def kwh_share(self, line: EnergyLine) -> float:
        return line.kwh / self.total_kwh if self.total_kwh else 0.0

    def eur_share(self, line: EnergyLine) -> float:
        return line.eur / self.total_eur if self.total_eur else 0.0

    @property
    def kwh_m2(self) -> float | None:
        return self.total_kwh / self.surface_m2 if self.surface_m2 else None

    @property
    def kwh_per_unit(self) -> float | None:
        return self.total_kwh / self.production if self.production else None

    @property
    def eur_per_unit(self) -> float | None:
        return self.total_eur / self.production if self.production else None


def period_end(db: Session, points: list[DeliveryPoint]) -> date | None:
    ids = [dp.id for dp in points]
    consented = [dp.id for dp in points if has_active_consent(db, dp.id)]
    candidates = [d for d in (data_as_of(db, consented) if consented else None, latest_declared_day(db, ids)) if d]
    return max(candidates) if candidates else None


def _balance(db: Session, points: list[DeliveryPoint], end: date | None) -> tuple[date, date, list[EnergyLine]] | None:
    end = end or period_end(db, points)
    if end is None:
        return None
    start = end - timedelta(days=364)
    factors = emission_factors_at(db, end)
    lines = []
    for fluid in Fluid:
        total = tariffs.Costing()
        for dp in (p for p in points if p.fluid == fluid):
            for costing in tariffs.monthly_costing(db, dp, start, end).values():
                total.merge(costing)
        if total.kwh <= 0:
            continue
        pricing = "indicatif" if total.contract_days == 0 else "contrat" if total.contract_days == total.days else "partiel"
        factor = factors.get(fluid)
        lines.append(EnergyLine(fluid, total.kwh, total.eur, total.kwh * factor.factor_kgco2_per_kwh if factor else 0.0,
                                total.n0_kwh, pricing))
    return (start, end, lines) if lines else None


def _production(db: Session, site: Site, start: date, end: date) -> float | None:
    """Production des 12 mois du bilan, si chaque mois est renseigné (variable d'ajustement F7)."""
    first, last = month_start(start), month_start(end)
    months = []
    while first <= last:
        months.append(first)
        first = add_months(first, 1)
    values = dict(db.execute(select(AdjustmentVariable.month, AdjustmentVariable.value).where(
        AdjustmentVariable.site_id == site.id, AdjustmentVariable.kind == AdjustmentKind.PRODUCTION,
        AdjustmentVariable.month.in_(months))).all())
    covered = [m for m in months if m in values]
    if len(covered) < len(months) - 1:  # le mois en cours peut manquer
        return None
    return sum(values.values()) * len(months) / len(covered) if covered else None


def site_balance(db: Session, site: Site, end: date | None = None) -> Balance | None:
    computed = _balance(db, list(site.delivery_points), end)
    if computed is None:
        return None
    start, end, lines = computed
    balance = Balance(start, end, lines, site.surface_m2, _production(db, site, start, end), site.production_unit)
    if any(line.pricing != "contrat" for line in lines):
        balance.notes.append("Une partie des coûts est chiffrée au prix indicatif : saisissez les contrats de "
                             "fourniture (page Suivi mensuel) pour un bilan au prix payé.")
    if any(line.n0_kwh for line in lines):
        balance.notes.append("Une partie des consommations vient des factures (N0), réparties au prorata des jours.")
    return balance


def organization_balance(db: Session, org: Organization, end: date | None = None) -> Balance | None:
    points = [dp for site in org.sites for dp in site.delivery_points]
    computed = _balance(db, points, end)
    if computed is None:
        return None
    start, end, lines = computed
    surface = sum(site.surface_m2 or 0 for site in org.sites) or None
    return Balance(start, end, lines, surface)


# --- 2. Poids économique de l'énergie --------------------------------------------------------------------


@dataclass
class FinancialWeight:
    bill_eur: float
    year: int | None
    revenue_eur: float | None
    ebitda_eur: float | None
    share_revenue: float | None
    share_ebitda: float | None
    messages: list[str] = field(default_factory=list)

    def ebitda_gain(self, savings_eur: float) -> float | None:
        """Progression de l'EBE qu'apporterait une économie annuelle (tout gain sur la facture va à l'EBE)."""
        return savings_eur / self.ebitda_eur if self.ebitda_eur and self.ebitda_eur > 0 else None


def financial_weight(org: Organization, bill_eur: float) -> FinancialWeight | None:
    if not org.revenue_eur and not org.ebitda_eur:
        return None
    share_revenue = bill_eur / org.revenue_eur if org.revenue_eur else None
    share_ebitda = bill_eur / org.ebitda_eur if org.ebitda_eur and org.ebitda_eur > 0 else None
    weight = FinancialWeight(bill_eur, org.finance_year, org.revenue_eur, org.ebitda_eur, share_revenue, share_ebitda)
    if share_revenue is not None:
        weight.messages.append(
            "La facture énergétique dépasse 2 % du chiffre d'affaires : un indicateur à suivre à chaque exercice."
            if share_revenue > REVENUE_WATCH_SHARE else
            "La facture énergétique pèse moins de 2 % du chiffre d'affaires : son poids sur l'EBE reste le plus parlant.")
    if share_ebitda is not None:
        weight.messages.append(
            f"Chaque euro économisé sur l'énergie s'ajoute à l'EBE : une économie de 10 % sur la facture le ferait "
            f"progresser d'environ {fr(share_ebitda * 10, 1)} %.")
    elif org.ebitda_eur is not None and org.ebitda_eur <= 0:
        weight.messages.append("EBE nul ou négatif : toute économie d'énergie améliore directement le résultat.")
    return weight


def set_finances(db: Session, repo: TenantRepository, user: User, organization_id: int, *,
                 revenue_eur: float | None, ebitda_eur: float | None, year: int | None) -> Organization:
    if not ipe.can_enter_variables(user):
        raise ipe.IpePermissionError("Les données financières sont saisies par le responsable énergie ou l'auditeur.")
    org = repo.get_organization(organization_id)
    if revenue_eur is not None and revenue_eur <= 0:
        raise BalanceError("Le chiffre d'affaires doit être positif.")
    org.revenue_eur, org.ebitda_eur, org.finance_year = revenue_eur, ebitda_eur, year
    db.commit()
    return org


# --- 3. Usages et Pareto -----------------------------------------------------------------------------------


@dataclass
class UsageRow:
    usage: str
    label: str
    kwh: float
    eur: float
    kgco2e: float
    share: float
    cumulative: float
    sources: list[str]
    by_fluid: dict[str, float]
    significant: bool = False


def usage_pareto(breakdowns: list[simulator.UsageBreakdown]) -> tuple[list[UsageRow], list[str]]:
    """Usages toutes énergies, du plus au moins consommateur, et usages énergétiques significatifs proposés."""
    kwh: dict[str, float] = defaultdict(float)
    eur: dict[str, float] = defaultdict(float)
    co2: dict[str, float] = defaultdict(float)
    sources: dict[str, set[str]] = defaultdict(set)
    by_fluid: dict[str, dict[str, float]] = defaultdict(dict)
    for b in breakdowns:
        for usage in b.shares:
            value = b.usage_kwh(usage)
            kwh[usage] += value
            eur[usage] += value * b.price
            co2[usage] += value * b.kgco2e_per_kwh
            sources[usage].add(b.sources.get(usage, simulator.SOURCE_ESTIMATED))
            by_fluid[usage][b.fluid.value] = value
    total = sum(kwh.values())
    if total <= 0:
        return [], []
    for usage in [u for u, v in kwh.items() if v < total * MIN_USAGE_SHARE and u != "OTHER"]:
        kwh["OTHER"] = kwh.get("OTHER", 0.0) + kwh.pop(usage)  # part négligeable : regroupée avec les autres usages
        sources.pop(usage, None)
        sources["OTHER"] = sources["OTHER"] or {simulator.SOURCE_REMAINDER}
    rows, cumulative = [], 0.0
    for usage in sorted(kwh, key=lambda u: (u == "OTHER", -kwh[u])):
        cumulative += kwh[usage] / total
        rows.append(UsageRow(usage, simulator.USAGE_LABELS.get(usage, usage), kwh[usage], eur[usage], co2[usage],
                             kwh[usage] / total, cumulative, sorted(sources[usage]), by_fluid[usage]))
    notes = []
    real = [r for r in rows if r.usage != "OTHER"]
    reached = 0.0
    for row in real:
        if reached >= UES_CUMULATIVE_SHARE:
            break
        row.significant = True
        reached += row.share
    other = next((r for r in rows if r.usage == "OTHER"), None)
    if reached < UES_CUMULATIVE_SHARE:
        for row in real:
            row.significant = row.share >= UES_MIN_SHARE
        notes.append(f"Les usages identifiés couvrent {round(reached * 100)} % de la consommation : le reste n'est pas "
                     "réparti. Sont proposés les usages d'au moins 10 % ; affinez la répartition (graphe des "
                     "équipements, sous-comptage).")
    else:
        notes.append("Usages énergétiques significatifs proposés (règle de Pareto) : les usages qui cumulent 80 % de "
                     "la consommation. À confirmer par le référent énergie : l'ISO 50001 lui laisse le choix des "
                     "critères (part de la consommation, potentiel d'amélioration).")
    if other is not None and other.share > 0.3:
        notes.append(f"{round(other.share * 100)} % de la consommation reste en « autres usages » : un plan de "
                     "comptage plus fin la rendrait lisible.")
    return rows, notes


# --- 4. Achats d'énergie : puissance souscrite --------------------------------------------------------------


@dataclass
class PowerCheck:
    delivery_point: DeliveryPoint
    subscribed_kva: float
    monthly_max_kw: dict[str, float]
    max_kw: float
    status: str  # OVER, TIGHT, OVERSIZED, OK
    suggested_kva: float | None
    reasoning: list[str]

    @property
    def max_kva(self) -> float:
        return self.max_kw / COS_PHI

    @property
    def months_over(self) -> int:
        return sum(1 for kw in self.monthly_max_kw.values() if kw / COS_PHI > self.subscribed_kva)

    def yearly_gain(self, eur_per_kva_year: float) -> float:
        if self.status != "OVERSIZED" or not self.suggested_kva:
            return 0.0
        return max(0.0, self.subscribed_kva - self.suggested_kva) * eur_per_kva_year


STATUS_LABELS = {"OVER": ("Dépassements probables", "danger"), "TIGHT": ("Marge faible", "warning"),
                 "OVERSIZED": ("Puissance surdimensionnée", "warning"), "OK": ("Adaptée", "success")}


def _round_power(kva: float) -> float:
    if kva <= C5_STEPS[-1]:
        return float(next(step for step in C5_STEPS if step >= kva))
    return float(math.ceil(kva))


def power_checks(db: Session, site: Site, end: date | None = None) -> list[PowerCheck]:
    """Puissance souscrite des points électricité à courbe de charge, comparée aux 12 derniers mois."""
    checks = []
    for dp in site.delivery_points:
        if dp.fluid != Fluid.ELEC or not dp.subscribed_power_kva or not has_active_consent(db, dp.id):
            continue
        last = end or data_as_of(db, [dp.id])
        if last is None:
            continue
        monthly: dict[str, float] = defaultdict(float)
        for moment, kw in half_hour_powers(db, dp.id, last - timedelta(days=364), last):
            key = moment.strftime("%Y-%m")
            monthly[key] = max(monthly[key], kw)
        if not monthly:
            continue
        max_kw = max(monthly.values())
        subscribed = dp.subscribed_power_kva
        max_kva = max_kw / COS_PHI
        reasoning = [
            f"Puissance maximale atteinte sur 12 mois : {fr(max_kw)} kW en moyenne 30 min, soit environ {fr(max_kva)} "
            f"kVA (cos φ de {fr(COS_PHI, 2)} supposé, seuil de facturation de l'énergie réactive).",
            f"Puissance souscrite : {fr(subscribed)} kVA ; le compteur mesure la puissance au pas 10 min, dont les "
            "pointes dépassent les moyennes 30 min : une marge de 15 % est conservée.",
        ]
        suggested = None
        if max_kva > subscribed:
            status = "OVER"
            reasoning.append("La puissance atteinte dépasse la puissance souscrite : des dépassements sont probables "
                             "et coûteux. Vérifier les composantes de dépassement sur les factures et réajuster.")
            suggested = _round_power(max_kva * POWER_MARGIN)
        elif max_kva * POWER_MARGIN > subscribed:
            status = "TIGHT"
            reasoning.append("Marge inférieure à 15 % : les pointes au pas 10 min peuvent dépasser la puissance "
                             "souscrite. Surveiller les dépassements sur les factures.")
        elif max_kva * POWER_MARGIN < subscribed * 0.85:
            status = "OVERSIZED"
            suggested = _round_power(max_kva * POWER_MARGIN)
            reasoning.append(f"Puissance souscrite supérieure au besoin : environ {fr(suggested)} kVA suffiraient, "
                             "marge comprise. La part fixe de l'acheminement se paie au kVA souscrit toute l'année : "
                             "à vérifier avec le fournisseur avant toute modification (délais, coût d'intervention).")
        else:
            status = "OK"
            reasoning.append("La puissance souscrite est cohérente avec les appels de puissance des 12 derniers mois.")
        checks.append(PowerCheck(dp, subscribed, dict(sorted(monthly.items())), max_kw, status, suggested, reasoning))
    return checks
