"""Modèle de données V1 (brief §4).

Hiérarchie multi-tenant :
Auditor → (AuditorClientLink daté) → Organization → Site → DeliveryPoint → Measurement
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.timeutils import utcnow


class Base(DeclarativeBase):
    pass


def _enum(enum_cls: type[enum.Enum]) -> SAEnum:
    # Stocké en VARCHAR (pas de type ENUM natif) : migrations et tests plus simples.
    return SAEnum(enum_cls, native_enum=False, length=32, validate_strings=True)


# --- Énumérations ---------------------------------------------------------


class Role(str, enum.Enum):
    AUDITOR = "AUDITOR"
    CLIENT_VIEWER = "CLIENT_VIEWER"
    ADMIN = "ADMIN"


class Fluid(str, enum.Enum):
    ELEC = "ELEC"
    GAS = "GAS"


class ProviderKind(str, enum.Enum):
    MOCK = "MOCK"
    ENEDIS_DATACONNECT = "ENEDIS_DATACONNECT"
    ENEDIS_SGE = "ENEDIS_SGE"
    GRDF_ADICT = "GRDF_ADICT"
    GENERIC_API = "GENERIC_API"  # connecteur REST configuré par l'auditeur (voir Connector)


class MeasurementStep(str, enum.Enum):
    PT30M = "PT30M"
    P1D = "P1D"


class Obligation(str, enum.Enum):
    DECRET_TERTIAIRE_OPERAT = "DECRET_TERTIAIRE_OPERAT"
    AUDIT_EED = "AUDIT_EED"
    VSME = "VSME"


class DeadlineStatus(str, enum.Enum):
    UPCOMING = "UPCOMING"
    DUE_SOON = "DUE_SOON"
    DONE = "DONE"


class ExportFormat(str, enum.Enum):
    OPERAT = "OPERAT"
    VSME = "VSME"


class ExportStatus(str, enum.Enum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


class DriftKind(str, enum.Enum):
    THRESHOLD = "THRESHOLD"
    CLIMATE_DEVIATION = "CLIMATE_DEVIATION"
    BASELOAD = "BASELOAD"


class DriftStatus(str, enum.Enum):
    OPEN = "OPEN"
    QUALIFIED = "QUALIFIED"
    IGNORED = "IGNORED"


# --- Tenants et utilisateurs ------------------------------------------------


class Auditor(Base):
    """Cabinet d'audit énergétique : le tenant."""

    __tablename__ = "auditors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    """Compte de connexion. Un AUDITOR est rattaché à un Auditor, un CLIENT_VIEWER à une Organization."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    auditor_id: Mapped[int | None] = mapped_column(ForeignKey("auditors.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    role: Mapped[Role] = mapped_column(_enum(Role))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Organization(Base):
    """Entreprise cliente."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    siren: Mapped[str | None] = mapped_column(String(9))
    address: Mapped[str | None] = mapped_column(String(300))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sites: Mapped[list[Site]] = relationship(back_populates="organization", order_by="Site.id")


class AuditorClientLink(Base):
    """Relation datée auditeur ↔ client (décision D8) : historisée, jamais écrasée."""

    __tablename__ = "auditor_client_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    auditor_id: Mapped[int] = mapped_column(ForeignKey("auditors.id"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# --- Patrimoine -------------------------------------------------------------


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300))
    surface_m2: Mapped[float | None] = mapped_column(Float)
    is_tertiary_decret: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="sites")
    delivery_points: Mapped[list[DeliveryPoint]] = relationship(
        back_populates="site", order_by="DeliveryPoint.id"
    )


class DeliveryPoint(Base):
    """Point de livraison : PRM (électricité) ou PCE (gaz). Point d'ancrage de la donnée."""

    __tablename__ = "delivery_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    fluid: Mapped[Fluid] = mapped_column(_enum(Fluid))
    external_ref: Mapped[str] = mapped_column(String(32), unique=True)
    provider: Mapped[ProviderKind] = mapped_column(_enum(ProviderKind), default=ProviderKind.MOCK)
    # Base de la future facturation (D5) : 1 site facturé = 1 PRM principal.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    subscribed_power_kva: Mapped[float | None] = mapped_column(Float)
    # Seuils paramétrables du détecteur « seuil » (F2a). Null = valeur par défaut.
    power_threshold_kw: Mapped[float | None] = mapped_column(Float)
    daily_threshold_kwh: Mapped[float | None] = mapped_column(Float)
    # Source GENERIC_API : connecteur qui fournit les mesures de ce point.
    connector_id: Mapped[int | None] = mapped_column(ForeignKey("connectors.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    site: Mapped[Site] = relationship(back_populates="delivery_points")


class Consent(Base):
    """Consentement RGPD : aucun accès aux données d'un point sans consentement actif."""

    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_point_id: Mapped[int] = mapped_column(ForeignKey("delivery_points.id"), index=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(String(200))
    proof_ref: Mapped[str] = mapped_column(String(200))
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Measurement(Base):
    """Série temporelle de consommation — hypertable TimescaleDB."""

    __tablename__ = "measurements"

    delivery_point_id: Mapped[int] = mapped_column(ForeignKey("delivery_points.id"), primary_key=True)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    value_kwh: Mapped[float] = mapped_column(Float)
    avg_power_kw: Mapped[float | None] = mapped_column(Float)
    step: Mapped[MeasurementStep] = mapped_column(_enum(MeasurementStep))


# --- F3 : réglementaire -----------------------------------------------------


class RegulatoryDeadline(Base):
    __tablename__ = "regulatory_deadlines"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    obligation: Mapped[Obligation] = mapped_column(_enum(Obligation))
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[DeadlineStatus] = mapped_column(_enum(DeadlineStatus), default=DeadlineStatus.UPCOMING)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    site: Mapped[Site] = relationship()


class ActionLog(Base):
    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    obligation: Mapped[Obligation] = mapped_column(_enum(Obligation))
    description: Mapped[str] = mapped_column(Text)
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    performed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


# --- Émissions et exports ---------------------------------------------------


class EmissionFactor(Base):
    """Facteur d'émission horodaté (Base Empreinte ADEME, décision D6)."""

    __tablename__ = "emission_factors"

    id: Mapped[int] = mapped_column(primary_key=True)
    fluid: Mapped[Fluid] = mapped_column(_enum(Fluid))
    factor_kgco2_per_kwh: Mapped[float] = mapped_column(Float)
    valid_from: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(300))
    version: Mapped[str] = mapped_column(String(100))


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    format: Mapped[ExportFormat] = mapped_column(_enum(ExportFormat))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[ExportStatus] = mapped_column(_enum(ExportStatus), default=ExportStatus.PENDING)
    file_ref: Mapped[str | None] = mapped_column(String(300))
    # Facteurs d'émission utilisés, horodatés (critère F4).
    factors_used: Mapped[list | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- F2a : dérives ----------------------------------------------------------


class Drift(Base):
    """Dérive détectée : une alerte à qualifier par un humain (principe P1), jamais une action."""

    __tablename__ = "drifts"
    __table_args__ = (UniqueConstraint("delivery_point_id", "kind", "day", name="uq_drift_dp_kind_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_point_id: Mapped[int] = mapped_column(ForeignKey("delivery_points.id"), index=True)
    kind: Mapped[DriftKind] = mapped_column(_enum(DriftKind))
    day: Mapped[date] = mapped_column(Date)
    measured_value: Mapped[float] = mapped_column(Float)
    reference_value: Mapped[float] = mapped_column(Float)
    deviation_pct: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))
    details: Mapped[str] = mapped_column(Text)
    status: Mapped[DriftStatus] = mapped_column(_enum(DriftStatus), default=DriftStatus.OPEN)
    comment: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    qualified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    delivery_point: Mapped[DeliveryPoint] = relationship()


class DocumentKind(str, enum.Enum):
    INVOICE_ELEC = "INVOICE_ELEC"
    INVOICE_GAS = "INVOICE_GAS"
    INVOICE_OTHER = "INVOICE_OTHER"  # fioul, réseau de chaleur, eau…
    METER_READING = "METER_READING"  # relevé de compteur (photo, CSV, tableur)
    OTHER = "OTHER"


class DocumentStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"


class Document(Base):
    """Document déposé (facture, relevé) : seule écriture ouverte à l'espace client.

    Le fichier est stocké sous un nom aléatoire (`stored_name`) ; le nom d'origine n'est
    conservé que pour l'affichage et le téléchargement.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    kind: Mapped[DocumentKind] = mapped_column(_enum(DocumentKind))
    status: Mapped[DocumentStatus] = mapped_column(_enum(DocumentStatus), default=DocumentStatus.RECEIVED)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(64), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column()
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    comment: Mapped[str | None] = mapped_column(Text)
    review_note: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site | None] = relationship()


# --- Intégrations ---------------------------------------------------------------


class IntegrationKind(str, enum.Enum):
    ENEDIS_DATACONNECT = "ENEDIS_DATACONNECT"
    GRDF_ADICT = "GRDF_ADICT"
    OPEN_METEO = "OPEN_METEO"


class PlatformIntegration(Base):
    """Source de données de la plateforme (contrats Enedis, GRDF, météo) : réglée par l'administrateur."""

    __tablename__ = "platform_integrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[IntegrationKind] = mapped_column(_enum(IntegrationKind), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict | None] = mapped_column(JSON)  # paramètres non secrets
    secrets_encrypted: Mapped[str | None] = mapped_column(Text)  # identifiants chiffrés (Fernet)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ConnectorAuth(str, enum.Enum):
    NONE = "NONE"
    API_KEY_HEADER = "API_KEY_HEADER"
    BEARER = "BEARER"
    BASIC = "BASIC"


class ValueUnit(str, enum.Enum):
    KWH = "KWH"
    WH = "WH"
    KW = "KW"
    W = "W"


class Connector(Base):
    """Connecteur REST générique d'un cabinet : source de relevés pour des points GENERIC_API."""

    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    auditor_id: Mapped[int] = mapped_column(ForeignKey("auditors.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    url_template: Mapped[str] = mapped_column(String(500))  # {ref}, {start}, {end} remplacés à l'appel
    auth_type: Mapped[ConnectorAuth] = mapped_column(_enum(ConnectorAuth), default=ConnectorAuth.NONE)
    auth_header: Mapped[str | None] = mapped_column(String(100))
    username: Mapped[str | None] = mapped_column(String(200))
    secret_encrypted: Mapped[str | None] = mapped_column(Text)
    records_path: Mapped[str] = mapped_column(String(200), default="")
    time_field: Mapped[str] = mapped_column(String(100))
    value_field: Mapped[str] = mapped_column(String(100))
    value_unit: Mapped[ValueUnit] = mapped_column(_enum(ValueUnit), default=ValueUnit.KWH)
    step: Mapped[MeasurementStep] = mapped_column(_enum(MeasurementStep), default=MeasurementStep.PT30M)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    """Clé d'accès en lecture à l'API partenaires. Seule l'empreinte SHA-256 est stockée."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    auditor_id: Mapped[int] = mapped_column(ForeignKey("auditors.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))  # restriction éventuelle
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Webhook(Base):
    """Envoi d'événements (nouvelle dérive, nouveau document) vers l'outil d'un partenaire."""

    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    auditor_id: Mapped[int] = mapped_column(ForeignKey("auditors.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(500))
    secret_encrypted: Mapped[str] = mapped_column(Text)  # clé de signature HMAC, chiffrée
    events: Mapped[list] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class WebhookDelivery(Base):
    """File d'envoi des webhooks (boîte d'envoi) : réessais avec délai croissant."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    webhook_id: Mapped[int] = mapped_column(ForeignKey("webhooks.id"), index=True)
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[DeliveryStatus] = mapped_column(_enum(DeliveryStatus), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_status_code: Mapped[int | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    drift_id: Mapped[int | None] = mapped_column(ForeignKey("drifts.id"))
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
