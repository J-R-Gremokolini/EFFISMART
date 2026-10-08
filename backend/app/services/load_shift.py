"""F12 — Optimisation tarifaire en mode conseil (décision D3) : recommandation de décalage de charge.

La plateforme identifie la fenêtre de fonctionnement la moins chère d'un équipement décalable selon le prix
de l'électricité de son contrat (heures creuses, couleur Tempo ou prix horaire du marché) et la recommande.
L'exploitant applique lui-même le réglage : aucune écriture vers les équipements (niveau N4 exclu en V1 et V2).

1. Équipements décalables : ceux du graphe P2 dont la durée de fonctionnement quotidienne est connue
   (chargeurs : 6 h, ballon d'eau chaude : 4 h par défaut ; tout autre équipement si l'auditeur la renseigne),
   alimentés par un compteur électrique sous contrat heures creuses, Tempo ou dynamique.
2. Fenêtre actuelle : sur les jours ouvrés des 28 derniers jours, la plage où la consommation dépasse le plus
   le talon de nuit ; si l'équipement n'y est pas reconnaissable, les heures d'occupation (hypothèse signalée).
3. Fenêtre conseillée : la plage de même durée au prix moyen le plus bas (Tempo pondéré par le nombre de jours
   de chaque couleur ; marché : prix des 28 derniers jours).
4. Gain : puissance × durée × écart de prix × jours de fonctionnement par an. L'énergie ne change pas : le gain
   est financier, le même que si la plateforme agissait elle-même, sans en porter la responsabilité.

Vocabulaire : « recommandation de décalage de charge ». La recommandation suit le principe P1 : proposée,
expliquée, validée par un humain, déclarée appliquée par un humain.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AssetNode,
    DeliveryPoint,
    Fluid,
    Recommendation,
    RecommendationKind,
    ReviewStatus,
    Site,
    SupplyContract,
    TariffOption,
)
from app.services import assets, tariffs
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of
from app.services.validation import Assessment, apply_assessment, fr, fr_kw, notify
from app.timeutils import LOCAL_TZ, daterange, ensure_utc

logger = logging.getLogger(__name__)

ALGORITHM = "Décalage de charge v1 (profil de charge, grille tarifaire du contrat ; mode conseil)"
# Catégorie : (heures de fonctionnement par jour, jours de fonctionnement par an) par défaut.
SHIFT_DEFAULTS = {"CHARGING": (6.0, 250), "DHW_TANK": (4.0, 365)}
DEFAULT_RUN_DAYS = 250
LOOKBACK_DAYS = 28
MIN_GAIN_EUR = 50.0
REJECTION_MEMORY_DAYS = 180
OCCUPANCY_START = 8
TEMPO_WEIGHTS = {"ROUGE": tariffs.TEMPO_RED_DAYS, "BLANC": tariffs.TEMPO_WHITE_DAYS,
                 "BLEU": 365 - tariffs.TEMPO_RED_DAYS - tariffs.TEMPO_WHITE_DAYS}


def shiftable(eq: AssetNode) -> tuple[float, int] | None:
    """(heures par jour, jours par an) si l'équipement est décalable."""
    hours, days = SHIFT_DEFAULTS.get(eq.category, (None, DEFAULT_RUN_DAYS))
    if eq.shift_hours:
        hours = eq.shift_hours
    if not hours or not 0 < hours < 24:
        return None
    return float(hours), days


def hourly_prices(contract: SupplyContract, days: list[date]) -> list[float]:
    """Prix moyen de chaque heure de la journée (€/kWh) selon le contrat."""
    if contract.option == TariffOption.TEMPO:
        return [sum(n * contract.tempo_prices[color]["HC" if tariffs.is_offpeak(contract, h) else "HP"]
                    for color, n in TEMPO_WEIGHTS.items()) / 365 for h in range(24)]
    prices = []
    for hour in range(24):
        values = [tariffs.slot_price(contract, datetime.combine(day, time(hour, 30), tzinfo=LOCAL_TZ))[0]
                  for day in days]
        prices.append(sum(values) / len(values))
    return prices


def window_price(prices: list[float], start: int, hours: float) -> float:
    """Prix moyen d'une plage de `hours` heures commençant à `start` (heures consécutives, minuit compris)."""
    whole = int(hours)
    weights = [1.0] * whole + ([hours - whole] if hours > whole else [])
    return sum(w * prices[(start + i) % 24] for i, w in enumerate(weights)) / hours


