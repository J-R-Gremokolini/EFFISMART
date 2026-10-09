"""R2 — Rentabilité des actions d'économies d'énergie (formation PRO-REFEI, SP4 ; outil énergieSIM de l'ATEE).

Données d'entrée d'une action (SP4) :
- RAE : réduction annuelle du coût énergétique (économies d'énergie × prix payé) ;
- ACA : variation du coût de maintenance et autres coûts récurrents (ici `recurring_eur`, gain > 0, coût < 0) ;
- I : investissement ; DV : durée de vie de l'équipement.

Indicateurs :
- économie annuelle nette AEN = RAE + gain récurrent (RAE − ACA dans la formation) ;
- temps de retour brut TRB = I / AEN, « immédiat » sans investissement ;
- valeur actuelle nette VAN = Σ Fi × (1 + k)^−i − I, sur min(DV, durée d'analyse) années, au taux d'actualisation k
  (5 à 12 % pour les investissements industriels ; 10 % par défaut, comme énergieSIM), avec un scénario
  d'évolution du prix de l'énergie (constant par défaut) ;
- taux de rentabilité interne TRI : le taux qui annule la VAN ;
- temps de retour actualisé : première année où le cumul des flux actualisés devient positif.
Chaque indicateur est donné sans et avec les certificats d'économies d'énergie (CEE) : kWh cumac × prix du
MWh cumac, déduits de l'investissement (aide à l'investissement). Les CEE supposent le rôle actif et incitatif
de l'obligé AVANT tout engagement (signature du devis) : la plateforme le rappelle.

Lecture du TRB (SP4) : moins d'1 an, action prioritaire ; de 1 à 3 ans, rentabilité intéressante ; au-delà,
les économies d'énergie ne sont souvent plus le seul critère de décision.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULTS = {
    "discount_rate": 0.10,  # taux d'actualisation
    "horizon_years": 10,  # durée d'analyse
    "price_escalation": 0.0,  # évolution annuelle du prix de l'énergie (scénario constant)
    "cee_price_eur_mwh": 7.0,  # valorisation des CEE, € HT par MWh cumac : à mettre à jour (cours, offre de l'obligé)
}
PRICE_SCENARIOS = {"Constant": 0.0, "Hausse modérée (+2 %/an)": 0.02, "Hausse forte (+5 %/an)": 0.05}
LIMITS = {"discount_rate": (0.0, 0.30), "horizon_years": (1, 30), "price_escalation": (-0.10, 0.20),
          "cee_price_eur_mwh": (0.0, 50.0)}
MAX_IRR = 10.0  # 1 000 % : au-delà, le TRI n'a plus de sens (investissement quasi nul)


def params_of(org) -> dict:
    """Paramètres de l'organisation, complétés par les valeurs par défaut."""
    return {**DEFAULTS, **(getattr(org, "economics", None) or {})}


def check_params(values: dict) -> dict:
    clean = {}
    for key, (low, high) in LIMITS.items():
        if key not in values or values[key] is None:
            continue
        value = float(values[key])
        if not low <= value <= high:
            raise ValueError(f"Paramètre « {key} » hors bornes ({low} à {high}).")
        clean[key] = int(value) if key == "horizon_years" else value
    return clean


def payback_class(trb: float | None, immediate: bool = False) -> tuple[str, str]:
    """Lecture du temps de retour brut (SP4) : (libellé, ton)."""
    if immediate:
        return "Retour immédiat : prioritaire", "success"
    if trb is None:
        return "Pas de retour sur investissement", "danger"
    if trb < 1:
        return "ROI < 1 an : prioritaire", "success"
    if trb <= 3:
        return "ROI de 1 à 3 ans : rentabilité intéressante", "success"
    return "ROI > 3 ans : d'autres critères entrent en jeu", "warning"


@dataclass
class Indicators:
    investment: float
    annual_net: float  # AEN, première année
    trb: float | None  # années ; None si jamais rentabilisé
    immediate: bool  # gain sans investissement
    npv: float
    irr: float | None
    discounted_payback: float | None
    flows: list[float] = field(default_factory=list)  # année 0 à n

    @property
    def payback_label(self) -> tuple[str, str]:
        return payback_class(self.trb, self.immediate)


