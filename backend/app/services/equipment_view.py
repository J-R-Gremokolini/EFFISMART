"""Équipements : consommation d'un élément du graphe physique, contenu d'une zone, disposition du graphe.

Consommation d'un équipement, sur les 12 derniers mois, à partir des compteurs qui l'alimentent directement :
- MESURÉE : il est le seul consommateur de son compteur, sans sous-compteur ;
- CALCULÉE : seul consommateur restant d'un compteur qui a des sous-compteurs (compteur − sous-compteurs) ;
- ESTIMÉE : le compteur alimente plusieurs équipements. Sa consommation (sous-compteurs déduits) est répartie au
  prorata de « puissance nominale × durée annuelle équivalente à pleine puissance » de chaque équipement (ordres de
  grandeur par type, ci-dessous) : la même méthode que la répartition par usage du simulateur. Une estimation, que
  seul un sous-compteur transforme en mesure (plan de comptage) ;
- SANS COMPTEUR : l'énergie arrive par un fluide (eau chaude, eau glacée…) : elle est comptée sur l'équipement qui
  le produit ;
- NON ESTIMABLE : pas de puissance connue, ou pas de données (consentement, historique).
Coût au prix effectivement payé (contrats F6, à défaut indicatif), émissions avec les facteurs ADEME.

Disposition du graphe (rendu cliquable de l'interface) : rangs de gauche à droite dans le sens de l'énergie
(compteur, équipement, fluide, équipement desservi, zone, usage), ordre dans chaque rang par barycentre des voisins
pour limiter les croisements.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import AssetNode, AssetNodeKind, AssetRelationKind, DeliveryPoint, Fluid, Site
from app.services import action_plan, assets
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of
from app.services.energy_data import combined_daily, latest_declared_day

K, R = AssetNodeKind, AssetRelationKind
# Durée annuelle équivalente à pleine puissance (heures), par type d'équipement : ordres de grandeur, cohérents avec
# les durées types du simulateur (éclairage 2 500 h, informatique 5 000 h…).
EQUIVALENT_HOURS = {
    "BOILER": 1800, "HEAT_PUMP": 1800, "TERMINAL": 1800, "CHILLER": 800, "AHU": 3000, "DHW_TANK": 1500,
    "LIGHTING": 2500, "IT": 5000, "REFRIGERATION": 5000, "PROCESS": 2000, "CHARGING": 2000, "COMPRESSOR": 4000,
    "PUMP_FAN": 3000, "OTHER": 2000,
}
METHOD_LABELS = {
    "MEASURED": ("Mesurée", "success", "compteur dédié"),
    "DIFFERENCE": ("Calculée", "success", "compteur moins ses sous-compteurs"),
    "ESTIMATED": ("Estimée", "warning", "part du compteur, au prorata puissance × durée type"),
    "NO_METER": ("Sans compteur en amont", "neutral", "énergie reçue par un fluide"),
    "UNKNOWN": ("Non estimable", "neutral", "puissance ou données manquantes"),
}
KIND_ORDER = {K.METER: 0, K.EQUIPMENT: 1, K.FLOW: 2, K.ZONE: 3, K.USAGE: 4}
PERIOD_DAYS = 365


# --- Consommation d'un compteur ou d'un équipement ------------------------------------------------------------


@dataclass
class Source:
    meter: AssetNode
    delivery_point: DeliveryPoint
    method: str
    share: float | None  # part du compteur (sous-compteurs déduits) attribuée à l'élément
    co_consumers: list[str] = field(default_factory=list)
    sub_meters: list[str] = field(default_factory=list)


@dataclass
class NodeConsumption:
    node: AssetNode
    method: str
    sources: list[Source]
    by_fluid: dict[Fluid, dict[date, float]]  # kWh par jour
    start: date | None
    end: date | None
    eur: float = 0.0
    kgco2e: float = 0.0
    explanation: list[str] = field(default_factory=list)

    @property
    def kwh(self) -> float:
        return sum(sum(days.values()) for days in self.by_fluid.values())

    def monthly(self) -> dict[tuple[str, Fluid], float]:
        out: dict[tuple[str, Fluid], float] = defaultdict(float)
        for fluid, days in self.by_fluid.items():
            for day, value in days.items():
                out[(day.strftime("%Y-%m"), fluid)] += value
        return dict(sorted(out.items()))


def _period(db: Session, points: list[DeliveryPoint]) -> tuple[date, date] | None:
    ids = [dp.id for dp in points]
    consented = [dp.id for dp in points if has_active_consent(db, dp.id)]
    ends = [d for d in (data_as_of(db, consented) if consented else None, latest_declared_day(db, ids)) if d]
    if not ends:
        return None
    end = max(ends)
    return end - timedelta(days=PERIOD_DAYS - 1), end


def _daily(db: Session, dp: DeliveryPoint, start: date, end: date) -> dict[date, float]:
    return {day: kwh for day, (kwh, _) in combined_daily(db, dp, start, end).items()}


def _own_daily(db: Session, graph: assets.SiteGraph, meter: AssetNode, start: date, end: date) -> tuple[dict, list]:
    """Consommation propre d'un compteur : la sienne, moins celle de ses sous-compteurs directs."""
    daily = _daily(db, meter.delivery_point, start, end)
    subs = [t for rel, t in graph.successors(meter.id) if rel.kind == R.SUPPLIES and t.kind == K.METER
            and t.delivery_point is not None]
    for sub in subs:
        for day, value in _daily(db, sub.delivery_point, start, end).items():
            if day in daily:
                daily[day] = max(0.0, daily[day] - value)
    return daily, subs


