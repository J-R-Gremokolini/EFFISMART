"""Fournisseur GRDF ADICT (compteurs Gazpar) : consommations quotidiennes, jusqu'à 36 mois d'historique.

Authentification OAuth2 « client credentials » ; les consommations sont renvoyées une ligne JSON par
jour (NDJSON). Formats implémentés d'après la documentation publique ADICT v2 ; à revalider sur le
bac à sable GRDF au moment du raccordement (identifiants fournis après signature du contrat).
"""
from __future__ import annotations

import json
import time as _time
from datetime import date, datetime, time, timedelta

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

GRDF_TOKEN_URL = "https://sofit-sso-oidc.grdf.fr/openam/oauth2/realms/externeGrdf/access_token"
GRDF_API_URL = "https://api.grdf.fr/adict/v2"


def parse_ndjson(text: str) -> list[dict]:
    """Accepte une ligne JSON par enregistrement (format ADICT) ou un tableau JSON."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class GrdfAdictProvider:
    def __init__(self, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            raise ProviderNotConfiguredError("Identifiants GRDF ADICT manquants")
        self.client_id, self.client_secret = client_id, client_secret
        self._token: str | None = None
        self._token_expiry = 0.0

    def token(self) -> str:
        if self._token and _time.monotonic() < self._token_expiry - 30:
            return self._token
        with net.client() as http:
            response = http.post(GRDF_TOKEN_URL, data={
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret, "scope": "/adict/v2",
            })
        if response.status_code != 200:
            raise ProviderApiError(f"GRDF a refusé l'authentification (code {response.status_code}).")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expiry = _time.monotonic() + float(payload.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        with net.client() as http:
            return http.get(f"{GRDF_API_URL}{path}", params=params,
                            headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/x-ndjson"})

    def fetch_load_curve(self, delivery_point_id: str, start: date, end: date) -> list[ProviderMeasurement]:
        end = min(end, yesterday_local())
        if end < start:
            return []
        response = self._get(f"/pce/{delivery_point_id}/donnees_consos_informatives",
                             {"date_debut": start.isoformat(), "date_fin": end.isoformat()})
        if response.status_code == 403:
            raise ProviderApiError("GRDF refuse l'accès à ce point : consentement absent ou expiré.")
        if response.status_code != 200:
            raise ProviderApiError(f"Consommations GRDF indisponibles (code {response.status_code}).")
        measurements = []
        for record in parse_ndjson(response.text):
            conso = record.get("consommation") or {}
            if conso.get("energie") is None or not conso.get("date_debut_consommation"):
                continue  # jour sans relevé exploitable
            day = datetime.strptime(conso["date_debut_consommation"][:10], "%Y-%m-%d").date()
            if not start <= day <= end:
                continue
            measurements.append(ProviderMeasurement(
                time=datetime.combine(day, time.min, tzinfo=LOCAL_TZ).astimezone(UTC),
                value_kwh=round(float(conso["energie"]), 3), avg_power_kw=None, step=MeasurementStep.P1D,
            ))
        return sorted(measurements, key=lambda m: m.time)

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        response = self._get(f"/pce/{delivery_point_id}/donnees_contractuelles")
        records = parse_ndjson(response.text) if response.status_code == 200 else []
        tariff = (records[0].get("donnees_contractuelles") or {}).get("tarif_acheminement") if records else None
        return ContractInfo(subscribed_power_kva=None, tariff_option=str(tariff or "inconnue"), meter_type="GAZPAR")

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        yesterday = yesterday_local()
        response = self._get(f"/pce/{delivery_point_id}/donnees_consos_informatives",
                             {"date_debut": (yesterday - timedelta(days=1)).isoformat(),
                              "date_fin": yesterday.isoformat()})
        return ConsentStatus(granted=response.status_code == 200, source="GRDF_ADICT")
