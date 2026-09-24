"""Job quotidien : ingestion de la veille, détection des dérives, statuts des échéances.

Lancement manuel : ``python -m app.scheduler``.
Note : un seul processus applicatif doit porter le scheduler (uvicorn sans --workers).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal
from app.services import regulatory
from app.services.drift import run_detection
from app.services.ingestion import ingest_all_for_day
from app.timeutils import today_local

logger = logging.getLogger(__name__)


def run_daily_pipeline(today: date | None = None) -> None:
    today = today or today_local()
    yesterday = today - timedelta(days=1)
    with SessionLocal() as db:
        count = ingest_all_for_day(db, yesterday)
        logger.info("Pipeline quotidien : %d mesures ingérées pour le %s", count, yesterday)
        run_detection(db, yesterday)
        regulatory.refresh_statuses(db, today)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run_daily_pipeline,
        CronTrigger(hour=settings.daily_job_hour, minute=0, timezone=settings.timezone),
        id="daily_pipeline",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler démarré (pipeline quotidien à %02dh00)", settings.daily_job_hour)
    return scheduler


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_daily_pipeline()