def _weight(node: AssetNode) -> float | None:
    if not node.power_kw:
        return None
    return node.power_kw * EQUIVALENT_HOURS.get(node.category, EQUIVALENT_HOURS["OTHER"])


def _fr(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def node_consumption(db: Session, site: Site, graph: assets.SiteGraph, node_id: int) -> NodeConsumption | None:
    """Consommation d'un compteur ou d'un équipement sur les 12 derniers mois ; None pour les autres éléments."""
    node = graph.nodes.get(node_id)
    if node is None or node.kind not in (K.METER, K.EQUIPMENT):
        return None
    meters = [node] if node.kind == K.METER else [m for rel, m in graph.predecessors(node.id)
                                                   if rel.kind == R.SUPPLIES and m.kind == K.METER]
    meters = [m for m in meters if m.delivery_point is not None]
    period = _period(db, [m.delivery_point for m in meters]) if meters else None
    result = NodeConsumption(node, "UNKNOWN", [], {}, period[0] if period else None, period[1] if period else None)
    if node.kind == K.EQUIPMENT and not meters:
        result.method = "NO_METER"
        feeds = [(f, [p.name for r, p in graph.predecessors(f.id) if r.kind == R.PRODUCES])
                 for rel, f in graph.predecessors(node.id) if rel.kind == R.SUPPLIES and f.kind == K.FLOW]
        for flow, producers in feeds:
            result.explanation.append(f"Alimenté par « {flow.name} »" + (f", produit par {', '.join(producers)}"
                                                                        if producers else "") +
                                      " : cette énergie est comptée sur l'équipement producteur.")
        if not feeds:
            result.explanation.append("Aucun compteur ni fluide n'alimente cet équipement dans le graphe.")
        return result
    if period is None:
        result.explanation.append("Pas de consommation connue sur le compteur (consentement, historique ou factures).")
        return result
    start, end = period
    methods = []
    for meter in meters:
        own, subs = _own_daily(db, graph, meter, start, end)
        fluid = meter.delivery_point.fluid
        consumers = [t for rel, t in graph.successors(meter.id) if rel.kind == R.SUPPLIES and t.kind == K.EQUIPMENT]
        source = Source(meter, meter.delivery_point, "MEASURED", 1.0, [c.name for c in consumers if c.id != node.id],
                        [s.name for s in subs])
        if node.kind == K.METER:
            daily = _daily(db, meter.delivery_point, start, end)  # le compteur : sa mesure entière
            source.co_consumers = [c.name for c in consumers]
        elif len(consumers) == 1:
            source.method = "DIFFERENCE" if subs else "MEASURED"
            daily = own
        else:
            weights = {c.id: _weight(c) for c in consumers}
            known = sum(w for w in weights.values() if w)
            if not weights.get(node.id) or not known:
                source.method, source.share = "UNKNOWN", None
                result.explanation.append(
                    f"« {meter.name} » alimente {len(consumers)} équipements : sans puissance connue pour "
                    f"{'celui-ci' if not weights.get(node.id) else 'les autres'}, sa part ne peut pas être estimée.")
                result.sources.append(source)
                methods.append("UNKNOWN")
                continue
            share = weights[node.id] / known
            source.method, source.share = "ESTIMATED", share
            daily = {day: value * share for day, value in own.items()}
            missing = [c.name for c in consumers if not weights[c.id]]
            result.explanation.append(
                f"« {meter.name} » alimente {len(consumers)} équipements ; part estimée de celui-ci : "
                f"{_fr(share * 100)} % ({_fr(node.power_kw, 1)} kW × "
                f"{_fr(EQUIVALENT_HOURS.get(node.category, EQUIVALENT_HOURS['OTHER']))} h équivalentes par an, "
                f"rapportés à l'ensemble des équipements du compteur)."
                + (f" Puissance inconnue pour {', '.join(missing)} : leur part n'est pas comptée, celle-ci est donc "
                   "surestimée." if missing else ""))
        if subs and node.kind == K.EQUIPMENT:
            result.explanation.append(f"Sous-compteurs déduits de « {meter.name} » : "
                                      f"{', '.join(s.name for s in subs)}.")
        for day, value in daily.items():
            result.by_fluid.setdefault(fluid, {})
            result.by_fluid[fluid][day] = result.by_fluid[fluid].get(day, 0.0) + value
        result.sources.append(source)
        methods.append(source.method)
    known_methods = [m for m in methods if m != "UNKNOWN"]
    if known_methods:
        order = ["ESTIMATED", "DIFFERENCE", "MEASURED"]  # la méthode la moins sûre qualifie le total
        result.method = min(known_methods, key=order.index)
    if result.method == "MEASURED" and node.kind == K.EQUIPMENT:
        result.explanation.insert(0, "Seul équipement de son compteur : sa consommation est mesurée.")
    if result.method == "DIFFERENCE":
        result.explanation.insert(0, "Seul équipement restant sur son compteur : consommation du compteur moins celle "
                                     "de ses sous-compteurs.")
    if result.by_fluid:
        prices = action_plan.site_prices(db, site)
        for fluid, days in result.by_fluid.items():
            kwh = sum(days.values())
            result.eur += kwh * prices.price.get(fluid, 0.0)
            result.kgco2e += kwh * prices.kgco2e.get(fluid, 0.0)
    return result


# --- Contenu d'une zone -----------------------------------------------------------------------------------------


@dataclass
class ZoneContent:
    zone: AssetNode
    serving: list[AssetNode]  # équipements qui desservent la zone (graphe physique)
    placed: list[AssetNode]  # équipements et compteurs placés dans la zone sur le plan, sans la desservir
    usages: list[AssetNode]
    shared: dict[int, list[str]]  # équipement → autres zones qu'il dessert


def _inside(node: AssetNode, zone: AssetNode) -> bool:
    if None in (node.plan_x, node.plan_y, zone.plan_x, zone.plan_y, zone.plan_w, zone.plan_h):
        return False
    return (zone.plan_x <= node.plan_x <= zone.plan_x + zone.plan_w
            and zone.plan_y <= node.plan_y <= zone.plan_y + zone.plan_h)


def zone_content(graph: assets.SiteGraph, zone_id: int) -> ZoneContent | None:
    zone = graph.nodes.get(zone_id)
    if zone is None or zone.kind != K.ZONE:
        return None
    serving = [n for rel, n in graph.predecessors(zone.id) if rel.kind == R.SERVES and n.kind == K.EQUIPMENT]
    serving_ids = {n.id for n in serving}
    placed = [n for n in graph.nodes.values() if n.kind in (K.EQUIPMENT, K.METER) and n.id not in serving_ids
              and _inside(n, zone)]
    usages = [n for rel, n in graph.successors(zone.id) if rel.kind == R.HOSTS]
    shared = {}
    for equipment in serving:
        others = [z.name for rel, z in graph.successors(equipment.id) if rel.kind == R.SERVES and z.id != zone.id]
        if others:
            shared[equipment.id] = others
    key = lambda n: n.name.lower()  # noqa: E731
    return ZoneContent(zone, sorted(serving, key=key), sorted(placed, key=key), sorted(usages, key=key), shared)


# --- Disposition du graphe ------------------------------------------------------------------------------------


@dataclass
class Layout:
    positions: dict[int, tuple[float, float]]  # nœud → (rang, ordonnée)
    ranks: int
    widest: int  # nombre de nœuds du rang le plus fourni


def layout(graph: assets.SiteGraph, sweeps: int = 4) -> Layout:
    """Rangs dans le sens de l'énergie, ordre par barycentre (heuristique de Sugiyama, simplifiée)."""
    nodes = graph.nodes
    rank = {i: KIND_ORDER[n.kind] for i, n in nodes.items()}
    edges = [(r.source_id, r.target_id) for r in graph.relations if r.source_id in nodes and r.target_id in nodes]
    for _ in range(len(nodes)):  # plus long chemin ; borné : une boucle physique ne fait pas diverger les rangs
        changed = False
        for source, target in edges:
            if rank[target] < rank[source] + 1 and rank[source] + 1 < 2 * len(nodes):
                rank[target], changed = rank[source] + 1, True
        if not changed:
            break
    used = sorted(set(rank.values()))
    rank = {i: used.index(r) for i, r in rank.items()}  # rangs consécutifs
    layers: dict[int, list[int]] = defaultdict(list)
    for i in sorted(nodes, key=lambda i: (KIND_ORDER[nodes[i].kind], nodes[i].category, nodes[i].name.lower())):
        layers[rank[i]].append(i)
    preds: dict[int, list[int]] = defaultdict(list)
    succs: dict[int, list[int]] = defaultdict(list)
    for source, target in edges:
        succs[source].append(target)
        preds[target].append(source)

    def reorder(layer: list[int], neighbours: dict[int, list[int]]) -> list[int]:
        position = {i: k for layer_nodes in layers.values() for k, i in enumerate(layer_nodes)}
        current = {i: k for k, i in enumerate(layer)}

        def barycenter(i: int) -> float:
            near = [position[j] for j in neighbours[i] if j in position]
            return sum(near) / len(near) if near else current[i]
        return sorted(layer, key=lambda i: (barycenter(i), current[i]))

    for _ in range(sweeps):
        for r in range(1, len(used)):
            layers[r] = reorder(layers[r], preds)
        for r in range(len(used) - 2, -1, -1):
            layers[r] = reorder(layers[r], succs)
    widest = max((len(v) for v in layers.values()), default=1)
    positions = {}
    for r, layer_nodes in layers.items():
        offset = (widest - len(layer_nodes)) / 2
        for k, i in enumerate(layer_nodes):
            positions[i] = (float(r), offset + k)
    return Layout(positions, len(used), widest)
