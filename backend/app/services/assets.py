"""Graphe physique des équipements d'un site.

Ce n'est pas une arborescence de rangement (site > bâtiment > local) : chaque relation décrit un
échange physique réel, orienté et typé, par exemple :

    Compteur gaz → alimente → Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage

Un nœud peut avoir plusieurs sources (une CTA alimentée en eau chaude ET en eau glacée) et plusieurs
cibles (une chaudière qui produit l'eau de chauffage et l'eau chaude sanitaire). La grammaire
`ALLOWED_RELATIONS` refuse les liens physiquement absurdes (une zone qui « produit » un compteur…).

Le graphe sert au raisonnement des anomalies contextualisées (F2b) et des recommandations : quels
équipements consomment l'énergie d'un compteur, quelles zones et quels usages ils desservent, lesquels
sont occupés 24 h/24. Modifier le graphe met à jour le contexte des anomalies encore à valider.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    AssetNode,
    AssetNodeKind,
    AssetRelation,
    AssetRelationKind,
    DeliveryPoint,
    Fluid,
    Recommendation,
    Role,
    Site,
    User,
)
from app.repositories import ResourceNotFound, TenantRepository

K, R = AssetNodeKind, AssetRelationKind


@dataclass(frozen=True)
class Category:
    label: str
    off_hours_ok: bool = True  # peut être arrêté ou ralenti hors occupation
    heat_producer: bool = False
    cold_producer: bool = False


CATEGORIES: dict[AssetNodeKind, dict[str, Category]] = {
    K.METER: {"ELEC": Category("Compteur électricité"), "GAS": Category("Compteur gaz")},
    K.EQUIPMENT: {
        "BOILER": Category("Chaudière", heat_producer=True),
        "HEAT_PUMP": Category("Pompe à chaleur", heat_producer=True, cold_producer=True),
        "CHILLER": Category("Groupe froid", cold_producer=True),
        "AHU": Category("Centrale de traitement d'air (CTA)"),
        "TERMINAL": Category("Émetteurs (radiateurs, ventilo-convecteurs)"),
        "DHW_TANK": Category("Production d'eau chaude sanitaire"),
        "LIGHTING": Category("Éclairage"),
        "IT": Category("Informatique, serveurs", off_hours_ok=False),
        "REFRIGERATION": Category("Froid alimentaire, chambre froide", off_hours_ok=False),
        "PROCESS": Category("Process, machines de production"),
        "CHARGING": Category("Recharge (véhicules, chariots)"),
        "OTHER": Category("Autre équipement"),
    },
    K.FLOW: {
        "HOT_WATER": Category("Eau chaude de chauffage"),
        "DHW": Category("Eau chaude sanitaire"),
        "CHILLED_WATER": Category("Eau glacée"),
        "AIR": Category("Air traité"),
        "STEAM": Category("Vapeur"),
        "OTHER": Category("Autre fluide"),
    },
    K.ZONE: {
        "OFFICE": Category("Bureaux"),
        "CARE": Category("Soins, hébergement"),
        "PRODUCTION": Category("Atelier, production"),
        "STORAGE": Category("Stockage, entrepôt"),
        "RETAIL": Category("Vente, accueil"),
        "TECHNICAL": Category("Local technique"),
        "OTHER": Category("Autre zone"),
    },
    K.USAGE: {
        "HEATING": Category("Chauffage"),
        "COOLING": Category("Refroidissement, climatisation"),
        "DHW": Category("Eau chaude sanitaire"),
        "VENTILATION": Category("Ventilation"),
        "LIGHTING": Category("Éclairage"),
        "IT": Category("Informatique"),
        "PROCESS": Category("Process"),
        "REFRIGERATION": Category("Froid alimentaire"),
        "OTHER": Category("Autre usage"),
    },
}
KIND_LABELS = {K.METER: "Compteur", K.EQUIPMENT: "Équipement", K.FLOW: "Fluide produit", K.ZONE: "Zone",
               K.USAGE: "Usage"}
RELATION_LABELS = {R.SUPPLIES: "alimente", R.PRODUCES: "produit", R.SERVES: "dessert", R.HOSTS: "accueille"}
# Usages qu'une catégorie d'équipement peut réellement servir : un groupe froid ne chauffe pas une zone,
# même si cette zone accueille aussi l'usage « chauffage ». Catégorie absente = pas de filtre.
RELEVANT_USAGES = {
    "BOILER": {"HEATING", "DHW"},
    "HEAT_PUMP": {"HEATING", "COOLING", "DHW"},
    "CHILLER": {"COOLING", "REFRIGERATION"},
    "AHU": {"HEATING", "COOLING", "VENTILATION"},
    "TERMINAL": {"HEATING", "COOLING"},
    "DHW_TANK": {"DHW"},
    "LIGHTING": {"LIGHTING"},
    "IT": {"IT"},
    "REFRIGERATION": {"REFRIGERATION"},
    "PROCESS": {"PROCESS"},
    "CHARGING": {"PROCESS"},
}
# Grammaire physique : (type de la source, relation, type de la cible).
ALLOWED_RELATIONS = {
    (K.METER, R.SUPPLIES, K.EQUIPMENT),
    (K.METER, R.SUPPLIES, K.METER),  # sous-comptage
    (K.EQUIPMENT, R.PRODUCES, K.FLOW),
    (K.FLOW, R.SUPPLIES, K.EQUIPMENT),
    (K.EQUIPMENT, R.SERVES, K.ZONE),
    (K.ZONE, R.HOSTS, K.USAGE),
}
# Couleurs du rendu (thème sombre) : fond, bordure, forme.
_NODE_STYLE = {
    K.METER: ("#0c4a6e", "#38bdf8", "cylinder"),
    K.EQUIPMENT: ("#14532d", "#22c55e", "box"),
    K.FLOW: ("#1e293b", "#94a3b8", "ellipse"),
    K.ZONE: ("#312e81", "#a5b4fc", "tab"),
    K.USAGE: ("#422006", "#fbbf24", "hexagon"),
}


class AssetGraphError(ValueError):
    """Élément ou relation refusé (grammaire physique, doublon, champ manquant…)."""


class AssetPermissionError(PermissionError):
    pass


def category(node: AssetNode) -> Category:
    return CATEGORIES[node.kind].get(node.category, Category(node.category))


def allowed_relations_from(kind: AssetNodeKind) -> list[tuple[AssetRelationKind, AssetNodeKind]]:
    return sorted(((rel, target) for source, rel, target in ALLOWED_RELATIONS if source == kind),
                  key=lambda item: (item[0].value, item[1].value))


# --- Lecture et raisonnement ----------------------------------------------------------------------


@dataclass
class SiteGraph:
    nodes: dict[int, AssetNode]
    relations: list[AssetRelation]
    _out: dict[int, list[tuple[AssetRelation, AssetNode]]] = field(default_factory=dict, repr=False)
    _in: dict[int, list[tuple[AssetRelation, AssetNode]]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for rel in self.relations:
            if rel.source_id in self.nodes and rel.target_id in self.nodes:
                self._out.setdefault(rel.source_id, []).append((rel, self.nodes[rel.target_id]))
                self._in.setdefault(rel.target_id, []).append((rel, self.nodes[rel.source_id]))

    def successors(self, node_id: int) -> list[tuple[AssetRelation, AssetNode]]:
        return self._out.get(node_id, [])

    def predecessors(self, node_id: int) -> list[tuple[AssetRelation, AssetNode]]:
        return self._in.get(node_id, [])

    def meter_for(self, delivery_point_id: int) -> AssetNode | None:
        return next((n for n in self.nodes.values() if n.delivery_point_id == delivery_point_id), None)

    def reachable(self, start_id: int) -> dict[int, int]:
        """Nœuds atteints en aval (profondeur minimale), le départ exclu. Les boucles physiques sont tolérées."""
        depths: dict[int, int] = {}
        queue = deque([(start_id, 0)])
        while queue:
            node_id, depth = queue.popleft()
            for _, target in self.successors(node_id):
                if target.id != start_id and target.id not in depths:
                    depths[target.id] = depth + 1
                    queue.append((target.id, depth + 1))
        return depths

    def downstream(self, start_id: int, kind: AssetNodeKind) -> list[AssetNode]:
        return [self.nodes[i] for i in self.reachable(start_id) if self.nodes[i].kind == kind]

    def direct_consumers(self, meter_id: int) -> list[AssetNode]:
        """Équipements qui consomment l'énergie du compteur (directement ou via un sous-compteur)."""
        found: dict[int, AssetNode] = {}
        stack, seen = [meter_id], {meter_id}
        while stack:
            for rel, target in self.successors(stack.pop()):
                if rel.kind != R.SUPPLIES or target.id in seen:
                    continue
                seen.add(target.id)
                if target.kind == K.METER:
                    stack.append(target.id)
                elif target.kind == K.EQUIPMENT:
                    found[target.id] = target
        return list(found.values())

    def paths_from(self, start_id: int, limit: int = 40) -> list[list[tuple[AssetRelation | None, AssetNode]]]:
        """Chaînes physiques du nœud jusqu'aux usages (ou jusqu'au dernier nœud atteint)."""
        paths: list[list[tuple[AssetRelation | None, AssetNode]]] = []

        def walk(path: list[tuple[AssetRelation | None, AssetNode]], visited: set[int]) -> None:
            if len(paths) >= limit:
                return
            nexts = [(rel, t) for rel, t in self.successors(path[-1][1].id) if t.id not in visited]
            if not nexts:
                if len(path) > 1:
                    paths.append(path)
                return
            for rel, target in nexts:
                walk(path + [(rel, target)], visited | {target.id})

        if start_id in self.nodes:
            walk([(None, self.nodes[start_id])], {start_id})
        return paths

    def physical_paths(self, start_id: int, limit: int = 60) -> list[list[tuple[AssetRelation | None, AssetNode]]]:
        """Chaînes physiquement pertinentes : l'usage final doit pouvoir être servi par le premier équipement
        de la chaîne (le groupe froid mène à la climatisation, pas au chauffage)."""
        return [p for p in self.paths_from(start_id, limit) if _relevant(p)]

    def usages_served(self, equipment_id: int) -> list[AssetNode]:
        """Usages atteints par une chaîne physiquement pertinente depuis l'équipement."""
        served: dict[int, AssetNode] = {}
        for path in self.physical_paths(equipment_id):
            if path[-1][1].kind == K.USAGE:
                served.setdefault(path[-1][1].id, path[-1][1])
        return list(served.values())

    def warnings(self) -> list[str]:
        """Maillons manquants : ce qui empêche la plateforme de localiser une anomalie."""
        messages = []
        for node in self.nodes.values():
            out = {rel.kind for rel, _ in self.successors(node.id)}
            into = {rel.kind for rel, _ in self.predecessors(node.id)}
            name = f"« {node.name} »"
            if node.kind == K.METER and not out:
                messages.append(f"Le compteur {name} n'alimente aucun équipement : ses anomalies ne peuvent pas être localisées.")
            elif node.kind == K.FLOW and R.PRODUCES not in into:
                messages.append(f"Le fluide {name} n'est produit par aucun équipement.")
            elif node.kind == K.FLOW and R.SUPPLIES not in out:
                messages.append(f"Le fluide {name} n'alimente aucun équipement.")
            elif node.kind == K.EQUIPMENT and R.SUPPLIES not in into:
                messages.append(f"L'équipement {name} n'est alimenté par aucun compteur ni aucun fluide.")
            elif node.kind == K.EQUIPMENT and not out & {R.SERVES, R.PRODUCES}:
                messages.append(f"L'équipement {name} ne dessert aucune zone et ne produit aucun fluide.")
            elif node.kind == K.ZONE and R.SERVES not in into:
                messages.append(f"La zone {name} n'est desservie par aucun équipement.")
            elif node.kind == K.ZONE and R.HOSTS not in out:
                messages.append(f"La zone {name} n'a aucun usage déclaré.")
            elif node.kind == K.USAGE and R.HOSTS not in into:
                messages.append(f"L'usage {name} n'est rattaché à aucune zone.")
        return messages

    def to_dot(self, highlight: set[int] | None = None) -> str:
        """Rendu Graphviz (affiché par Streamlit, sans dépendance système)."""
        highlight = highlight or set()
        lines = [
            "digraph G {",
            '  graph [rankdir=LR, bgcolor="transparent", pad="0.3", nodesep="0.3", ranksep="0.55"];',
            '  node [style="rounded,filled", fontname="Helvetica", fontsize=11, fontcolor="#f8fafc", penwidth=1.4];',
            '  edge [color="#94a3b8", fontcolor="#cbd5e1", fontname="Helvetica", fontsize=9, arrowsize=0.7];',
        ]
        for node in self.nodes.values():
            fill, border, shape = _NODE_STYLE[node.kind]
            details = [KIND_LABELS[node.kind].lower() if node.kind != K.METER else category(node).label.lower()]
            if node.power_kw:
                details.append(f"{node.power_kw:g} kW".replace(".", ","))
            if node.always_occupied:
                details.append("occupée 24 h/24")
            label = f"{node.name}\\n{', '.join(details)}"
            width = 3.2 if node.id in highlight else 1.4
            color = "#fbbf24" if node.id in highlight else border
            lines.append(f'  n{node.id} [label="{_dot(label)}", shape={shape}, fillcolor="{fill}", color="{color}", '
                         f"penwidth={width}];")
        for rel in self.relations:
            if rel.source_id in self.nodes and rel.target_id in self.nodes:
                lines.append(f'  n{rel.source_id} -> n{rel.target_id} [label="{RELATION_LABELS[rel.kind]}"];')
        lines.append("}")
        return "\n".join(lines)


