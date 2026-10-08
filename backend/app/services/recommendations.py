"""Recommandations d'optimisation (principe P1).

Une recommandation naît d'une anomalie **validée par un humain** et du graphe physique du site.
Elle est proposée avec son raisonnement, son gain estimé et son niveau de confiance, puis validée,
écartée ou déclarée appliquée par l'auditeur ou le responsable énergie. La plateforme n'agit jamais
sur les équipements.

Règles d'expert, sans apprentissage automatique, partagées avec la contextualisation des anomalies (F2b) :
l'équipement visé est l'équipement suspect désigné par le contexte de l'anomalie.
- talon de nuit ou consommation en inoccupation → arrêt ou ralenti hors occupation de l'équipement dont la
  puissance correspond à l'excès, en écartant ceux qui desservent uniquement des zones occupées 24 h/24 ;
- écart climatique (ou seuil journalier de gaz) → contrôle de la régulation du générateur qui alimente les zones ;
- dépassement de puissance → délestage ou décalage de l'équipement capable d'expliquer le pic ;
- graphe incomplet → identifier l'équipement en cause (confiance faible).
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AssetNode,
    AssetNodeKind,
    Drift,
    DriftKind,
    DriftStatus,
    Fluid,
    Recommendation,
    RecommendationKind,
    ReviewStatus,
    User,
)
from app.services import assets
from app.services.anomaly_context import (
    heating_season,
    peak_candidates,
    rank_by_power,
    schedule_candidates,
    season_dju,
    usual_peak,
)
from app.services.drift import DRIFT_LABELS, night_period, off_hours_period
from app.services.validation import Assessment, apply_assessment, fr, fr_kw, notify, validator_label

logger = logging.getLogger(__name__)

ALGORITHM = "Recommandations v1 (règles d'expert + graphe physique)"
ACTIVE_STATUSES = (ReviewStatus.PROPOSED, ReviewStatus.VALIDATED)
KIND_LABELS = {
    RecommendationKind.SCHEDULE_OFF_HOURS: "Arrêt hors occupation",
    RecommendationKind.HEATING_CONTROL: "Régulation chauffage / climatisation",
    RecommendationKind.PEAK_SHAVING: "Écrêtement des pointes",
    RecommendationKind.INVESTIGATE: "Recherche de la cause",
    RecommendationKind.LOAD_SHIFT: "Décalage de charge (conseil)",
}


def _names(nodes: list[AssetNode]) -> str:
    return ", ".join(n.name for n in nodes) if nodes else "non renseignées"


def _chains(a: Assessment, graph: assets.SiteGraph, node: AssetNode) -> None:
    summaries = assets.chain_summaries(graph.physical_paths(node.id))
    if summaries:
        a.step("Chaîne physique concernée : " + " ; ".join(summaries[:4]) + ".")


def _off_hours(drift: Drift) -> str:
    return off_hours_period(drift.day) if drift.kind == DriftKind.OFF_HOURS else night_period()


def _investigate(a: Assessment, drift: Drift, reason: str, when: str) -> tuple:
    dp = drift.delivery_point
    a.base_confidence = 0.35
    a.step(f"Graphe physique : {reason}")
    a.factor("Équipement en cause non identifiable avec le graphe actuel", -0.05)
    return (
        RecommendationKind.INVESTIGATE, None,
        f"Identifier l'équipement en cause ({dp.site.name}, compteur {dp.external_ref})",
        f"Relever sur site, ou via la GTB, les équipements en fonctionnement {when}, puis compléter le graphe "
        "physique du site (page « Équipements ») : la plateforme pourra alors cibler sa recommandation.",
        1.0,
    )


def _match_power(a: Assessment, candidates: list[AssetNode], target_kw: float, what: str) -> tuple[AssetNode, float | None]:
    """Équipement dont la puissance nominale correspond le mieux à `target_kw`, avec l'explication du choix."""
    scored = rank_by_power(candidates, target_kw)
    best, share = scored[0]
    if share is None:
        a.factor("Puissance nominale de l'équipement inconnue : correspondance non vérifiable", -0.1)
    else:
        a.step(f"Correspondance de puissance : {best.name} ({fr_kw(best.power_kw)}) pour {what} de "
               f"{fr(target_kw)} kW, soit {fr(share * 100)} % de correspondance.")
        if share >= 0.7:
            a.factor(f"Puissance nominale cohérente avec {what}", 0.15)
        elif share >= 0.4:
            a.factor(f"Puissance nominale partiellement cohérente avec {what}", 0.05)
        else:
            a.factor(f"Puissance nominale peu cohérente avec {what}", -0.1)
    if len(scored) == 1:
        a.factor("Un seul équipement candidat derrière ce compteur", 0.05)
    elif scored[1][1] is not None and share is not None and share - scored[1][1] < 0.15:
        a.factor(f"{scored[1][0].name} pourrait aussi l'expliquer", -0.1)
    return best, share


