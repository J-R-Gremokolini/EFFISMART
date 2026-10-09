"""Modèle de données V1 (brief §4).

Hiérarchie multi-tenant :
Auditor → (AuditorClientLink daté) → Organization → Site → DeliveryPoint → Measurement

Graphe physique d'un site (AssetNode / AssetRelation), distinct de cette hiérarchie de rangement :
Compteur → alimente → Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage

Version 2 : contrats de fourniture (F6, F12), variables d'ajustement (F7), plan 2D (F8), bibliothèque de
gestes types et scénarios (F9), rapports trimestriels (F10), trajectoires Décret Tertiaire (F11).

Module référent énergie (formation PRO-REFEI de l'ATEE) : plan d'actions chiffré et suivi (R2, R4),
auto-évaluation et socle du management de l'énergie (R3), messages de sensibilisation (R7).
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
    MODEL_DEVIATION = "MODEL_DEVIATION"  # F11 : écart au modèle de consommation (12 à 24 mois d'historique)


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
    # F7, passerelle ISO 50001 : une entreprise certifiée est exemptée de l'audit énergétique obligatoire.
    iso50001_certified_until: Mapped[date | None] = mapped_column(Date)
    # R1 : poids économique de l'énergie (formation PRO-REFEI, SP5) : chiffre d'affaires et excédent brut
    # d'exploitation (EBE) du dernier exercice, saisis par l'auditeur ou le responsable énergie.
    revenue_eur: Mapped[float | None] = mapped_column(Float)
    ebitda_eur: Mapped[float | None] = mapped_column(Float)
    finance_year: Mapped[int | None] = mapped_column()
    # R2 : paramètres de rentabilité du plan d'actions (taux d'actualisation, durée d'analyse, évolution du prix
    # de l'énergie, valorisation des CEE). Vide = valeurs par défaut de `economics.DEFAULTS`.
    economics: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
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
    # F7 : unité de production du site (ex. « tonnes de pain ») et année de la situation énergétique de
    # référence (SER, ISO 50001).
    production_unit: Mapped[str | None] = mapped_column(String(60))
    energy_baseline_year: Mapped[int | None] = mapped_column()
    # F11 : année et consommation de référence du Décret Tertiaire (déclarées sur OPERAT), en kWh d'énergie finale.
    dt_reference_year: Mapped[int | None] = mapped_column()
    dt_reference_kwh: Mapped[float | None] = mapped_column(Float)
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
    # F8 : position sur le plan 2D du site, en % de la largeur et de la hauteur ; une zone est un rectangle.
    plan_x: Mapped[float | None] = mapped_column(Float)
    plan_y: Mapped[float | None] = mapped_column(Float)
    plan_w: Mapped[float | None] = mapped_column(Float)
    plan_h: Mapped[float | None] = mapped_column(Float)
    # F12 : durée de fonctionnement quotidienne d'un équipement dont la charge peut être décalée (heures).
    shift_hours: Mapped[float | None] = mapped_column(Float)
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
    LOAD_SHIFT = "LOAD_SHIFT"  # F12 : recommandation de décalage de charge vers les heures les moins chères


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
    ENEDIS_SGE = "ENEDIS_SGE"  # décision D10 : accès industriel SGE-Tiers, référencement suivi dès la semaine 1


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


# --- Version 2 ----------------------------------------------------------------------------------------------


class TariffOption(str, enum.Enum):
    BASE = "BASE"  # prix unique
    HPHC = "HPHC"  # heures pleines / heures creuses
    TEMPO = "TEMPO"  # prix par couleur de jour (bleu, blanc, rouge) et par plage (signal RTE)
    DYNAMIC = "DYNAMIC"  # prix horaire indexé sur le marché (spot) + marge du fournisseur


class SupplyContract(Base):
    """F6 — Contrat de fourniture d'un point de livraison : la grille tarifaire qui chiffre ses consommations.

    Prix complets (fourniture, acheminement, taxes) en €/kWh, saisis par l'auditeur d'après le contrat.
    Le contrat en vigueur à une date est le plus récent dont `valid_from` la précède.
    """

    __tablename__ = "supply_contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    delivery_point_id: Mapped[int] = mapped_column(ForeignKey("delivery_points.id"), index=True)
    supplier: Mapped[str] = mapped_column(String(120))
    option: Mapped[TariffOption] = mapped_column(_enum(TariffOption))
    subscription_eur_month: Mapped[float] = mapped_column(Float, default=0.0)
    price_base: Mapped[float | None] = mapped_column(Float)
    price_hp: Mapped[float | None] = mapped_column(Float)
    price_hc: Mapped[float | None] = mapped_column(Float)
    offpeak_start_hour: Mapped[int] = mapped_column(default=22)
    offpeak_end_hour: Mapped[int] = mapped_column(default=6)
    tempo_prices: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))  # {"BLEU": {"HP": …, "HC": …}, …}
    dynamic_margin_eur_kwh: Mapped[float | None] = mapped_column(Float)
    valid_from: Mapped[date] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    delivery_point: Mapped[DeliveryPoint] = relationship()


class AdjustmentKind(str, enum.Enum):
    PRODUCTION = "PRODUCTION"  # unités produites, dans l'unité propre au site
    HEADCOUNT = "HEADCOUNT"  # effectif en équivalents temps plein (ETP)


class AdjustmentVariable(Base):
    """F7 — Variable d'ajustement mensuelle fournie par le client (production, effectif)."""

    __tablename__ = "adjustment_variables"
    __table_args__ = (UniqueConstraint("site_id", "kind", "month", name="uq_adjustment_site_kind_month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    kind: Mapped[AdjustmentKind] = mapped_column(_enum(AdjustmentKind))
    month: Mapped[date] = mapped_column(Date)  # premier jour du mois
    value: Mapped[float] = mapped_column(Float)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SitePlan(Base):
    """F8 — Plan 2D d'un site (image facultative) ; les éléments du graphe P2 y sont positionnés."""

    __tablename__ = "site_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), unique=True)
    file_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(64))
    stored_name: Mapped[str] = mapped_column(String(80))
    aspect_ratio: Mapped[float] = mapped_column(Float, default=0.62)  # hauteur / largeur
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionTemplate(Base):
    """F9 — Geste type de la bibliothèque : économie relative sur un usage et investissement indicatif.

    `auditor_id` vide : bibliothèque commune ; sinon, geste propre au cabinet.
    """

    __tablename__ = "action_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    auditor_id: Mapped[int | None] = mapped_column(ForeignKey("auditors.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    usage: Mapped[str] = mapped_column(String(16))  # catégorie d'usage du graphe P2 (HEATING, LIGHTING…)
    savings_pct: Mapped[float] = mapped_column(Float)  # part de la consommation de l'usage économisée (0 à 1)
    investment_eur_m2: Mapped[float] = mapped_column(Float, default=0.0)
    investment_eur: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text)
    # R6 : famille d'actions du plan de préconisations, fiche CEE de référence et durée de vie (calcul de la VAN).
    category: Mapped[str | None] = mapped_column(String(32))
    cee_sheet: Mapped[str | None] = mapped_column(String(20))
    lifetime_years: Mapped[int | None] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SavingsScenario(Base):
    """F9 — Scénario d'actions simulé pour un site ; le scénario retenu alimente la trajectoire (F11)."""

    __tablename__ = "savings_scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    template_ids: Mapped[list] = mapped_column(JSON)
    recommendation_ids: Mapped[list] = mapped_column(JSON)
    usage_shares: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))  # répartitions ajustées
    results: Mapped[dict] = mapped_column(JSON)  # résultats au moment de l'enregistrement
    retained: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    site: Mapped[Site] = relationship()