def _dot(text: str) -> str:
    """Échappe une étiquette DOT (le « \\n » de mise en forme est conservé)."""
    return text.replace("\\n", "\u0000").replace("\\", "\\\\").replace('"', '\\"').replace("\u0000", "\\n")


def _relevant(path: list[tuple[AssetRelation | None, AssetNode]]) -> bool:
    """L'usage final doit pouvoir être servi par CHAQUE équipement de la chaîne : la chaudière et les
    radiateurs mènent au chauffage ; la chaudière et le ballon, à l'eau chaude sanitaire."""
    last = path[-1][1]
    if last.kind != K.USAGE:
        return True
    return all(last.category in RELEVANT_USAGES.get(node.category, {last.category})
               for _, node in path if node.kind == K.EQUIPMENT)


def chain_text(path: list[tuple[AssetRelation | None, AssetNode]]) -> str:
    """« Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage »."""
    parts = []
    for rel, node in path:
        if rel is not None:
            parts.append(RELATION_LABELS[rel.kind])
        parts.append(node.name)
    return " → ".join(parts)


def chain_summaries(paths: list[list[tuple[AssetRelation | None, AssetNode]]]) -> list[str]:
    """Regroupe les chaînes qui ne diffèrent que par leur dernier élément :
    « … → Bloc opératoire → accueille → Chauffage, Ventilation »."""
    groups: dict[tuple, list[AssetNode]] = {}
    heads: dict[tuple, list] = {}
    for path in paths:
        if len(path) < 2:
            continue
        key = (tuple(node.id for _, node in path[:-1]), path[-1][0].kind)
        heads.setdefault(key, path[:-1])
        groups.setdefault(key, []).append(path[-1][1])
    return [f"{chain_text(heads[key])} → {RELATION_LABELS[key[1]]} → {', '.join(n.name for n in ends)}"
            for key, ends in groups.items()]