def best_window(prices: list[float], hours: float, anchor: int = 0) -> int:
    """Début de la plage la moins chère ; à prix égal, la plus proche du début des heures creuses (`anchor`)."""
    return min(range(24), key=lambda start: (round(window_price(prices, start, hours), 6), (start - anchor) % 24))


def current_window(db: Session, dp: DeliveryPoint, as_of: date, hours: float,
                   power_kw: float | None) -> tuple[int, bool, float]:
    """(heure de début, reconnue dans la courbe, excès moyen en kW) de la plage de fonctionnement actuelle."""
    from app.services.drift import half_hour_powers  # import local : évite un cycle

    sums, counts = [0.0] * 24, [0] * 24
    for t, kw in half_hour_powers(db, dp.id, as_of - timedelta(days=LOOKBACK_DAYS - 1), as_of):
        if t.weekday() < 5:
            sums[t.hour] += kw
            counts[t.hour] += 1
    if not any(counts):
        return OCCUPANCY_START, False, 0.0
    profile = [s / c if c else 0.0 for s, c in zip(sums, counts)]
    talon = min(p for p, c in zip(profile, counts) if c)
    excess = [max(0.0, p - talon) for p in profile]
    start = max(range(24), key=lambda s: (window_price(excess, s, hours), -s))
    mean_excess = window_price(excess, start, hours)
    recognised = bool(power_kw) and mean_excess >= 0.3 * power_kw
    return (start, True, mean_excess) if recognised else (OCCUPANCY_START, False, mean_excess)


def _window(start: int, hours: float) -> str:
    end = (start + hours) % 24
    end_text = f"{int(end)} h" if float(end).is_integer() else f"{int(end)} h {round((end % 1) * 60):02d}"
    return f"{start} h – {end_text}"


def _active_or_rejected(db: Session, eq: AssetNode, as_of: date) -> bool:
    recent = datetime.combine(as_of - timedelta(days=REJECTION_MEMORY_DAYS), time(), tzinfo=LOCAL_TZ)
    for rec in db.scalars(select(Recommendation).where(Recommendation.equipment_id == eq.id,
                                                       Recommendation.kind == RecommendationKind.LOAD_SHIFT)):
        if rec.status in (ReviewStatus.PROPOSED, ReviewStatus.VALIDATED):
            return True
        if rec.status == ReviewStatus.REJECTED and rec.reviewed_at and ensure_utc(rec.reviewed_at) >= recent:
            return True  # écartée récemment par un humain : pas de nouvelle proposition
    return False


