"""F2b — Anomalies contextualisées (V2) : qualification et propagation d'impact par le graphe physique (P2).

F2a établit, par la seule statistique, qu'un compteur dérive. F2b replace l'anomalie dans le graphe physique
du site :
1. qualification : nature probable de l'anomalie (équipement resté en marche la nuit, fonctionnement hors
   occupation, dérive de régulation, appel de puissance) et équipement(s) suspect(s) derrière le compteur,
   retenus par des règles physiques : catégorie, puissance nominale comparée à l'excès mesuré, zones
   occupées 24 h/24, saison ;
2. propagation : depuis l'équipement suspect, la plateforme suit les relations physiques en aval
   (produit → alimente → dessert → accueille) jusqu'aux équipements, zones et usages touchés :

       « Dérive détectée sur la chaudière ; zones potentiellement impactées : atelier 2, bureaux R+1 »

C'est une hypothèse de la plateforme (principe P1) : présentée avec ses raisons, elle est validée avec
l'anomalie. Elle suit le graphe tant que l'anomalie est à valider, puis elle est figée telle que validée.
Sans graphe derrière le compteur, l'anomalie reste signalée (F2a) mais n'est ni localisée ni propagée.
Les règles sont celles des recommandations : l'équipement suspect est celui que la recommandation visera.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssetNode, AssetNodeKind, DeliveryPoint, Drift, DriftKind, DriftStatus, Fluid
from app.services import assets
from app.timeutils import utcnow

logger = logging.getLogger(__name__)

CONTEXT_VERSION = 1
CONTEXT_STEP_PREFIX = "Contexte physique (graphe des équipements) : "
LOCALISATION_LABELS = {
    "precise": "Localisation précise",
    "probable": "Localisation probable",
    "incertaine": "Plusieurs causes possibles",
    "absente": "Non localisée",
}
NATURE_LABELS = {
    "NIGHT_LOAD": "Équipement resté en marche la nuit",
    "OFF_HOURS": "Équipement en marche hors des horaires d'occupation",
    "HEAT_REGULATION": "Dérive de la production de chaleur (régulation)",
    "COLD_REGULATION": "Dérive de la production de froid (régulation)",
    "OVERCONSUMPTION": "Surconsommation d'un équipement",
    "POWER_CALL": "Appel de puissance anormal",
}
MAX_SUSPECTS = 3


# --- Règles physiques partagées avec les recommandations ------------------------------------------------


def power_share(power_kw: float | None, excess_kw: float) -> float | None:
    """Part de l'excès que la puissance nominale de l'équipement peut expliquer (1 = correspondance parfaite)."""
    if not power_kw or excess_kw <= 0:
        return None
    return min(power_kw, excess_kw) / max(power_kw, excess_kw)


def season_dju(db: Session, drift: Drift) -> float | None:
    """DJU du jour de l'anomalie (None si la météo est indisponible)."""
    from app.services import integrations  # import local : évite un cycle

    try:
        return integrations.weather_provider(db).daily_dju(drift.day, drift.day).get(drift.day)
    except Exception:  # météo indisponible : on raisonne sans la saison
        logger.warning("DJU indisponibles pour la dérive #%s", drift.id)
        return None


def usual_peak(db: Session, drift: Drift) -> tuple[float | None, int]:
    """Médiane des pics journaliers des 28 jours précédents, même type de jour (ouvré / week-end)."""
    from app.services.drift import half_hour_powers  # import local : évite un cycle

    workday = drift.day.weekday() < 5
    peaks: dict = {}
    for t, kw in half_hour_powers(db, drift.delivery_point_id, drift.day - timedelta(days=28),
                                  drift.day - timedelta(days=1)):
        if (t.weekday() < 5) == workday:
            peaks[t.date()] = max(peaks.get(t.date(), 0.0), kw)
    if len(peaks) < 5:
        return None, len(peaks)
    return statistics.median(peaks.values()), len(peaks)


def heating_season(db: Session, drift: Drift) -> tuple[bool, float | None]:
    """Besoin de chauffage (sinon de froid) le jour de l'anomalie, et les DJU du jour."""
    dju = season_dju(db, drift)
    return dju is None or dju > 1 or drift.delivery_point.fluid == Fluid.GAS, dju


