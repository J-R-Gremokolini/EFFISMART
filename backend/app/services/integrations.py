"""Intégrations d'API : sources de données, connecteurs génériques, clés d'API partenaires, webhooks.

Droits :
- sources de données de la plateforme (Enedis, GRDF, météo) : administrateur uniquement — les contrats
  Enedis / GRDF sont signés par EffiSmart ; les auditeurs en voient l'état ;
- connecteurs, clés d'API et webhooks : propres à chaque cabinet (auditeur), jamais visibles d'un autre ;
- aucun accès pour l'espace client.

Secrets : chiffrés (`app.services.secrets`), jamais relus en clair par l'interface. Les clés d'API ne
sont stockées que sous forme d'empreinte ; la clé complète n'est montrée qu'une fois, à la création.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets as pysecrets
import threading
from datetime import date, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import (
    ApiKey,
    AuditorClientLink,
    Connector,
    ConnectorAuth,
    DeliveryPoint,
    DeliveryStatus,
    IntegrationKind,
    MeasurementStep,
    PlatformIntegration,
    ProviderKind,
    Role,
    User,
    ValueUnit,
    Webhook,
    WebhookDelivery,
)
from app.providers.base import EnergyDataProvider, ProviderMeasurement, ProviderNotConfiguredError
from app.providers.weather import MockWeatherProvider, OpenMeteoWeatherProvider, WeatherProvider
from app.repositories import ResourceNotFound, TenantRepository, active_links_clause
from app.services import net
from app.services import secrets as secret_store
from app.timeutils import utcnow

logger = logging.getLogger(__name__)


class IntegrationError(ValueError):
    """Réglage refusé ; message affichable."""


class IntegrationPermissionError(PermissionError):
    pass


# --- Sources de données de la plateforme --------------------------------------------------------

PLATFORM_SPECS: dict[IntegrationKind, dict] = {
    IntegrationKind.ENEDIS_DATACONNECT: {
        "label": "Enedis Data Connect",
        "description": "Courbes de charge électriques (pas de 30 min) des points dont le client a donné son accord "
                       "sur son espace Enedis. Identifiants fournis par Enedis après signature du contrat.",
        "settings": {"environment": {"label": "Environnement", "options": ["sandbox", "production"],
                                     "default": "sandbox"}},
        "secrets": {"client_id": "Identifiant client (client_id)", "client_secret": "Secret client (client_secret)"},
    },
    IntegrationKind.GRDF_ADICT: {
        "label": "GRDF ADICT",
        "description": "Consommations de gaz quotidiennes (compteurs Gazpar) des points dont le client a donné son "
                       "accord sur son espace GRDF. Identifiants fournis par GRDF après signature du contrat.",
        "settings": {},
        "secrets": {"client_id": "Identifiant client (client_id)", "client_secret": "Secret client (client_secret)"},
    },
    IntegrationKind.OPEN_METEO: {
        "label": "Météo réelle (Open-Meteo)",
        "description": "Températures réelles pour les degrés-jours du détecteur « écart climatique ». Sans clé "
                       "d'API. À activer avec des données de consommation réelles : avec les données simulées, "
                       "la météo réelle ferait apparaître de fausses dérives.",
        "settings": {"latitude": {"label": "Latitude", "default": 48.8566},
                     "longitude": {"label": "Longitude", "default": 2.3522}},
        "secrets": {},
    },
    IntegrationKind.SMTP: {
        "label": "Envoi des e-mails (SMTP)",
        "description": "Notification immédiate par e-mail : alertes à valider, décisions, rappels d'échéances "
                       "réglementaires. Tant qu'aucun serveur n'est activé, la version locale écrit les e-mails "
                       "dans le dossier backend/data/outbox au lieu de les envoyer.",
        "settings": {
            "host": {"label": "Serveur SMTP", "type": "text", "default": ""},
            "port": {"label": "Port", "type": "int", "default": 587},
            "security": {"label": "Sécurité", "options": ["starttls", "ssl", "aucune"], "default": "starttls"},
            "sender": {"label": "Adresse d'expédition", "type": "text", "default": "alertes@effismart.local"},
            "username": {"label": "Identifiant (facultatif)", "type": "text", "default": ""},
        },
        "secrets": {"password": "Mot de passe (facultatif)"},
        "optional_secrets": True,
    },
}
PROVIDER_TO_INTEGRATION = {
    ProviderKind.ENEDIS_DATACONNECT: IntegrationKind.ENEDIS_DATACONNECT,
    ProviderKind.GRDF_ADICT: IntegrationKind.GRDF_ADICT,
}


def _require_admin(user: User) -> None:
    if user.role != Role.ADMIN:
        raise IntegrationPermissionError("Seul l'administrateur de la plateforme règle les sources de données.")


def _auditor_id(user: User) -> int:
    if user.role != Role.AUDITOR or user.auditor_id is None:
        raise IntegrationPermissionError("Réservé aux comptes auditeur (connecteurs, clés d'API et webhooks).")
    return user.auditor_id


def get_platform(db: Session, kind: IntegrationKind) -> PlatformIntegration:
    row = db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == kind))
    if row is None:
        row = PlatformIntegration(kind=kind, enabled=False, settings={})
        db.add(row)
        db.commit()
    return row


def platform_settings(row: PlatformIntegration) -> dict:
    spec = PLATFORM_SPECS[row.kind]["settings"]
    return {name: (row.settings or {}).get(name, field["default"]) for name, field in spec.items()}


def secret_status(row: PlatformIntegration) -> dict[str, bool]:
    """Pour l'affichage : quel secret est enregistré (jamais sa valeur)."""
    stored = secret_store.decrypt(row.secrets_encrypted)
    return {name: bool(stored.get(name)) for name in PLATFORM_SPECS[row.kind]["secrets"]}