def load_site_graph(db: Session, site_id: int) -> SiteGraph:
    """Usage interne (périmètre déjà vérifié par l'appelant)."""
    nodes = {n.id: n for n in db.scalars(select(AssetNode).where(AssetNode.site_id == site_id).order_by(AssetNode.id))}
    ids = list(nodes)
    relations = list(db.scalars(select(AssetRelation).where(
        or_(AssetRelation.source_id.in_(ids), AssetRelation.target_id.in_(ids))).order_by(AssetRelation.id))) if ids else []
    return SiteGraph(nodes, relations)


def site_graph(db: Session, repo: TenantRepository, site_id: int) -> SiteGraph:
    """Graphe d'un site du périmètre de l'utilisateur (sinon ResourceNotFound)."""
    repo.get_site(site_id)
    return load_site_graph(db, site_id)


# --- Compteurs : un nœud par point de livraison ---------------------------------------------------


def ensure_meter_node(db: Session, dp: DeliveryPoint) -> AssetNode:
    node = db.scalar(select(AssetNode).where(AssetNode.delivery_point_id == dp.id))
    if node is None:
        site = db.get(Site, dp.site_id)
        node = AssetNode(
            organization_id=site.organization_id, site_id=site.id, kind=K.METER,
            category="ELEC" if dp.fluid == Fluid.ELEC else "GAS", delivery_point_id=dp.id,
            name=f"Compteur {'élec.' if dp.fluid == Fluid.ELEC else 'gaz'} {dp.external_ref}",
        )
        db.add(node)
        db.flush()
    return node


