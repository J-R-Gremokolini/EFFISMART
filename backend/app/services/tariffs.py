"""F6 — Suivi mensuel tri-axe : consommations, émissions, coûts ; socle tarifaire de F9 et F12.

Coûts : grille tarifaire du contrat de fourniture de chaque point (`SupplyContract`) ; sans contrat, prix
indicatif, signalé comme tel. Émissions : facteurs ADEME de la Base Empreinte (décision D6).

Options tarifaires (prix complets en €/kWh : fourniture, acheminement, taxes) :
- BASE : prix unique ;
- HPHC : heures pleines / heures creuses, plage creuse du contrat (22 h – 6 h par défaut) ;
- TEMPO : prix par couleur de jour (bleu, blanc, rouge) et par plage. La couleur vaut de 6 h à 6 h le
  lendemain ; publiée la veille par RTE, elle est simulée en démonstration (jours les plus froids) ;
- DYNAMIC : prix horaire du marché (spot) + marge du fournisseur ; prix spot simulés en démonstration.
L'abonnement est réparti au prorata des jours. Les consommations de factures (N0) sont chiffrées au prix
moyen du contrat, faute de connaître leur répartition horaire.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DeliveryPoint,
    Fluid,
    Measurement,
    Organization,
    Role,
    Site,
    SupplyContract,
    TariffOption,
    User,
)
from app.providers.weather import MockWeatherProvider
from app.repositories import TenantRepository
from app.services import memo
from app.services.consent import has_active_consent
from app.services.dashboard import data_as_of, emission_factors_at, estimated_price
from app.services.energy_data import declared_daily
from app.timeutils import daterange, local_day_bounds, to_local

OPTION_LABELS = {
    TariffOption.BASE: "Base (prix unique)",
    TariffOption.HPHC: "Heures pleines / heures creuses",
    TariffOption.TEMPO: "Tempo (jours bleus, blancs, rouges)",
    TariffOption.DYNAMIC: "Prix dynamique (marché horaire)",
}
TEMPO_COLORS = ("BLEU", "BLANC", "ROUGE")
TEMPO_RED_DAYS, TEMPO_WHITE_DAYS = 22, 43
HP_SHARE = 2 / 3  # hypothèse de répartition HP / HC des consommations sans courbe (factures N0)
INDICATIVE = "Prix indicatif"
INVOICES = "Factures (N0)"
SIMULATED_SIGNALS = "couleurs Tempo et prix spot simulés (démonstration)"
# Forme journalière du prix spot simulé (€/kWh) : creux de nuit et de mi-journée solaire, pointes matin et soir.
_SPOT_SHAPE = (-0.020, -0.025, -0.030, -0.032, -0.030, -0.020, 0.005, 0.030, 0.045, 0.035, 0.010, -0.005,
               -0.020, -0.025, -0.022, -0.010, 0.010, 0.035, 0.055, 0.050, 0.030, 0.010, -0.005, -0.015)


class ContractError(ValueError):
    """Contrat refusé (prix manquant ou négatif, plage horaire impossible…)."""


class ContractPermissionError(PermissionError):
    pass


# --- Signaux de prix -------------------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _tempo_season(start_year: int) -> dict[date, str]:
    """Saison Tempo du 1er septembre au 31 août : 22 jours rouges (jours ouvrés de novembre à mars) et 43 blancs
    (hors dimanches, d'octobre à mai), les plus froids d'après la météo simulée."""
    days = list(daterange(date(start_year, 9, 1), date(start_year + 1, 8, 31)))
    temperature = {d: MockWeatherProvider.mean_temperature(d) for d in days}
    red = set(sorted((d for d in days if d.weekday() < 5 and (d.month >= 11 or d.month <= 3)),
                     key=temperature.__getitem__)[:TEMPO_RED_DAYS])
    white = set(sorted((d for d in days if d not in red and d.weekday() != 6 and (d.month >= 10 or d.month <= 5)),
                       key=temperature.__getitem__)[:TEMPO_WHITE_DAYS])
    return {d: "ROUGE" if d in red else "BLANC" if d in white else "BLEU" for d in days}


def tempo_color(local_dt: datetime) -> str:
    """Couleur Tempo en vigueur : le jour Tempo va de 6 h à 6 h le lendemain."""
    day = (local_dt - timedelta(hours=6)).date()
    return _tempo_season(day.year if day.month >= 9 else day.year - 1).get(day, "BLEU")


def spot_price(local_dt: datetime) -> float:
    """Prix horaire du marché simulé (€/kWh) : saison, forme journalière et aléa déterministe."""
    day_of_year = local_dt.timetuple().tm_yday
    seasonal = 0.025 * math.cos(2 * math.pi * (day_of_year - 15) / 365.25)
    noise = random.Random(f"spot:{local_dt:%Y%m%d%H}").uniform(-0.008, 0.008)
    weekend = -0.012 if local_dt.weekday() >= 5 else 0.0
    return max(0.0, 0.085 + seasonal + _SPOT_SHAPE[local_dt.hour] + weekend + noise)


# --- Prix d'un contrat -----------------------------------------------------------------------------------


def is_offpeak(contract: SupplyContract, hour: int) -> bool:
    start, end = contract.offpeak_start_hour, contract.offpeak_end_hour
    return start <= hour < end if start < end else hour >= start or hour < end


def slot_price(contract: SupplyContract, local_dt: datetime) -> tuple[float, str]:
    """Prix (€/kWh) et plage tarifaire d'un instant."""
    option = contract.option
    if option == TariffOption.BASE:
        return contract.price_base or 0.0, "Base"
    period = "HC" if is_offpeak(contract, local_dt.hour) else "HP"
    if option == TariffOption.HPHC:
        return (contract.price_hc if period == "HC" else contract.price_hp) or 0.0, period
    if option == TariffOption.TEMPO:
        color = tempo_color(local_dt)
        return float(contract.tempo_prices[color][period]), f"{color.capitalize()} {period}"
    return spot_price(local_dt) + (contract.dynamic_margin_eur_kwh or 0.0), "Marché"


@lru_cache(maxsize=1)
def _mean_spot(year: int) -> float:
    return sum(spot_price(datetime(year, 1, 1) + timedelta(hours=h)) for h in range(365 * 24)) / (365 * 24)


def average_price(contract: SupplyContract | None, fluid: Fluid, year: int | None = None) -> float:
    """Prix moyen d'un contrat, faute de courbe : 2/3 en heures pleines, 1/3 en heures creuses ; Tempo pondéré
    par le nombre de jours de chaque couleur ; marché : moyenne annuelle des prix horaires."""
    if contract is None:
        return estimated_price(fluid)
    option = contract.option
    if option == TariffOption.BASE:
        return contract.price_base or 0.0
    if option == TariffOption.HPHC:
        return HP_SHARE * (contract.price_hp or 0.0) + (1 - HP_SHARE) * (contract.price_hc or 0.0)
    if option == TariffOption.TEMPO:
        weights = {"ROUGE": TEMPO_RED_DAYS, "BLANC": TEMPO_WHITE_DAYS,
                   "BLEU": 365 - TEMPO_RED_DAYS - TEMPO_WHITE_DAYS}
        return sum(n * (HP_SHARE * contract.tempo_prices[c]["HP"] + (1 - HP_SHARE) * contract.tempo_prices[c]["HC"])
                   for c, n in weights.items()) / 365
    return _mean_spot(year or date.today().year) + (contract.dynamic_margin_eur_kwh or 0.0)


def contracts_of(db: Session, delivery_point_id: int) -> list[SupplyContract]:
    return list(db.scalars(select(SupplyContract).where(SupplyContract.delivery_point_id == delivery_point_id)
                           .order_by(SupplyContract.valid_from, SupplyContract.id)))


def contract_at(contracts: list[SupplyContract], day: date) -> SupplyContract | None:
    """Contrat en vigueur un jour donné : le plus récent dont la date d'effet le précède."""
    current = None
    for contract in contracts:
        if contract.valid_from <= day:
            current = contract
    return current


def active_contract(db: Session, delivery_point_id: int, day: date) -> SupplyContract | None:
    return contract_at(contracts_of(db, delivery_point_id), day)


# --- Chiffrage --------------------------------------------------------------------------------------------


@dataclass
class Costing:
    kwh: float = 0.0
    n0_kwh: float = 0.0
    energy_eur: float = 0.0
    subscription_eur: float = 0.0
    by_period: dict[str, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0.0, 0.0]))
    contract_days: int = 0
    days: int = 0

    @property
    def eur(self) -> float:
        return self.energy_eur + self.subscription_eur

    def add(self, period: str, kwh: float, eur: float) -> None:
        self.kwh += kwh
        self.energy_eur += eur
        self.by_period[period][0] += kwh
        self.by_period[period][1] += eur

    def merge(self, other: Costing) -> None:
        self.kwh += other.kwh
        self.n0_kwh += other.n0_kwh
        self.energy_eur += other.energy_eur
        self.subscription_eur += other.subscription_eur
        self.contract_days += other.contract_days
        self.days += other.days
        for period, (kwh, eur) in other.by_period.items():
            self.by_period[period][0] += kwh
            self.by_period[period][1] += eur


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def monthly_costing(db: Session, dp: DeliveryPoint, start: date, end: date) -> dict[str, Costing]:
    """Consommation et coût d'un point, mois par mois : mesures N1 au prix de chaque pas, factures N0 au prix
    moyen, abonnement au prorata des jours sous contrat.

    Mémorisé (`memo`) : refait seulement si les mesures, les factures, les contrats ou le consentement changent."""
    contracts = contracts_of(db, dp.id)
    consented = has_active_consent(db, dp.id)
    key = (dp.id, dp.fluid, start, end, consented,
           memo.measurement_stamp(db, dp.id, start, end) if consented else None,
           memo.declared_stamp(db, dp.id), memo.contracts_stamp(contracts))
    return memo.cached("costing", key, lambda: _monthly_costing(db, dp, start, end, contracts, consented))


