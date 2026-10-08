"""F2 — Regroupement des alertes : une cause, une alerte.

Une nouvelle anomalie se rattache à une alerte encore à valider quand elles ont vraisemblablement la même cause :
1. répétition : même compteur, même type d'anomalie, à moins de 30 jours d'une occurrence du groupe ;
2. excès expliqué : même compteur, même jour, l'excès d'une anomalie sur la journée (écart climatique, seuil
   journalier) est couvert à au moins 50 % par celui d'une anomalie sur une plage horaire (talon de nuit,
   consommation en inoccupation) ;
3. même équipement suspect (contexte F2b, localisation précise ou probable), à un jour près.

Seule la plus ancienne, l'anomalie principale, est signalée : alerte, e-mail, file de validation. Les suivantes
s'y ajoutent ; l'alerte est mise à jour (nombre d'occurrences, raison du regroupement), sans nouvel e-mail.
Le même jour, les détecteurs les plus précis passent en premier (plages horaires, puis journée).

Le regroupement est une proposition de la plateforme (principe P1) : la décision humaine porte sur tout le
groupe, et un valideur peut détacher une anomalie qu'il juge sans rapport. Une alerte déjà décidée ne reçoit
plus de nouvelles occurrences : la suivante ouvre une nouvelle alerte.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DeliveryPoint, Drift, DriftKind, DriftStatus, Notification, User
from app.repositories import TenantRepository
from app.services import anomaly_context
from app.services.drift import DETECTOR_BY_KIND, DETECTORS, DRIFT_LABELS
from app.services.validation import (
    ValidationError,
    ValidationPermissionError,
    can_validate,
    confidence_level,
    fr,
    notify,
    validator_label,
)
from app.timeutils import to_local, utcnow

REPEAT_WINDOW_DAYS = 30
CAUSE_WINDOW_DAYS = 1
EXPLAINED_SHARE = 0.5
ALERT_PREFIX = "À valider : "

RULE_REPEAT, RULE_EXPLAINED, RULE_SUSPECT, RULE_DETACHED = "REPEAT", "EXPLAINED", "SUSPECT", "DETACHED"
RULE_SAME_DAY = "SAME_DAY"
RULE_WHY = {
    RULE_REPEAT: "le problème se répète (même compteur, même type d'anomalie)",
    RULE_EXPLAINED: "l'excès de la journée s'explique par une anomalie déjà signalée",
    RULE_SAME_DAY: "le même excès de la journée est vu par deux détecteurs",
    RULE_SUSPECT: "même équipement suspect",
}
INTRADAY = (DriftKind.BASELOAD, DriftKind.OFF_HOURS)
# « La moins récente » : par jour, puis, le même jour, dans l'ordre des détecteurs (plages horaires avant la journée).
DETECTION_ORDER = {kind: DETECTORS.index(detector) for kind, detector in DETECTOR_BY_KIND.items()}


def detection_key(drift: Drift) -> tuple:
    return drift.day, DETECTION_ORDER[drift.kind], drift.id


def _label(drift: Drift) -> str:
    return DRIFT_LABELS[drift.kind].lower()


# --- Même cause ? -----------------------------------------------------------------------------------------


def _intraday_hours(drift: Drift) -> float:
    """Durée de la plage horaire analysée par le détecteur (talon de nuit ou inoccupation)."""
    night = 24 - settings.inactive_night_start_hour + settings.inactive_night_end_hour
    if drift.kind == DriftKind.BASELOAD:
        return night
    if drift.day.weekday() >= 5:
        return 24 - night
    return (settings.occupancy_start_hour - settings.inactive_night_end_hour
            + settings.inactive_night_start_hour - settings.occupancy_end_hour)


def excess_kwh(drift: Drift) -> float | None:
    """Énergie consommée en trop : plage horaire × excès de puissance, ou excès de la journée (kWh)."""
    excess = max(0.0, drift.measured_value - drift.reference_value)
    if drift.kind in INTRADAY:
        return excess * _intraday_hours(drift)
    if drift.unit == "kWh":
        return excess
    return None  # pic de puissance : pas d'énergie journalière comparable


def _suspects(drift: Drift) -> dict[int, str]:
    ctx = drift.context or {}
    if ctx.get("localisation") not in ("precise", "probable"):
        return {}
    return {s["id"]: s["name"] for s in ctx.get("suspects", []) if s.get("retained")}


def _whole_day(*drifts: Drift) -> bool:
    """Anomalies portant toutes sur la journée entière (aucune sur une plage horaire)."""
    return all(d.kind not in INTRADAY for d in drifts)


def same_cause(drift: Drift, member: Drift) -> tuple[str, str] | None:
    """Règle de même cause entre une nouvelle anomalie et une anomalie du groupe : (règle, raison) ou None."""
    dp = drift.delivery_point
    same_meter = drift.delivery_point_id == member.delivery_point_id
    gap = (drift.day - member.day).days
    if same_meter and drift.kind == member.kind and 0 <= gap <= REPEAT_WINDOW_DAYS:
        return RULE_REPEAT, (f"même compteur ({dp.external_ref}) et même type d'anomalie ({_label(drift)}) que "
                             f"l'occurrence du {member.day:%d/%m/%Y} : le problème se répète")
    if abs(gap) > CAUSE_WINDOW_DAYS:
        return None
    if same_meter and gap == 0 and drift.unit == member.unit == "kWh" and _whole_day(drift, member):
        return RULE_SAME_DAY, (f"même excès de la journée, déjà signalé par l'anomalie « {_label(member)} » du même "
                               "compteur, le même jour")
    if same_meter and gap == 0:
        pair = (drift, member) if drift.kind not in INTRADAY else (member, drift)
        daily, intraday = pair
        if daily.kind not in INTRADAY and intraday.kind in INTRADAY:
            day_kwh, part_kwh = excess_kwh(daily), excess_kwh(intraday)
            if day_kwh and part_kwh and part_kwh >= EXPLAINED_SHARE * day_kwh:
                return RULE_EXPLAINED, (
                    f"l'excès de la journée ({fr(day_kwh)} kWh, {_label(daily)}) s'explique à "
                    f"{fr(min(part_kwh / day_kwh, 1.0) * 100)} % par celui de l'anomalie « {_label(intraday)} » "
                    f"du même compteur, le même jour ({fr(part_kwh)} kWh)")
    common = _suspects(drift).keys() & _suspects(member).keys()
    if common:
        name = _suspects(drift)[next(iter(common))]
        return RULE_SUSPECT, (f"même équipement suspect (« {name} ») que l'anomalie « {_label(member)} » du "
                              f"{member.day:%d/%m/%Y}, à un jour près")
    return None


# --- Groupes ---------------------------------------------------------------------------------------------


def members(db: Session, lead: Drift) -> list[Drift]:
    """Anomalies rattachées à l'anomalie principale, de la plus ancienne à la plus récente."""
    return list(db.scalars(select(Drift).where(Drift.grouped_with_id == lead.id).order_by(Drift.day, Drift.id)))


