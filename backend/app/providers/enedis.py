"""Fournisseurs Enedis.

- `EnedisDataConnectProvider` : API Data Connect (consentement donné par le client depuis son espace
  Enedis). Authentification OAuth2 « client credentials », courbe de charge par fenêtres de 7 jours.
- `EnedisSgeProvider` : accès B2B SGE-Tiers, après référencement (plusieurs mois) — non raccordé en V1.

Formats implémentés d'après la documentation publique Data Connect (v5) ; à revalider sur le bac à
sable Enedis au moment du raccordement (identifiants fournis par Enedis après signature du contrat).
"""
from __future__ import annotations

import re
import time as _time
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx

from app.models import MeasurementStep
from app.providers.base import (
    ConsentStatus,
    ContractInfo,
    ProviderApiError,
    ProviderMeasurement,
    ProviderNotConfiguredError,
)
from app.services import net
from app.timeutils import LOCAL_TZ, UTC, yesterday_local

ENEDIS_URLS = {
    "sandbox": "https://gw.ext.prod-sandbox.api.enedis.fr",
    "production": "https://gw.ext.prod.api.enedis.fr",
}
MAX_DAYS_PER_CALL = 7  # limite de l'API pour la courbe de charge


def _slot_start(dt: datetime) -> datetime:
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def _interval_minutes(value: str | None) -> int:
    match = re.fullmatch(r"PT(\d+)M", value or "PT30M")
    return int(match.group(1)) if match else 30


class EnedisDataConnectProvider:
    def __init__(self, client_id: str, client_secret: str, environment: str = "sandbox") -> None:
        if not client_id or not client_secret:
            raise ProviderNotConfiguredError("Identifiants Enedis Data Connect manquants")
        self.client_id, self.client_secret = client_id, client_secret
        self.base_url = ENEDIS_URLS.get(environment, ENEDIS_URLS["sandbox"])
        self._token: str | None = None
        self._token_expiry = 0.0

    # --- Authentification ---------------------------------------------------------

    def token(self) -> str:
        if self._token and _time.monotonic() < self._token_expiry - 30:
            return self._token
        with net.client() as http:
            response = http.post(
                f"{self.base_url}/oauth2/v3/token",
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.client_secret},
            )
        if response.status_code != 200:
            raise ProviderApiError(f"Enedis a refusé l'authentification (code {response.status_code}).")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expiry = _time.monotonic() + float(payload.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: dict) -> httpx.Response:
        with net.client() as http:
            return http.get(f"{self.base_url}{path}", params=params,
                            headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"})

    # --- Contrat EnergyDataProvider ------------------------------------------------

    def fetch_load_curve(self, delivery_point_id: str, start: date, end: date) -> list[ProviderMeasurement]:
        end = min(end, yesterday_local())
        energy: dict[datetime, float] = defaultdict(float)  # début de créneau 30 min (UTC) → kWh
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=MAX_DAYS_PER_CALL - 1), end)
            response = self._get(
                "/metering_data_clc/v5/consumption_load_curve",
                {"usage_point_id": delivery_point_id, "start": chunk_start.isoformat(),
                 "end": (chunk_end + timedelta(days=1)).isoformat()},  # borne de fin exclue par l'API
            )
            if response.status_code == 403:
                raise ProviderApiError("Enedis refuse l'accès à ce point : consentement absent ou expiré.")
            if response.status_code != 200:
                raise ProviderApiError(f"Courbe de charge Enedis indisponible (code {response.status_code}).")
            for reading in response.json().get("meter_reading", {}).get("interval_reading", []):
                minutes = _interval_minutes(reading.get("interval_length"))
                # Horodatage Enedis = FIN de l'intervalle, en heure locale ; valeur = puissance moyenne en W.
                local_end = datetime.strptime(reading["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
                start_utc = (local_end - timedelta(minutes=minutes)).astimezone(UTC)
                energy[_slot_start(start_utc)] += float(reading["value"]) / 1000 * minutes / 60
            chunk_start = chunk_end + timedelta(days=1)
        return [
            ProviderMeasurement(time=slot, value_kwh=round(kwh, 4), avg_power_kw=round(kwh * 2, 4),
                                step=MeasurementStep.PT30M)
            for slot, kwh in sorted(energy.items())
        ]

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        response = self._get("/customers_upc/v5/usage_points/contracts", {"usage_point_id": delivery_point_id})
        if response.status_code != 200:
            raise ProviderApiError(f"Contrat Enedis indisponible (code {response.status_code}).")
        point = (response.json().get("customer", {}).get("usage_points") or [{}])[0]
        contract = point.get("contracts", {})
        power = re.search(r"[\d.,]+", str(contract.get("subscribed_power", "")))
        return ContractInfo(
            subscribed_power_kva=float(power.group().replace(",", ".")) if power else None,
            tariff_option=str(contract.get("distribution_tariff") or contract.get("offpeak_hours") or "inconnue"),
            meter_type=str(point.get("usage_point", {}).get("meter_type") or "LINKY"),
        )

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        response = self._get("/customers_upc/v5/usage_points/contracts", {"usage_point_id": delivery_point_id})
        return ConsentStatus(granted=response.status_code == 200, source="ENEDIS_DATACONNECT")


class EnedisSgeProvider:
    """Accès B2B SGE-Tiers (services web SOAP) : raccordé après le référencement Enedis."""

    def fetch_load_curve(self, delivery_point_id: str, start: date, end: date) -> list[ProviderMeasurement]:
        raise ProviderNotConfiguredError("Référencement Enedis SGE-Tiers en cours")

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        raise ProviderNotConfiguredError("Référencement Enedis SGE-Tiers en cours")

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        raise ProviderNotConfiguredError("Référencement Enedis SGE-Tiers en cours")