class Trajectory(ExplainedOutput, Base):
    """F11 — Trajectoire Décret Tertiaire d'un site : où mène le rythme actuel en 2030, 2040, 2050.

    Projection (principe P1) : visible par le client seulement une fois validée par un humain.
    """

    __tablename__ = "trajectories"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    data_as_of: Mapped[date] = mapped_column(Date)
    reference_year: Mapped[int] = mapped_column()
    reference_kwh: Mapped[float] = mapped_column(Float)
    current_kwh: Mapped[float] = mapped_column(Float)  # 12 derniers mois, corrigés du climat
    annual_rate: Mapped[float] = mapped_column(Float)  # évolution annuelle moyenne depuis la référence
    projected_2030_kwh: Mapped[float] = mapped_column(Float)
    reduction_2030: Mapped[float] = mapped_column(Float)  # baisse projetée par rapport à la référence
    reduction_2030_with_actions: Mapped[float | None] = mapped_column(Float)
    points: Mapped[list] = mapped_column(JSON)  # [{"year", "trend", "objective", "with_actions"}]
    status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus), default=ReviewStatus.PROPOSED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship()


class ReportStatus(str, enum.Enum):
    DRAFT = "DRAFT"  # projet produit par la plateforme, à relire et à enrichir
    VALIDATED = "VALIDATED"  # relu, enrichi et validé par l'auditeur
    DELIVERED = "DELIVERED"  # délivré au client, sous l'identité du cabinet