def _list_consumers(a: Assessment, consumers: list[AssetNode]) -> None:
    a.step("Équipements alimentés par ce compteur : " + ", ".join(
        f"{e.name} ({fr_kw(e.power_kw)})" if e.power_kw else f"{e.name} (puissance inconnue)" for e in consumers) + ".")


def _baseload_rule(a: Assessment, graph: assets.SiteGraph, drift: Drift, consumers: list[AssetNode]) -> tuple:
    excess_kw = drift.measured_value - drift.reference_value
    when = _off_hours(drift)
    a.step(f"Excès mesuré {when} : {fr(excess_kw)} kW au-dessus du niveau attendu.")
    if not consumers:
        return _investigate(a, drift, "aucun équipement n'est relié à ce compteur.", when)
    candidates, excluded = schedule_candidates(graph, consumers)
    _list_consumers(a, consumers)
    if excluded:
        a.step("Écartés d'après le graphe : " + " ; ".join(excluded) + ".")
    if not candidates:
        return _investigate(a, drift, "tous les équipements de ce compteur fonctionnent légitimement en continu ; "
                                      "l'excès vient d'un équipement non modélisé.", when)
    best, _ = _match_power(a, candidates, excess_kw, "l'excès mesuré")
    _chains(a, graph, best)
    zones = graph.downstream(best.id, AssetNodeKind.ZONE)
    usages = graph.usages_served(best.id)
    explained = min(1.0, (best.power_kw or excess_kw) / excess_kw) if excess_kw > 0 else 1.0
    return (
        RecommendationKind.SCHEDULE_OFF_HOURS, best,
        f"Arrêter « {best.name} » hors occupation ({drift.delivery_point.site.name})",
        f"Programmer l'arrêt ou le ralenti de « {best.name} » {night_period()}, le week-end et hors des horaires "
        "d'occupation, via la GTB ou l'horloge de l'équipement. "
        f"Avant d'intervenir, vérifier avec l'exploitant qu'aucun usage des zones desservies ({_names(zones)} ; "
        f"usages : {_names(usages)}) ne l'exige en dehors des heures d'occupation.",
        explained,
    )


def _control_rule(a: Assessment, db: Session, graph: assets.SiteGraph, drift: Drift,
                  consumers: list[AssetNode]) -> tuple:
    heating, dju = heating_season(db, drift)
    if dju is not None:
        a.step(f"Saison : {fr(dju, 1)} DJU ce jour-là, besoin de {'chauffage' if heating else 'refroidissement'}.")
    if not consumers:
        return _investigate(a, drift, "aucun équipement n'est relié à ce compteur.", "aux heures de dérive")
    producers = [c for c in consumers if (assets.category(c).heat_producer if heating
                                          else assets.category(c).cold_producer)]
    site_name = drift.delivery_point.site.name
    if not producers:
        # Aucun générateur de la bonne nature : l'excès vient d'un autre équipement, ciblé par sa puissance.
        excess_kwh = drift.measured_value - drift.reference_value
        a.step(f"Aucun générateur de {'chaleur' if heating else 'froid'} derrière ce compteur : l'excès de "
               f"{fr(excess_kwh)} kWh sur la journée correspond à {fr(excess_kwh / 24)} kW en moyenne.")
        _list_consumers(a, consumers)
        best, _ = _match_power(a, consumers, excess_kwh / 24, "l'excès moyen")
        a.factor("Cause déduite de la seule puissance, sans générateur identifié", -0.05)
        _chains(a, graph, best)
        return (
            RecommendationKind.INVESTIGATE, best, f"Vérifier le fonctionnement de « {best.name} » ({site_name})",
            f"Vérifier le fonctionnement et la programmation de « {best.name} », dont la puissance est compatible "
            f"avec l'excès moyen de {fr(excess_kwh / 24)} kW : consignes, horaires, défaut éventuel. Si un autre "
            "équipement est en cause, compléter le graphe physique du site.",
            1.0,
        )
    producers.sort(key=lambda n: n.power_kw or 0, reverse=True)
    best = producers[0]
    a.step(f"Générateur de {'chaleur' if heating else 'froid'} alimenté par ce compteur : {best.name}"
           + (f" ({fr_kw(best.power_kw)})" if best.power_kw else "") + ".")
    if len(producers) == 1:
        a.factor("Un seul générateur derrière ce compteur : la cause est bien localisée", 0.1)
    else:
        a.factor(f"{len(producers)} générateurs possibles derrière ce compteur", -0.05)
    zones = graph.downstream(best.id, AssetNodeKind.ZONE)
    if zones:
        a.factor("Zones desservies identifiées dans le graphe", 0.05)
    else:
        a.factor("Zones desservies non renseignées dans le graphe", -0.05)
    _chains(a, graph, best)
    if heating:
        action = (f"Contrôler la régulation de « {best.name} » : loi d'eau, consignes de température des zones "
                  f"desservies ({_names(zones)}), réduits de nuit et de week-end, état des vannes et des sondes. "
                  "Suivre ensuite la consommation corrigée du climat pendant 4 semaines.")
    else:
        action = (f"Contrôler les consignes de refroidissement de « {best.name} » et des zones desservies "
                  f"({_names(zones)}) : température de consigne, plages horaires, relances en inoccupation.")
    return (RecommendationKind.HEATING_CONTROL, best, f"Contrôler la régulation de « {best.name} » ({site_name})",
            action, 1.0)


