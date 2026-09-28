"""Chiffrement des identifiants d'intégration (clés d'API, mots de passe, secrets OAuth).

Fernet (AES-128-CBC + HMAC-SHA256). La clé vient de EFFISMART_SECRET_KEY ; à défaut, elle est
générée une fois dans `settings.secret_key_file` (mode local, dossier exclu de Git).
Les secrets déchiffrés ne sont jamais journalisés ni réaffichés dans l'interface.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SecretError(RuntimeError):
    pass


@lru_cache
def _fernet() -> Fernet:
    if settings.secret_key:
        return Fernet(settings.secret_key.encode())
    path = Path(settings.secret_key_file)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # Création exclusive, droits restreints au propriétaire quand le système le permet.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    return Fernet(path.read_bytes().strip())


def encrypt(value: dict) -> str:
    return _fernet().encrypt(json.dumps(value).encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode("ascii")))
    except InvalidToken as exc:
        raise SecretError("Identifiants illisibles : la clé de chiffrement a changé.") from exc
