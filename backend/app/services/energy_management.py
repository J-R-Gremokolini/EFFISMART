"""R3 — Management de l'énergie : auto-évaluation de la démarche et socle ISO 50001 (formation PRO-REFEI, SP1, SP5).

Auto-évaluation : 38 questions réparties en 12 thèmes et 5 axes, qui suivent la logique PDCA de l'ISO 50001
(définir sa stratégie, planifier, mettre en œuvre, mesurer et vérifier, améliorer en continu). Démarche inspirée
de la check-list « énergie CHECK » de l'ATEE, diffusée pendant la formation ; questions reformulées pour EffiSmart.
Réponses : tout à fait (2), partiellement (1), pas du tout (0), non applicable (exclue). Un thème vaut la somme
de ses points sur le maximum possible ; un axe, la moyenne de ses thèmes. Une question sans réponse compte 0.

La plateforme ne répond pas à la place du référent énergie : pour chaque question, elle montre ce que ses données
en disent (indices). Les évaluations successives sont conservées pour suivre la progression.

Socle : politique énergétique validée par la direction, périmètre, équipe énergie (rôles et missions), objectifs
SMART (indicateur, valeur de départ, cible, échéance, responsable) et périodicité de la revue énergétique.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ActionStatus,
    AssetNode,
    AssetNodeKind,
    CommunicationMessage,
    CommunicationStatus,
    DeliveryPoint,
    Drift,
    DriftStatus,
    EnergyAction,
    EnergyManagement,
    IpeDefinition,
    MaturityAssessment,
    Organization,
    Recommendation,
    RegulatoryDeadline,
    ReviewStatus,
    Site,
    User,
)
from app.repositories import TenantRepository
from app.services import ipe
from app.timeutils import add_months, today_local, utcnow

AXES = {
    "STRATEGY": "Définir sa stratégie énergétique",
    "PLAN": "Planifier sa démarche",
    "DO": "Mettre en œuvre",
    "CHECK": "Mesurer et vérifier",
    "ACT": "Améliorer en continu",
}
THEMES = {
    "POLICY": ("STRATEGY", "Politique énergétique"),
    "ORGANISATION": ("STRATEGY", "Organisation, équipe énergie"),
    "REGULATION": ("PLAN", "Exigences réglementaires"),
    "KNOWLEDGE": ("PLAN", "Connaissance des consommations"),
    "ACTION_PLAN": ("PLAN", "Objectifs et plan d'actions"),
    "SKILLS": ("DO", "Compétences et sensibilisation"),
    "COMMUNICATION": ("DO", "Communication"),
    "OPERATIONS": ("DO", "Maîtrise opérationnelle"),
    "PURCHASING": ("DO", "Achats, conception, investissements"),
    "MONITORING": ("CHECK", "Mesure et suivi"),
    "VERIFICATION": ("CHECK", "Vérification des résultats"),
    "IMPROVEMENT": ("ACT", "Revue et amélioration continue"),
}
QUESTIONS = [
    ("P1", "POLICY", "La direction a-t-elle validé une politique énergétique écrite ?"),
    ("P2", "POLICY", "Cette politique fixe-t-elle des objectifs chiffrés et un engagement d'amélioration continue ?"),
    ("P3", "POLICY", "La direction s'engage-t-elle à fournir les moyens nécessaires (budget, temps, compétences) ?"),
    ("P4", "POLICY", "La politique privilégie-t-elle l'achat et la conception d'équipements et de services performants ?"),
    ("P5", "POLICY", "La politique est-elle connue de l'ensemble du personnel ?"),
    ("O1", "ORGANISATION", "Le périmètre de la démarche (sites, énergies, activités) est-il clairement défini ?"),
    ("O2", "ORGANISATION", "Une équipe énergie, animée par un référent énergie, pilote-t-elle la démarche ?"),
    ("O3", "ORGANISATION", "L'équipe énergie dispose-t-elle de l'autorité, du temps et du budget nécessaires ?"),
    ("O4", "ORGANISATION", "L'équipe énergie se réunit-elle régulièrement et rend-elle compte à la direction ?"),
    ("G1", "REGULATION", "Les exigences réglementaires liées à l'énergie (audit, Décret Tertiaire, CEE…) sont-elles "
                         "identifiées ?"),
    ("G2", "REGULATION", "Une veille réglementaire et technique (réglementation, BREF, aides) est-elle organisée ?"),
    ("G3", "REGULATION", "Le respect de ces exigences est-il suivi (échéances, preuves) ?"),
    ("K1", "KNOWLEDGE", "Disposez-vous d'un bilan énergétique par énergie et par usage ?"),
    ("K2", "KNOWLEDGE", "Les usages énergétiques significatifs (UES) sont-ils identifiés et analysés ?"),
    ("K3", "KNOWLEDGE", "Les facteurs d'influence (production, climat, occupation…) sont-ils identifiés ?"),
    ("K4", "KNOWLEDGE", "Une situation énergétique de référence et des IPE sont-ils définis ?"),
    ("K5", "KNOWLEDGE", "Les gisements d'économies sont-ils identifiés et hiérarchisés ?"),
    ("A1", "ACTION_PLAN", "Des objectifs chiffrés et datés découlent-ils de cette analyse ?"),
    ("A2", "ACTION_PLAN", "Le plan d'actions donne-t-il à chaque action un responsable, une échéance, des moyens et "
                          "une méthode de vérification ?"),
    ("S1", "SKILLS", "Les compétences des personnes clés (énergie, maintenance, production) sont-elles évaluées ?"),
    ("S2", "SKILLS", "Des formations sont-elles proposées selon les besoins identifiés ?"),
    ("S3", "SKILLS", "Le personnel et les sous-traitants sont-ils sensibilisés à l'effet de leurs pratiques sur les "
                     "consommations ?"),
    ("C1", "COMMUNICATION", "Le personnel est-il informé des résultats et des actions (affichage, réunions, écrans) ?"),
    ("C2", "COMMUNICATION", "Chacun peut-il faire remonter ses idées d'économies à l'équipe énergie ?"),
    ("C3", "COMMUNICATION", "La performance énergétique fait-elle l'objet d'une communication externe ?"),
    ("M1", "OPERATIONS", "La chasse aux gaspillages fait-elle partie des plans de maintenance ?"),
    ("M2", "OPERATIONS", "Les consignes d'exploitation (process, utilités, bâtiments) intègrent-elles la performance "
                         "énergétique ?"),
    ("B1", "PURCHASING", "Les achats d'équipements et d'énergie intègrent-ils des critères de performance "
                         "énergétique ?"),
    ("B2", "PURCHASING", "La performance énergétique est-elle prise en compte dès la conception des projets ?"),
    ("B3", "PURCHASING", "Les investissements sont-ils évalués en coût global, sur la durée de vie (VAN, TRI) ?"),
    ("B4", "PURCHASING", "Les aides (CEE, ADEME, Fonds Chaleur…) sont-elles recherchées avant d'engager les "
                         "investissements ?"),
    ("N1", "MONITORING", "Un plan de comptage adapté au site est-il en place ?"),
    ("N2", "MONITORING", "Les consommations des UES, les IPE et les facteurs d'influence sont-ils suivis ?"),
    ("N3", "MONITORING", "Les dérives de consommation sont-elles détectées rapidement et traitées ?"),
    ("V1", "VERIFICATION", "L'atteinte des objectifs du plan d'actions est-elle vérifiée (mesure et vérification) ?"),
    ("V2", "VERIFICATION", "Des actions correctives sont-elles engagées quand un objectif n'est pas atteint ?"),
    ("I1", "IMPROVEMENT", "La démarche est-elle revue périodiquement (politique, IPE, objectifs, plan de comptage) ?"),
    ("I2", "IMPROVEMENT", "Ces revues débouchent-elles sur des améliorations (objectifs, moyens, indicateurs) ?"),
]
ANSWERS = {2: "Tout à fait", 1: "Partiellement", 0: "Pas du tout", -1: "Non applicable"}
# Pistes quand un thème est faible : ce que la formation recommande et où le faire dans EffiSmart.
THEME_ADVICE = {
    "POLICY": "Faire valider par la direction une politique énergétique écrite, avec des objectifs chiffrés (page "
              "Management de l'énergie, socle).",
    "ORGANISATION": "Constituer l'équipe énergie (direction, maintenance, production, achats, travaux neufs) autour "
                    "du référent, avec des missions écrites et une réunion régulière.",
    "REGULATION": "Recenser les obligations (audit énergétique, Décret Tertiaire, ISO 50001) et suivre leurs "
                  "échéances (page Réglementaire).",
    "KNOWLEDGE": "Établir le bilan énergétique, le Pareto des usages et les IPE (pages Bilan énergétique et "
                 "Performance).",
    "ACTION_PLAN": "Construire le plan d'actions : un responsable et une échéance par action, chiffrage et "
                   "cotation (page Plan d'actions).",
    "SKILLS": "Évaluer les compétences des personnes clés et former ; sensibiliser le personnel et les sous-traitants.",
    "COMMUNICATION": "Afficher un message à la fois, chiffré, renouvelé régulièrement (kit de sensibilisation) ; "
                     "ouvrir une boîte à idées.",
    "OPERATIONS": "Intégrer la chasse aux gaspillages (fuites, talons, consignes) aux gammes de maintenance et aux "
                  "consignes d'exploitation.",
    "PURCHASING": "Évaluer les investissements sur leur durée de vie (VAN, TRI) et solliciter les CEE avant toute "
                  "commande (page Plan d'actions).",
    "MONITORING": "Déployer le plan de comptage progressivement et suivre les IPE avec une cible et un seuil "
                  "d'alerte (pages Bilan énergétique et Performance).",
    "VERIFICATION": "Décrire pour chaque action son plan de mesure et vérification (IPMVP) et contrôler l'atteinte "
                    "des objectifs.",
    "IMPROVEMENT": "Planifier la revue énergétique (au moins annuelle) et en tirer des améliorations.",
}
LEVELS = [(0.0, "Démarrage", "danger"), (1 / 3, "En progression", "warning"), (2 / 3, "Structurée", "success")]


class ManagementError(ValueError):
    pass


def can_edit(user: User) -> bool:
    return ipe.can_enter_variables(user)


def _require_editor(user: User) -> None:
    if not can_edit(user):
        from app.services.action_plan import ActionPlanPermissionError

        raise ActionPlanPermissionError("La démarche est renseignée par le responsable énergie ou l'auditeur.")


def level(score: float) -> tuple[str, str]:
    label, tone = LEVELS[0][1], LEVELS[0][2]
    for threshold, name, color in LEVELS:
        if score >= threshold:
            label, tone = name, color
    return label, tone


# --- Auto-évaluation --------------------------------------------------------------------------------------------


def compute_scores(answers: dict[str, int]) -> dict:
    themes = {}
    for code in THEMES:
        notes = [answers.get(q, 0) for q, theme, _ in QUESTIONS if theme == code]
        applicable = [n for n in notes if n >= 0]
        themes[code] = round(sum(applicable) / (2 * len(applicable)), 4) if applicable else None
    axes = {}
    for axis in AXES:
        values = [v for code, v in themes.items() if THEMES[code][0] == axis and v is not None]
        axes[axis] = round(sum(values) / len(values), 4) if values else None
    known = [v for v in axes.values() if v is not None]
    return {"themes": themes, "axes": axes, "global": round(sum(known) / len(known), 4) if known else 0.0,
            "answered": sum(1 for q, _, _ in QUESTIONS if q in answers)}


def save_assessment(db: Session, repo: TenantRepository, user: User, organization_id: int, answers: dict[str, int],
                    comments: dict[str, str] | None = None) -> MaturityAssessment:
    _require_editor(user)
    org = repo.get_organization(organization_id)
    codes = {q for q, _, _ in QUESTIONS}
    if set(answers) - codes:
        raise ManagementError("Question inconnue.")
    if any(v not in ANSWERS for v in answers.values()):
        raise ManagementError("Réponse inconnue.")
    if not answers:
        raise ManagementError("Répondez au moins à une question.")
    assessment = MaturityAssessment(organization_id=org.id, answers=dict(answers),
                                    comments={k: v.strip() for k, v in (comments or {}).items() if v and v.strip()} or None,
                                    scores=compute_scores(answers), created_by=user.id)
    db.add(assessment)
    db.commit()
    return assessment


def weakest_themes(scores: dict, limit: int = 4) -> list[tuple[str, float]]:
    ranked = sorted(((code, value) for code, value in scores["themes"].items() if value is not None),
                    key=lambda item: item[1])
    return [(code, value) for code, value in ranked if value < 0.5][:limit]


def evidence(db: Session, org: Organization) -> dict[str, str]:
    """Ce que les données EffiSmart disent de chaque question : des indices, pas des réponses."""
    site_ids = [s.id for s in org.sites]
    hints: dict[str, str] = {}
    management = db.scalar(select(EnergyManagement).where(EnergyManagement.organization_id == org.id))
    if management and management.policy:
        hints["P1"] = ("Politique enregistrée" + (f", validée le {management.policy_approved_on:%d/%m/%Y}."
                                                  if management.policy_approved_on else ", sans date de validation."))
    if management and management.scope:
        hints["O1"] = "Périmètre décrit dans le socle du management de l'énergie."
    if management and management.team:
        hints["O2"] = f"Équipe énergie : {len(management.team)} membre(s) enregistré(s)."
    if management and management.last_review_on:
        hints["I1"] = f"Dernière revue énergétique : {management.last_review_on:%d/%m/%Y}."
        hints["O4"] = hints["I1"]
    deadlines = db.scalar(select(func.count(RegulatoryDeadline.id)).where(RegulatoryDeadline.site_id.in_(site_ids))) or 0
    if deadlines:
        hints["G1"] = f"{deadlines} échéance(s) réglementaire(s) suivie(s) dans EffiSmart."
        hints["G3"] = hints["G1"]
    if org.iso50001_certified_until and org.iso50001_certified_until >= today_local():
        hints["G1"] = (hints.get("G1", "") + " Certification ISO 50001 en cours de validité.").strip()
    points = db.scalar(select(func.count(DeliveryPoint.id)).where(DeliveryPoint.site_id.in_(site_ids))) or 0
    usages = db.scalar(select(func.count(AssetNode.id)).where(AssetNode.site_id.in_(site_ids),
                                                              AssetNode.kind == AssetNodeKind.USAGE)) or 0
    if points:
        hints["K1"] = f"{points} point(s) de livraison suivi(s) : bilan par énergie disponible (page Bilan énergétique)" + (
            "; répartition par usage d'après le graphe des équipements." if usages else ".")
        hints["N1"] = f"{points} compteur(s) suivi(s) ; voir le plan de comptage (page Bilan énergétique)."
    if usages:
        hints["K2"] = "Pareto des usages et UES proposés disponibles (page Bilan énergétique)."
    validated = list(db.scalars(select(IpeDefinition).where(IpeDefinition.organization_id == org.id,
                                                            IpeDefinition.status == ReviewStatus.VALIDATED)))
    if validated:
        drivers = sorted({d for v in validated for d in v.drivers})
        hints["K3"] = "Facteurs d'influence utilisés par les IPE validés : " + ", ".join(drivers) + "."
        hints["K4"] = f"{len(validated)} IPE validé(s)."
        targeted = [v for v in validated if v.target_value is not None]
        hints["N2"] = f"{len(validated)} IPE suivi(s), dont {len(targeted)} avec une valeur cible."
    actions = list(db.scalars(select(EnergyAction).where(EnergyAction.organization_id == org.id)))
    if actions:
        planned = [a for a in actions if a.owner and a.due_date]
        hints["K5"] = f"{len(actions)} action(s) au plan, chiffrées et cotées."
        hints["A2"] = f"{len(planned)} action(s) sur {len(actions)} avec un responsable et une échéance."
        with_mv = [a for a in actions if (a.mv_plan or {}).get("option")]
        verified = [a for a in actions if a.status == ActionStatus.VERIFIED]
        hints["V1"] = f"{len(with_mv)} plan(s) de mesure et vérification, {len(verified)} action(s) vérifiée(s)."
        hints["B3"] = "VAN et TRI calculés pour chaque action du plan."
        if any(a.cee_kwh_cumac for a in actions):
            hints["B4"] = "Des CEE sont valorisés dans le plan d'actions."
    if management and management.objectives:
        hints["A1"] = f"{len(management.objectives)} objectif(s) enregistré(s)."
    since = utcnow() - timedelta(days=90)
    treated = db.scalar(select(func.count(Drift.id)).join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id)
                        .where(DeliveryPoint.site_id.in_(site_ids), Drift.status != DriftStatus.OPEN,
                               Drift.qualified_at >= since)) or 0
    if points:
        hints["N3"] = (f"Détection quotidienne des dérives (données J+1) : {treated} anomalie(s) traitée(s) en 90 jours.")
    applied = db.scalar(select(func.count(Recommendation.id)).where(Recommendation.organization_id == org.id,
                                                                    Recommendation.status == ReviewStatus.APPLIED)) or 0
    if applied:
        hints["V2"] = f"{applied} recommandation(s) appliquée(s) après une anomalie."
    approved = db.scalar(select(func.count(CommunicationMessage.id)).where(
        CommunicationMessage.organization_id == org.id, CommunicationMessage.status == CommunicationStatus.APPROVED)) or 0
    if approved:
        hints["C1"] = f"{approved} message(s) de sensibilisation approuvé(s) pour l'affichage."
    return hints


# --- Socle : politique, équipe, objectifs ---------------------------------------------------------------------


def save_management(db: Session, repo: TenantRepository, user: User, organization_id: int, *, policy: str | None,
                    policy_approved_on: date | None, scope: str | None, team: list[dict], objectives: list[dict],
                    review_months: int | None, last_review_on: date | None) -> EnergyManagement:
    _require_editor(user)
    org = repo.get_organization(organization_id)
    team = [{k: (m.get(k) or "").strip() for k in ("name", "role", "missions")} for m in team or []
            if (m.get("name") or "").strip()]
    clean_objectives = []
    for o in objectives or []:
        if not (o.get("label") or "").strip():
            continue
        clean_objectives.append({
            "label": o["label"].strip(), "indicator": (o.get("indicator") or "").strip(),
            "baseline": o.get("baseline"), "target": o.get("target"),
            "deadline": o["deadline"].isoformat() if isinstance(o.get("deadline"), date) else o.get("deadline"),
            "owner": (o.get("owner") or "").strip()})
    if review_months is not None and not 1 <= review_months <= 36:
        raise ManagementError("Périodicité de la revue : entre 1 et 36 mois.")
    record = db.scalar(select(EnergyManagement).where(EnergyManagement.organization_id == org.id))
    if record is None:
        record = EnergyManagement(organization_id=org.id)
        db.add(record)
    record.policy = (policy or "").strip() or None
    record.policy_approved_on = policy_approved_on
    record.scope = (scope or "").strip() or None
    record.team = team or None
    record.objectives = clean_objectives or None
    record.review_months = review_months
    record.last_review_on = last_review_on
    record.updated_by, record.updated_at = user.id, utcnow()
    db.commit()
    return record


def smart_gaps(objective: dict) -> list[str]:
    """Ce qui manque à un objectif pour être SMART (SP5) : mesurable, cible, échéance, responsable."""
    gaps = []
    if not objective.get("indicator"):
        gaps.append("indicateur (mesurable)")
    if objective.get("target") in (None, ""):
        gaps.append("valeur cible")
    if not objective.get("deadline"):
        gaps.append("échéance")
    if not objective.get("owner"):
        gaps.append("responsable")
    return gaps


def next_review(record: EnergyManagement | None) -> tuple[date | None, bool]:
    """Prochaine revue énergétique et retard éventuel."""
    if record is None or not record.review_months or not record.last_review_on:
        return None, False
    due = add_months(record.last_review_on, record.review_months)
    return due, due < today_local()


# --- R7 : kit de sensibilisation (SP6) --------------------------------------------------------------------------


@dataclass
class Draft:
    theme: str
    title: str
    body: str
    call_to_action: str
    figures: dict


THEME_LABELS = {"OFF_HOURS": "Consommation hors activité", "SAVINGS": "Économies obtenues", "ACTION": "Action réalisée",
                "IPE": "Indicateur de performance", "COMPRESSED_AIR": "Air comprimé"}


def _off_hours_draft(db: Session, site: Site, prices) -> Draft | None:
    from app.models import Fluid
    from app.services.dashboard import data_as_of
    from app.services.drift import half_hour_powers, is_night_slot, is_off_hours_slot
    from app.services.consent import has_active_consent
    from app.services.validation import fr

    points = [dp for dp in site.delivery_points if dp.fluid == Fluid.ELEC and has_active_consent(db, dp.id)]
    end = data_as_of(db, [dp.id for dp in points]) if points else None
    if end is None:
        return None
    total = off = 0.0
    for dp in points:
        for moment, kw in half_hour_powers(db, dp.id, end - timedelta(days=27), end):
            kwh = kw / 2
            total += kwh
            if is_night_slot(moment) or is_off_hours_slot(moment):
                off += kwh
    if total <= 0 or off / total < 0.15:
        return None
    yearly_kwh = off * 365 / 28
    euros = yearly_kwh * prices.price[Fluid.ELEC]
    return Draft("OFF_HOURS", f"{site.name} : la nuit et le week-end, on consomme encore",
                 f"Sur les 4 dernières semaines, {fr(off / total * 100)} % de l'électricité du site a été consommée "
                 f"hors des heures d'activité, soit environ {fr(euros, 0)} € par an. Une partie sert (froid, serveurs, "
                 "sécurité) ; le reste, ce sont des machines, des éclairages et des postes restés allumés.",
                 "En partant, j'éteins ce qui ne sert pas : machines, éclairages, écrans.",
                 {"share_off_hours": round(off / total, 3), "yearly_kwh": round(yearly_kwh),
                  "yearly_eur": round(euros), "period_end": end.isoformat()})


def generate_drafts(db: Session, repo: TenantRepository, user: User, site_id: int,
                    weather=None) -> list[CommunicationMessage]:
    """Prépare des messages d'après les données du site : brouillons à relire (les approuvés sont conservés)."""
    _require_editor(user)
    from app.services import action_plan, assets, economics, ipe_definitions
    from app.services.validation import fr

    site = repo.get_site(site_id)
    prices = action_plan.site_prices(db, site)
    drafts: list[Draft] = []
    off = _off_hours_draft(db, site, prices)
    if off:
        drafts.append(off)
    for rec in db.scalars(select(Recommendation).where(Recommendation.site_id == site.id,
                                                       Recommendation.savings_validated_at.is_not(None))):
        snap = rec.savings_snapshot or {}
        if snap.get("annualized_kwh", 0) > 0:
            drafts.append(Draft("SAVINGS", f"Bravo : {rec.title.rstrip('.')}",
                                f"Depuis le {date.fromisoformat(snap['applied_on']):%d/%m/%Y}, cette action fait économiser "
                                f"environ {fr(snap['annualized_kwh'])} kWh par an, soit {fr(snap['annualized_eur'])} € : "
                                "un résultat mesuré au compteur et vérifié.",
                                "Continuons : signalez à l'équipe énergie tout gaspillage que vous repérez.",
                                {"recommendation_id": rec.id, "annualized_kwh": snap["annualized_kwh"],
                                 "annualized_eur": snap["annualized_eur"]}))
            break
    done = db.scalars(select(EnergyAction).where(EnergyAction.site_id == site.id,
                                                 EnergyAction.status.in_([ActionStatus.DONE, ActionStatus.VERIFIED]))
                      .order_by(EnergyAction.done_on.desc())).first()
    if done is not None:
        econ = action_plan.evaluate(done, prices, economics.params_of(site.organization))
        drafts.append(Draft("ACTION", f"C'est fait : {done.title}",
                            f"Objectif : {fr(econ.saved_kwh / 1000, 1)} MWh et {fr(econ.energy_gain_eur)} € économisés "
                            f"chaque année, {fr(econ.saved_kgco2e / 1000, 1)} tCO₂e évitées. Les résultats seront "
                            "mesurés et affichés ici.",
                            "Une idée d'économie ? Parlez-en à l'équipe énergie.",
                            {"action_id": done.id, "saved_kwh": round(econ.saved_kwh)}))
    for definition in db.scalars(select(IpeDefinition).where(IpeDefinition.site_id == site.id,
                                                             IpeDefinition.status == ReviewStatus.VALIDATED)):
        value = ipe_definitions.evaluate(db, definition, weather)
        if value.variation is not None and value.variation < -0.03:
            drafts.append(Draft("IPE", f"Notre performance s'améliore : {fr(-value.variation * 100, 1)} %",
                                f"L'indicateur « {definition.name} » s'établit à {fr(value.current, 2)} sur les 12 "
                                f"derniers mois, contre {fr(value.baseline, 2)} en {value.baseline_year}. À activité "
                                "comparable, nous consommons moins.",
                                "Gardons le cap : chaque geste compte.",
                                {"ipe_definition_id": definition.id, "variation": round(value.variation, 4)}))
            break
    graph = assets.load_site_graph(db, site.id)
    if any(n.category in ("COMPRESSOR", "COMPRESSED_AIR") for n in graph.nodes.values()):
        from app.models import Fluid

        kwh = 4.3 * 8760 * 0.11  # fuite d'1 mm à 7 bars : 4,3 m³/h (ADEME) ; 110 Wh par m³ d'air
        drafts.append(Draft("COMPRESSED_AIR", "Une fuite d'air de 1 mm, c'est de l'argent qui s'envole",
                            f"À 7 bars, un trou de 1 mm laisse fuir 4,3 m³ d'air par heure : environ {fr(kwh)} kWh et "
                            f"{fr(kwh * prices.price[Fluid.ELEC])} € par an, pour une seule fuite. La plupart s'entendent "
                            "quand l'atelier est à l'arrêt.",
                            "Je signale toute fuite d'air comprimé à la maintenance.",
                            {"yearly_kwh_per_leak": round(kwh)}))
    for old in db.scalars(select(CommunicationMessage).where(CommunicationMessage.site_id == site.id,
                                                             CommunicationMessage.status == CommunicationStatus.DRAFT)):
        db.delete(old)
    created = []
    for draft in drafts:
        message = CommunicationMessage(organization_id=site.organization_id, site_id=site.id, theme=draft.theme,
                                       title=draft.title[:160], body=draft.body,
                                       call_to_action=draft.call_to_action[:200], figures=draft.figures)
        db.add(message)
        created.append(message)
    db.commit()
    return created