class QuarterlyReport(Base):
    """F10 — Rapport d'analyse trimestrielle (décision D2, option A) : outil de productivité de l'auditeur.

    La plateforme en produit le projet ; l'auditeur l'enrichit, le valide et le délivre sous sa propre identité.
    EffiSmart ne délivre jamais d'analyse en direct au client final.
    """

    __tablename__ = "quarterly_reports"
    __table_args__ = (UniqueConstraint("organization_id", "year", "quarter", name="uq_report_org_quarter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    auditor_id: Mapped[int | None] = mapped_column(ForeignKey("auditors.id"))  # cabinet signataire
    year: Mapped[int] = mapped_column()
    quarter: Mapped[int] = mapped_column()
    status: Mapped[ReportStatus] = mapped_column(_enum(ReportStatus), default=ReportStatus.DRAFT)
    sections: Mapped[list] = mapped_column(JSON)  # [{"key", "title", "platform_text", "rows", "auditor_text"}]
    introduction: Mapped[str | None] = mapped_column(Text)  # synthèse de l'auditeur
    conclusion: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    validated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship()


# --- F7 : IPE personnalisés et proposés par l'IA -----------------------------------------------------------


class IpeKind(str, enum.Enum):
    RATIO = "RATIO"  # consommation / facteur (kWh par unité)
    MODEL = "MODEL"  # consommation mesurée / consommation modélisée (ISO 50006), base 100


class IpeVariable(Base):
    """Variable d'ajustement personnalisée d'un site (repas servis, nuitées, heures d'ouverture…)."""

    __tablename__ = "ipe_variables"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(40))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IpeVariableValue(Base):
    __tablename__ = "ipe_variable_values"
    __table_args__ = (UniqueConstraint("variable_id", "month", name="uq_ipe_variable_month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    variable_id: Mapped[int] = mapped_column(ForeignKey("ipe_variables.id"), index=True)
    month: Mapped[date] = mapped_column(Date)  # premier jour du mois
    value: Mapped[float] = mapped_column(Float)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IpeDefinition(ExplainedOutput, Base):
    """IPE d'un site : créé par un humain (actif d'emblée) ou proposé par l'IA de la plateforme (à valider, P1).

    `drivers` : facteurs explicatifs, parmi SURFACE, DJU, DJF, WORKDAYS, PRODUCTION, HEADCOUNT et « VAR:<id> »
    (variable personnalisée). `model` : coefficients de la régression (IPE modélisé) et ses statistiques.
    """

    __tablename__ = "ipe_definitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    energy: Mapped[str] = mapped_column(String(8))  # ALL, ELEC ou GAS
    kind: Mapped[IpeKind] = mapped_column(_enum(IpeKind))
    drivers: Mapped[list] = mapped_column(JSON)
    model: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    origin: Mapped[str] = mapped_column(String(16))  # USER (créé par un humain) ou PLATFORM (proposé par l'IA)
    status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus), default=ReviewStatus.PROPOSED)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)
    # R6 : valeur cible fixée par un humain et seuil d'alerte autour de la cible (formation PRO-REFEI, SP5 :
    # « seuil d'alerte à +10 % ; si dépassement, le référent énergie intervient pour en analyser la cause »).
    target_value: Mapped[float | None] = mapped_column(Float)
    alert_threshold_pct: Mapped[float | None] = mapped_column(Float)

    site: Mapped[Site] = relationship()