def _monthly_costing(db: Session, dp: DeliveryPoint, start: date, end: date, contracts: list[SupplyContract],
                     consented: bool) -> dict[str, Costing]:
    months: dict[str, Costing] = defaultdict(Costing)
    covered: set[date] = set()
    if consented:
        t0, t1 = local_day_bounds(start, end)
        rows = db.execute(select(Measurement.time, Measurement.value_kwh).where(
            Measurement.delivery_point_id == dp.id, Measurement.time >= t0, Measurement.time < t1))
        # Le contrat et le mois ne dépendent que du jour, le prix que du jour et de l'heure : calculés une fois par
        # jour ou par heure, pas à chaque pas de 30 min (douze mois de courbe = 17 500 pas par point).
        by_day: dict[date, tuple[Costing, SupplyContract | None]] = {}
        by_hour: dict[tuple[date, int], tuple[float, str]] = {}
        indicative = (estimated_price(dp.fluid), INDICATIVE)
        for time, kwh in rows:
            local = to_local(time)
            day = local.date()
            entry = by_day.get(day)
            if entry is None:
                covered.add(day)
                entry = by_day[day] = (months[month_key(day)], contract_at(contracts, day))
            costing, contract = entry
            if contract is None:
                price, period = indicative
            else:
                slot = by_hour.get((day, local.hour))
                if slot is None:
                    slot = by_hour[(day, local.hour)] = slot_price(contract, local)
                price, period = slot
            costing.add(period, kwh, kwh * price)
    for day, kwh in declared_daily(db, dp, start, end, covered).items():
        contract = contract_at(contracts, day)
        costing = months[month_key(day)]
        costing.add(INVOICES if contract else INDICATIVE, kwh, kwh * average_price(contract, dp.fluid, day.year))
        costing.n0_kwh += kwh
    for day in daterange(start, end):
        costing = months[month_key(day)]
        costing.days += 1
        contract = contract_at(contracts, day)
        if contract is not None:
            costing.contract_days += 1
            costing.subscription_eur += contract.subscription_eur_month * 12 / 365
    return {month: costing for month, costing in months.items() if costing.kwh or costing.subscription_eur}