def save_platform(db: Session, user: User, kind: IntegrationKind, *, enabled: bool, settings_values: dict,
                  secrets_values: dict) -> PlatformIntegration:
    """Les champs secrets laissés vides conservent la valeur déjà enregistrée."""
    _require_admin(user)
    row = get_platform(db, kind)
    spec = PLATFORM_SPECS[kind]
    clean = {}
    for name, field in spec["settings"].items():
        value = settings_values.get(name, field["default"])
        if "options" in field and value not in field["options"]:
            raise IntegrationError(f"{field['label']} : valeur non autorisée.")
        if name in ("latitude", "longitude"):
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise IntegrationError(f"{field['label']} : nombre attendu.") from exc
            if not (-90 <= value <= 90 if name == "latitude" else -180 <= value <= 180):
                raise IntegrationError(f"{field['label']} hors limites.")
        elif field.get("type") == "int":
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise IntegrationError(f"{field['label']} : nombre entier attendu.") from exc
            if not 1 <= value <= 65535:
                raise IntegrationError(f"{field['label']} hors limites.")
        elif field.get("type") == "text":
            value = str(value or "").strip()[:200]
        clean[name] = value
    if kind == IntegrationKind.SMTP and enabled and (not clean.get("host") or "@" not in clean.get("sender", "")):
        raise IntegrationError("Renseignez le serveur SMTP et une adresse d'expédition valide avant d'activer l'envoi.")
    stored = secret_store.decrypt(row.secrets_encrypted)
    for name in spec["secrets"]:
        new_value = (secrets_values.get(name) or "").strip()
        if new_value:
            stored[name] = new_value
    if enabled and not spec.get("optional_secrets") and any(not stored.get(name) for name in spec["secrets"]):
        raise IntegrationError("Renseignez tous les identifiants avant d'activer cette source.")
    row.enabled = enabled
    row.settings = clean
    row.secrets_encrypted = secret_store.encrypt(stored) if stored else None
    row.updated_at, row.updated_by = utcnow(), user.id
    db.commit()
    return row


def _platform_instance(row: PlatformIntegration):
    creds = secret_store.decrypt(row.secrets_encrypted)
    values = platform_settings(row)
    if row.kind == IntegrationKind.ENEDIS_DATACONNECT:
        from app.providers.enedis import EnedisDataConnectProvider

        return EnedisDataConnectProvider(creds.get("client_id", ""), creds.get("client_secret", ""),
                                         values["environment"])
    if row.kind == IntegrationKind.GRDF_ADICT:
        from app.providers.grdf import GrdfAdictProvider

        return GrdfAdictProvider(creds.get("client_id", ""), creds.get("client_secret", ""))
    return OpenMeteoWeatherProvider(values["latitude"], values["longitude"])


