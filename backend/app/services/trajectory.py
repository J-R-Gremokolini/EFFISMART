"""F11 — Trajectoire Décret Tertiaire : « au rythme actuel, −27 % en 2030 au lieu des −40 % exigés ».

1. Consommation actuelle corrigée du climat : modèle de consommation appris sur les 12 à 24 derniers mois de
   chaque énergie, appliqué à une année de météo normale ; total en kWh d'énergie finale.
2. Référence : année et consommation déclarées sur OPERAT, saisies par l'auditeur.
3. Rythme actuel : évolution annuelle moyenne entre la référence et aujourd'hui, prolongée jusqu'en 2050.
4. Objectifs en valeur relative : −40 % en 2030, −50 % en 2040, −60 % en 2050 par rapport à la référence.
   L'objectif en valeur absolue (Cabs) et les modulations ne sont pas calculés.
5. Avec actions : l'économie annuelle du scénario retenu dans le simulateur (F9) est retranchée dès l'année
   suivante ; l'écart entre les deux trajectoires mesure l'effet du plan d'actions.

Projection (principe P1) : proposée, expliquée, validée par un humain avant d'être montrée au client.
Mode dégradé : moins de 12 mois d'historique sur une énergie du site, pas de trajectoire.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Fluid, ReviewStatus, SavingsScenario, Site, Trajectory
from app.providers.weather import WeatherProvider
from app.services import consumption_model
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of, emission_factors_at, estimated_price
from app.services.validation import Assessment, apply_assessment, fr, notify
from app.timeutils import today_local

logger = logging.getLogger(__name__)

ALGORITHM = "Trajectoire Décret Tertiaire v1 (modèle de consommation, rythme annuel moyen depuis la référence)"
OBJECTIVES = {2030: 0.40, 2040: 0.50, 2050: 0.60}
HORIZON = 2050
MIN_SPAN_YEARS = 1.0
FLUID_NAMES = {Fluid.ELEC: "électricité", Fluid.GAS: "gaz"}


@dataclass
class FluidPart:
    fluid: Fluid
    kwh: float
    cv: float
    days: int
    model: str


def decimal_year(day: date) -> float:
    days_in_year = (date(day.year + 1, 1, 1) - date(day.year, 1, 1)).days
    return day.year + (day.timetuple().tm_yday - 0.5) / days_in_year


def objective_path(reference_kwh: float, reference_year: int, year: int) -> float:
    """Trajectoire cible : linéaire entre la référence et chaque jalon (−40 % 2030, −50 % 2040, −60 % 2050)."""
    milestones = [(reference_year, 0.0), *sorted(OBJECTIVES.items())]
    for (y0, r0), (y1, r1) in zip(milestones, milestones[1:]):
        if y0 <= year <= y1:
            return reference_kwh * (1 - (r0 + (r1 - r0) * (year - y0) / (y1 - y0)))
    return reference_kwh * (1 - OBJECTIVES[HORIZON])


def normalized_consumption(db: Session, site: Site, as_of: date,
                           weather: WeatherProvider) -> list[FluidPart] | None:
    """Consommation d'une année de météo normale, par énergie ; None en mode dégradé."""
    parts = []
    for fluid in Fluid:
        points = [dp for dp in site.delivery_points if dp.fluid == fluid and has_active_consent(db, dp.id)]
        if not points:
            continue
        model = consumption_model.train(db, points, as_of, weather)
        if model is None:
            return None
        parts.append(FluidPart(fluid, model.normal_year_kwh(as_of + timedelta(days=1)), model.cv, model.days,
                               model.describe()))
    return parts or None


def retained_scenario(db: Session, site_id: int) -> SavingsScenario | None:
    return db.scalar(select(SavingsScenario).where(SavingsScenario.site_id == site_id,
                                                   SavingsScenario.retained.is_(True))
                     .order_by(SavingsScenario.created_at.desc()).limit(1))


