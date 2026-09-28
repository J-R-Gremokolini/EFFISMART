"""Connecteur REST générique : toute API JSON devient une source de relevés (`EnergyDataProvider`).

Configuration (table `connectors`) :
- URL modèle avec `{ref}`, `{start}`, `{end}` (dates AAAA-MM-JJ, fin incluse) ;
- authentification : aucune, clé dans un en-tête, jeton « Bearer » ou identifiant / mot de passe ;
- chemin vers la liste des relevés dans la réponse (`data.readings`, vide = racine) ;
- champ de date (ISO 8601 ou horodatage Unix) et champ de valeur, avec leur unité.

L'URL finale est revérifiée avant chaque appel (protection SSRF, voir `app.services.net`).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from urllib.parse import quote

import httpx

from app.models import Connector, ConnectorAuth, MeasurementStep, ValueUnit
from app.providers.base import ConsentStatus, ContractInfo, ProviderApiError, ProviderMeasurement
from app.services import net
from app.timeutils import LOCAL_TZ, UTC, yesterday_local

MAX_RECORDS = 50_000


def dig(data, path: str):
    """Suit un chemin pointé (`a.b.0.c`) dans une structure JSON ; chemin vide = la structure elle-même."""
    for part in [p for p in path.split(".") if p]:
        if isinstance(data, list):
            data = data[int(part)] if part.isdigit() and int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def parse_timestamp(value) -> datetime:
    """ISO 8601 (avec ou sans fuseau ; sans fuseau = heure de Paris) ou horodatage Unix (s ou ms)."""
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_TZ)).astimezone(UTC)


def to_kwh(value: float, unit: ValueUnit, step_minutes: int) -> float:
    """Énergie du pas en kWh : les puissances (kW, W) sont multipliées par la durée du pas."""
    hours = step_minutes / 60
    return {
        ValueUnit.KWH: value,
        ValueUnit.WH: value / 1000,
        ValueUnit.KW: value * hours,
        ValueUnit.W: value / 1000 * hours,
    }[unit]


class GenericApiProvider:
    def __init__(self, connector: Connector, secret: dict) -> None:
        self.connector = connector
        self.secret = secret

    def _auth(self) -> tuple[dict, httpx.Auth | None]:
        c = self.connector
        if c.auth_type == ConnectorAuth.API_KEY_HEADER:
            return {c.auth_header or "X-API-Key": self.secret.get("value", "")}, None
        if c.auth_type == ConnectorAuth.BEARER:
            return {"Authorization": f"Bearer {self.secret.get('value', '')}"}, None
        if c.auth_type == ConnectorAuth.BASIC:
            return {}, httpx.BasicAuth(c.username or "", self.secret.get("value", ""))
        return {}, None

    def build_url(self, ref: str, start: date, end: date) -> str:
        url = (self.connector.url_template
               .replace("{ref}", quote(ref, safe=""))
               .replace("{start}", start.isoformat())
               .replace("{end}", end.isoformat()))
        return net.check_public_url(url)

    def fetch_raw(self, ref: str, start: date, end: date):
        headers, auth = self._auth()
        with net.client() as http:
            response = http.get(self.build_url(ref, start, end), headers={"Accept": "application/json", **headers},
                                auth=auth)
        if response.status_code in (401, 403):
            raise ProviderApiError(f"L'API a refusé l'accès (code {response.status_code}) : vérifiez l'authentification.")
        if response.is_redirect:
            raise ProviderApiError("L'API répond par une redirection : indiquez directement l'adresse finale.")
        if response.status_code != 200:
            raise ProviderApiError(f"L'API a répondu avec le code {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderApiError("La réponse de l'API n'est pas du JSON.") from exc

    def fetch_load_curve(self, delivery_point_id: str, start: date, end: date) -> list[ProviderMeasurement]:
        end = min(end, yesterday_local())
        if end < start:
            return []
        c = self.connector
        records = dig(self.fetch_raw(delivery_point_id, start, end), c.records_path or "")
        if not isinstance(records, list):
            raise ProviderApiError(f"Aucune liste de relevés trouvée au chemin « {c.records_path or '(racine)'} ».")
        step_minutes = 30 if c.step == MeasurementStep.PT30M else 1440
        lower = datetime.combine(start, time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
        upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
        energy: dict[datetime, float] = defaultdict(float)
        for record in records[:MAX_RECORDS]:
            raw_time, raw_value = dig(record, c.time_field), dig(record, c.value_field)
            if raw_time is None or raw_value in (None, ""):
                continue
            try:
                moment = parse_timestamp(raw_time)
                value = float(str(raw_value).replace(",", "."))
            except (ValueError, TypeError, OverflowError) as exc:
                raise ProviderApiError(f"Relevé illisible : {c.time_field}={raw_time!r}, {c.value_field}={raw_value!r}.") from exc
            if not lower <= moment < upper:
                continue
            if c.step == MeasurementStep.P1D:
                local_day = moment.astimezone(LOCAL_TZ).date()
                slot = datetime.combine(local_day, time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
            else:
                slot = moment.replace(minute=0 if moment.minute < 30 else 30, second=0, microsecond=0)
            energy[slot] += to_kwh(value, c.value_unit, step_minutes)
        return [
            ProviderMeasurement(
                time=slot, value_kwh=round(kwh, 4),
                avg_power_kw=round(kwh * 2, 4) if c.step == MeasurementStep.PT30M else None, step=c.step,
            )
            for slot, kwh in sorted(energy.items())
        ]

    def fetch_contract_info(self, delivery_point_id: str) -> ContractInfo:
        return ContractInfo(subscribed_power_kva=None, tariff_option="inconnue", meter_type=f"API {self.connector.name}")

    def check_consent(self, delivery_point_id: str) -> ConsentStatus:
        # Le consentement est porté par l'enregistrement `Consent` d'EffiSmart (étape d'onboarding).
        return ConsentStatus(granted=True, source="GENERIC_API")