# --- Module référent énergie (formation PRO-REFEI de l'ATEE) ---------------------------------------------------


class ActionStatus(str, enum.Enum):
    """Avancement d'une action du plan (SP5 : suivre l'avancement, clôturer, ouvrir les actions nouvelles)."""

    IDENTIFIED = "IDENTIFIED"  # gisement identifié, pas encore décidé
    PLANNED = "PLANNED"  # décidée : un responsable, une échéance
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"  # réalisée, déclarée par un humain
    VERIFIED = "VERIFIED"  # économies vérifiées (plan de mesure et vérification)
    ABANDONED = "ABANDONED"


class EnergyAction(Base):
    """R2 — Action du plan d'actions du référent énergie : chiffrée, hiérarchisée, planifiée et suivie.

    Créée par un humain (auditeur ou responsable énergie), éventuellement à partir d'un geste type (F9) ou
    d'une recommandation validée. Économies annuelles par énergie dans `savings` ({"ELEC": kWh, "GAS": kWh},
    valeur négative = surconsommation). Fiche QQOQPCC (SP8) et plan de mesure et vérification IPMVP (SP5).
    """

    __tablename__ = "energy_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))  # Quoi ?
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))  # famille : éclairage, air comprimé, froid…
    nature: Mapped[str] = mapped_column(String(32))  # conception, technique, pilotage-maintenance, organisationnelle
    priority: Mapped[str] = mapped_column(String(16), default="UNRANKED")  # prioritaire, ambitieuse, très ambitieuse
    horizon: Mapped[str] = mapped_column(String(16), default="SHORT")  # court terme, moyen terme, pour mémoire
    savings: Mapped[dict] = mapped_column(JSON)
    recurring_eur: Mapped[float] = mapped_column(Float, default=0.0)  # gain (+) ou coût (−) récurrent non énergétique
    investment_eur: Mapped[float] = mapped_column(Float, default=0.0)
    # Situation de référence (SP3) : coût de la solution qu'on aurait de toute façon payée (ex. rebobiner un moteur
    # en fin de vie). La rentabilité porte alors sur le surinvestissement : investissement − référence.
    reference_investment_eur: Mapped[float] = mapped_column(Float, default=0.0)
    lifetime_years: Mapped[int] = mapped_column(default=10)
    cee_kwh_cumac: Mapped[float | None] = mapped_column(Float)
    cee_sheet: Mapped[str | None] = mapped_column(String(20))
    # Cotation (SP5, exemple WinErgia) : notes de 1 (nul) à 4 (fort) ; co-bénéfices (SP4, autres critères).
    score_economic: Mapped[int | None] = mapped_column()
    score_technical: Mapped[int | None] = mapped_column()
    score_risk: Mapped[int | None] = mapped_column()
    cobenefits: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    # QQOQPCC (SP8) : un seul responsable, une échéance datée (jamais « asap »).
    owner: Mapped[str | None] = mapped_column(String(120))  # Qui ?
    location: Mapped[str | None] = mapped_column(String(200))  # Où ?
    equipment_id: Mapped[int | None] = mapped_column(ForeignKey("asset_nodes.id"))
    due_date: Mapped[date | None] = mapped_column(Date)  # Quand ?
    why: Mapped[str | None] = mapped_column(Text)  # Pourquoi ?
    how_much: Mapped[str | None] = mapped_column(Text)  # Combien ? (quantités)
    how: Mapped[str | None] = mapped_column(Text)  # Comment ? (méthode, rétroplanning)
    contacts: Mapped[list | None] = mapped_column(JSON(none_as_null=True))  # [{"who", "channel", "when", "purpose"}]
    needs: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))  # {"training", "documentation", "monitoring"}
    status: Mapped[ActionStatus] = mapped_column(_enum(ActionStatus), default=ActionStatus.IDENTIFIED)
    progress_note: Mapped[str | None] = mapped_column(Text)
    done_on: Mapped[date | None] = mapped_column(Date)
    ipe_definition_id: Mapped[int | None] = mapped_column(ForeignKey("ipe_definitions.id"))  # IPE de vérification
    recommendation_id: Mapped[int | None] = mapped_column(ForeignKey("recommendations.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("action_templates.id"))
    origin: Mapped[str] = mapped_column(String(16), default="USER")  # USER, GESTURE ou RECOMMENDATION
    mv_plan: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))  # plan de M&V IPMVP en 13 points
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship()


