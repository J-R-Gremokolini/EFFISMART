"""Sélection du fournisseur de données d'un point de livraison (par configuration).

- MOCK : données simulées (démonstration) ;
- ENEDIS_DATACONNECT / GRDF_ADICT : API réelles, actives seulement si l'administrateur a activé
  l'intégration et saisi les identifiants (page « Intégrations ») ;
- GENERIC_API : connecteur REST rattaché au point (`DeliveryPoint.connector_id`).
"""
from __future__ import annotations

from collections.abc import Callable

from app.models import DeliveryPoint, Fluid, ProviderKind
from app.providers.base import EnergyDataProvider
from app.providers.enedis import EnedisSgeProvider
from app.providers.mock import MockDataProvider

ProviderFactory = Callable[[ProviderKind, Fluid], EnergyDataProvider]


def _default_factory(provider: ProviderKind, fluid: Fluid, delivery_point: DeliveryPoint | None) -> EnergyDataProvider:
    if provider == ProviderKind.MOCK:
        return MockDataProvider(fluid=fluid)
    if provider == ProviderKind.ENEDIS_SGE:
        return EnedisSgeProvider()
    # Sources réelles : identifiants lus (et déchiffrés) dans les intégrations.
    from app.services import integrations  # import local : évite un cycle providers ↔ services

    return integrations.build_energy_provider(provider, delivery_point)


_override: ProviderFactory | None = None


def get_energy_provider(
    provider: ProviderKind, fluid: Fluid, delivery_point: DeliveryPoint | None = None
) -> EnergyDataProvider:
    if _override is not None:
        return _override(provider, fluid)
    return _default_factory(provider, fluid, delivery_point)


def set_provider_factory(factory: ProviderFactory | None) -> None:
    """Permet aux tests d'injecter un fournisseur (ex. mock avec anomalies spécifiques)."""
    global _override
    _override = factory