def schedule_candidates(graph: assets.SiteGraph, consumers: list[AssetNode]) -> tuple[list[AssetNode], list[str]]:
    """Équipements pouvant être arrêtés hors occupation ; les autres sont écartés avec leur raison."""
    candidates, excluded = [], []
    for eq in consumers:
        zones = graph.downstream(eq.id, AssetNodeKind.ZONE)
        if not assets.category(eq).off_hours_ok:
            excluded.append(f"{eq.name} (fonctionnement continu attendu)")
        elif zones and all(z.always_occupied for z in zones):
            excluded.append(f"{eq.name} (dessert uniquement des zones occupées 24 h/24 : "
                            f"{', '.join(z.name for z in zones)})")
        else:
            candidates.append(eq)
    return candidates, excluded


def peak_candidates(graph: assets.SiteGraph, consumers: list[AssetNode],
                    dju: float | None) -> tuple[list[AssetNode], list[str]]:
    """Équipements pouvant expliquer un pic : le chauffage seul est écarté hors saison."""
    candidates, excluded = [], []
    for eq in consumers:
        usages = {u.category for u in graph.usages_served(eq.id)}
        if dju is not None and dju < 1 and usages and usages <= {"HEATING"}:
            excluded.append(f"{eq.name} (chauffage seul, hors saison : {dju:.1f} DJU)".replace(".", ","))
        else:
            candidates.append(eq)
    return candidates, excluded


def rank_by_power(candidates: list[AssetNode], target_kw: float) -> list[tuple[AssetNode, float | None]]:
    """Du plus au moins plausible : la puissance nominale la plus proche de l'excès (inconnue : 0,3)."""
    return sorted(((eq, power_share(eq.power_kw, target_kw)) for eq in candidates),
                  key=lambda c: c[1] if c[1] is not None else 0.3, reverse=True)


# --- Qualification ------------------------------------------------------------------------------------


@dataclass
class Qualification:
    nature: str
    ranked: list[tuple[AssetNode, float | None]]  # suspects, du plus au moins plausible
    reasons: dict[int, str]
    excluded: list[str]
    localisation: str
    note: str = ""  # pourquoi l'anomalie n'est pas localisée


def _excess_kw(db: Session, drift: Drift) -> tuple[float, str]:
    """Excès à expliquer (kW) et ce qu'il représente."""
    excess = drift.measured_value - drift.reference_value
    if drift.kind in (DriftKind.BASELOAD, DriftKind.OFF_HOURS):
        return excess, "l'excès mesuré"
    if drift.kind == DriftKind.THRESHOLD and drift.delivery_point.fluid == Fluid.ELEC:
        usual, _ = usual_peak(db, drift)
        if usual is not None and drift.measured_value > usual:
            return drift.measured_value - usual, "la hausse du pic"
        return excess, "la hausse du pic"
    return excess / 24, "l'excès moyen sur la journée"


def _localisation(ranked: list[tuple[AssetNode, float | None]]) -> str:
    if len(ranked) == 1:
        return "precise"
    top = ranked[0][1]
    second = ranked[1][1]
    if top is not None and top >= 0.7 and (second is None or top - second >= 0.15):
        return "precise"
    if top is not None and top >= 0.4:
        return "probable"
    return "incertaine"


def qualify(db: Session, graph: assets.SiteGraph, drift: Drift, consumers: list[AssetNode]) -> Qualification:
    """Nature probable de l'anomalie et équipements suspects, selon les règles des recommandations."""
    dp = drift.delivery_point
    if drift.kind == DriftKind.BASELOAD:
        nature = "NIGHT_LOAD"
    elif drift.kind == DriftKind.OFF_HOURS:
        nature = "OFF_HOURS"
    elif drift.kind == DriftKind.THRESHOLD and dp.fluid == Fluid.ELEC:
        nature = "POWER_CALL"
    else:
        nature = "OVERCONSUMPTION"
    if not consumers:
        return Qualification(nature, [], {}, [], "absente",
                             f"aucun équipement n'est modélisé derrière le compteur {dp.external_ref}")
    target_kw, what = _excess_kw(db, drift)
    excluded: list[str] = []
    reasons: dict[int, str] = {}

    if nature in ("NIGHT_LOAD", "OFF_HOURS"):
        candidates, excluded = schedule_candidates(graph, consumers)
    elif nature == "POWER_CALL":
        candidates, excluded = peak_candidates(graph, consumers, season_dju(db, drift))
    else:
        heating, _ = heating_season(db, drift)
        producers = [c for c in consumers if (assets.category(c).heat_producer if heating
                                              else assets.category(c).cold_producer)]
        if producers:
            producers.sort(key=lambda n: n.power_kw or 0, reverse=True)
            for eq in producers:
                reasons[eq.id] = (f"générateur de {'chaleur' if heating else 'froid'} alimenté par ce compteur, "
                                  f"en saison de {'chauffage' if heating else 'refroidissement'}")
            ranked = [(eq, None) for eq in producers]
            return Qualification("HEAT_REGULATION" if heating else "COLD_REGULATION", ranked, reasons, [],
                                 "precise" if len(producers) == 1 else "probable")
        candidates = consumers
    if not candidates:
        return Qualification(nature, [], {}, excluded, "absente",
                             "tous les équipements de ce compteur fonctionnent légitimement à ce moment-là ; "
                             "l'excès vient d'un équipement non modélisé")
    ranked = rank_by_power(candidates, target_kw)
    for eq, share in ranked:
        if share is None:
            reasons[eq.id] = "puissance nominale inconnue : correspondance non vérifiable"
        else:
            power = f"{eq.power_kw:g}".replace(".", ",")
            reasons[eq.id] = (f"{power} kW pour {what} de {target_kw:.0f} kW, soit "
                              f"{share * 100:.0f} % de correspondance")
    return Qualification(nature, ranked, reasons, excluded, _localisation(ranked))