def compute(db: Session, site: Site, weather: WeatherProvider, as_of: date | None = None) -> Trajectory | None:
    """Trajectoire d'un site assujetti dont la référence est connue (non enregistrée) ; None sinon."""
    if not site.is_tertiary_decret or not site.dt_reference_kwh or not site.dt_reference_year:
        return None
    points = [dp for dp in site.delivery_points if has_active_consent(db, dp.id)]
    as_of = as_of or data_as_of(db, [dp.id for dp in points])
    if as_of is None:
        return None
    parts = normalized_consumption(db, site, as_of, weather)
    if parts is None:
        return None
    reference, ref_year = site.dt_reference_kwh, site.dt_reference_year
    current = sum(p.kwh for p in parts)
    t_now = decimal_year(as_of) - 0.5  # milieu des 12 mois appris
    span = t_now - (ref_year + 0.5)
    if span < MIN_SPAN_YEARS or current <= 0:
        return None
    rate = (current / reference) ** (1 / span) - 1

    def trend(year: int) -> float:
        return current * (1 + rate) ** (year + 0.5 - t_now)

    scenario = retained_scenario(db, site.id)
    saved = float(scenario.results.get("saved_kwh", 0.0)) if scenario else 0.0

    def with_actions(year: int) -> float | None:
        if not saved:
            return None
        return trend(year) if year <= as_of.year else (current - saved) * (1 + rate) ** (year + 0.5 - t_now)

    years = list(range(ref_year, HORIZON + 1))
    points_json = [{"year": y, "trend": round(trend(y)) if y >= as_of.year else None,
                    "objective": round(objective_path(reference, ref_year, y)),
                    "with_actions": round(v) if (v := with_actions(y)) is not None and y >= as_of.year else None}
                   for y in years]
    reductions = {y: 1 - trend(y) / reference for y in OBJECTIVES}
    reduction_actions = 1 - with_actions(2030) / reference if saved else None

    a = Assessment(algorithm=ALGORITHM)
    a.step(f"Référence Décret Tertiaire : {ref_year}, {fr(reference)} kWh d'énergie finale (déclarée sur OPERAT, "
           "saisie par l'auditeur).")
    a.step(f"Consommation actuelle corrigée du climat : {fr(current)} kWh sur une année de météo normale, d'après le "
           "modèle de consommation de chaque énergie, appris sur ses 12 à 24 derniers mois jusqu'au "
           f"{as_of:%d/%m/%Y} : " + " ; ".join(f"{FLUID_NAMES[p.fluid]} {fr(p.kwh)} kWh ({p.model}, erreur "
                                                  f"journalière {fr(p.cv * 100, 1)} %)" for p in parts) + ".")
    a.step(f"Rythme actuel : {fr(rate * 100, 1)} % par an en moyenne depuis {ref_year} (de {fr(reference)} à "
           f"{fr(current)} kWh en {fr(span, 1)} ans), prolongé jusqu'en {HORIZON}.")
    verdict = "objectif atteint" if reductions[2030] >= OBJECTIVES[2030] else \
        f"au lieu des −{fr(OBJECTIVES[2030] * 100)} % exigés"
    a.step(f"Au rythme actuel : {fr(-reductions[2030] * 100)} % en 2030 ({verdict}) ; "
           f"{fr(-reductions[2040] * 100)} % en 2040 (−50 % exigés) ; {fr(-reductions[2050] * 100)} % en 2050 "
           "(−60 % exigés).")
    gap = trend(2030) - reference * (1 - OBJECTIVES[2030])
    if gap > 0:
        a.step(f"Écart à l'objectif 2030 : {fr(gap)} kWh par an à économiser en plus du rythme actuel.")
    if saved:
        a.step(f"Avec le scénario retenu « {scenario.name} » (simulateur, {fr(saved)} kWh économisés par an dès "
               f"{as_of.year + 1}) : {fr(-reduction_actions * 100)} % en 2030.")
    a.step("Limites : objectif en valeur relative seulement (la valeur absolue Cabs et les modulations ne sont pas "
           "calculées) ; prolongation d'un rythme moyen, pas une prévision des actions à venir.")
    a.step("Statut : proposition de la plateforme, à valider avant d'être montrée au client.")
    cv = sum(p.cv * p.kwh for p in parts) / current
    if cv <= 0.10:
        a.factor(f"Modèles de consommation précis ({fr(cv * 100, 1)} % d'erreur journalière)", 0.1)
    elif cv > 0.20:
        a.factor(f"Modèles de consommation peu précis ({fr(cv * 100, 1)} % d'erreur journalière)", -0.1)
    if span >= 3:
        a.factor(f"Rythme mesuré sur {fr(span, 1)} ans depuis la référence", 0.1)
    elif span < 2:
        a.factor(f"Rythme mesuré sur {fr(span, 1)} an(s) seulement : extrapolation fragile", -0.15)
    if 2030 - as_of.year > 5:
        a.factor("Horizon 2030 éloigné de plus de cinq ans", -0.05)
    if min(p.days for p in parts) < 600:
        a.factor("Moins de deux ans d'historique pour au moins une énergie", -0.05)
    if str(getattr(weather, "source", "")).startswith("MOCK"):
        a.factor("Météo simulée (démonstration)", -0.05)

    trajectory = Trajectory(
        organization_id=site.organization_id, site_id=site.id, data_as_of=as_of, reference_year=ref_year,
        reference_kwh=round(reference, 1), current_kwh=round(current, 1), annual_rate=round(rate, 5),
        projected_2030_kwh=round(trend(2030), 1), reduction_2030=round(reductions[2030], 4),
        reduction_2030_with_actions=round(reduction_actions, 4) if reduction_actions is not None else None,
        points=points_json,
    )
    a.gain_basis = ("Enjeu : économie annuelle supplémentaire nécessaire, au-delà du rythme actuel, pour atteindre "
                    "l'objectif 2030 ; répartie entre énergies selon leur part ; prix et facteurs indicatifs.")
    apply_assessment(db, trajectory, a, Fluid.ELEC, as_of)
    if gap > 0:
        factors = emission_factors_at(db, as_of)
        trajectory.gain_kwh = round(gap)
        trajectory.gain_eur = round(sum(gap * p.kwh / current * estimated_price(p.fluid) for p in parts))
        trajectory.gain_kgco2e = round(sum(gap * p.kwh / current * factors[p.fluid].factor_kgco2_per_kwh
                                           for p in parts if p.fluid in factors))
    else:
        trajectory.gain_kwh = trajectory.gain_eur = trajectory.gain_kgco2e = None
        trajectory.gain_basis = "Objectif 2030 atteint au rythme actuel : pas d'économie supplémentaire nécessaire."
    return trajectory


