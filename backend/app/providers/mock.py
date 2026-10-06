"""MockDataProvider : courbes de charge plausibles, déterministes et paramétrables.

Caractéristiques des données générées (brief §3.2) :
- profil jour/nuit : occupation 8h–18h30 en semaine, rampes 7h–8h et 18h30–20h ;
- creux du week-end : aucune occupation samedi et dimanche ;
- saisonnalité : besoin de chauffage selon la température (cohérente avec `MockWeatherProvider`) ;
- talon de nuit : consommation de base permanente ;
- anomalies injectables (`AnomalySpec`) pour démontrer et tester F2a.

Les points de démonstration ont en plus une physique de bâtiment réaliste (`DEMO_BEHAVIOURS`) :
inertie thermique, apports solaires (température sol-air), température d'équilibre propre au bâtiment,
besoin de chauffage non linéaire, climatisation et rythme hebdomadaire (fermeture du lundi, vendredi
en télétravail…). Les autres points gardent le comportement standard, identique à la version
précédente (tests reproductibles).

Tout est déterministe : même point, même jour ⇒ mêmes valeurs, quel que soit
le processus. Les caractéristiques d'un point sont dérivées de sa référence.
"""
from __future__ import annotations

import enum
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.config import settings
from app.models import Fluid, MeasurementStep
from app.providers.base import ConsentStatus, ContractInfo, ProviderMeasurement
from app.providers.weather import MockWeatherProvider, sol_air
from app.timeutils import LOCAL_TZ, UTC, daterange, yesterday_local


class AnomalyKind(str, enum.Enum):
    WEEKEND_ON = "WEEKEND_ON"  # équipement resté allumé le week-end → talon anormal
    POWER_SPIKE = "POWER_SPIKE"  # pic de puissance l'après-midi → dépassement de seuil
    OVERCONSUMPTION = "OVERCONSUMPTION"  # surconsommation en occupation → écart climatique


@dataclass(frozen=True)
class AnomalySpec:
    """Anomalie injectée dans les données mock.

    `intensity` : fraction de la puissance de pointe du profil (élec.) ou
    surconsommation relative (gaz, ex. 0.4 = +40 %).
    """

    external_ref: str
    kind: AnomalyKind
    applies_on: Callable[[date], bool]
    intensity: float
    label: str = ""


def between(start: date, end: date) -> Callable[[date], bool]:
    return lambda day: start <= day <= end


def iso_week_multiple_of(n: int) -> Callable[[date], bool]:
    return lambda day: day.isocalendar()[1] % n == 0


def ordinal_multiple_of(n: int) -> Callable[[date], bool]:
    return lambda day: day.toordinal() % n == 0


# Anomalies récurrentes des points de démonstration (cf. app/seed.py).
# Règles calendaires stables : identiques au seed et aux ingestions quotidiennes.
DEFAULT_ANOMALIES: list[AnomalySpec] = [
    AnomalySpec("30001000000003", AnomalyKind.WEEKEND_ON, iso_week_multiple_of(3), 0.35,
                "Groupe froid resté allumé le week-end"),
    AnomalySpec("30001000000005", AnomalyKind.POWER_SPIKE, ordinal_multiple_of(11), 0.8,
                "Pic de puissance l'après-midi"),
    AnomalySpec("30001000000006", AnomalyKind.OVERCONSUMPTION, iso_week_multiple_of(7), 0.45,
                "Consigne de chauffage/climatisation déréglée"),
    AnomalySpec("21000000000008", AnomalyKind.OVERCONSUMPTION, iso_week_multiple_of(8), 0.4,
                "Chaudière mal réglée"),
]


@dataclass(frozen=True)
class ElecProfile:
    peak_kw: float
    base_ratio: float  # talon permanent, en fraction de la pointe
    heating_share: float  # part de chauffage électrique à 15 DJU


@dataclass(frozen=True)
class GasProfile:
    base_kwh_per_day: float  # eau chaude sanitaire, cuisson, process
    kwh_per_dju: float


def elec_profile(external_ref: str) -> ElecProfile:
    rng = random.Random(f"profile:{external_ref}")
    return ElecProfile(
        peak_kw=float(round(rng.uniform(40, 220))),
        base_ratio=rng.uniform(0.15, 0.25),
        heating_share=rng.uniform(0.10, 0.30),
    )


def gas_profile(external_ref: str) -> GasProfile:
    rng = random.Random(f"profile:{external_ref}")
    return GasProfile(base_kwh_per_day=rng.uniform(80, 400), kwh_per_dju=rng.uniform(40, 160))


