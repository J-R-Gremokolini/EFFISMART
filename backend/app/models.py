"""Modèle de données V1 (brief §4).

Hiérarchie multi-tenant :
Auditor → (AuditorClientLink daté) → Organization → Site → DeliveryPoint → Measurement

Graphe physique d'un site (AssetNode / AssetRelation), distinct de cette hiérarchie de rangement :
Compteur → alimente → Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage
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
    false,
    true,
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
    """Détecteurs F2a (seuils et dérive), purement statistiques."""

    THRESHOLD = "THRESHOLD"  # dépassement de seuil
    CLIMATE_DEVIATION = "CLIMATE_DEVIATION"  # écart à l'historique corrigé du climat (DJU)
    BASELOAD = "BASELOAD"  # talon de nuit anormal
    OFF_HOURS = "OFF_HOURS"  # consommation en période d'inoccupation (hors nuit)


class DriftStatus(str, enum.Enum):
    """Validation humaine d'une anomalie (principe P1) : à valider → validée / écartée."""

    OPEN = "OPEN"  # proposée par la plateforme, à valider
    QUALIFIED = "QUALIFIED"  # validée par un humain
    IGNORED = "IGNORED"  # écartée par un humain


class ReviewStatus(str, enum.Enum):
    """Cycle de vie d'une recommandation ou d'une prévision (principe P1)."""

    PROPOSED = "PROPOSED"  # proposée par la plateforme, à valider
    VALIDATED = "VALIDATED"  # validée par l'auditeur ou le responsable énergie
    REJECTED = "REJECTED"  # écartée par un humain
    APPLIED = "APPLIED"  # recommandation mise en œuvre, déclarée par un humain
    SUPERSEDED = "SUPERSEDED"  # prévision non validée remplacée par une plus récente


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
    # Responsable énergie du client (CLIENT_VIEWER uniquement) : voit les sorties algorithmiques
    # non validées de son organisation et peut les valider ou les écarter (principe P1).
    is_energy_manager: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Notification immédiate par e-mail (F2, F3) : réglable par chaque utilisateur dans « Mon compte ».
    email_notifications: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
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


class DeclaredSource(str, enum.Enum):
    INVOICE = "INVOICE"  # facture du fournisseur
    METER_READING = "METER_READING"  # relevé d'index fait sur place
    OTHER = "OTHER"


class DeclaredConsumption(Base):
    """Donnée de niveau N0 : consommation d'une période issue d'une facture ou d'un relevé.

    Elle complète les données de niveau N1 (compteurs communicants) : chaque jour sans mesure N1 reçoit
    sa part de la période, au prorata des jours (mode dégradé). Pas de courbe ni de détection d'anomalies
    sur des données N0.
    """

    __tablename__ = "declared_consumptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    delivery_point_id: Mapped[int] = mapped_column(ForeignKey("delivery_points.id"), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    kwh: Mapped[float] = mapped_column(Float)
    amount_eur: Mapped[float | None] = mapped_column(Float)  # montant TTC de la facture, si connu
    source: Mapped[DeclaredSource] = mapped_column(_enum(DeclaredSource), default=DeclaredSource.INVOICE)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))  # justificatif déposé
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    delivery_point: Mapped[DeliveryPoint] = relationship()


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
    # Dernier rappel envoyé, en jours avant l'échéance (60, 30, 7, 0 ; -1 = en retard). Remis à zéro si la date change.
    reminder_level: Mapped[int | None] = mapped_column()
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
    # F4 : jeu de données produit automatiquement par la plateforme (année écoulée, année en cours).
    automatic: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Principe P1 : sorties algorithmiques expliquées, validées par un humain ----------------


class ExplainedOutput:
    """Colonnes communes à toute sortie algorithmique (anomalie, recommandation, prévision).

    Chaque sortie porte son raisonnement, son gain estimé et son niveau de confiance, et attend
    la validation de l'auditeur ou du responsable énergie du client : la plateforme ne valide jamais seule.
    """

    # none_as_null : « pas encore d'explication » est un vrai NULL SQL, pas la valeur JSON null.
    reasoning: Mapped[list | None] = mapped_column(JSON(none_as_null=True))  # étapes du raisonnement, dans l'ordre
    # Score de 0 à 1, borné à 0,95 : la plateforme n'est jamais certaine.
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_factors: Mapped[list | None] = mapped_column(JSON(none_as_null=True))  # [{"label": …, "delta": …}]
    # Gain annuel estimé (valeur négative = surcoût, pour une prévision au-dessus de N-1).
    gain_kwh: Mapped[float | None] = mapped_column(Float)
    gain_eur: Mapped[float | None] = mapped_column(Float)
    gain_kgco2e: Mapped[float | None] = mapped_column(Float)
    gain_basis: Mapped[str | None] = mapped_column(Text)  # hypothèses du calcul du gain
    algorithm: Mapped[str | None] = mapped_column(String(120))  # algorithme et version (traçabilité)


# --- F2a : dérives ----------------------------------------------------------


class Drift(ExplainedOutput, Base):
    """Anomalie détectée : une proposition à valider par un humain (principe P1), jamais une action."""

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
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # F2b : qualification et propagation d'impact par le graphe physique (P2), figées à la validation.
    context: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    # F2 : alertes regroupées. Une anomalie de même cause qu'une alerte encore à valider s'y rattache ; seule la
    # plus ancienne (l'anomalie principale) est signalée et se décide pour tout le groupe.
    grouped_with_id: Mapped[int | None] = mapped_column(ForeignKey("drifts.id"), index=True)
    grouping_rule: Mapped[str | None] = mapped_column(String(16))  # REPEAT, EXPLAINED, SUSPECT ou DETACHED
    grouping_reason: Mapped[str | None] = mapped_column(Text)

    delivery_point: Mapped[DeliveryPoint] = relationship()


