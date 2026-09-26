"""Onboarding d'un client : organisation, sites, points de livraison, accès espace client.

Utilisé par l'API et par l'interface locale Streamlit (une seule implémentation).
Les contrôles de périmètre (tenant) et de rôle restent à la charge de l'appelant.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditorClientLink,
    DeliveryPoint,
    Fluid,
    Organization,
    ProviderKind,
    Role,
    Site,
    User,
)
from app.security import hash_password
from app.services import regulatory
from app.timeutils import today_local

_DELIVERY_POINT_REF = re.compile(r"^\d{14}$")
_SIREN = re.compile(r"^\d{9}$")
MIN_PASSWORD_LENGTH = 8


class ConflictError(Exception):
    """La ressource existe déjà (e-mail, numéro PRM/PCE…)."""


def create_organization(
    db: Session, user: User, *, name: str, siren: str | None = None, address: str | None = None
) -> Organization:
    """Crée l'organisation et la rattache à l'auditeur créateur (lien daté, décision D8)."""
    name = name.strip()
    if not name:
        raise ValueError("La raison sociale est obligatoire")
    if siren and not _SIREN.match(siren):
        raise ValueError("Le SIREN doit comporter 9 chiffres")
    org = Organization(name=name, siren=siren or None, address=address or None)
    db.add(org)
    db.flush()
    if user.auditor_id is not None:
        db.add(
            AuditorClientLink(
                auditor_id=user.auditor_id, organization_id=org.id, start_date=today_local(), active=True
            )
        )
    db.commit()
    return org


def create_site(
    db: Session,
    org: Organization,
    *,
    name: str,
    address: str | None = None,
    surface_m2: float | None = None,
    is_tertiary_decret: bool = False,
) -> Site:
    """Crée le site ; s'il est assujetti au Décret Tertiaire, son échéance OPERAT est générée (F3)."""
    name = name.strip()
    if not name:
        raise ValueError("Le nom du site est obligatoire")
    if surface_m2 is not None and surface_m2 <= 0:
        raise ValueError("La surface doit être positive")
    site = Site(
        organization_id=org.id,
        name=name,
        address=address or None,
        surface_m2=surface_m2,
        is_tertiary_decret=is_tertiary_decret,
    )
    db.add(site)
    db.flush()
    regulatory.ensure_operat_deadline(db, site)
    db.commit()
    return site


def create_delivery_point(
    db: Session,
    site: Site,
    *,
    fluid: Fluid,
    external_ref: str,
    provider: ProviderKind = ProviderKind.MOCK,
    is_primary: bool = False,
    power_threshold_kw: float | None = None,
    daily_threshold_kwh: float | None = None,
) -> DeliveryPoint:
    external_ref = external_ref.strip()
    if not _DELIVERY_POINT_REF.match(external_ref):
        raise ValueError("Le numéro PRM / PCE doit comporter 14 chiffres")
    if db.scalar(select(DeliveryPoint.id).where(DeliveryPoint.external_ref == external_ref)):
        raise ConflictError("Ce point de livraison est déjà enregistré")
    dp = DeliveryPoint(
        site_id=site.id,
        fluid=fluid,
        external_ref=external_ref,
        provider=provider,
        is_primary=is_primary,
        power_threshold_kw=power_threshold_kw,
        daily_threshold_kwh=daily_threshold_kwh,
    )
    db.add(dp)
    db.commit()
    return dp


def create_viewer(db: Session, org: Organization, *, email: str, password: str) -> User:
    """Crée un compte « espace client » en lecture seule (F5)."""
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("Adresse e-mail invalide")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Le mot de passe doit comporter au moins {MIN_PASSWORD_LENGTH} caractères")
    if db.scalar(select(User.id).where(User.email == email)):
        raise ConflictError("Un compte existe déjà avec cet e-mail")
    viewer = User(
        email=email, password_hash=hash_password(password), role=Role.CLIENT_VIEWER, organization_id=org.id
    )
    db.add(viewer)
    db.commit()
    return viewer