@dataclass(frozen=True)
class BuildingBehaviour:
    """Physique simplifiée d'un bâtiment.

    - inertie : la température « ressentie » est une moyenne des derniers jours, pondérée par
      exp(−24 k / τ), τ étant la constante de temps du bâtiment en heures (0 = aucune inertie) ;
    - apports solaires : température sol-air, modulée par l'exposition (`solar_factor`) ;
    - chauffage sous la température d'équilibre (apports internes), avec une part quadratique ;
    - climatisation au-delà d'un seuil, en fraction de la pointe par degré ;
    - rythme hebdomadaire : coefficient d'activité par jour (lundi → dimanche).
    """

    time_constant_h: float = 0.0
    solar_factor: float = 0.0
    balance_temperature: float | None = None  # None : base des DJU (18 °C)
    curvature: float = 0.0
    cooling_threshold: float | None = None
    cooling_ratio: float = 0.0
    weekly: tuple[float, ...] = (1, 1, 1, 1, 1, 1, 1)  # électricité (le week-end reste inoccupé)
    gas_weekly: tuple[float, ...] = (1, 1, 1, 1, 1, 0.6, 0.6)

    @property
    def weights(self) -> tuple[float, ...]:
        if self.time_constant_h <= 0:
            return (1.0,)
        return tuple(math.exp(-24 * k / self.time_constant_h) for k in range(4))


STANDARD_BEHAVIOUR = BuildingBehaviour()
# Points de démonstration (cf. app/seed.py) : de quoi montrer ce que le moteur de prévision sait détecter.
DEMO_BEHAVIOURS: dict[str, BuildingBehaviour] = {
    # Boulangeries Martin — atelier : fours (apports internes), production gaz le samedi, fermé le dimanche.
    "30001000000001": BuildingBehaviour(time_constant_h=30, solar_factor=0.3, balance_temperature=15),
    "21000000000007": BuildingBehaviour(time_constant_h=30, solar_factor=0.3, balance_temperature=15,
                                        gas_weekly=(1, 1, 1, 1, 1, 0.85, 0.2)),
    # Boutique : vitrine plein sud, construction légère, fermée le lundi.
    "30001000000002": BuildingBehaviour(solar_factor=1.0, balance_temperature=17,
                                        weekly=(0.1, 1, 1, 1, 1, 1, 1)),
    # Clinique : béton lourd (forte inertie), groupe froid, activité 7 j/7.
    "30001000000003": BuildingBehaviour(time_constant_h=70, solar_factor=0.5, balance_temperature=17,
                                        curvature=0.02, cooling_threshold=21, cooling_ratio=0.02),
    "21000000000008": BuildingBehaviour(time_constant_h=70, solar_factor=0.5, balance_temperature=17,
                                        curvature=0.03, gas_weekly=(1, 1, 1, 1, 1, 0.9, 0.9)),
    # Entrepôt : bardage métallique (peu d'inertie), chauffé hors gel à 12 °C, réassort du lundi.
    "30001000000004": BuildingBehaviour(time_constant_h=12, solar_factor=0.2, balance_temperature=12,
                                        weekly=(1.15, 1, 1, 1, 1, 1, 1)),
    "30001000000005": BuildingBehaviour(time_constant_h=12, solar_factor=0.2, balance_temperature=12,
                                        weekly=(1.15, 1, 1, 1, 1, 1, 1)),
    # Bureaux : façades vitrées, pompe à chaleur réversible, vendredi en télétravail.
    "30001000000006": BuildingBehaviour(time_constant_h=40, solar_factor=0.9, balance_temperature=16,
                                        cooling_threshold=22, cooling_ratio=0.03,
                                        weekly=(1, 1, 1, 1, 0.7, 1, 1)),
}


def behaviour(external_ref: str) -> BuildingBehaviour:
    return DEMO_BEHAVIOURS.get(external_ref, STANDARD_BEHAVIOUR)


def occupancy(local_dt: datetime) -> float:
    """Taux d'occupation théorique (0 à 1) ; nul la nuit et le week-end."""
    if local_dt.weekday() >= 5:
        return 0.0
    hour = local_dt.hour + local_dt.minute / 60
    if 8 <= hour < 18.5:
        return 1.0
    if 7 <= hour < 8 or 18.5 <= hour < 20:
        return 0.5
    return 0.0