def review_message(db: Session, repo: TenantRepository, user: User, message_id: int, *, title: str, body: str,
                   call_to_action: str, status: CommunicationStatus) -> CommunicationMessage:
    """Relecture humaine (P1) : le texte peut être ajusté ; seul un message approuvé est diffusable."""
    _require_editor(user)
    message = repo.get_message(message_id)
    title, body, call = (title or "").strip(), (body or "").strip(), (call_to_action or "").strip()
    if not title or not body or not call:
        raise ManagementError("Titre, message et appel à l'action sont obligatoires : un message, une action.")
    message.title, message.body, message.call_to_action = title[:160], body, call[:200]
    message.status = status
    if status == CommunicationStatus.APPROVED:
        message.approved_by, message.approved_at = user.id, utcnow()
    db.commit()
    return message


def poster_html(message: CommunicationMessage) -> str:
    """Affiche A4 imprimable : un message, un chiffre, une action (communication engageante, SP6)."""
    import html as _html

    e = _html.escape
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{e(message.title)}</title>
<style>@page{{size:A4;margin:18mm}}body{{font-family:Arial,sans-serif;color:#0f172a;margin:0;padding:40px}}
.band{{background:#0f766e;color:#fff;padding:28px 32px;border-radius:12px}}h1{{font-size:40px;margin:0;line-height:1.15}}
p{{font-size:22px;line-height:1.5}}.cta{{margin-top:36px;border:4px solid #0f766e;border-radius:12px;padding:24px;
font-size:28px;font-weight:bold;color:#0f766e}}small{{color:#64748b;font-size:13px}}</style></head><body>
<div class="band"><h1>{e(message.title)}</h1></div><p>{e(message.body)}</p><div class="cta">{e(message.call_to_action)}</div>
<p><small>{e(message.site.name)} · équipe énergie · chiffres EffiSmart</small></p></body></html>"""