@dataclass
class MonthRow:
    month: str
    site_id: int
    site: str
    fluid: Fluid
    costing: Costing
    kgco2e: float

    @property
    def pricing(self) -> str:
        if self.costing.contract_days == 0:
            return "indicatif"
        return "contrat" if self.costing.contract_days == self.costing.days else "partiel"


def monthly_triaxis(db: Session, org: Organization, start: date, end: date) -> list[MonthRow]:
    """F6 : consommations, émissions et coûts par mois, site et énergie (N1 et N0)."""
    points = list(db.scalars(select(DeliveryPoint).join(Site, DeliveryPoint.site_id == Site.id)
                             .where(Site.organization_id == org.id).order_by(DeliveryPoint.id)))
    last = data_as_of(db, [dp.id for dp in points])
    end = min(end, last) if last else end
    grouped: dict[tuple[str, int, Fluid], Costing] = defaultdict(Costing)
    sites = {}
    for dp in points:
        sites[dp.site_id] = dp.site.name
        for month, costing in monthly_costing(db, dp, start, end).items():
            grouped[(month, dp.site_id, dp.fluid)].merge(costing)
    rows = []
    factors_by_month: dict[str, dict] = {}
    for (month, site_id, fluid), costing in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1],
                                                                                   item[0][2].value)):
        if month not in factors_by_month:
            year, number = map(int, month.split("-"))
            factors_by_month[month] = emission_factors_at(db, date(year, number, 1))
        factor = factors_by_month[month].get(fluid)
        rows.append(MonthRow(month, site_id, sites[site_id], fluid, costing,
                             costing.kwh * factor.factor_kgco2_per_kwh if factor else 0.0))
    return rows


def effective_price(db: Session, points: list[DeliveryPoint], start: date, end: date, fluid: Fluid) -> tuple[float, str]:
    """Prix moyen réellement payé (énergie, hors abonnement) sur une période, et sa source."""
    total = Costing()
    for dp in points:
        for costing in monthly_costing(db, dp, start, end).values():
            total.merge(costing)
    if total.kwh <= 0:
        return estimated_price(fluid), "prix indicatif"
    source = "contrat de fourniture" if total.contract_days else "prix indicatif"
    return total.energy_eur / total.kwh, source