def group_sizes(db: Session, lead_ids: list[int]) -> dict[int, int]:
    """Nombre d'anomalies de chaque groupe, anomalie principale comprise."""
    rows = db.execute(select(Drift.grouped_with_id, func.count(Drift.id)).where(
        Drift.grouped_with_id.in_(lead_ids)).group_by(Drift.grouped_with_id)).all() if lead_ids else []
    sizes = {lead_id: count + 1 for lead_id, count in rows}
    return {lead_id: sizes.get(lead_id, 1) for lead_id in lead_ids}


def find_group(db: Session, drift: Drift) -> tuple[Drift, str, str] | None:
    """Alerte encore à valider, plus ancienne, de même cause : (anomalie principale, règle, raison)."""
    leads = db.scalars(
        select(Drift).join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id).where(
            DeliveryPoint.site_id == drift.delivery_point.site_id, Drift.id != drift.id, Drift.day <= drift.day,
            Drift.grouped_with_id.is_(None), Drift.status == DriftStatus.OPEN,
        )
    ).all()
    for lead in sorted((d for d in leads if detection_key(d) < detection_key(drift)), key=detection_key):
        for member in reversed([lead, *members(db, lead)]):  # la plus récente d'abord : fenêtre glissante
            match = same_cause(drift, member)
            if match is not None:
                return lead, *match
    return None


def attach(db: Session, drift: Drift) -> Drift | None:
    """Rattache une nouvelle anomalie à l'alerte de même cause ; renvoie l'anomalie principale (sans commit)."""
    if drift.status != DriftStatus.OPEN or drift.grouped_with_id is not None:
        return None
    found = find_group(db, drift)
    if found is None:
        return None
    lead, rule, reason = found
    drift.grouped_with_id, drift.grouping_rule, drift.grouping_reason = lead.id, rule, reason
    db.flush()
    return lead


