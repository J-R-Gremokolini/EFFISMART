"""Schémas d'entrée / sortie de l'API (Pydantic)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import (
    DeadlineStatus,
    DriftKind,
    DriftStatus,
    ExportFormat,
    ExportStatus,
    Fluid,
    Obligation,
    ProviderKind,
    Role,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth ------------------------------------------------------------------


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(ORMModel):
    id: int
    email: str
    role: Role
    auditor_id: int | None
    organization_id: int | None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ViewerIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


# --- Patrimoine ------------------------------------------------------------


class OrganizationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    siren: str | None = Field(default=None, pattern=r"^\d{9}$")
    address: str | None = Field(default=None, max_length=300)


class OrganizationOut(ORMModel):
    id: int
    name: str
    siren: str | None
    address: str | None
    timezone: str
    currency: str


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    surface_m2: float | None = Field(default=None, gt=0)
    is_tertiary_decret: bool = False


class DeliveryPointIn(BaseModel):
    fluid: Fluid
    external_ref: str = Field(pattern=r"^\d{14}$", description="Numéro PRM (élec.) ou PCE (gaz), 14 chiffres")
    provider: ProviderKind = ProviderKind.MOCK
    is_primary: bool = False
    power_threshold_kw: float | None = Field(default=None, gt=0)
    daily_threshold_kwh: float | None = Field(default=None, gt=0)


class ConsentOut(ORMModel):
    id: int
    delivery_point_id: int
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    scope: str
    proof_ref: str


class DeliveryPointOut(ORMModel):
    id: int
    site_id: int
    fluid: Fluid
    external_ref: str
    provider: ProviderKind
    is_primary: bool
    subscribed_power_kva: float | None
    power_threshold_kw: float | None
    daily_threshold_kwh: float | None
    has_active_consent: bool = False
    active_consent: ConsentOut | None = None


class SiteOut(ORMModel):
    id: int
    organization_id: int
    name: str
    address: str | None
    surface_m2: float | None
    is_tertiary_decret: bool


class SiteDetailOut(SiteOut):
    delivery_points: list[DeliveryPointOut] = []


class ConsentIn(BaseModel):
    """Étape de recueil du consentement (onboarding)."""

    client_authorized: bool = Field(description="Le client a autorisé EffiSmart depuis son espace Enedis/GRDF")
    scope: str = Field(default="Consommations et données contractuelles", max_length=200)
    proof_ref: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _must_be_authorized(self) -> ConsentIn:
        if not self.client_authorized:
            raise ValueError("L'autorisation explicite du client est requise")
        return self


# --- Dérives -------------------------------------------------------------------


class DriftOut(BaseModel):
    id: int
    delivery_point_id: int
    external_ref: str
    fluid: Fluid
    site_name: str
    kind: DriftKind
    day: date
    measured_value: float
    reference_value: float
    deviation_pct: float
    unit: str
    details: str
    status: DriftStatus
    comment: str | None
    detected_at: datetime


class DriftUpdate(BaseModel):
    status: DriftStatus
    comment: str | None = Field(default=None, max_length=2000)


class NotificationOut(ORMModel):
    id: int
    drift_id: int | None
    organization_id: int | None
    message: str
    created_at: datetime


# --- Réglementaire -----------------------------------------------------------------


class DeadlineIn(BaseModel):
    obligation: Obligation
    due_date: date
    notes: str | None = Field(default=None, max_length=2000)


class DeadlineUpdate(BaseModel):
    status: DeadlineStatus | None = None
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class DeadlineOut(BaseModel):
    id: int
    site_id: int
    site_name: str
    organization_id: int
    organization_name: str
    obligation: Obligation
    due_date: date
    status: DeadlineStatus
    days_left: int
    notes: str | None


class ActionLogIn(BaseModel):
    obligation: Obligation
    description: str = Field(min_length=1, max_length=2000)
    performed_at: datetime | None = None


class ActionLogOut(ORMModel):
    id: int
    site_id: int
    obligation: Obligation
    description: str
    performed_at: datetime
    performed_by: int | None


# --- Exports -----------------------------------------------------------------------


class ExportIn(BaseModel):
    period_start: date
    period_end: date
    formats: list[ExportFormat] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_period(self) -> ExportIn:
        if self.period_start > self.period_end:
            raise ValueError("La date de début doit précéder la date de fin")
        return self


class ExportJobOut(ORMModel):
    id: int
    organization_id: int
    format: ExportFormat
    period_start: date
    period_end: date
    status: ExportStatus
    factors_used: list | None
    error: str | None
    created_at: datetime