# --- Saisie des contrats (auditeur) --------------------------------------------------------------------


def can_edit(user: User) -> bool:
    return user.role in (Role.AUDITOR, Role.ADMIN)


def save_contract(
    db: Session, repo: TenantRepository, user: User, delivery_point_id: int, *, supplier: str,
    option: TariffOption, valid_from: date, subscription_eur_month: float = 0.0, price_base: float | None = None,
    price_hp: float | None = None, price_hc: float | None = None, offpeak_start_hour: int = 22,
    offpeak_end_hour: int = 6, tempo_prices: dict | None = None, dynamic_margin_eur_kwh: float | None = None,
) -> SupplyContract:
    """Enregistre un contrat (ou une nouvelle grille à partir de `valid_from`) ; les précédents sont conservés."""
    if not can_edit(user):
        raise ContractPermissionError("Les contrats de fourniture sont saisis par l'auditeur.")
    dp = repo.get_delivery_point(delivery_point_id)
    supplier = (supplier or "").strip()
    if not supplier or len(supplier) > 120:
        raise ContractError("Nom du fournisseur obligatoire (120 caractères au plus).")
    if subscription_eur_month is None or subscription_eur_month < 0:
        raise ContractError("L'abonnement mensuel doit être positif ou nul.")
    if dp.fluid == Fluid.GAS and option != TariffOption.BASE:
        raise ContractError("Un contrat de gaz se chiffre au prix unique (option Base).")

    def price(value: float | None, label: str) -> float:
        if value is None or value < 0 or value > 5:
            raise ContractError(f"Prix « {label} » obligatoire, en €/kWh (entre 0 et 5).")
        return float(value)

    fields = {}
    if option == TariffOption.BASE:
        fields["price_base"] = price(price_base, "base")
    elif option == TariffOption.HPHC:
        fields["price_hp"], fields["price_hc"] = price(price_hp, "heures pleines"), price(price_hc, "heures creuses")
    elif option == TariffOption.TEMPO:
        tempo_prices = tempo_prices or {}
        fields["tempo_prices"] = {color: {period: price((tempo_prices.get(color) or {}).get(period),
                                                         f"{color.lower()} {period}") for period in ("HP", "HC")}
                                  for color in TEMPO_COLORS}
    else:
        margin = dynamic_margin_eur_kwh
        if margin is None or margin < 0 or margin > 1:
            raise ContractError("Marge du fournisseur obligatoire, en €/kWh (entre 0 et 1), ajoutée au prix horaire.")
        fields["dynamic_margin_eur_kwh"] = float(margin)
    if option in (TariffOption.HPHC, TariffOption.TEMPO):
        if not (0 <= offpeak_start_hour <= 23 and 0 <= offpeak_end_hour <= 23) or offpeak_start_hour == offpeak_end_hour:
            raise ContractError("Plage d'heures creuses impossible : heures de début et de fin entre 0 et 23, distinctes.")
    contract = SupplyContract(
        organization_id=dp.site.organization_id, delivery_point_id=dp.id, supplier=supplier, option=option,
        subscription_eur_month=float(subscription_eur_month), offpeak_start_hour=offpeak_start_hour,
        offpeak_end_hour=offpeak_end_hour, valid_from=valid_from, created_by=user.id, **fields,
    )
    db.add(contract)
    db.commit()
    return contract


def describe(contract: SupplyContract) -> str:
    """Grille tarifaire en une ligne, pour l'interface et les raisonnements."""
    def eur(value: float) -> str:
        return f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",") + " €/kWh"

    hours = f"heures creuses {contract.offpeak_start_hour} h – {contract.offpeak_end_hour} h"
    if contract.option == TariffOption.BASE:
        grid = eur(contract.price_base or 0.0)
    elif contract.option == TariffOption.HPHC:
        grid = f"HP {eur(contract.price_hp or 0.0)}, HC {eur(contract.price_hc or 0.0)} ({hours})"
    elif contract.option == TariffOption.TEMPO:
        grid = ", ".join(f"{c.lower()} {eur(contract.tempo_prices[c]['HP'])} / {eur(contract.tempo_prices[c]['HC'])}"
                         for c in TEMPO_COLORS) + f" (HP / HC, {hours})"
    else:
        grid = f"prix horaire du marché + {eur(contract.dynamic_margin_eur_kwh or 0.0)}"
    subscription = f"{contract.subscription_eur_month:.2f}".replace(".", ",")
    return (f"{contract.supplier}, {OPTION_LABELS[contract.option].lower()} : {grid} ; abonnement "
            f"{subscription} €/mois, depuis le {contract.valid_from:%d/%m/%Y}")