@dataclass
class MockDataProvider:
    fluid: Fluid = Fluid.ELEC
    anomalies: list[AnomalySpec] | None = None
    weather: MockWeatherProvider = field(default_factory=MockWeatherProvider)
    # Dernier jour disponible : la veille par défaut (réalité J+1 des gestionnaires de réseau).
    available_until: date | None = None

    def __post_init__(self) -> None:
        if self.anomalies is None:
            self.anomalies = list(DEFAULT_ANOMALIES) if settings.mock_anomalies_enabled else []

    # --- Contrat EnergyDataProvider ---------------------------------------

    def fetch_load_curve(
        self, delivery_point_id: str, start: date, end: date
    ) -> list[ProviderMeasurement]:
        end = min(end, self.available_until or yesterday_local())
        generate = self._elec_day if self.fluid == Fluid.ELEC else self._gas_day
        measurements: list[ProviderMeasurement] = []
        for day in daterange(start, end):
            measurements.extend(generate(delivery_point_id, day))
        return measurements

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        if self.fluid == Fluid.GAS:
            return ContractInfo(subscribed_power_kva=None, tariff_option="T2", meter_type="GAZPAR")
        profile = elec_profile(delivery_point_id)
        subscribed = math.ceil(profile.peak_kw * 1.6 / 6) * 6
        return ContractInfo(subscribed_power_kva=float(subscribed), tariff_option="HP/HC", meter_type="LINKY")

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        # Côté mock, le « geste » Enedis/GRDF est réputé effectué ; la preuve
        # applicative reste l'enregistrement `Consent` en base.
        return ConsentStatus(granted=True, source="MOCK")

    # --- Génération -------------------------------------------------------

    def _anomalies_for(self, ref: str, day: date) -> list[AnomalySpec]:
        return [a for a in self.anomalies or [] if a.external_ref == ref and a.applies_on(day)]

    def _degrees(self, ref: str, day: date) -> tuple[float, float]:
        """(degrés de chauffage, degrés de refroidissement) ressentis par le bâtiment ce jour-là.

        Comportement standard : max(0, 18 − T) et 0, soit exactement les DJU du jour.
        """
        b = behaviour(ref)
        weights = b.weights
        felt = []
        for k in range(len(weights)):
            past = day - timedelta(days=k)
            temperature = self.weather.mean_temperature(past)
            if b.solar_factor:
                temperature = sol_air(temperature, self.weather.solar(past), b.solar_factor)
            felt.append(temperature)
        effective = sum(w * t for w, t in zip(weights, felt)) / sum(weights)
        balance = self.weather.base_temperature if b.balance_temperature is None else b.balance_temperature
        heating = max(0.0, balance - effective)
        heating *= 1 + b.curvature * heating
        cooling = max(0.0, effective - b.cooling_threshold) if b.cooling_threshold is not None else 0.0
        return heating, cooling

    def _elec_day(self, ref: str, day: date) -> list[ProviderMeasurement]:
        profile = elec_profile(ref)
        b = behaviour(ref)
        rng = random.Random(f"elec:{ref}:{day.isoformat()}")
        dju, cooling = self._degrees(ref, day)
        anomalies = self._anomalies_for(ref, day)

        base_kw = profile.peak_kw * profile.base_ratio
        usage_kw = profile.peak_kw * (1 - profile.base_ratio) * 0.75
        heating_kw_per_dju = profile.peak_kw * profile.heating_share / 15
        cooling_kw_per_degree = profile.peak_kw * b.cooling_ratio
        activity = b.weekly[day.weekday()]

        # Itération en UTC : gère naturellement les jours de 46/50 pas (changement d'heure).
        slot = datetime.combine(day, time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
        measurements = []
        while slot < day_end:
            local = slot.astimezone(LOCAL_TZ)
            occ = occupancy(local) * activity
            kw = base_kw + usage_kw * occ + heating_kw_per_dju * dju * (0.5 + 0.5 * occ)
            if cooling:
                kw += cooling_kw_per_degree * cooling * (0.3 + 0.7 * occ)
            kw *= rng.uniform(0.95, 1.05)
            for anomaly in anomalies:
                kw += self._elec_anomaly_kw(anomaly, profile, local, occ)
            measurements.append(
                ProviderMeasurement(
                    time=slot,
                    value_kwh=round(kw * 0.5, 3),
                    avg_power_kw=round(kw, 3),
                    step=MeasurementStep.PT30M,
                )
            )
            slot += timedelta(minutes=30)
        return measurements

    @staticmethod
    def _elec_anomaly_kw(anomaly: AnomalySpec, profile: ElecProfile, local: datetime, occ: float) -> float:
        extra = profile.peak_kw * anomaly.intensity
        if anomaly.kind == AnomalyKind.WEEKEND_ON:
            return extra if local.weekday() >= 5 else 0.0
        if anomaly.kind == AnomalyKind.POWER_SPIKE:
            return extra if local.weekday() < 5 and 13 <= local.hour < 16 else 0.0
        if anomaly.kind == AnomalyKind.OVERCONSUMPTION:
            return extra * occ
        return 0.0

    def _gas_day(self, ref: str, day: date) -> list[ProviderMeasurement]:
        profile = gas_profile(ref)
        rng = random.Random(f"gas:{ref}:{day.isoformat()}")
        dju, _ = self._degrees(ref, day)
        activity = behaviour(ref).gas_weekly[day.weekday()]
        kwh = (profile.base_kwh_per_day * activity + profile.kwh_per_dju * dju * activity)
        kwh *= rng.uniform(0.93, 1.07)
        for anomaly in self._anomalies_for(ref, day):
            if anomaly.kind == AnomalyKind.OVERCONSUMPTION:
                kwh *= 1 + anomaly.intensity
        start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
        return [ProviderMeasurement(time=start, value_kwh=round(kwh, 3), avg_power_kw=None, step=MeasurementStep.P1D)]
