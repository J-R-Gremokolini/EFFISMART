"""Mode local 100 % Python : base SQLite dans un fichier, sans Docker ni PostgreSQL.

- `configure_local_environment()` fixe les variables EFFISMART_* par défaut ;
  à appeler AVANT tout import de `app.config`.
- `prepare_database()` crée le schéma (et ajoute les colonnes apparues depuis) puis le jeu de démonstration.
- `catch_up()` remplace le scheduler : il rattrape les jours manquants depuis la
  dernière donnée (ingestion, détection des dérives, statuts des échéances, envoi des webhooks).
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"

logger = logging.getLogger(__name__)


def configure_local_environment() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("EFFISMART_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'effismart.db').as_posix()}")
    os.environ.setdefault("EFFISMART_EXPORT_DIR", str(DATA_DIR / "exports"))
    os.environ.setdefault("EFFISMART_DOCUMENT_DIR", str(DATA_DIR / "documents"))
    os.environ.setdefault("EFFISMART_SECRET_KEY_FILE", str(DATA_DIR / "secret.key"))
    os.environ.setdefault("EFFISMART_MAIL_OUTBOX_DIR", str(DATA_DIR / "outbox"))
    os.environ.setdefault("EFFISMART_ENABLE_SCHEDULER", "false")


def _add_missing_columns(engine) -> None:
    """Mise à niveau légère de la base locale : ajoute les colonnes apparues depuis sa création
    (facultatives, ou obligatoires avec une valeur par défaut côté base).

    (PostgreSQL passe par les migrations Alembic ; ceci ne concerne que la base SQLite de démonstration.)
    """
    from sqlalchemy import inspect, text

    from app.models import Base

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                constraint = ""
                if not column.nullable:
                    if column.server_default is None:
                        continue
                    default = column.server_default.arg.compile(dialect=engine.dialect)
                    constraint = f" NOT NULL DEFAULT {default}"
                column_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{constraint}'))
                logger.info("Colonne ajoutée : %s.%s", table.name, column.name)


def prepare_database() -> None:
    from app.db import engine
    from app.models import Base
    from app.seed import seed

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    seed()


def catch_up() -> int:
    """Ingère et analyse les jours manquants pour chaque point consenti. Renvoie le nombre de jours rattrapés."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import DeliveryPoint
    from app.services import (
        exports,
        integrations,
        load_shift,
        mailer,
        predictions,
        quarterly_reports,
        regulatory,
        trajectory,
    )
    from app.services.consent import active_consent_clause
    from app.services.dashboard import data_as_of
    from app.services.drift import run_detection
    from app.services.ingestion import ingest_delivery_point
    from app.timeutils import daterange, yesterday_local

    yesterday = yesterday_local()
    caught_up = 0
    with SessionLocal() as db:
        for dp in db.scalars(select(DeliveryPoint).where(active_consent_clause())).all():
            last = data_as_of(db, [dp.id])
            if last is not None and last >= yesterday:
                continue
            start = last + timedelta(days=1) if last else yesterday - timedelta(days=6)
            try:
                ingest_delivery_point(db, dp, start, yesterday)
            except Exception:  # une source indisponible (API externe) ne bloque pas les autres points
                db.rollback()
                logger.exception("Rattrapage impossible pour le point #%s", dp.id)
                continue
            for day in daterange(start, yesterday):
                run_detection(db, day, delivery_point_ids=[dp.id])
                caught_up += 1
        regulatory.refresh_statuses(db)
        regulatory.send_reminders(db)
        predictions.refresh_predictions(db)
        trajectory.refresh_trajectories(db)
        load_shift.propose_load_shifts(db)
        exports.produce_automatic(db)
        quarterly_reports.generate_due(db)
        integrations.dispatch_pending(db)
        mailer.dispatch_pending(db)
    logger.info("Rattrapage terminé : %d jour(s) x point(s)", caught_up)
    return caught_up