def assess(db: Session, site: Site, dp: DeliveryPoint, contract: SupplyContract, eq: AssetNode,
           as_of: date) -> Recommendation | None:
    """Recommandation de décalage de charge (non enregistrée) ; None si aucun gain notable."""
    shift = shiftable(eq)
    if shift is None:
        return None
    hours, run_days = shift
    days = list(daterange(as_of - timedelta(days=LOOKBACK_DAYS - 1), as_of))
    prices = hourly_prices(contract, days)
    start, recognised, excess = current_window(db, dp, as_of, hours, eq.power_kw)
    best = best_window(prices, hours, contract.offpeak_start_hour)
    current_price, best_price = window_price(prices, start, hours), window_price(prices, best, hours)
    power = eq.power_kw or excess
    if best == start or power <= 0:
        return None
    gain = power * hours * (current_price - best_price) * run_days
    if gain < MIN_GAIN_EUR:
        return None

    a = Assessment(algorithm=ALGORITHM)
    a.step(f"Contrat du compteur {dp.external_ref} : {tariffs.describe(contract)}.")
    a.step(f"Équipement décalable : « {eq.name} »"
           + (f" ({fr_kw(eq.power_kw)})" if eq.power_kw else " (puissance inconnue)")
           + f", {fr(hours, 1)} h de fonctionnement par jour, {run_days} jours par an"
           + (" (durée renseignée dans le graphe)." if eq.shift_hours else " (durée type de sa catégorie)."))
    if recognised:
        a.step(f"Fenêtre actuelle : {_window(start, hours)}, la plage où la consommation dépasse le plus le talon de "
               f"nuit en jours ouvrés ({fr(excess)} kW en moyenne sur les {LOOKBACK_DAYS} derniers jours).")
        a.factor("Fenêtre actuelle reconnue dans la courbe de charge", 0.1)
    else:
        a.step(f"Fenêtre actuelle : l'équipement n'est pas reconnaissable dans la courbe de charge ; hypothèse : "
               f"il fonctionne pendant les heures d'occupation ({_window(start, hours)}).")
        a.factor("Fenêtre actuelle supposée (heures d'occupation), non mesurée", -0.15)
    a.step(f"Prix moyen de la fenêtre actuelle : {fr(current_price, 4)} €/kWh ; fenêtre conseillée "
           f"{_window(best, hours)} : {fr(best_price, 4)} €/kWh, la moins chère de la journée pour cette durée.")
    a.step(f"Gain : {fr(power)} kW × {fr(hours, 1)} h × {fr(current_price - best_price, 4)} €/kWh × {run_days} jours = "
           f"{fr(gain)} € par an. L'énergie consommée ne change pas : le gain est financier.")
    a.step("Mode conseil : l'exploitant applique le réglage lui-même ; la plateforme n'envoie aucune commande aux "
           "équipements. Statut : proposition de la plateforme, à valider.")
    if eq.power_kw:
        a.factor("Puissance nominale connue", 0.05)
    else:
        a.factor("Puissance déduite de la courbe de charge, faute de puissance nominale", -0.1)
    if contract.option == TariffOption.DYNAMIC:
        a.factor("Prix de marché variables d'un jour à l'autre", -0.05)
    if gain >= 1000:
        a.factor(f"Gain notable ({fr(gain)} € par an)", 0.05)
    a.gain_basis = (f"Puissance × durée × écart de prix moyen entre la fenêtre actuelle et la fenêtre conseillée × "
                    f"{run_days} jours de fonctionnement par an. Énergie inchangée ; "
                    + ("signaux de prix simulés (démonstration)." if contract.option in (TariffOption.TEMPO,
                                                                                          TariffOption.DYNAMIC)
                       else "prix du contrat."))
    rec = Recommendation(
        organization_id=site.organization_id, site_id=site.id, delivery_point_id=dp.id, equipment_id=eq.id,
        kind=RecommendationKind.LOAD_SHIFT, supporting_drift_ids=[],
        title=f"Décaler la charge de « {eq.name} » sur {_window(best, hours)} ({site.name})"[:200],
        action=(f"Recommandation de décalage de charge : programmer « {eq.name} » pour fonctionner de "
                f"{_window(best, hours)} au lieu de {_window(start, hours)}. L'exploitant applique le réglage "
                "(horloge, GTB ou consigne de l'équipement) ; la plateforme n'envoie aucune commande. Vérifier avant "
                "que la plage conseillée est compatible avec l'exploitation (disponibilité des équipements, besoins "
                "aux heures d'usage)."),
    )
    apply_assessment(db, rec, a, Fluid.ELEC, as_of)
    rec.gain_kwh, rec.gain_eur, rec.gain_kgco2e = 0, round(gain), None
    return rec


def propose_load_shifts(db: Session, organization_id: int | None = None) -> list[Recommendation]:
    """Propose (sans valider) les décalages de charge rentables ; pas de doublon ni de reprise d'un refus récent."""
    stmt = select(Site).order_by(Site.id)
    if organization_id is not None:
        stmt = stmt.where(Site.organization_id == organization_id)
    created = []
    for site in db.scalars(stmt).all():
        graph = assets.load_site_graph(db, site.id)
        for dp in site.delivery_points:
            if dp.fluid != Fluid.ELEC or not has_active_consent(db, dp.id):
                continue
            as_of = data_as_of(db, [dp.id])
            contract = tariffs.active_contract(db, dp.id, as_of) if as_of else None
            meter = graph.meter_for(dp.id)
            if contract is None or contract.option == TariffOption.BASE or meter is None:
                continue
            for eq in graph.direct_consumers(meter.id):
                if _active_or_rejected(db, eq, as_of):
                    continue
                rec = assess(db, site, dp, contract, eq, as_of)
                if rec is not None:
                    db.add(rec)
                    db.flush()
                    notify(db, site.organization_id, f"À valider : recommandation de décalage de charge « {rec.title} »",
                           validators_only=True)
                    created.append(rec)
    db.commit()
    return created