def test_platform(db: Session, user: User, kind: IntegrationKind) -> tuple[bool, str]:
    """Essai de connexion : authentification (Enedis, GRDF) ou lecture des températures d'hier (météo)."""
    _require_admin(user)
    row = get_platform(db, kind)
    try:
        if kind == IntegrationKind.SMTP:
            from app.services import mailer

            values = platform_settings(row)
            values["password"] = secret_store.decrypt(row.secrets_encrypted).get("password", "")
            if not values.get("host"):
                raise RuntimeError("aucun serveur SMTP renseigné")
            message = mailer.test_connection(values)
            row.last_test_at, row.last_test_ok, row.last_test_message = utcnow(), True, message[:500]
            db.commit()
            return True, message
        instance = _platform_instance(row)
        if kind == IntegrationKind.OPEN_METEO:
            from app.timeutils import yesterday_local

            day = yesterday_local()
            temperatures = instance.mean_temperatures(day - timedelta(days=2), day)
            if not temperatures:
                raise RuntimeError("aucune température reçue")
            last = max(temperatures)
            message = f"Connexion réussie : {temperatures[last]:.1f} °C en moyenne le {last:%d/%m/%Y}."
        else:
            instance.token()
            message = "Connexion réussie : authentification acceptée."
        ok = True
    except Exception as exc:  # message affichable, sans secret
        ok, message = False, f"Échec : {exc}"
    row.last_test_at, row.last_test_ok, row.last_test_message = utcnow(), ok, message[:500]
    db.commit()
    return ok, message


def build_energy_provider(provider: ProviderKind, delivery_point: DeliveryPoint | None) -> EnergyDataProvider:
    """Fournisseur réel d'un point : Enedis / GRDF (si activés) ou connecteur générique."""
    with SessionLocal() as db:
        if provider == ProviderKind.GENERIC_API:
            if delivery_point is None or delivery_point.connector_id is None:
                raise ProviderNotConfiguredError("Aucun connecteur n'est rattaché à ce point de livraison.")
            connector = db.get(Connector, delivery_point.connector_id)
            if connector is None:
                raise ProviderNotConfiguredError("Le connecteur de ce point a été supprimé.")
            from app.providers.generic import GenericApiProvider

            return GenericApiProvider(connector, secret_store.decrypt(connector.secret_encrypted))
        kind = PROVIDER_TO_INTEGRATION.get(provider)
        if kind is None:
            raise ValueError(f"Fournisseur inconnu : {provider}")
        row = db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == kind))
        if row is None or not row.enabled:
            raise ProviderNotConfiguredError(
                f"L'intégration {PLATFORM_SPECS[kind]['label']} n'est pas activée (page « Intégrations »)."
            )
        return _platform_instance(row)


def weather_provider(db: Session) -> WeatherProvider:
    row = db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == IntegrationKind.OPEN_METEO))
    if row is not None and row.enabled:
        return _platform_instance(row)
    return MockWeatherProvider()


# --- Connecteurs génériques ---------------------------------------------------------------------

_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_FIELD_PATH = re.compile(r"^[\w.-]{0,200}$")


def list_connectors(db: Session, user: User) -> list[Connector]:
    stmt = select(Connector).order_by(Connector.name)
    if user.role != Role.ADMIN:
        stmt = stmt.where(Connector.auditor_id == _auditor_id(user))
    return list(db.scalars(stmt))


def get_connector(db: Session, user: User, connector_id: int) -> Connector:
    connector = db.get(Connector, connector_id)
    if connector is None or (user.role != Role.ADMIN and connector.auditor_id != _auditor_id(user)):
        raise ResourceNotFound()
    return connector


def validate_url_template(template: str) -> str:
    template = template.strip()
    host_part = urlsplit(template).netloc
    if "{" in host_part or "}" in host_part:
        raise IntegrationError("Les variables {ref}, {start} et {end} ne peuvent pas figurer dans le nom d'hôte.")
    sample = template.replace("{ref}", "ref").replace("{start}", "2026-01-01").replace("{end}", "2026-01-02")
    try:
        net.check_public_url(sample)
    except net.UnsafeUrlError as exc:
        raise IntegrationError(str(exc)) from exc
    return template