# --- Propagation ----------------------------------------------------------------------------------------


def _zone_label(zone: AssetNode) -> str:
    return f"{zone.name} (occupée 24 h/24)" if zone.always_occupied else zone.name


def _propagate(graph: assets.SiteGraph, sources: list[AssetNode]) -> dict:
    """Équipements, zones et usages atteints en aval des équipements suspects."""
    equipment: dict[int, AssetNode] = {}
    zones: dict[int, AssetNode] = {}
    usages: dict[int, AssetNode] = {}
    highlight: set[int] = set()
    chains: list[str] = []
    for source in sources:
        highlight.add(source.id)
        for node_id in graph.reachable(source.id):
            node = graph.nodes[node_id]
            if node.kind == AssetNodeKind.EQUIPMENT and node.id not in {s.id for s in sources}:
                equipment.setdefault(node.id, node)
            elif node.kind == AssetNodeKind.ZONE:
                zones.setdefault(node.id, node)
        for usage in graph.usages_served(source.id):
            usages.setdefault(usage.id, usage)
        paths = graph.physical_paths(source.id)
        highlight.update(node.id for path in paths for _, node in path)
        chains.extend(assets.chain_summaries(paths))
    return {
        "equipment": [n.name for n in equipment.values()],
        "zones": [{"id": z.id, "name": z.name, "surface_m2": z.surface_m2, "always_occupied": z.always_occupied}
                  for z in zones.values()],
        "usages": [u.name for u in usages.values()],
        "chains": chains[:6],
        "highlight_ids": sorted(highlight),
    }


def _summary(qualification: Qualification, impacted: dict, dp: DeliveryPoint) -> str:
    if qualification.localisation == "absente":
        return (f"Dérive non localisée : {qualification.note}. Compléter le graphe des équipements du site "
                "pour connaître l'équipement en cause et les zones touchées.")
    zones = impacted["zones"]
    zone_text = (", ".join(z["name"] + (" (occupée 24 h/24)" if z["always_occupied"] else "") for z in zones)
                 if zones else "non renseignées dans le graphe")
    if qualification.localisation in ("precise", "probable"):
        where = f"Dérive détectée sur « {qualification.ranked[0][0].name} »"
    else:
        suspects = ", ".join(f"« {eq.name} »" for eq, _ in qualification.ranked[:MAX_SUSPECTS])
        where = f"Dérive derrière le compteur {dp.external_ref} ; équipements suspects : {suspects}"
    return f"{where} ; zones potentiellement impactées : {zone_text}."


def build_context(db: Session, drift: Drift) -> dict:
    """Contexte physique de l'anomalie (F2b), à partir du graphe actuel du site. Sans écriture en base."""
    dp = drift.delivery_point
    graph = assets.load_site_graph(db, dp.site_id)
    meter = graph.meter_for(dp.id)
    consumers = graph.direct_consumers(meter.id) if meter else []
    qualification = qualify(db, graph, drift, consumers)
    retained = qualification.ranked[:1] if qualification.localisation in ("precise", "probable") \
        else qualification.ranked[:MAX_SUSPECTS]
    impacted = _propagate(graph, [eq for eq, _ in retained])
    if meter is not None:
        impacted["highlight_ids"] = sorted(set(impacted["highlight_ids"]) | {meter.id})
    return {
        "version": CONTEXT_VERSION,
        "nature": qualification.nature,
        "nature_label": NATURE_LABELS[qualification.nature],
        "localisation": qualification.localisation,
        "summary": _summary(qualification, impacted, dp),
        "suspects": [
            {"id": eq.id, "name": eq.name, "category": assets.category(eq).label, "power_kw": eq.power_kw,
             "match": round(share, 2) if share is not None else None,
             "reason": qualification.reasons.get(eq.id, ""), "retained": i < len(retained)}
            for i, (eq, share) in enumerate(qualification.ranked[:MAX_SUSPECTS + 2])
        ],
        "excluded": qualification.excluded,
        "impacted": {key: impacted[key] for key in ("equipment", "zones", "usages")},
        "chains": impacted["chains"],
        "highlight_ids": impacted["highlight_ids"],
        "computed_at": utcnow().isoformat(),
        "after_validation": drift.status != DriftStatus.OPEN,
    }


