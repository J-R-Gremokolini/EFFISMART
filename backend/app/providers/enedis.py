"""Fournisseurs Enedis — à brancher une fois les accès obtenus (jalon 8).

- DataConnect : API grand public, consentement donné par le client depuis
  son espace Enedis (OAuth2), courbe de charge 30 min sur 24 mois.
- SGE Tiers : accès B2B après référencement (plusieurs mois de délai).

Ces classes implémentent `EnergyDataProvider` ; les activer consiste à
changer `DeliveryPoint.provider`, aucun autre code ne change.
"""
from __future__ import annotations

from datetime import date

from app.providers.base import ConsentStatus, ContractInfo, ProviderMeasurement, ProviderNotConfiguredError


class EnedisDataConnectProvider:
    def fetch_load_curve(self, delivery_point_id: str, start: date, end: date) -> list[ProviderMeasurement]:
        # TODO(jalon 8) : GET /metering_data_clc/v5/consumption_load_curve (pas 30 min, fenêtres ≤ 7 jours).
        raise ProviderNotConfiguredError("Accès Enedis DataConnect non encore configuré")

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        # TODO(jalon 8) : GET /customers_upc/v5/usage_points/contracts.
        raise ProviderNotConfiguredError("Accès Enedis DataConnect non encore configuré")

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        raise ProviderNotConfiguredError("Accès Enedis DataConnect non encore configuré")


class EnedisSgeProvider:
    def fetch_load_curve(self, delivery_point_id: str, start: date, end: date) -> list[ProviderMeasurement]:
        raise ProviderNotConfiguredError("Référencement Enedis SGE-Tiers en cours")

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        raise ProviderNotConfiguredError("Référencement Enedis SGE-Tiers en cours")

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        raise ProviderNotConfiguredError("Référencement Enedis SGE-Tiers en cours")