def _peak_rule(a: Assessment, db: Session, graph: assets.SiteGraph, drift: Drift, consumers: list[AssetNode]) -> tuple:
    excess_kw = drift.measured_value - drift.reference_value
    match = re.search(r"à (\d{2}:\d{2})", drift.details)
    when = f"vers {match.group(1)}" if match else "au moment du pic"
    a.step(f"Excès au pic : {fr(excess_kw)} kW au-dessus du seuil, {when}.")
    usual, days = usual_peak(db, drift)
    jump = excess_kw
    if usual is not None and drift.measured_value > usual:
        jump = drift.measured_value - usual
        a.step(f"Pic habituel : {fr(usual)} kW (médiane des {days} derniers jours "
               f"{'ouvrés' if drift.day.weekday() < 5 else 'de week-end'}) : ce jour-là, {fr(jump)} kW "
               "supplémentaires ont été appelés.")
    if not consumers:
        return _investigate(a, drift, "aucun équipement n'est relié à ce compteur.", when)
    candidates, excluded = peak_candidates(graph, consumers, season_dju(db, drift))
    _list_consumers(a, consumers)
    if excluded:
        a.step("Écartés d'après le graphe : " + " ; ".join(excluded) + ".")
    if not candidates:
        return _investigate(a, drift, "aucun équipement compatible avec ce pic n'est modélisé.", when)
    best, _ = _match_power(a, candidates, jump, "la hausse du pic")
    _chains(a, graph, best)
    return (
        RecommendationKind.PEAK_SHAVING, best,
        f"Écrêter les appels de puissance de « {best.name} » ({drift.delivery_point.site.name})",
        f"Décaler ou délester « {best.name} » pendant la plage du pic ({when}) : réduire l'appel d'environ "
        f"{fr(excess_kw)} kW suffit à rester sous le seuil. Étaler aussi les démarrages des équipements ; si les "
        "dépassements persistent, étudier avec le fournisseur l'ajustement de la puissance souscrite.",
        1.0,
    )


