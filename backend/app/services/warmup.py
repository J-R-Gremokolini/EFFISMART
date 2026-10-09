"""Préchauffage en arrière-plan : les calculs lourds sont faits dès le démarrage, pas au premier clic.

Au lancement de l'interface (après la mise à niveau de la base et le rattrapage des données), un fil d'exécution
parcourt les clients et prépare ce que leurs pages demanderont : chiffrage mensuel des 12 et 24 derniers mois,
bilan énergétique, puissance souscrite, puis apprentissage des modèles de consommation (répartition par usage).
Les résultats rejoignent la mémoire des calculs (`memo`, modèles de `consumption_model`) : la première visite d'une
page est alors aussi rapide que les suivantes. Lecture seule ; une erreur ne bloque ni l'interface ni le fil.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import DeliveryPoint, Organization, Site
from app.services import action_plan, energy_balance, integrations, simulator, tariffs
from app.services.dashboard import data_as_of
from app.timeutils import add_months, month_start, yesterday_local

logger = logging.getLogger(__name__)
MONTHLY_SPANS = (12, 24)  # périodes proposées par la page « Suivi mensuel »
_started = threading.Event()


def _org_end(db: Session, org: Organization) -> date:
    points = list(db.scalars(select(DeliveryPoint).join(Site, DeliveryPoint.site_id == Site.id)
                             .where(Site.organization_id == org.id)))
    return data_as_of(db, [dp.id for dp in points]) or yesterday_local()


def _quietly(label: str, step) -> None:
    try:
        step()
    except Exception:  # un préchauffage raté ne doit jamais gêner l'interface
        logger.exception("Préchauffage : échec de « %s » (sans conséquence)", label)


def warm(db: Session) -> dict[str, float]:
    """Prépare les calculs de tous les clients ; renvoie la durée de chaque phase (secondes)."""
    durations: dict[str, float] = {}
    organizations = list(db.scalars(select(Organization).order_by(Organization.id)))
    start = time.perf_counter()
    for org in organizations:  # phase rapide d'abord : chiffrages et bilans (sans apprentissage)
        end = _org_end(db, org)
        for span in MONTHLY_SPANS:
            _quietly(f"suivi mensuel {org.name}", lambda: tariffs.monthly_triaxis(
                db, org, add_months(month_start(end), -(span - 1)), end))
        _quietly(f"bilan {org.name}", lambda: energy_balance.organization_balance(db, org))
        for site in org.sites:
            _quietly(f"bilan {site.name}", lambda: energy_balance.site_balance(db, site))
            _quietly(f"prix {site.name}", lambda: action_plan.site_prices(db, site))
            _quietly(f"puissance {site.name}", lambda: energy_balance.power_checks(db, site))
    durations["chiffrages"] = time.perf_counter() - start
    start = time.perf_counter()
    weather = integrations.weather_provider(db)
    for org in organizations:  # phase lente : modèles de consommation (répartition par usage, simulateur, bilan)
        for site in org.sites:
            _quietly(f"usages {site.name}", lambda: simulator.breakdown(db, site, weather))
    durations["modèles"] = time.perf_counter() - start
    return durations


def _run() -> None:
    try:
        with SessionLocal() as db:
            durations = warm(db)
        logger.info("Préchauffage terminé : %s.", ", ".join(f"{k} {v:.0f} s" for k, v in durations.items()))
    except Exception:
        logger.exception("Préchauffage interrompu (sans conséquence)")


def start() -> threading.Thread | None:
    """Lance le préchauffage une seule fois par processus, en arrière-plan."""
    if _started.is_set():
        return None
    _started.set()
    thread = threading.Thread(target=_run, name="effismart-prechauffage", daemon=True)
    thread.start()
    return thread