def save_connector(db: Session, user: User, *, connector_id: int | None = None, name: str, url_template: str,
                   auth_type: ConnectorAuth, auth_header: str | None = None, username: str | None = None,
                   secret: str | None = None, records_path: str = "", time_field: str, value_field: str,
                   value_unit: ValueUnit, step: MeasurementStep) -> Connector:
    auditor_id = _auditor_id(user)
    name = name.strip()
    if not name:
        raise IntegrationError("Donnez un nom au connecteur.")
    url_template = validate_url_template(url_template)
    if auth_type == ConnectorAuth.API_KEY_HEADER and not _HEADER_NAME.match((auth_header or "").strip()):
        raise IntegrationError("Nom d'en-tête invalide (lettres, chiffres et tirets, ex. X-API-Key).")
    for label, value in (("Chemin des relevés", records_path), ("Champ de date", time_field),
                         ("Champ de valeur", value_field)):
        if not _FIELD_PATH.match(value.strip()):
            raise IntegrationError(f"{label} : lettres, chiffres, points, tirets et soulignés uniquement.")
    if not time_field.strip() or not value_field.strip():
        raise IntegrationError("Indiquez le champ de date et le champ de valeur.")

    connector = get_connector(db, user, connector_id) if connector_id else Connector(auditor_id=auditor_id,
                                                                                       created_by=user.id)
    connector.name, connector.url_template, connector.auth_type = name, url_template, auth_type
    connector.auth_header = (auth_header or "").strip() or None
    connector.username = (username or "").strip() or None
    connector.records_path, connector.time_field = records_path.strip(), time_field.strip()
    connector.value_field, connector.value_unit, connector.step = value_field.strip(), value_unit, step
    if (secret or "").strip():
        connector.secret_encrypted = secret_store.encrypt({"value": secret.strip()})
    if auth_type != ConnectorAuth.NONE and not connector.secret_encrypted:
        raise IntegrationError("Renseignez la clé, le jeton ou le mot de passe de l'API.")
    if connector_id is None:
        db.add(connector)
    db.commit()
    return connector


def delete_connector(db: Session, user: User, connector_id: int) -> None:
    connector = get_connector(db, user, connector_id)
    if db.scalar(select(DeliveryPoint.id).where(DeliveryPoint.connector_id == connector.id)):
        raise IntegrationError("Ce connecteur alimente encore des points de livraison : changez-les d'abord de source.")
    db.delete(connector)
    db.commit()


def test_connector(db: Session, user: User, connector_id: int, ref: str, start: date, end: date
                   ) -> list[ProviderMeasurement]:
    """Appel réel de l'API avec aperçu des relevés reconnus (rien n'est enregistré)."""
    from app.providers.generic import GenericApiProvider

    connector = get_connector(db, user, connector_id)
    provider = GenericApiProvider(connector, secret_store.decrypt(connector.secret_encrypted))
    return provider.fetch_load_curve(ref.strip(), start, end)


# --- Clés d'API partenaires ------------------------------------------------------------------------

API_KEY_PREFIX = "esk_"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_key(db: Session, user: User, repo: TenantRepository, *, name: str,
                   organization_id: int | None = None) -> tuple[ApiKey, str]:
    """Renvoie la clé et sa valeur complète — à montrer une seule fois, elle n'est pas conservée."""
    auditor_id = _auditor_id(user)
    name = name.strip()
    if not name:
        raise IntegrationError("Donnez un nom à la clé (ex. « ERP du client »).")
    if organization_id is not None:
        repo.get_organization(organization_id)
    raw = API_KEY_PREFIX + pysecrets.token_urlsafe(32)
    key = ApiKey(auditor_id=auditor_id, organization_id=organization_id, name=name, prefix=raw[:12],
                 key_hash=_hash_key(raw), created_by=user.id)
    db.add(key)
    db.commit()
    return key, raw


def list_api_keys(db: Session, user: User) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).where(ApiKey.auditor_id == _auditor_id(user))
                           .order_by(ApiKey.created_at.desc())))