# --- Enregistrement --------------------------------------------------------------------------------------


def reasoning_step(context: dict) -> str:
    if context["localisation"] == "absente":
        return CONTEXT_STEP_PREFIX + context["summary"]
    return (CONTEXT_STEP_PREFIX + f"{context['nature_label']}. {context['summary']} Une fois l'anomalie validée, "
            "la plateforme proposera une recommandation ciblée, elle aussi à valider.")


def apply_context(drift: Drift, context: dict) -> None:
    """Enregistre le contexte et remplace l'étape correspondante du raisonnement (sans commit)."""
    drift.context = context
    steps = [s for s in (drift.reasoning or []) if not s.startswith(CONTEXT_STEP_PREFIX)]
    # Avant l'étape « Statut », dernière du raisonnement.
    position = next((i for i, s in enumerate(steps) if s.startswith("Statut :")), len(steps))
    drift.reasoning = steps[:position] + [reasoning_step(context)] + steps[position:]


def contextualize(db: Session, drift: Drift) -> dict:
    context = build_context(db, drift)
    apply_context(drift, context)
    return context


def refresh_open(db: Session, site_id: int) -> int:
    """Le graphe du site a changé : les anomalies encore à valider suivent ; les décisions passées sont figées."""
    drifts = db.scalars(select(Drift).join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id).where(
        DeliveryPoint.site_id == site_id, Drift.status == DriftStatus.OPEN)).all()
    for drift in drifts:
        contextualize(db, drift)
    db.commit()
    return len(drifts)


def backfill(db: Session) -> int:
    """Mise à niveau : contexte des anomalies détectées avant F2b (signalé comme calculé après coup)."""
    missing = [d for d in db.scalars(select(Drift).order_by(Drift.day, Drift.id)) if not d.context]
    for drift in missing:
        contextualize(db, drift)
    db.commit()
    return len(missing)


# --- Lecture ------------------------------------------------------------------------------------------------


def short_text(context: dict | None) -> str:
    """Résumé pour une alerte ou un e-mail : équipement suspect et zones touchées."""
    if not context or context["localisation"] == "absente":
        return ""
    zones = ", ".join(z["name"] for z in context["impacted"]["zones"]) or "non renseignées"
    retained = [s["name"] for s in context["suspects"] if s["retained"]]
    label = "équipement suspect" if len(retained) == 1 else "équipements suspects"
    return f"{label} : {', '.join(retained)} ; zones potentiellement impactées : {zones}"


def related(db: Session, drift: Drift, statuses: tuple[DriftStatus, ...] | None = None) -> list[Drift]:
    """Autres anomalies du site à ±1 jour qui touchent les mêmes zones ou visent le même équipement : à examiner
    ensemble, une même cause pouvant être vue par plusieurs compteurs ou détecteurs."""
    if not drift.context:
        return []
    zones = {z["id"] for z in drift.context["impacted"]["zones"]}
    suspects = {s["id"] for s in drift.context["suspects"] if s["retained"]}
    if not zones and not suspects:
        return []
    stmt = select(Drift).join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id).where(
        DeliveryPoint.site_id == drift.delivery_point.site_id, Drift.id != drift.id,
        Drift.day >= drift.day - timedelta(days=1), Drift.day <= drift.day + timedelta(days=1))
    if statuses is not None:
        stmt = stmt.where(Drift.status.in_(statuses))
    found = []
    for other in db.scalars(stmt.order_by(Drift.day, Drift.id)):
        ctx = other.context or {}
        other_zones = {z["id"] for z in ctx.get("impacted", {}).get("zones", [])}
        other_suspects = {s["id"] for s in ctx.get("suspects", []) if s.get("retained")}
        if zones & other_zones or suspects & other_suspects:
            found.append(other)
    return found