def sync_meter_nodes(db: Session) -> int:
    """Crée les nœuds « compteur » manquants (bases antérieures au graphe). Renvoie le nombre créé."""
    with_node = select(AssetNode.delivery_point_id).where(AssetNode.delivery_point_id.is_not(None))
    missing = list(db.scalars(select(DeliveryPoint).where(DeliveryPoint.id.not_in(with_node))))
    for dp in missing:
        ensure_meter_node(db, dp)
    db.commit()
    return len(missing)


# --- Édition (auditeur) ------------------------------------------------------------------------------


def can_edit(user: User) -> bool:
    return user.role in (Role.AUDITOR, Role.ADMIN)


def _require_editor(user: User) -> None:
    if not can_edit(user):
        raise AssetPermissionError("Le graphe des équipements est tenu par l'auditeur ; l'espace client le consulte.")


def _graph_changed(db: Session, site_id: int) -> None:
    """F2b : les anomalies encore à valider suivent le graphe ; les décisions passées restent figées."""
    from app.services import anomaly_context  # import local : anomaly_context s'appuie sur ce module

    anomaly_context.refresh_open(db, site_id)


def _check_fields(kind: AssetNodeKind, category_code: str, name: str, power_kw: float | None,
                  surface_m2: float | None) -> str:
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise AssetGraphError("Nom obligatoire (120 caractères au plus).")
    if category_code not in CATEGORIES[kind]:
        raise AssetGraphError("Catégorie inconnue pour ce type d'élément.")
    if power_kw is not None and (kind != K.EQUIPMENT or power_kw < 0):
        raise AssetGraphError("La puissance nominale ne concerne qu'un équipement (valeur positive).")
    if surface_m2 is not None and (kind != K.ZONE or surface_m2 <= 0):
        raise AssetGraphError("La surface ne concerne qu'une zone (valeur positive).")
    return name