def propose_for_drift(db: Session, drift: Drift) -> Recommendation | None:
    """Propose (sans valider) une recommandation pour une anomalie validée. Sans commit.

    Si une recommandation active vise déjà le même équipement pour le même motif, l'anomalie vient
    l'appuyer au lieu d'en créer une seconde.
    """
    if drift.status != DriftStatus.QUALIFIED:
        return None
    dp = drift.delivery_point
    site = dp.site
    graph = assets.load_site_graph(db, site.id)
    meter = graph.meter_for(dp.id)
    consumers = graph.direct_consumers(meter.id) if meter else []

    a = Assessment(algorithm=ALGORITHM)
    validator = db.get(User, drift.qualified_by) if drift.qualified_by else None
    a.step(f"Point de départ : anomalie « {DRIFT_LABELS[drift.kind].lower()} » du {drift.day:%d/%m/%Y} sur le "
           f"compteur {dp.external_ref} ({site.name}), validée par {validator_label(validator)}"
           + (f" : « {drift.comment} »." if drift.comment else "."))
    a.factor("Anomalie d'origine validée par un humain", 0.1)
    if drift.confidence is not None:
        a.factor(f"Confiance de l'anomalie d'origine ({fr(drift.confidence * 100)} %)",
                 round((drift.confidence - 0.5) / 2, 2))

    if drift.kind in (DriftKind.BASELOAD, DriftKind.OFF_HOURS):
        kind, target, title, action, share = _baseload_rule(a, graph, drift, consumers)
    elif drift.kind == DriftKind.THRESHOLD and dp.fluid == Fluid.ELEC:
        kind, target, title, action, share = _peak_rule(a, db, graph, drift, consumers)
    else:
        kind, target, title, action, share = _control_rule(a, db, graph, drift, consumers)

    existing = db.scalar(select(Recommendation).where(
        Recommendation.delivery_point_id == dp.id, Recommendation.kind == kind,
        Recommendation.equipment_id.is_(None) if target is None else Recommendation.equipment_id == target.id,
        Recommendation.status.in_(ACTIVE_STATUSES),
    ))
    if existing is not None:
        support = list(existing.supporting_drift_ids or [])
        if drift.id != existing.drift_id and drift.id not in support:
            existing.supporting_drift_ids = support + [drift.id]
        return existing

    a.step("Statut : proposition de la plateforme, à valider. L'intervention sur l'équipement reste une décision "
           "et un geste humains ; la plateforme ne pilote aucun équipement.")
    if drift.gain_kwh:
        a.gain_kwh = drift.gain_kwh * share
        a.gain_basis = (f"Gain annuel estimé de l'anomalie validée ({fr(drift.gain_kwh)} kWh/an)"
                        + (f" × part de l'excès attribuable à l'équipement ({fr(share * 100)} %)" if share < 1 else "")
                        + ". " + (drift.gain_basis or ""))
    else:
        a.gain_basis = "Gain non chiffrable : l'anomalie d'origine n'a pas de gain estimé."

    rec = Recommendation(
        organization_id=site.organization_id, site_id=site.id, delivery_point_id=dp.id, drift_id=drift.id,
        equipment_id=target.id if target is not None else None, kind=kind, title=title[:200], action=action,
        supporting_drift_ids=[],
    )
    apply_assessment(db, rec, a, dp.fluid, drift.day)
    db.add(rec)
    db.flush()
    notify(db, site.organization_id, f"À valider : recommandation « {rec.title} »",
           validators_only=True, exclude_user_id=drift.qualified_by)
    return rec


def propose_missing(db: Session) -> int:
    """Mise à niveau : anomalies validées avant l'existence des recommandations → recommandation proposée."""
    covered = {rec.drift_id for rec in db.scalars(select(Recommendation))}
    for ids in db.scalars(select(Recommendation.supporting_drift_ids)):
        covered.update(ids or [])
    count = 0
    for drift in db.scalars(select(Drift).where(Drift.status == DriftStatus.QUALIFIED).order_by(Drift.day)).all():
        if drift.id not in covered and propose_for_drift(db, drift) is not None:
            count += 1
    db.commit()
    return count


def withdraw_for_drift(db: Session, drift: Drift) -> None:
    """L'anomalie n'est plus validée : ses recommandations encore *proposées* perdent leur fondement. Sans commit.

    Retirer sa propre proposition non validée n'est pas une décision : une recommandation déjà validée
    par un humain reste en l'état.
    """
    for rec in db.scalars(select(Recommendation).where(
            Recommendation.delivery_point_id == drift.delivery_point_id,
            Recommendation.status == ReviewStatus.PROPOSED)):
        support = [i for i in (rec.supporting_drift_ids or []) if i != drift.id]
        still_valid = [i for i in support if (d := db.get(Drift, i)) is not None and d.status == DriftStatus.QUALIFIED]
        if rec.drift_id == drift.id:
            if still_valid:
                rec.drift_id, rec.supporting_drift_ids = still_valid[0], still_valid[1:]
            else:
                rec.status = ReviewStatus.SUPERSEDED
                rec.review_comment = "Retirée par la plateforme : l'anomalie d'origine n'est plus validée."
        elif drift.id in (rec.supporting_drift_ids or []):
            rec.supporting_drift_ids = support
