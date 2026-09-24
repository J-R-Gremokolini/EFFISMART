"""Fournisseur GRDF ADICT — à brancher une fois les accès obtenus (jalon 8).

Consommations quotidiennes (compteurs Gazpar) jusqu'à 36 mois d'historique,
après consentement du client depuis son espace GRDF.
"""
from __future__ import annotations

from datetime import date

from app.providers.base import ConsentStatus, ContractInfo, ProviderMeasurement, ProviderNotConfiguredError


class GrdfAdictProvider:
    def fetch_load_curve(self, delivery_point_id: str, start: date, end: date) -> list[ProviderMeasurement]:
        # TODO(jalon 8) : GET /adict/v2/pce/{pce}/donnees_consos_informatives.
        raise ProviderNotConfiguredError("Accès GRDF ADICT non encore configuré")

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        raise ProviderNotConfiguredError("Accès GRDF ADICT non encore configuré")

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        # TODO(jalon 8) : GET /adict/v2/droits_acces.
        raise ProviderNotConfiguredError("Accès GRDF ADICT non encore configuré")