def create_node(
    db: Session, repo: TenantRepository, user: User, site_id: int, *, kind: AssetNodeKind, category_code: str,
    name: str, power_kw: float | None = None, surface_m2: float | None = None, always_occupied: bool = False,
    notes: str | None = None,
) -> AssetNode:
    _require_editor(user)
    site = repo.get_site(site_id)
    if kind == K.METER:
        raise AssetGraphError("Les compteurs sont créés avec les points de livraison (page « Patrimoine »).")
    name = _check_fields(kind, category_code, name, power_kw, surface_m2)
    node = AssetNode(
        organization_id=site.organization_id, site_id=site.id, kind=kind, category=category_code, name=name,
        power_kw=power_kw, surface_m2=surface_m2, always_occupied=bool(always_occupied) and kind == K.ZONE,
        notes=(notes or "").strip() or None, created_by=user.id,
    )
    db.add(node)
    db.commit()
    _graph_changed(db, site.id)
    return node


def update_node(
    db: Session, repo: TenantRepository, user: User, node_id: int, *, name: str, category_code: str,
    power_kw: float | None = None, surface_m2: float | None = None, always_occupied: bool = False,
    notes: str | None = None,
) -> AssetNode:
    _require_editor(user)
    node = repo.get_asset_node(node_id)
    node.name = _check_fields(node.kind, category_code, name, power_kw, surface_m2)
    if node.kind != K.METER:
        node.category = category_code
    node.power_kw, node.surface_m2 = power_kw, surface_m2
    node.always_occupied = bool(always_occupied) and node.kind == K.ZONE
    node.notes = (notes or "").strip() or None
    db.commit()
    _graph_changed(db, node.site_id)
    return node


