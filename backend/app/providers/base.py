"""Contrat d'accès aux données énergie.

Toute la donnée de consommation passe par `EnergyDataProvider`. Le reste de
l'application ne connaît que ce contrat : brancher Enedis ou GRDF revient à
fournir une nouvelle implémentation, sans toucher au reste du code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.models import MeasurementStep


@dataclass(frozen=True)
class ProviderMeasurement:
    """Une mesure renvoyée par un fournisseur (distincte de la table `measurements`)."""

    time: datetime  # début du pas de mesure, en UTC
    value_kwh: float
    avg_power_kw: float | None
    step: MeasurementStep


@dataclass(frozen=True)
class ContractInfo:
    subscribed_power_kva: float | None
    tariff_option: str
    meter_type: str


@dataclass(frozen=True)
class ConsentStatus:
    granted: bool
    expires_at: datetime | None = None
    source: str = ""


class EnergyDataProvider(Protocol):
    def fetch_load_curve(
        self, delivery_point_id: str, start: date, end: date
    ) -> list[ProviderMeasurement]:
        """Courbe de charge au pas 30 min (élec.) ou conso quotidienne (gaz), jours inclus."""
        ...

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        """Puissance souscrite, option tarifaire, type de compteur."""
        ...

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        ...


class ProviderNotConfiguredError(RuntimeError):
    """Levée par un fournisseur réel tant que les accès (contrat, référencement) ne sont pas obtenus."""


class ProviderApiError(RuntimeError):
    """Réponse inattendue d'une API de fournisseur ; message affichable (jamais de secret)."""