# --- Graphe physique des équipements ------------------------------------------------------------


class AssetNodeKind(str, enum.Enum):
    METER = "METER"  # compteur : un point de livraison dans le graphe
    EQUIPMENT = "EQUIPMENT"  # chaudière, CTA, groupe froid, éclairage…
    FLOW = "FLOW"  # fluide produit : eau chaude, eau glacée, air neuf…
    ZONE = "ZONE"  # zone desservie
    USAGE = "USAGE"  # usage final : chauffage, froid, éclairage…


class AssetRelationKind(str, enum.Enum):
    SUPPLIES = "SUPPLIES"  # alimente
    PRODUCES = "PRODUCES"  # produit
    SERVES = "SERVES"  # dessert
    HOSTS = "HOSTS"  # accueille (zone → usage)


class AssetNode(Base):
    """Nœud du graphe physique d'un site. Les relations sont typées, pas une arborescence de rangement."""

    __tablename__ = "asset_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    kind: Mapped[AssetNodeKind] = mapped_column(_enum(AssetNodeKind))
    category: Mapped[str] = mapped_column(String(32))  # ex. BOILER (équipement), HOT_WATER (fluide), HEATING (usage)
    name: Mapped[str] = mapped_column(String(120))
    delivery_point_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_points.id"), unique=True)
    power_kw: Mapped[float | None] = mapped_column(Float)  # puissance nominale d'un équipement
    surface_m2: Mapped[float | None] = mapped_column(Float)  # surface d'une zone
    # Zone occupée 24 h/24 (chambres, local serveur) : ses équipements fonctionnent légitimement la nuit.
    always_occupied: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    delivery_point: Mapped[DeliveryPoint | None] = relationship()


class AssetRelation(Base):
    """Relation physique orientée : source → (alimente | produit | dessert | accueille) → cible."""

    __tablename__ = "asset_relations"
    __table_args__ = (UniqueConstraint("source_id", "target_id", "kind", name="uq_asset_relation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("asset_nodes.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("asset_nodes.id"), index=True)
    kind: Mapped[AssetRelationKind] = mapped_column(_enum(AssetRelationKind))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Recommandations et prévisions ------------------------------------------------------------------


class RecommendationKind(str, enum.Enum):
    SCHEDULE_OFF_HOURS = "SCHEDULE_OFF_HOURS"  # arrêt ou réduit en période d'inoccupation
    HEATING_CONTROL = "HEATING_CONTROL"  # régulation : consignes, loi d'eau, programmation
    PEAK_SHAVING = "PEAK_SHAVING"  # délestage ou décalage des appels de puissance
    INVESTIGATE = "INVESTIGATE"  # graphe incomplet : identifier l'équipement en cause


class Recommendation(ExplainedOutput, Base):
    """Recommandation d'optimisation, proposée à partir d'une anomalie validée et du graphe physique."""

    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    delivery_point_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_points.id"))
    drift_id: Mapped[int | None] = mapped_column(ForeignKey("drifts.id"))  # anomalie validée à l'origine
    equipment_id: Mapped[int | None] = mapped_column(ForeignKey("asset_nodes.id"))  # équipement visé
    supporting_drift_ids: Mapped[list | None] = mapped_column(JSON)  # autres anomalies validées du même motif
    kind: Mapped[RecommendationKind] = mapped_column(_enum(RecommendationKind))
    title: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(Text)  # ce qu'un humain devra faire ; la plateforme n'agit jamais
    status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus), default=ReviewStatus.PROPOSED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)
    applied_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_comment: Mapped[str | None] = mapped_column(Text)
    # F1 « avant / après » : mesure des économies, figée au moment où un humain la valide (principe P1).
    savings_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    savings_validated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    savings_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship()
    delivery_point: Mapped[DeliveryPoint | None] = relationship()


class PredictionKind(str, enum.Enum):
    ANNUAL_CONSUMPTION = "ANNUAL_CONSUMPTION"  # projection de la consommation de l'année civile


class Prediction(ExplainedOutput, Base):
    """Prévision : visible par le client seulement une fois validée par un humain."""

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    fluid: Mapped[Fluid] = mapped_column(_enum(Fluid))
    kind: Mapped[PredictionKind] = mapped_column(_enum(PredictionKind))
    year: Mapped[int] = mapped_column()
    data_as_of: Mapped[date] = mapped_column(Date)  # dernière donnée utilisée
    measured_kwh: Mapped[float] = mapped_column(Float)  # consommé depuis le 1er janvier
    predicted_kwh: Mapped[float] = mapped_column(Float)  # total projeté sur l'année
    low_kwh: Mapped[float] = mapped_column(Float)
    high_kwh: Mapped[float] = mapped_column(Float)
    reference_kwh: Mapped[float | None] = mapped_column(Float)  # année N-1 complète, si disponible
    monthly: Mapped[list | None] = mapped_column(JSON)  # [{"month", "measured", "predicted", "reference"}]
    # Modèles comparés en validation hors échantillon : [{"family", "label", "cv_rmse", "chosen"}]
    model_comparison: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus), default=ReviewStatus.PROPOSED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship()


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
    SMTP = "SMTP"  # serveur d'envoi des e-mails de notification


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
    """Envoi d'événements (sorties validées par un humain, nouveau document) vers l'outil d'un partenaire."""

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


class OutgoingEmail(Base):
    """File d'envoi des e-mails de notification (F2 alertes, F3 rappels) : réessais comme les webhooks."""

    __tablename__ = "outgoing_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    to_address: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[DeliveryStatus] = mapped_column(_enum(DeliveryStatus), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    channel: Mapped[str | None] = mapped_column(String(16))  # « smtp » ou « outbox » (dossier local de démo)
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