def revoke_api_key(db: Session, user: User, key_id: int) -> None:
    key = db.get(ApiKey, key_id)
    if key is None or key.auditor_id != _auditor_id(user):
        raise ResourceNotFound()
    key.revoked_at = key.revoked_at or utcnow()
    db.commit()


def authenticate_api_key(db: Session, raw: str | None) -> ApiKey | None:
    if not raw or not raw.startswith(API_KEY_PREFIX):
        return None
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == _hash_key(raw.strip()), ApiKey.revoked_at.is_(None)))
    if key is not None:
        key.last_used_at = utcnow()
        db.commit()
    return key


# --- Webhooks ---------------------------------------------------------------------------------------

# Principe P1 : seules les sorties validées par un humain partent vers les outils des partenaires.
WEBHOOK_EVENTS = {
    "drift.validated": "Anomalie validée",
    "recommendation.validated": "Recommandation validée",
    "recommendation.applied": "Recommandation déclarée appliquée",
    "savings.validated": "Économies mesurées validées (avant / après)",
    "prediction.validated": "Prévision validée",
    "document.deposited": "Nouveau document déposé",
}
# Abonnements créés avant le principe P1 : « drift.created » reçoit désormais les anomalies validées.
LEGACY_EVENTS = {"drift.created": "drift.validated"}
RETRY_DELAYS_MIN = [1, 5, 30, 120]


def create_webhook(db: Session, user: User, repo: TenantRepository, *, name: str, url: str, events: list[str],
                   organization_id: int | None = None) -> tuple[Webhook, str]:
    """Renvoie le webhook et sa clé de signature (affichée une fois, conservée chiffrée pour signer)."""
    auditor_id = _auditor_id(user)
    name = name.strip()
    if not name:
        raise IntegrationError("Donnez un nom au webhook.")
    try:
        url = net.check_public_url(url)
    except net.UnsafeUrlError as exc:
        raise IntegrationError(str(exc)) from exc
    events = [e for e in dict.fromkeys(LEGACY_EVENTS.get(e, e) for e in events) if e in WEBHOOK_EVENTS]
    if not events:
        raise IntegrationError("Choisissez au moins un événement.")
    if organization_id is not None:
        repo.get_organization(organization_id)
    secret = "whsec_" + pysecrets.token_urlsafe(24)
    webhook = Webhook(auditor_id=auditor_id, organization_id=organization_id, name=name, url=url,
                      secret_encrypted=secret_store.encrypt({"value": secret}), events=events, created_by=user.id)
    db.add(webhook)
    db.commit()
    return webhook, secret


def list_webhooks(db: Session, user: User) -> list[Webhook]:
    return list(db.scalars(select(Webhook).where(Webhook.auditor_id == _auditor_id(user))
                           .order_by(Webhook.created_at.desc())))


def get_webhook(db: Session, user: User, webhook_id: int) -> Webhook:
    webhook = db.get(Webhook, webhook_id)
    if webhook is None or webhook.auditor_id != _auditor_id(user):
        raise ResourceNotFound()
    return webhook


def set_webhook_enabled(db: Session, user: User, webhook_id: int, enabled: bool) -> None:
    get_webhook(db, user, webhook_id).enabled = enabled
    db.commit()


def delete_webhook(db: Session, user: User, webhook_id: int) -> None:
    webhook = get_webhook(db, user, webhook_id)
    for delivery in db.scalars(select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook.id)):
        db.delete(delivery)
    db.delete(webhook)
    db.commit()


def recent_deliveries(db: Session, user: User, webhook_id: int, limit: int = 10) -> list[WebhookDelivery]:
    webhook = get_webhook(db, user, webhook_id)
    return list(db.scalars(select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook.id)
                           .order_by(WebhookDelivery.id.desc()).limit(limit)))


