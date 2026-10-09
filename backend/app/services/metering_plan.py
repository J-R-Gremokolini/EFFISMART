"""R5 — Plan de comptage d'un site (formation PRO-REFEI, SP5 ; guide ComptIAA ; fascicule AFNOR FD X30-147).

« Sans mesure, pas de progrès » : le plan de comptage est l'outil d'aide à la décision qui donne une vue
d'ensemble des énergies consommées ; il se déploie progressivement, en amélioration continue.

1. Niveau atteint (ADEME) : 1 = compteurs de livraison et factures ; 2 = sous-comptage des postes clés ;
   3 = cartographie complète (chaque usage significatif a son comptage).
2. Qualité des données par compteur : courbe 30 min (N1), factures (N0) ou rien.
3. Recoupement : un plan de comptage bien conçu permet de vérifier la cohérence des valeurs comptées. Pour un
   compteur général et ses sous-compteurs (graphe des équipements, relation « alimente » entre compteurs), la
   somme des sous-compteurs ne peut pas dépasser le général ; l'écart est la part non mesurée, un compteur
   virtuel (général − sous-compteurs).
4. Sous-compteurs à poser en priorité : les usages significatifs sans comptage propre, du plus gros au plus petit.
5. Les 4 étapes de la méthode ComptIAA, cochées d'après les données de la plateforme.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssetNodeKind, AssetRelationKind, DeliveryPoint, Fluid, IpeDefinition, ReviewStatus, Site
from app.services import assets
from app.services.consent import has_active_consent
from app.services.energy_balance import UsageRow
from app.services.energy_data import combined_daily, has_declared
from app.services.drift import daily_kwh, half_hour_powers
from app.services.validation import fr

K, R = AssetNodeKind, AssetRelationKind
LEVELS = {1: "Compteurs de livraison et factures", 2: "Sous-comptage des postes clés", 3: "Cartographie complète"}
# Ordre de grandeur de la formation (cas fil rouge : 10 sous-compteurs électriques, 9 000 € HT).
SUBMETER_COST_EUR = {Fluid.ELEC: 900}
N1_DATA = ("Courbe 30 min", "Index journaliers")
RECONCILIATION_TOLERANCE = 0.02  # au-delà de +2 %, la somme des sous-compteurs dépasse le général : incohérence


@dataclass
class MeterInfo:
    delivery_point: DeliveryPoint
    name: str
    data: str  # « Courbe 30 min », « Index journaliers », « Factures (N0) », « Sans consentement », « Aucune donnée »
    kwh: float
    parent_id: int | None = None  # point de livraison du compteur amont (sous-comptage)


@dataclass
class Reconciliation:
    main: MeterInfo
    subs: list[MeterInfo]
    status: str  # OK, INCOHERENT
    message: str

    @property
    def subs_kwh(self) -> float:
        return sum(s.kwh for s in self.subs)

    @property
    def unmetered_kwh(self) -> float:
        return self.main.kwh - self.subs_kwh

    @property
    def unmetered_share(self) -> float | None:
        return self.unmetered_kwh / self.main.kwh if self.main.kwh else None


@dataclass
class Suggestion:
    usage: str
    label: str
    share: float
    kwh: float
    equipment: list[str]
    cost_hint: str


@dataclass
class MeteringAssessment:
    level: int
    meters: list[MeterInfo]
    reconciliations: list[Reconciliation]
    measured_usages: dict[str, str]  # usage → compteur qui le mesure seul
    suggestions: list[Suggestion]
    steps: list[dict]  # [{"title", "done", "detail"}]
    notes: list[str] = field(default_factory=list)

    @property
    def level_label(self) -> str:
        return LEVELS[self.level]

    @property
    def smart_share(self) -> float:
        """Part de la consommation suivie par compteur communicant (N1 : courbe 30 min ou index journaliers)."""
        total = sum(m.kwh for m in self.meters if m.parent_id is None)
        smart = sum(m.kwh for m in self.meters if m.parent_id is None and m.data in N1_DATA)
        return smart / total if total else 0.0


def _meter_info(db: Session, dp: DeliveryPoint, start: date, end: date, parent_id: int | None) -> MeterInfo:
    name = f"{'Électricité' if dp.fluid == Fluid.ELEC else 'Gaz'} {dp.external_ref}"
    consented = has_active_consent(db, dp.id)
    recent = consented and bool(daily_kwh(db, dp.id, end - timedelta(days=30), end))
    kwh = sum(v for v, _ in combined_daily(db, dp, start, end).values())
    if recent:
        curve = bool(half_hour_powers(db, dp.id, end - timedelta(days=2), end))
        data = "Courbe 30 min" if curve else "Index journaliers"
    elif dp.id in has_declared(db, [dp.id]):
        data = "Factures (N0)"
    else:
        data = "Aucune donnée" if consented else "Sans consentement"
    return MeterInfo(dp, name, data, kwh, parent_id)


def assess(db: Session, site: Site, end: date, usages: list[UsageRow] | None = None) -> MeteringAssessment:
    start = end - timedelta(days=364)
    graph = assets.load_site_graph(db, site.id)
    parent_of: dict[int, int] = {}
    for rel in graph.relations:
        source, target = graph.nodes.get(rel.source_id), graph.nodes.get(rel.target_id)
        if (source and target and rel.kind == R.SUPPLIES and source.kind == K.METER and target.kind == K.METER
                and source.delivery_point_id and target.delivery_point_id):
            parent_of[target.delivery_point_id] = source.delivery_point_id
    meters = [_meter_info(db, dp, start, end, parent_of.get(dp.id)) for dp in site.delivery_points]
    by_dp = {m.delivery_point.id: m for m in meters}
    reconciliations = []
    for main in meters:
        subs = [m for m in meters if m.parent_id == main.delivery_point.id]
        if not subs:
            continue
        total = sum(s.kwh for s in subs)
        if main.kwh and total > main.kwh * (1 + RECONCILIATION_TOLERANCE):
            status = "INCOHERENT"
            message = (f"Les sous-compteurs totalisent {fr(total)} kWh, plus que le compteur général "
                       f"({fr(main.kwh)} kWh) : vérifier le câblage, les coefficients ou le rattachement dans le graphe.")
        else:
            status = "OK"
            share = (main.kwh - total) / main.kwh if main.kwh else 0.0
            message = (f"Part non mesurée (compteur virtuel « général − sous-compteurs ») : {fr(share * 100)} % de la "
                       "consommation, soit les usages non sous-comptés et les pertes.")
        reconciliations.append(Reconciliation(main, subs, status, message))
    # Usage mesuré : un compteur dont tous les équipements alimentés ne servent que cet usage.
    measured: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.kind != K.METER or node.delivery_point_id not in by_dp:
            continue
        served = {u.category for eq in graph.direct_consumers(node.id) for u in graph.usages_served(eq.id)}
        if len(served) == 1:
            measured.setdefault(served.pop(), by_dp[node.delivery_point_id].name)
    suggestions = []
    for row in usages or []:
        if not row.significant or row.usage in measured or row.usage == "OTHER":
            continue
        equipment = sorted({eq.name for eq in graph.nodes.values() if eq.kind == K.EQUIPMENT
                            and any(u.category == row.usage for u in graph.usages_served(eq.id))})
        elec = "ELEC" in row.by_fluid and len(row.by_fluid) == 1
        hint = (f"environ {fr(SUBMETER_COST_EUR[Fluid.ELEC])} € HT par sous-compteur électrique (cas de la formation)"
                if elec else "compteur gaz ou thermique : sur devis")
        suggestions.append(Suggestion(row.usage, row.label, row.share, row.kwh, equipment, hint))
    has_sub = bool(parent_of) or len([m for m in meters if m.delivery_point.fluid == Fluid.ELEC]) > 1
    significant = [r for r in usages or [] if r.significant and r.usage != "OTHER"]
    level = 1
    if has_sub or measured:
        level = 2
    if significant and all(r.usage in measured for r in significant):
        level = 3
    validated_ipe = db.scalar(select(IpeDefinition.id).where(IpeDefinition.site_id == site.id,
                                                             IpeDefinition.status == ReviewStatus.VALIDATED).limit(1))
    targeted_ipe = db.scalar(select(IpeDefinition.id).where(IpeDefinition.site_id == site.id,
                                                            IpeDefinition.status == ReviewStatus.VALIDATED,
                                                            IpeDefinition.target_value.is_not(None)).limit(1))
    equipment_count = sum(1 for n in graph.nodes.values() if n.kind == K.EQUIPMENT)
    steps = [
        {"title": "1. État des lieux énergétique et métrologique",
         "done": bool(meters) and equipment_count > 0,
         "detail": f"{len(meters)} compteur(s) suivi(s), {equipment_count} équipement(s) décrit(s) dans le graphe."},
        {"title": "2. Facteurs d'influence et IPE",
         "done": validated_ipe is not None,
         "detail": "IPE validé pour le site." if validated_ipe else "Aucun IPE validé : page Performance (IPE)."},
        {"title": "3. Points de mesure et instrumentation",
         "done": level >= 2,
         "detail": LEVELS[level] + (f" ; {len(suggestions)} sous-compteur(s) à envisager." if suggestions else ".")},
        {"title": "4. Suivi des IPE et pérennisation des économies",
         "done": targeted_ipe is not None,
         "detail": "Valeur cible et seuil d'alerte fixés." if targeted_ipe else
                   "Fixez une valeur cible et un seuil d'alerte à un IPE validé."},
    ]
    notes = []
    if any(m.data == "Sans consentement" for m in meters):
        notes.append("Un compteur sans consentement actif n'est pas exploité : l'étape d'accès aux données reste à "
                     "faire (page Patrimoine & consentements).")
    return MeteringAssessment(level, meters, reconciliations, measured, suggestions, steps, notes)