def refresh_trajectories(db: Session, organization_id: int | None = None, *, force: bool = False) -> list[Trajectory]:
    """Recalcule les trajectoires, au plus une fois par mois de données (sauf `force`) ; une trajectoire plus
    récente remplace une trajectoire encore à valider, jamais une trajectoire validée."""
    from app.services import integrations

    weather = integrations.weather_provider(db)
    stmt = select(Site).where(Site.is_tertiary_decret.is_(True), Site.dt_reference_kwh.is_not(None)).order_by(Site.id)
    if organization_id is not None:
        stmt = stmt.where(Site.organization_id == organization_id)
    created = []
    for site in db.scalars(stmt).all():
        points = [dp for dp in site.delivery_points if has_active_consent(db, dp.id)]
        as_of = data_as_of(db, [dp.id for dp in points])
        if as_of is None:
            continue
        previous = list(db.scalars(select(Trajectory).where(
            Trajectory.site_id == site.id, Trajectory.status != ReviewStatus.SUPERSEDED)
            .order_by(Trajectory.created_at.desc(), Trajectory.id.desc())))
        latest = previous[0] if previous else None
        if not force and latest is not None and (latest.data_as_of.year, latest.data_as_of.month) == (
                as_of.year, as_of.month):
            continue
        trajectory = compute(db, site, weather, as_of)
        if trajectory is None:
            continue
        for old in previous:
            if old.status == ReviewStatus.PROPOSED:
                old.status = ReviewStatus.SUPERSEDED
                old.review_comment = f"Remplacée par une trajectoire plus récente (données au {as_of:%d/%m/%Y})."
        db.add(trajectory)
        created.append(trajectory)
    db.flush()
    for org_id in {t.organization_id for t in created}:
        count = sum(1 for t in created if t.organization_id == org_id)
        notify(db, org_id, f"À valider : {count} trajectoire(s) Décret Tertiaire", validators_only=True)
    db.commit()
    return created


def set_reference(db: Session, repo, user, site_id: int, year: int | None, kwh: float | None) -> Site:
    """Référence Décret Tertiaire d'un site (déclarée sur OPERAT), saisie par l'auditeur."""
    from app.models import Role

    if user.role != Role.AUDITOR or user.auditor_id is None:
        raise PermissionError("La référence Décret Tertiaire est saisie par l'auditeur, d'après OPERAT.")
    site = repo.get_site(site_id)
    if (year is None) != (kwh is None):
        raise ValueError("Saisissez l'année et la consommation de référence, ou aucune des deux.")
    if year is not None and (not 2010 <= year <= today_local().year - 1 or kwh <= 0):
        raise ValueError("Référence : une année entre 2010 et l'an dernier, et une consommation positive (kWh).")
    site.dt_reference_year, site.dt_reference_kwh = year, kwh
    db.commit()
    return site


def reduction_with(trajectory: Trajectory, saved_kwh: float, year: int = 2030) -> float:
    """Réduction projetée en `year` si l'on retranche `saved_kwh` par an dès l'année suivant la trajectoire."""
    t_now = decimal_year(trajectory.data_as_of) - 0.5
    projected = (trajectory.current_kwh - saved_kwh) * (1 + trajectory.annual_rate) ** (year + 0.5 - t_now)
    return 1 - projected / trajectory.reference_kwh
