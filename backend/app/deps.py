"""Dépendances FastAPI : authentification, périmètre tenant, garde-fous de rôle."""
from __future__ import annotations

import re

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Role, User
from app.repositories import TenantRepository
from app.security import decode_access_token
from app.services.validation import can_validate

_bearer = HTTPBearer(auto_error=False)
READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
# Seule exception à la lecture seule de l'espace client (principe P1) : le responsable énergie valide
# ou écarte les sorties algorithmiques de son organisation, et rien d'autre.
VALIDATION_ROUTE = re.compile(r"^/api/(drifts|recommendations|predictions)/\d+$")


def _energy_manager_validation(user: User, request: Request) -> bool:
    return bool(user.is_energy_manager) and request.method == "PATCH" and bool(VALIDATION_ROUTE.match(request.url.path))


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentification requise")
    try:
        payload = decode_access_token(credentials.credentials)
        user = db.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        user = None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton invalide ou expiré")
    # Défense en profondeur (F5) : un CLIENT_VIEWER n'emprunte jamais une méthode d'écriture,
    # quelle que soit la route — en plus des gardes explicites posées sur chaque route.
    if (user.role == Role.CLIENT_VIEWER and request.method not in READ_ONLY_METHODS
            and not _energy_manager_validation(user, request)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Espace client en lecture seule")
    return user


def get_repo(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> TenantRepository:
    return TenantRepository(db, user)


def require_roles(*roles: Role):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Action non autorisée pour ce rôle")
        return user

    return checker


# Toute route d'écriture doit dépendre de `require_writer`, ou de `require_validator` pour une décision P1.
require_writer = require_roles(Role.AUDITOR, Role.ADMIN)
# Fonctions réservées à l'auditeur (portefeuille, F3).
require_auditor = require_roles(Role.AUDITOR, Role.ADMIN)


def require_validator(user: User = Depends(get_current_user)) -> User:
    """Principe P1 : valident l'auditeur partenaire et le responsable énergie du client, jamais la plateforme."""
    if not can_validate(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Seuls l'auditeur partenaire et le responsable énergie du client valident.")
    return user
