"""Notification immédiate par e-mail (F2 alertes, F3 rappels d'échéance).

Les e-mails passent par une file d'envoi (`OutgoingEmail`), traitée dès qu'une alerte est créée :
- si l'administrateur a réglé et activé un serveur SMTP (page « Intégrations ») : envoi réel ;
- sinon, en version locale : un fichier .eml par message dans `mail_outbox_dir` (lisible, jamais envoyé) ;
- sinon : échec signalé (« aucun serveur d'e-mail configuré »), l'alerte reste dans l'application.

Chaque utilisateur peut couper ses e-mails dans « Mon compte ». Le principe P1 s'applique tel quel :
une sortie non validée n'est envoyée qu'à ses valideurs (voir `validation.notify`).
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import DeliveryStatus, IntegrationKind, OutgoingEmail, PlatformIntegration, User
from app.services import secrets as secret_store
from app.timeutils import utcnow

logger = logging.getLogger(__name__)

RETRY_DELAYS_MIN = [1, 5, 30, 120]
DEFAULT_SENDER = "alertes@effismart.local"


def enqueue(db: Session, user: User, message: str, organization_name: str | None = None) -> OutgoingEmail | None:
    """Met un e-mail en file pour l'utilisateur (sans commit) ; rien s'il a coupé les e-mails."""
    if not user.email_notifications or "@" not in (user.email or ""):
        return None
    body = (
        f"{message}\n\n"
        + (f"Client : {organization_name}\n" if organization_name else "")
        + f"Ouvrir EffiSmart : {settings.public_url}\n\n"
        "—\n"
        "La plateforme propose, l'auditeur ou le responsable énergie décide : aucune action n'est automatique.\n"
        "Vous recevez ce message car les e-mails sont activés sur votre compte EffiSmart "
        "(« Mon compte » > « Recevoir les alertes par e-mail »).\n"
    )
    email = OutgoingEmail(user_id=user.id, to_address=user.email, subject=f"[EffiSmart] {message}"[:300], body=body)
    db.add(email)
    return email


def smtp_config(db: Session) -> dict | None:
    row = db.scalar(select(PlatformIntegration).where(PlatformIntegration.kind == IntegrationKind.SMTP))
    if row is None or not row.enabled:
        return None
    values = dict(row.settings or {})
    values["password"] = secret_store.decrypt(row.secrets_encrypted).get("password", "")
    return values


def _message(email: OutgoingEmail, sender: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email.to_address
    message["Subject"] = email.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or "effismart.local")
    message.set_content(email.body)
    return message


def _connect(config: dict) -> smtplib.SMTP:
    host, port = config.get("host", ""), int(config.get("port") or 587)
    timeout = settings.integrations_http_timeout_s
    if config.get("security") == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        if config.get("security", "starttls") == "starttls":
            server.starttls(context=ssl.create_default_context())
    if config.get("username"):
        server.login(config["username"], config.get("password", ""))
    return server


def test_connection(config: dict) -> str:
    """Connexion et authentification, sans envoyer d'e-mail."""
    server = _connect(config)
    server.noop()
    server.quit()
    return f"Connexion réussie au serveur {config.get('host')}:{config.get('port')}."


def _write_outbox(email: OutgoingEmail, sender: str) -> None:
    folder = Path(settings.mail_outbox_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    (folder / f"{stamp}-{email.id:06d}.eml").write_bytes(bytes(_message(email, sender)))


def dispatch_pending(db: Session, limit: int = 100) -> tuple[int, int]:
    """Envoie les e-mails en attente dont l'heure est venue. Renvoie (envoyés, en échec définitif)."""
    pending = db.scalars(select(OutgoingEmail).where(
        OutgoingEmail.status == DeliveryStatus.PENDING, OutgoingEmail.next_attempt_at <= utcnow(),
    ).order_by(OutgoingEmail.id).limit(limit)).all()
    if not pending:
        return 0, 0
    config = smtp_config(db)
    sender = (config or {}).get("sender") or DEFAULT_SENDER
    server = None
    sent = failed = 0
    for email in pending:
        email.attempts += 1
        try:
            if config:
                server = server or _connect(config)
                server.send_message(_message(email, sender))
                email.channel = "smtp"
            elif settings.mail_outbox_dir:
                _write_outbox(email, sender)
                email.channel = "outbox"
            else:
                raise RuntimeError("aucun serveur d'e-mail configuré")
            email.status, email.sent_at, email.last_error = DeliveryStatus.SENT, utcnow(), None
            sent += 1
        except Exception as exc:  # serveur injoignable, refus… : réessai différé
            server = None
            email.last_error = f"{exc.__class__.__name__} : {exc}"[:500]
            if email.attempts >= settings.mail_max_attempts:
                email.status = DeliveryStatus.FAILED
                failed += 1
            else:
                delay = RETRY_DELAYS_MIN[min(email.attempts - 1, len(RETRY_DELAYS_MIN) - 1)]
                email.next_attempt_at = utcnow() + timedelta(minutes=delay)
        db.commit()
    if server is not None:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass
    logger.info("E-mails : %d envoyé(s), %d en échec définitif", sent, failed)
    return sent, failed


def dispatch_in_background() -> None:
    def run() -> None:
        with SessionLocal() as db:
            try:
                dispatch_pending(db)
            except Exception:
                logger.exception("Envoi des e-mails impossible")

    threading.Thread(target=run, name="effismart-emails", daemon=True).start()