# --- Alerte ----------------------------------------------------------------------------------------------


def alert_text(db: Session, lead: Drift) -> str:
    """Texte de l'alerte « à valider » : une anomalie, ou un groupe avec la raison du regroupement."""
    dp = lead.delivery_point
    group = [lead, *members(db, lead)]
    level = confidence_level(lead.confidence)[0].lower()
    context = anomaly_context.short_text(lead.context)
    context = f". {context[0].upper()}{context[1:]}" if context else ""
    if len(group) == 1:
        return (f"{ALERT_PREFIX}{_label(lead)} le {lead.day:%d/%m/%Y}, {dp.site.name} ({dp.external_ref}), "
                f"écart de {lead.deviation_pct:+.0f} %, confiance {level}{context}")
    last = max(d.day for d in group)
    kinds = list(dict.fromkeys(_label(d) for d in group[1:] if d.kind != lead.kind))
    why = list(dict.fromkeys(RULE_WHY[d.grouping_rule] for d in group[1:] if d.grouping_rule in RULE_WHY))
    deviation = max(d.deviation_pct for d in group)
    return (f"{ALERT_PREFIX}{_label(lead)}, {dp.site.name} ({dp.external_ref}) : {len(group)} alertes regroupées en "
            f"une seule, du {lead.day:%d/%m} au {last:%d/%m/%Y}" + (f" (avec : {', '.join(kinds)})" if kinds else "")
            + f". Pourquoi ce regroupement : {' ; '.join(why)} ; une seule décision vaut pour tout le groupe. "
            f"Écart jusqu'à {deviation:+.0f} %, confiance {level}{context}")


def refresh_alert(db: Session, lead: Drift) -> None:
    """Met à jour l'alerte de l'anomalie principale, remontée en tête de la cloche, sans nouvel e-mail."""
    message = alert_text(db, lead)
    alerts = db.scalars(select(Notification).where(
        Notification.drift_id == lead.id, Notification.message.startswith(ALERT_PREFIX))).all()
    for alert in alerts:
        alert.message, alert.created_at = message, utcnow()
    if not alerts:
        notify(db, lead.delivery_point.site.organization_id, message, validators_only=True, drift_id=lead.id,
               email=False)


def detach(db: Session, repo: TenantRepository, user: User, drift_id: int) -> Drift:
    """Un valideur juge l'anomalie sans rapport avec son groupe : elle devient une alerte à part (principe P1)."""
    if not can_validate(user):
        raise ValidationPermissionError("Seuls l'auditeur partenaire et le responsable énergie du client peuvent "
                                        "modifier un regroupement d'alertes.")
    drift = repo.get_drift(drift_id)
    if drift.grouped_with_id is None:
        raise ValidationError("Cette anomalie n'est rattachée à aucun groupe.")
    lead = db.get(Drift, drift.grouped_with_id)
    if lead.status != DriftStatus.OPEN:
        raise ValidationError("Ce groupe a déjà été décidé : rouvrez d'abord son alerte principale.")
    drift.grouped_with_id, drift.grouping_rule = None, RULE_DETACHED
    drift.grouping_reason = (f"Détachée de l'alerte du {lead.day:%d/%m/%Y} par {validator_label(user)} le "
                             f"{to_local(utcnow()):%d/%m/%Y} : traitée comme une alerte à part.")
    db.flush()
    notify(db, drift.delivery_point.site.organization_id, alert_text(db, drift) + " (détachée d'un groupe)",
           validators_only=True, drift_id=drift.id, exclude_user_id=user.id)
    refresh_alert(db, lead)
    db.commit()
    return drift


def regroup_existing(db: Session) -> int:
    """Mise à niveau : regroupe les anomalies à valider détectées avant le regroupement. Les alertes déjà
    envoyées restent telles quelles ; le regroupement vaut pour la file de validation et les alertes à venir."""
    count = 0
    pending = db.scalars(select(Drift).where(Drift.status == DriftStatus.OPEN, Drift.grouped_with_id.is_(None))).all()
    for drift in sorted(pending, key=detection_key):
        if drift.grouping_rule == RULE_DETACHED or members(db, drift):
            continue
        if attach(db, drift) is not None:
            count += 1
    db.commit()
    return count