def cash_flows(investment: float, energy_gain_eur: float, recurring_eur: float, years: int,
               escalation: float) -> list[float]:
    """Flux annuels : −I en année 0, puis gain énergie (indexé sur le prix) + gain récurrent."""
    flows = [-investment]
    for year in range(1, years + 1):
        flows.append(energy_gain_eur * (1 + escalation) ** (year - 1) + recurring_eur)
    return flows


def npv(flows: list[float], rate: float) -> float:
    return sum(f / (1 + rate) ** i for i, f in enumerate(flows))


def irr(flows: list[float]) -> float | None:
    """TRI par dichotomie ; None si la VAN ne s'annule pas (jamais rentable, ou rien à rentabiliser)."""
    if not flows or flows[0] >= 0 or sum(flows[1:]) <= -flows[0]:
        return None
    low, high = -0.99, MAX_IRR
    if npv(flows, high) > 0:
        return MAX_IRR
    for _ in range(200):
        middle = (low + high) / 2
        if npv(flows, middle) > 0:
            low = middle
        else:
            high = middle
    return round((low + high) / 2, 4)


def discounted_payback(flows: list[float], rate: float) -> float | None:
    cumulative = flows[0]
    if cumulative >= 0:
        return 0.0
    for year in range(1, len(flows)):
        step = flows[year] / (1 + rate) ** year
        if cumulative + step >= 0 and step > 0:
            return round(year - 1 + (-cumulative) / step, 2)
        cumulative += step
    return None


def indicators(investment: float, energy_gain_eur: float, recurring_eur: float, lifetime: int, params: dict,
               cee_eur: float = 0.0) -> Indicators:
    """Indicateurs d'une action ou d'un groupe d'actions ; `cee_eur` est déduit de l'investissement."""
    net_investment = max(0.0, investment - cee_eur)
    years = max(1, min(int(lifetime or params["horizon_years"]), int(params["horizon_years"])))
    flows = cash_flows(net_investment, energy_gain_eur, recurring_eur, years, params["price_escalation"])
    annual = energy_gain_eur + recurring_eur
    immediate = net_investment <= 0 and annual > 0
    trb = None if annual <= 0 else (0.0 if net_investment <= 0 else round(net_investment / annual, 2))
    return Indicators(net_investment, annual, trb, immediate, npv(flows, params["discount_rate"]),
                      None if immediate else irr(flows), discounted_payback(flows, params["discount_rate"]), flows)


def cee_value_eur(kwh_cumac: float | None, params: dict) -> float:
    return (kwh_cumac or 0.0) / 1000 * params["cee_price_eur_mwh"]


def cumulative(flows: list[float], rate: float | None = None) -> list[float]:
    """Cumul des flux, bruts ou actualisés (graphique des flux du plan, comme énergieSIM)."""
    total, out = 0.0, []
    for i, flow in enumerate(flows):
        total += flow if rate is None else flow / (1 + rate) ** i
        out.append(total)
    return out


def combine(flow_lists: list[list[float]]) -> list[float]:
    """Somme de flux de durées différentes (plan d'actions)."""
    length = max((len(f) for f in flow_lists), default=0)
    return [sum(f[i] for f in flow_lists if i < len(f)) for i in range(length)]


def group_indicators(flow_lists: list[list[float]], investment: float, annual_net: float, params: dict) -> Indicators:
    """Indicateurs d'un ensemble d'actions : somme des flux de chacune (durées de vie respectées)."""
    flows = combine(flow_lists) or [0.0]
    immediate = investment <= 0 and annual_net > 0
    trb = None if annual_net <= 0 else (0.0 if investment <= 0 else round(investment / annual_net, 2))
    return Indicators(investment, annual_net, trb, immediate, npv(flows, params["discount_rate"]),
                      None if immediate else irr(flows), discounted_payback(flows, params["discount_rate"]), flows)
