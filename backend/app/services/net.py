"""Appels HTTP sortants des intégrations, avec protection contre les requêtes détournées (SSRF).

Règles appliquées à toute URL fournie par un utilisateur (connecteur, webhook) :
- HTTPS obligatoire, pas d'identifiants dans l'URL ;
- l'hôte doit se résoudre uniquement vers des adresses publiques (pas de réseau interne, de boucle
  locale, de métadonnées cloud 169.254.169.254…) — vérifié à l'enregistrement ET avant chaque appel ;
- les redirections ne sont pas suivies (elles pourraient pointer vers une adresse interne).

Les tests remplacent le transport réseau (`set_transport`) : aucune résolution DNS n'est alors faite.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from app.config import settings

_transport: httpx.BaseTransport | None = None


class UnsafeUrlError(ValueError):
    pass


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Réservé aux tests : remplace le réseau par un transport simulé."""
    global _transport
    _transport = transport


def check_public_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise UnsafeUrlError("L'adresse doit commencer par https://.")
    if not parts.hostname:
        raise UnsafeUrlError("L'adresse ne contient pas de nom d'hôte.")
    if parts.username or parts.password:
        raise UnsafeUrlError("N'incluez pas d'identifiants dans l'adresse : utilisez l'authentification prévue.")
    if _transport is not None or settings.integrations_allow_private_urls:
        return url.strip()
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Hôte introuvable : {parts.hostname}.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise UnsafeUrlError("Cette adresse pointe vers un réseau privé ou local : elle est refusée.")
    return url.strip()


def client() -> httpx.Client:
    return httpx.Client(
        timeout=settings.integrations_http_timeout_s,
        follow_redirects=False,
        transport=_transport,
        headers={"User-Agent": "EffiSmart/1.0"},
    )
