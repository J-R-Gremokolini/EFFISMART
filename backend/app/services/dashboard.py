"""F1 — Dashboard de pilotage (portefeuille auditeur + dashboard client).

Les agrégats sont calculés à la volée. Optimisation prévue si besoin :
agrégats continus TimescaleDB (journaliers / mensuels).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DeliveryPoint, Drift, DriftStatus, EmissionFactor, Fluid, Measurement, Organization, Site
from app.repositories import TenantRepository
from app.services.consent import active_consent_clause, require_active_consent
from app.timeutils import (
    add_months,
    ensure_utc,
    local_day_bounds,
    local_midnight_utc,
    month_start,
    to_local,
    today_local,
    yesterday_local,
)

PERIOD_PRESETS = {"7d": 7, "30d": 30, "12m": 365}


def estimated_price(fluid: Fluid) -> float:
    if fluid == Fluid.ELEC:
        return settings.estimated_price_elec_eur_kwh
    return settings.estimated_price_gas_eur_kwh


def emission_factors_at(db: Session, at: date) -> dict[Fluid, EmissionFactor]:
    """Facteur en vigueur à la date `at` pour chaque fluide (à défaut, le plus ancien connu)."""
    factors: dict[Fluid, EmissionFactor] = {}
    for fluid in Fluid:
        factor = db.scalar(
            select(EmissionFactor)
            .where(EmissionFactor.fluid == fluid, EmissionFactor.valid_from <= at)
            .order_by(EmissionFactor.valid_from.desc())
            .limit(1)
        ) or db.scalar(
            select(EmissionFactor).where(EmissionFactor.fluid == fluid).order_by(EmissionFactor.valid_from).limit(1)
        )
        if factor is not None:
            factors[fluid] = factor
    return factors


def consented_delivery_points(db: Session, organization_id: int) -> list[DeliveryPoint]:
    stmt = (
        select(DeliveryPoint)
        .join(Site, DeliveryPoint.site_id == Site.id)
        .where(Site.organization_id == organization_id, active_consent_clause())
        .order_by(DeliveryPoint.id)
    )
    return list(db.scalars(stmt))


def sum_kwh_by_fluid(db: Session, delivery_point_ids: list[int], t0, t1) -> dict[Fluid, float]:
    if not delivery_point_ids:
        return {}
    rows = db.execute(
        select(DeliveryPoint.fluid, func.sum(Measurement.value_kwh))
        .join(DeliveryPoint, DeliveryPoint.id == Measurement.delivery_point_id)
        .where(
            Measurement.delivery_point_id.in_(delivery_point_ids),
            Measurement.time >= t0,
            Measurement.time < t1,
        )
        .group_by(DeliveryPoint.fluid)
    )
    return {Fluid(fluid): float(total or 0) for fluid, total in rows}


def data_as_of(db: Session, delivery_point_ids: list[int]) -> date | None:
    """Date de la donnée la plus récente (affichée « données arrêtées au … »)."""
    if not delivery_point_ids:
        return None
    latest = db.scalar(
        select(func.max(Measurement.time)).where(Measurement.delivery_point_id.in_(delivery_point_ids))
    )
    return to_local(latest).date() if latest else None


def count_open_drifts(db: Session, organization_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(Drift.id))
            .join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id)
            .join(Site, DeliveryPoint.site_id == Site.id)
            .where(Site.organization_id == organization_id, Drift.status == DriftStatus.OPEN)
        )
        or 0
    )


def resolve_period(
    period: str, start: date | None, end: date | None, anchor: date | None
) -> tuple[date, date]:
    """Période du sélecteur : 7j / 30j / 12 mois (glissants jusqu'à la dernière donnée) ou personnalisée."""
    anchor = anchor or yesterday_local()
    if period == "custom" and start and end:
        return (start, end) if start <= end else (end, start)
    days = PERIOD_PRESETS.get(period, 30)
    return anchor - timedelta(days=days - 1), anchor


def n0_by_fluid(db: Session, organization_id: int, start: date, end: date) -> tuple[dict[Fluid, float], dict]:
    """Part des consommations déclarées (N0, factures) sur les jours sans mesure de compteur (N1).

    Renvoie (total par fluide, {mois « AAAA-MM »: {fluide: kWh}}).
    """
    from app.services.energy_data import organization_n0  # import local : évite un cycle

    shares = organization_n0(db, organization_id, start, end)
    fluids = dict(db.execute(select(DeliveryPoint.id, DeliveryPoint.fluid).where(DeliveryPoint.id.in_(list(shares)))).all()) \
        if shares else {}
    totals: dict[Fluid, float] = defaultdict(float)
    monthly: dict[str, dict[Fluid, float]] = defaultdict(lambda: defaultdict(float))
    for dp_id, days in shares.items():
        for day, kwh in days.items():
            totals[fluids[dp_id]] += kwh
            monthly[day.strftime("%Y-%m")][fluids[dp_id]] += kwh
    return dict(totals), monthly


def _add(a: dict[Fluid, float], b: dict[Fluid, float]) -> dict[Fluid, float]:
    return {fluid: a.get(fluid, 0.0) + b.get(fluid, 0.0) for fluid in set(a) | set(b)}


def portfolio(db: Session, repo: TenantRepository) -> list[dict]:
    """Vue portefeuille : conso du dernier mois complet (N1 et N0), variation vs mois précédent, dérives ouvertes."""
    current_month = month_start(today_local())
    last_month = add_months(current_month, -1)
    previous_month = add_months(current_month, -2)
    rows = []
    for org in repo.list_organizations():
        ids = [dp.id for dp in consented_delivery_points(db, org.id)]
        _, n0_monthly = n0_by_fluid(db, org.id, previous_month, current_month - timedelta(days=1))
        last = sum(
            sum_kwh_by_fluid(db, ids, local_midnight_utc(last_month), local_midnight_utc(current_month)).values()
        ) + sum(n0_monthly.get(last_month.strftime("%Y-%m"), {}).values())
        before = sum(
            sum_kwh_by_fluid(db, ids, local_midnight_utc(previous_month), local_midnight_utc(last_month)).values()
        ) + sum(n0_monthly.get(previous_month.strftime("%Y-%m"), {}).values())
        rows.append(
            {
                "organization_id": org.id,
                "name": org.name,
                "sites_count": len(org.sites),
                "delivery_points_count": len(ids),
                "month": last_month.strftime("%Y-%m"),
                "last_month_kwh": round(last, 1),
                "previous_month_kwh": round(before, 1),
                "variation_pct": round((last - before) / before * 100, 1) if before > 0 else None,
                "open_drifts": count_open_drifts(db, org.id),
                "data_as_of": data_as_of(db, ids),
            }
        )
    return rows


def organization_dashboard(
    db: Session, org: Organization, period: str, start: date | None, end: date | None
) -> dict:
    from app.services.energy_data import data_level, latest_declared_day  # import local : évite un cycle

    consented = consented_delivery_points(db, org.id)
    ids = [dp.id for dp in consented]
    all_ids = list(db.scalars(select(DeliveryPoint.id).join(Site, DeliveryPoint.site_id == Site.id)
                              .where(Site.organization_id == org.id)))
    as_of = data_as_of(db, ids) or latest_declared_day(db, all_ids)  # N0 seul : fin de la dernière facture
    start, end = resolve_period(period, start, end, as_of)
    t0, t1 = local_day_bounds(start, end)

    n1_by_fluid = sum_kwh_by_fluid(db, ids, t0, t1)
    n0_totals, _ = n0_by_fluid(db, org.id, start, end)
    by_fluid = _add(n1_by_fluid, n0_totals)
    factors = emission_factors_at(db, end)
    cost = sum(kwh * estimated_price(fluid) for fluid, kwh in by_fluid.items())
    emissions = sum(
        kwh * factors[fluid].factor_kgco2_per_kwh for fluid, kwh in by_fluid.items() if fluid in factors
    )

    # Consommation mensuelle : 12 mois se terminant au mois de fin de période (N1, complété par N0).
    monthly = []
    last_month = month_start(end)
    _, n0_monthly = n0_by_fluid(db, org.id, add_months(last_month, -11), add_months(last_month, 1) - timedelta(days=1))
    for offset in range(11, -1, -1):
        m = add_months(last_month, -offset)
        n1 = sum_kwh_by_fluid(db, ids, local_midnight_utc(m), local_midnight_utc(add_months(m, 1)))
        n0 = n0_monthly.get(m.strftime("%Y-%m"), {})
        sums = _add(n1, n0)
        monthly.append(
            {
                "month": m.strftime("%Y-%m"),
                "elec_kwh": round(sums.get(Fluid.ELEC, 0.0), 1),
                "gas_kwh": round(sums.get(Fluid.GAS, 0.0), 1),
                "n0_kwh": round(sum(n0.values()), 1),
            }
        )

    all_points = db.execute(
        select(DeliveryPoint, Site.name)
        .join(Site, DeliveryPoint.site_id == Site.id)
        .where(Site.organization_id == org.id)
        .order_by(DeliveryPoint.id)
    ).all()

    return {
        "organization": {"id": org.id, "name": org.name, "siren": org.siren},
        "data_as_of": as_of,
        "period": {"start": start, "end": end},
        "totals": {
            "elec_kwh": round(by_fluid.get(Fluid.ELEC, 0.0), 1),
            "gas_kwh": round(by_fluid.get(Fluid.GAS, 0.0), 1),
            "total_kwh": round(sum(by_fluid.values()), 1),
            "cost_eur": round(cost, 2),
            "emissions_kgco2e": round(emissions, 1),
            "n0_kwh": round(sum(n0_totals.values()), 1),  # dont : factures et relevés (mode dégradé)
        },
        "monthly": monthly,
        "delivery_points": [
            {
                "id": dp.id,
                "site_name": site_name,
                "fluid": dp.fluid,
                "external_ref": dp.external_ref,
                "is_primary": dp.is_primary,
                "has_active_consent": dp.id in ids,
                "data_level": data_level(db, dp, start, end),  # « N1 », « N0 » ou None
            }
            for dp, site_name in all_points
        ],
        "emission_factors": [
            {
                "fluid": fluid,
                "factor_kgco2_per_kwh": f.factor_kgco2_per_kwh,
                "version": f.version,
                "source": f.source,
                "valid_from": f.valid_from,
            }
            for fluid, f in factors.items()
        ],
        "estimated_prices_eur_kwh": {fluid.value: estimated_price(fluid) for fluid in Fluid},
        "open_drifts": count_open_drifts(db, org.id),
    }


def performance(db: Session, org: Organization, end: date) -> dict:
    """F1 — indicateurs de performance sur les 12 mois se terminant à `end`, par site et pour l'organisation :
    intensité énergétique (kWh/m²/an), coût et émissions par m², et niveau des données utilisées (N1, N0)."""
    from app.services.energy_data import organization_n0  # import local : évite un cycle

    start = end - timedelta(days=364)
    t0, t1 = local_day_bounds(start, end)
    consented = {dp.id for dp in consented_delivery_points(db, org.id)}
    n0 = organization_n0(db, org.id, start, end)
    factors = emission_factors_at(db, end)
    sites, totals = [], {"kwh": 0.0, "cost_eur": 0.0, "kgco2e": 0.0, "surface_m2": 0.0}
    for site in db.scalars(select(Site).where(Site.organization_id == org.id).order_by(Site.id)):
        points = list(site.delivery_points)
        by_fluid = sum_kwh_by_fluid(db, [dp.id for dp in points if dp.id in consented], t0, t1)
        n0_fluid: dict[Fluid, float] = defaultdict(float)
        for dp in points:
            n0_fluid[dp.fluid] += sum(n0.get(dp.id, {}).values())
        merged = _add(by_fluid, dict(n0_fluid))
        kwh = sum(merged.values())
        cost = sum(v * estimated_price(f) for f, v in merged.items())
        co2 = sum(v * factors[f].factor_kgco2_per_kwh for f, v in merged.items() if f in factors)
        levels = (["N1"] if any(v for v in by_fluid.values()) else []) + (["N0"] if any(n0_fluid.values()) else [])
        surface = site.surface_m2 or 0.0
        sites.append({
            "site_id": site.id, "name": site.name, "surface_m2": site.surface_m2, "kwh": round(kwh, 1),
            "cost_eur": round(cost, 2), "kgco2e": round(co2, 1), "levels": levels,
            "kwh_m2": round(kwh / surface, 1) if surface else None,
            "eur_m2": round(cost / surface, 2) if surface else None,
            "kgco2e_m2": round(co2 / surface, 2) if surface else None,
        })
        totals["kwh"] += kwh
        totals["cost_eur"] += cost
        totals["kgco2e"] += co2
        if surface and kwh:
            totals["surface_m2"] += surface
    measured_kwh = sum(s["kwh"] for s in sites if s["surface_m2"])
    totals["kwh_m2"] = round(measured_kwh / totals["surface_m2"], 1) if totals["surface_m2"] else None
    return {"start": start, "end": end, "sites": sites, "totals": totals}


def load_curve(db: Session, delivery_point: DeliveryPoint, start: date, end: date) -> dict:
    """Courbe de charge : pas 30 min (kW) pour l'élec., quotidien (kWh) pour le gaz.

    Au-delà de `load_curve_max_raw_days`, la courbe élec. est agrégée au jour.
    """
    require_active_consent(db, delivery_point)
    t0, t1 = local_day_bounds(start, end)
    rows = db.execute(
        select(Measurement.time, Measurement.value_kwh, Measurement.avg_power_kw)
        .where(
            Measurement.delivery_point_id == delivery_point.id,
            Measurement.time >= t0,
            Measurement.time < t1,
        )
        .order_by(Measurement.time)
    ).all()

    raw = (end - start).days + 1 <= settings.load_curve_max_raw_days
    if delivery_point.fluid == Fluid.ELEC and raw:
        points = [
            {"t": ensure_utc(t).isoformat(), "value": kw if kw is not None else kwh * 2}
            for t, kwh, kw in rows
        ]
        return {"step": "PT30M", "unit": "kW", "aggregated": False, "points": points}

    daily: dict[date, float] = defaultdict(float)
    for t, kwh, _ in rows:
        daily[to_local(t).date()] += kwh
    points = [{"t": day.isoformat(), "value": round(v, 2)} for day, v in sorted(daily.items())]
    return {
        "step": "P1D",
        "unit": "kWh",
        "aggregated": delivery_point.fluid == Fluid.ELEC,
        "points": points,
    }
