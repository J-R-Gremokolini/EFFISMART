"""Sélection du fournisseur de données d'un point de livraison (par configuration)."""
from __future__ import annotations

from collections.abc import Callable

from app.models import Fluid, ProviderKind
from app.providers.base import EnergyDataProvider
from app.providers.enedis import EnedisDataConnectProvider, EnedisSgeProvider
from app.providers.grdf import GrdfAdictProvider
from app.providers.mock import MockDataProvider

ProviderFactory = Callable[[ProviderKind, Fluid], EnergyDataProvider]


def _default_factory(provider: ProviderKind, fluid: Fluid) -> EnergyDataProvider:
    if provider == ProviderKind.MOCK:
        return MockDataProvider(fluid=fluid)
    if provider == ProviderKind.ENEDIS_DATACONNECT:
        return EnedisDataConnectProvider()
    if provider == ProviderKind.ENEDIS_SGE:
        return EnedisSgeProvider()
    if provider == ProviderKind.GRDF_ADICT:
        return GrdfAdictProvider()
    raise ValueError(f"Fournisseur inconnu : {provider}")


_factory: ProviderFactory = _default_factory


def get_energy_provider(provider: ProviderKind, fluid: Fluid) -> EnergyDataProvider:
    return _factory(provider, fluid)


def set_provider_factory(factory: ProviderFactory | None) -> None:
    """Permet aux tests d'injecter un fournisseur (ex. mock avec anomalies spécifiques)."""
    global _factory
    _factory = factory or _default_factory
