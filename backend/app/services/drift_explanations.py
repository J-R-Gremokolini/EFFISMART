"""Explication des anomalies (principe P1) : raisonnement, gain estimé, niveau de confiance.

Le niveau de confiance part de 0,5 et chaque facteur l'ajuste d'un montant affiché à l'utilisateur :
netteté de l'écart, complétude des données, stabilité de la référence, récurrence du motif, source météo.
Le gain annualise l'énergie consommée en trop au rythme observé sur 90 jours (hypothèse prudente).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DeliveryPoint, Drift, DriftKind, Fluid
from app.services import anomaly_context
from app.services.validation import Assessment, apply_assessment, fr

logger = logging.getLogger(__name__)

ALGORITHMS = {
    DriftKind.THRESHOLD: "F2a « dépassement de seuil » v1",
    DriftKind.CLIMATE_DEVIATION: "F2a « écart climatique » v1 (régression N-1 × DJU)",
    DriftKind.BASELOAD: "F2a « talon de nuit » v2 (moyenne de la nuit, médiane des nuits précédentes)",
    DriftKind.OFF_HOURS: "F2a « consommation en inoccupation » v1 (talon du jour + écart habituel)",
    DriftKind.MODEL_DEVIATION: "F11 « écart au modèle » v1 (moteur de prévision v2, 12 à 24 mois)",
}
RECURRENCE_WINDOW_DAYS = 90


def recurrence(db: Session, drift: Drift) -> int:
    """Occurrences du même motif (point, type) sur les 90 derniers jours, celle-ci comprise."""
    return int(db.scalar(select(func.count(Drift.id)).where(
        Drift.delivery_point_id == drift.delivery_point_id, Drift.kind == drift.kind,
        Drift.day > drift.day - timedelta(days=RECURRENCE_WINDOW_DAYS), Drift.day <= drift.day,
    )) or 1)


def _pct(value: float) -> str:
    return f"{fr(value * 100)} %"


def _margin_factor(a: Assessment, deviation_pct: float, tolerance: float) -> None:
    ratio = (deviation_pct / 100) / tolerance if tolerance else 0
    if ratio >= 3:
        a.factor(f"Écart très net : {fr(ratio, 1)} fois la tolérance de détection", 0.2)
    elif ratio >= 1.5:
        a.factor(f"Écart net : {fr(ratio, 1)} fois la tolérance de détection", 0.1)
    else:
        a.factor("Écart proche du seuil de détection", -0.05)


def explain_drift(db: Session, dp: DeliveryPoint, drift: Drift, candidate=None) -> None:
    """Calcule et enregistre l'explication de l'anomalie (sans commit)."""
    facts = candidate.facts if candidate is not None else {}
    a = Assessment(algorithm=ALGORITHMS[drift.kind])
    site = dp.site
    source = "courbe de charge au pas de 30 min" if dp.fluid == Fluid.ELEC else "relevé journalier"
    cov = facts.get("coverage")
    completeness = f" ({_pct(cov)} des mesures attendues reçues)" if cov is not None else ""
    a.step(f"Donnée analysée : {source} du compteur {dp.external_ref} ({site.name}), "
           f"journée du {drift.day:%d/%m/%Y}{completeness}.")

    if not facts:
        a.step(f"Mesure et référence : {drift.details}.")
        a.step(f"Écart : {drift.deviation_pct:+.0f} %.")
        a.factor("Explication reconstituée a posteriori : le détail du calcul n'est plus disponible", -0.1)
    elif drift.kind == DriftKind.THRESHOLD and dp.fluid == Fluid.ELEC:
        a.step(f"Mesure : pic de {fr(drift.measured_value)} kW à {facts['peak_time']}, "
               f"{facts['slots_above']} créneau(x) de 30 min au-dessus du seuil.")
        if facts["threshold_source"] == "subscribed":
            a.step(f"Référence : seuil de {fr(drift.reference_value)} kW = {_pct(facts['ratio'])} de la puissance "
                   f"souscrite ({fr(facts['subscribed_kva'])} kVA, hypothèse cos φ ≈ 1).")
            a.factor("Seuil déduit de la puissance souscrite (hypothèse cos φ ≈ 1)", -0.05)
        else:
            a.step(f"Référence : seuil de {fr(drift.reference_value)} kW fixé pour ce point.")
            a.factor("Seuil fixé spécifiquement pour ce point", 0.05)
        a.step(f"Écart : {drift.deviation_pct:+.0f} % au-dessus du seuil ; "
               f"{fr(facts['excess_kwh'])} kWh appelés au-delà du seuil dans la journée.")
        if drift.deviation_pct >= 20:
            a.factor(f"Dépassement marqué ({drift.deviation_pct:+.0f} %)", 0.15)
        elif drift.deviation_pct >= 5:
            a.factor(f"Dépassement modéré ({drift.deviation_pct:+.0f} %)", 0.05)
        else:
            a.factor("Dépassement faible : proche du seuil", -0.05)
        if facts["slots_above"] >= 2:
            a.factor(f"Dépassement sur {facts['slots_above']} créneaux de 30 min", 0.05)
        else:
            a.factor("Pic isolé sur un seul créneau de 30 min", -0.05)
    elif drift.kind == DriftKind.THRESHOLD:
        a.step(f"Mesure : {fr(drift.measured_value)} kWh consommés dans la journée.")
        a.step(f"Référence : seuil journalier de {fr(drift.reference_value)} kWh fixé pour ce point.")
        a.step(f"Écart : {drift.deviation_pct:+.0f} % au-dessus du seuil.")
        a.factor("Seuil fixé spécifiquement pour ce point", 0.05)
        _margin_factor(a, drift.deviation_pct, 0.1)
    elif drift.kind == DriftKind.CLIMATE_DEVIATION:
        day_class = facts["day_class"]
        origin = "classe déduite des données" if facts.get("classes_learned") else "classe par défaut"
        days = f"jours comparables ({day_class})"
        a.step(f"Mesure : {fr(drift.measured_value)} kWh consommés ; jour comparé aux jours « {day_class} » "
               f"({origin}).")
        center = facts["center"]
        if facts["slope"] <= 0:
            model = ("à cette saison, la consommation ne dépend pas de la météo : la référence est leur moyenne")
        elif facts.get("r2") is not None and facts["r2"] >= 0.3:
            model = (f"la consommation suit conso = a + b × DJU (b = {fr(facts['slope'], 1)} kWh par DJU, "
                     f"R² = {fr(facts['r2'], 2)})")
        else:
            model = (f"la consommation suit conso = a + b × DJU (b = {fr(facts['slope'], 1)} kWh par DJU ; "
                     "lien faible avec la météo à cette saison)")
        a.step(f"Référence : {fr(drift.reference_value)} kWh attendus. Sur les {facts['n_points']} {days} "
               f"de la même période l'an dernier (± {facts['window']} jours autour du "
               f"{center[8:10]}/{center[5:7]}/{center[:4]}), {model} ; ce jour-là : {fr(facts['dju'], 1)} DJU.")
        a.step(f"Écart : {drift.deviation_pct:+.0f} % pour une tolérance de {_pct(facts['tolerance'])}.")
        _margin_factor(a, drift.deviation_pct, facts["tolerance"])
        cv = facts.get("residual_cv")
        if cv is not None and cv < 0.1:
            a.factor(f"Référence N-1 stable (écart-type résiduel {_pct(cv)})", 0.1)
        elif cv is not None and cv > 0.25:
            a.factor(f"Référence N-1 dispersée (écart-type résiduel {_pct(cv)})", -0.1)
        if facts["n_points"] < 12:
            a.factor(f"Peu de jours comparables en N-1 ({facts['n_points']})", -0.05)
        if str(facts.get("weather_source", "")).startswith("MOCK"):
            a.factor("DJU issus de la météo simulée (démonstration)", -0.05)
    elif drift.kind == DriftKind.BASELOAD:
        a.step(f"Mesure : puissance moyenne de {fr(drift.measured_value)} kW sur les {facts['night_slots']} "
               f"créneaux de 30 min de {facts['period']}.")
        a.step(f"Référence : talon médian de {fr(drift.reference_value)} kW sur les nuits des "
               f"{facts['reference_days']} jours précédents ({facts['reference_slots']} mesures).")
        a.step(f"Écart : {drift.deviation_pct:+.0f} % pour une tolérance de {_pct(facts['tolerance'])}, soit "
               f"{fr(facts['excess_kw'])} kW en trop pendant {fr(facts['night_slots'] * 0.5, 1)} h.")
        _margin_factor(a, drift.deviation_pct, facts["tolerance"])
        cv = facts.get("reference_cv")
        if cv is not None and cv < 0.15:
            a.factor(f"Talon de référence stable (variation {_pct(cv)})", 0.1)
        elif cv is not None and cv > 0.35:
            a.factor(f"Talon de référence variable (variation {_pct(cv)})", -0.1)
    elif drift.kind == DriftKind.MODEL_DEVIATION:
        start, end = facts["train_start"], facts["train_end"]
        a.step(f"Mesure : {fr(drift.measured_value)} kWh consommés"
               + (f", température moyenne {fr(facts['temperature'], 1)} °C." if facts.get("temperature") is not None
                  else "."))
        a.step(f"Référence : {fr(drift.reference_value)} kWh attendus par le modèle de consommation ({facts['model']}), "
               f"appris sur {facts['train_days']} jours du {start[8:10]}/{start[5:7]}/{start[:4]} au "
               f"{end[8:10]}/{end[5:7]}/{end[:4]} et alimenté par la météo réelle du jour.")
        a.step(f"Écart : {drift.deviation_pct:+.0f} % pour un seuil de {_pct(facts['tolerance'])} : au moins 15 %, et "
               f"2,5 fois l'erreur journalière du modèle ({_pct(facts['cv'])}, mesurée sur des périodes non apprises).")
        _margin_factor(a, drift.deviation_pct, facts["tolerance"])
        if facts["cv"] <= 0.10:
            a.factor(f"Modèle précis ({_pct(facts['cv'])} d'erreur journalière)", 0.1)
        elif facts["cv"] > 0.20:
            a.factor(f"Modèle peu précis ({_pct(facts['cv'])} d'erreur journalière)", -0.1)
        if facts["train_days"] >= 600:
            a.factor(f"Modèle appris sur près de deux ans ({facts['train_days']} jours)", 0.05)
        if str(facts.get("weather_source", "")).startswith("MOCK"):
            a.factor("Météo simulée (démonstration)", -0.05)
    else:  # OFF_HOURS
        origin = "classe déduite des données" if facts.get("classes_learned") else "classe par défaut"
        a.step(f"Mesure : puissance moyenne de {fr(drift.measured_value)} kW sur les {facts['off_slots']} "
               f"créneaux de 30 min inoccupés hors nuit ({facts['period']}).")
        a.step(f"Référence : {fr(drift.reference_value)} kW attendus = talon de nuit du jour même "
               f"({fr(facts['talon'])} kW) + écart habituel entre inoccupation et nuit ({fr(facts['usual_gap'], 1)} kW, "
               f"médiane de {facts['comparable_days']} jours comparables des {facts['window']} jours précédents : "
               f"jours « {facts['day_class']} », {origin}). Partir du talon du jour évite de signaler deux fois "
               "le même excès : un talon trop haut relève du détecteur « talon de nuit ».")
        a.step(f"Écart : {drift.deviation_pct:+.0f} % pour une tolérance de {_pct(facts['tolerance'])}, soit "
               f"{fr(facts['excess_kw'])} kW en trop pendant {fr(facts['off_slots'] * 0.5, 1)} h.")
        _margin_factor(a, drift.deviation_pct, facts["tolerance"])
        if facts["comparable_days"] >= 6:
            a.factor(f"Référence établie sur {facts['comparable_days']} jours comparables", 0.05)
        elif facts["comparable_days"] < 4:
            a.factor(f"Peu de jours comparables ({facts['comparable_days']})", -0.05)

    if cov is not None:
        if cov >= 0.98:
            a.factor("Données complètes ce jour-là", 0.05)
        elif cov < 0.9:
            a.factor(f"Données incomplètes ce jour-là ({_pct(cov)})", -0.15)

    occurrences = recurrence(db, drift)
    per_year = occurrences * 365 / RECURRENCE_WINDOW_DAYS
    a.step(f"Récurrence : {occurrences} occurrence(s) du même motif sur ce compteur en {RECURRENCE_WINDOW_DAYS} jours.")
    if occurrences >= 3:
        a.factor(f"Motif récurrent ({occurrences} occurrences en {RECURRENCE_WINDOW_DAYS} jours)", 0.1)
    elif occurrences == 1:
        a.factor("Première occurrence : peut être ponctuelle (événement, maintenance)", -0.05)

    # F2b : qualification et propagation d'impact par le graphe physique, ajoutées par `anomaly_context`.
    a.step("Statut : proposition de la plateforme, à valider par l'auditeur ou le responsable énergie. "
           "Aucune action n'est déclenchée automatiquement.")

    excess = facts.get("excess_kwh")
    if excess and excess > 0:
        a.gain_kwh = excess * per_year
        basis = (f"{fr(excess)} kWh consommés en trop ce jour-là × {fr(per_year, 1)} occurrences par an "
                 f"(rythme observé : {occurrences} en {RECURRENCE_WINDOW_DAYS} jours). Hypothèse : la correction "
                 "supprime l'excès ; prix et facteurs d'émission indicatifs.")
        if drift.kind == DriftKind.THRESHOLD and dp.fluid == Fluid.ELEC:
            basis += " Hors pénalités de dépassement de puissance, non estimées."
        a.gain_basis = basis
    else:
        a.gain_basis = "Gain non chiffrable : le détail du calcul n'est pas disponible."
    apply_assessment(db, drift, a, dp.fluid, drift.day)
    anomaly_context.contextualize(db, drift)


def backfill_explanations(db: Session) -> int:
    """Explique les anomalies enregistrées avant le principe P1 (mise à niveau d'une base existante)."""
    from app.services import integrations
    from app.services.drift import DETECTOR_BY_KIND

    # Filtre en Python : couvre aussi une valeur JSON « null » écrite par une version antérieure.
    missing = [d for d in db.scalars(select(Drift).order_by(Drift.day, Drift.id)) if not d.reasoning]
    if not missing:
        return 0
    weather = integrations.weather_provider(db)
    for drift in missing:
        dp = drift.delivery_point
        try:
            candidate = DETECTOR_BY_KIND[drift.kind](db, dp, drift.day, weather)
        except Exception:  # source indisponible : explication reconstituée à partir des valeurs enregistrées
            logger.exception("Recalcul impossible pour la dérive #%s", drift.id)
            candidate = None
        if candidate is not None and candidate.kind != drift.kind:
            candidate = None
        explain_drift(db, dp, drift, candidate)
    db.commit()
    return len(missing)