def enqueue_event(db: Session, event: str, organization_id: int, data: dict) -> int:
    """Met l'événement en file pour les webhooks concernés (cabinets liés au client). Pas de commit ici."""
    linked = select(AuditorClientLink.auditor_id).where(
        AuditorClientLink.organization_id == organization_id, *active_links_clause()
    )
    webhooks = db.scalars(select(Webhook).where(Webhook.enabled.is_(True), Webhook.auditor_id.in_(linked)))
    count = 0
    for webhook in webhooks:
        subscribed = {LEGACY_EVENTS.get(e, e) for e in webhook.events or []}
        if event not in subscribed or webhook.organization_id not in (None, organization_id):
            continue
        db.add(WebhookDelivery(webhook_id=webhook.id, event=event, payload={
            "event": event, "organization_id": organization_id, "created_at": utcnow().isoformat(), "data": data,
        }))
        count += 1
    return count


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 de « horodatage.corps » : le destinataire vérifie l'origine et refuse les rejeux."""
    return hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def _send(db: Session, delivery: WebhookDelivery) -> None:
    webhook = db.get(Webhook, delivery.webhook_id)
    delivery.attempts += 1
    try:
        if webhook is None or not webhook.enabled:
            raise RuntimeError("webhook désactivé")
        url = net.check_public_url(webhook.url)
        body = json.dumps({"id": delivery.id, **delivery.payload}, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        timestamp = str(int(utcnow().timestamp()))
        secret = secret_store.decrypt(webhook.secret_encrypted)["value"]
        with net.client() as http:
            response = http.post(url, content=body, headers={
                "Content-Type": "application/json",
                "X-EffiSmart-Event": delivery.event,
                "X-EffiSmart-Delivery": str(delivery.id),
                "X-EffiSmart-Timestamp": timestamp,
                "X-EffiSmart-Signature": f"sha256={signature(secret, timestamp, body)}",
            })
        delivery.last_status_code = response.status_code
        if 200 <= response.status_code < 300:
            delivery.status, delivery.sent_at, delivery.last_error = DeliveryStatus.SENT, utcnow(), None
            return
        delivery.last_error = f"Réponse {response.status_code}"
    except Exception as exc:  # réseau, adresse refusée, webhook désactivé…
        delivery.last_error = str(exc)[:500] or exc.__class__.__name__
    if delivery.attempts >= settings.webhook_max_attempts or webhook is None or not webhook.enabled:
        delivery.status = DeliveryStatus.FAILED
    else:
        delay = RETRY_DELAYS_MIN[min(delivery.attempts - 1, len(RETRY_DELAYS_MIN) - 1)]
        delivery.next_attempt_at = utcnow() + timedelta(minutes=delay)


def dispatch_pending(db: Session, limit: int = 50) -> tuple[int, int]:
    """Envoie les webhooks en attente dont l'heure est venue. Renvoie (envoyés, en échec définitif)."""
    pending = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.status == DeliveryStatus.PENDING, WebhookDelivery.next_attempt_at <= utcnow())
        .order_by(WebhookDelivery.id).limit(limit)
    ).all()
    sent = failed = 0
    for delivery in pending:
        _send(db, delivery)
        db.commit()
        sent += delivery.status == DeliveryStatus.SENT
        failed += delivery.status == DeliveryStatus.FAILED
    if pending:
        logger.info("Webhooks : %d envoyé(s), %d en échec définitif", sent, failed)
    return sent, failed


def send_test(db: Session, user: User, webhook_id: int) -> WebhookDelivery:
    """Envoie immédiatement un événement « ping » pour vérifier l'adresse et la signature."""
    webhook = get_webhook(db, user, webhook_id)
    delivery = WebhookDelivery(webhook_id=webhook.id, event="ping", payload={
        "event": "ping", "created_at": utcnow().isoformat(), "data": {"message": "Test EffiSmart"},
    })
    db.add(delivery)
    db.flush()
    _send(db, delivery)
    if delivery.status == DeliveryStatus.PENDING:
        delivery.status = DeliveryStatus.FAILED  # un test n'est pas réessayé
    db.commit()
    return delivery


def dispatch_in_background() -> None:
    """Envoi sans bloquer l'interface (mode local, après un dépôt, une décision ou une analyse) :
    webhooks et e-mails de notification."""
    from app.services import mailer

    def run() -> None:
        with SessionLocal() as db:
            for name, dispatch in (("webhooks", dispatch_pending), ("e-mails", mailer.dispatch_pending)):
                try:
                    dispatch(db)
                except Exception:
                    logger.exception("Envoi des %s impossible", name)

    threading.Thread(target=run, name="effismart-envois", daemon=True).start()