def delete_node(db: Session, repo: TenantRepository, user: User, node_id: int) -> None:
    _require_editor(user)
    node = repo.get_asset_node(node_id)
    if node.kind == K.METER:
        raise AssetGraphError("Un compteur suit son point de livraison : il ne se supprime pas ici.")
    for rel in db.scalars(select(AssetRelation).where(
            or_(AssetRelation.source_id == node.id, AssetRelation.target_id == node.id))):
        db.delete(rel)
    # Les recommandations gardent leur texte ; seul le lien vers l'équipement disparaît.
    for rec in db.scalars(select(Recommendation).where(Recommendation.equipment_id == node.id)):
        rec.equipment_id = None
    site_id = node.site_id
    db.delete(node)
    db.commit()
    _graph_changed(db, site_id)


def create_relation(
    db: Session, repo: TenantRepository, user: User, source_id: int, kind: AssetRelationKind, target_id: int
) -> AssetRelation:
    _require_editor(user)
    source, target = repo.get_asset_node(source_id), repo.get_asset_node(target_id)
    if source.id == target.id:
        raise AssetGraphError("Un élément ne peut pas être relié à lui-même.")
    if source.site_id != target.site_id:
        raise AssetGraphError("Les deux éléments doivent appartenir au même site.")
    if (source.kind, kind, target.kind) not in ALLOWED_RELATIONS:
        raise AssetGraphError(
            f"Relation physiquement incohérente : un élément de type « {KIND_LABELS[source.kind].lower()} » "
            f"ne peut pas « {RELATION_LABELS[kind]} » un élément de type « {KIND_LABELS[target.kind].lower()} »."
        )
    if source.kind == K.METER and target.kind == K.METER and source.category != target.category:
        raise AssetGraphError("Un sous-compteur mesure la même énergie que son compteur parent.")
    if db.scalar(select(AssetRelation.id).where(AssetRelation.source_id == source.id,
                                                AssetRelation.target_id == target.id, AssetRelation.kind == kind)):
        raise AssetGraphError("Cette relation existe déjà.")
    relation = AssetRelation(organization_id=source.organization_id, source_id=source.id, target_id=target.id,
                             kind=kind, created_by=user.id)
    db.add(relation)
    db.commit()
    _graph_changed(db, source.site_id)
    return relation


def delete_relation(db: Session, repo: TenantRepository, user: User, relation_id: int) -> None:
    _require_editor(user)
    relation = db.scalar(select(AssetRelation).where(
        AssetRelation.id == relation_id, repo.org_clause(AssetRelation.organization_id)))
    if relation is None:
        raise ResourceNotFound()
    site_id = db.get(AssetNode, relation.source_id).site_id
    db.delete(relation)
    db.commit()
    _graph_changed(db, site_id)