class MaturityAssessment(Base):
    """R3 — Auto-évaluation de la démarche de management de l'énergie (5 axes PDCA de l'ISO 50001).

    Réponses d'un humain : 2 (tout à fait), 1 (partiellement), 0 (pas du tout), −1 (non applicable).
    Les évaluations successives sont conservées : elles montrent la progression (amélioration continue).
    """

    __tablename__ = "maturity_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    answers: Mapped[dict] = mapped_column(JSON)  # {code de la question: note}
    comments: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    scores: Mapped[dict] = mapped_column(JSON)  # {"axes": {…}, "themes": {…}, "global": …}
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EnergyManagement(Base):
    """R3 — Socle du management de l'énergie d'une organisation : politique, équipe énergie, objectifs.

    `team` : [{"name", "role", "missions"}] ; `objectives` : [{"label", "indicator", "baseline", "target",
    "deadline", "owner", "ipe_definition_id"}] (objectifs SMART, SP5).
    """

    __tablename__ = "energy_management"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), unique=True)
    policy: Mapped[str | None] = mapped_column(Text)
    policy_approved_on: Mapped[date | None] = mapped_column(Date)  # validation par la direction
    scope: Mapped[str | None] = mapped_column(Text)  # domaine d'application et périmètre
    team: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    objectives: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    review_months: Mapped[int | None] = mapped_column()  # périodicité de la revue énergétique
    last_review_on: Mapped[date | None] = mapped_column(Date)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommunicationStatus(str, enum.Enum):
    DRAFT = "DRAFT"  # préparé par la plateforme d'après les données, à relire
    APPROVED = "APPROVED"  # relu et approuvé par un humain : diffusable
    ARCHIVED = "ARCHIVED"


class CommunicationMessage(Base):
    """R7 — Message de sensibilisation (SP6) : un message à la fois, chiffré, qui appelle à agir.

    Préparé par la plateforme d'après les données du site ; un humain le relit, l'ajuste et l'approuve avant
    toute diffusion (affichage, écran d'information). Principe P1 : jamais diffusé sans relecture.
    """

    __tablename__ = "communication_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    theme: Mapped[str] = mapped_column(String(32))  # BASELOAD, SAVINGS, ACTION, IPE, COMPRESSED_AIR…
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    call_to_action: Mapped[str] = mapped_column(String(200))
    figures: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))  # chiffres sources, pour la traçabilité
    status: Mapped[CommunicationStatus] = mapped_column(_enum(CommunicationStatus), default=CommunicationStatus.DRAFT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship()
