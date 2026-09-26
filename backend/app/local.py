"""Mode local 100 % Python : base SQLite dans un fichier, sans Docker ni PostgreSQL.

- `configure_local_environment()` fixe les variables EFFISMART_* par défaut ;
  à appeler AVANT tout import de `app.config`.
- `prepare_database()` crée le schéma et le jeu de démonstration au premier lancement.
- `catch_up()` remplace le scheduler : il rattrape les jours manquants depuis la
  dernière donnée (ingestion, détection des dérives, statuts des échéances).
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
    os.environ.setdefault("EFFISMART_ENABLE_SCHEDULER", "false")


def prepare_database() -> None:
    from app.db import engine
    from app.models import Base
    from app.seed import seed

    Base.metadata.create_all(engine)
    seed()


def catch_up() -> int:
    """Ingère et analyse les jours manquants pour chaque point consenti. Renvoie le nombre de jours rattrapés."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import DeliveryPoint
    from app.services import regulatory
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
            if last is None or last >= yesterday:
                continue
            start = last + timedelta(days=1)
            ingest_delivery_point(db, dp, start, yesterday)
            for day in daterange(start, yesterday):
                run_detection(db, day, delivery_point_ids=[dp.id])
                caught_up += 1
        regulatory.refresh_statuses(db)
    logger.info("Rattrapage terminé : %d jour(s) x point(s)", caught_up)
    return caught_up
