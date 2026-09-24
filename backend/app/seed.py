"""Seed de démonstration (brief §9) — idempotent.

1 auditeur, 3 organisations clientes, 5 sites, 8 points de livraison,
~21 mois de courbes mock (plus que les 12 mois du brief : année civile N-1
complète pour l'export OPERAT et période N-1 pour le détecteur climatique),
dont quelques anomalies (cf. DEFAULT_ANOMALIES dans app/providers/mock.py).

Usage : ``python -m app.seed``
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import (
    ActionLog,
    Auditor,
    AuditorClientLink,
    Consent,
    DeliveryPoint,
    EmissionFactor,
    Fluid,
    Obligation,
    Organization,
    ProviderKind,
    Role,
    Site,
    User,
)
from app.security import hash_password
from app.services import regulatory
from app.services.consent import grant_consent
from app.services.drift import run_detection
from app.services.ingestion import ingest_delivery_point
from app.timeutils import today_local, utcnow, yesterday_local

logger = logging.getLogger("effismart.seed")

DEMO_PASSWORD = "demo1234"

# Facteurs indicatifs issus de la Base Empreinte ADEME — à vérifier / mettre à jour
# à chaque nouvelle version de la base (la version utilisée est tracée dans les exports).
EMISSION_FACTORS = [
    (Fluid.ELEC, 0.052, date(2023, 1, 1), "Électricité — mix moyen France, consommation"),
    (Fluid.GAS, 0.227, date(2023, 1, 1), "Gaz naturel — combustion + amont, kWh PCI"),
]

ORGANIZATIONS = [
    {
        "name": "Boulangeries Martin",
        "siren": "552100554",
        "address": "12 rue de la Paix, 69002 Lyon",
        "viewer": "client@boulangeries-martin.demo",
        "sites": [
            {"name": "Atelier central", "address": "4 rue Garibaldi, 69003 Lyon", "surface_m2": 1800,
             "is_tertiary_decret": True,
             "points": [("30001000000001", Fluid.ELEC, True), ("21000000000007", Fluid.GAS, False)]},
            {"name": "Boutique Bellecour", "address": "1 place Bellecour, 69002 Lyon", "surface_m2": 240,
             "is_tertiary_decret": False,
             "points": [("30001000000002", Fluid.ELEC, True)]},
        ],
    },
    {
        "name": "Clinique du Parc",
        "siren": "383474814",
        "address": "155 boulevard Stalingrad, 69006 Lyon",
        "viewer": "client@clinique-du-parc.demo",
        "sites": [
            {"name": "Bâtiment principal", "address": "155 boulevard Stalingrad, 69006 Lyon", "surface_m2": 6500,
             "is_tertiary_decret": True,
             "points": [("30001000000003", Fluid.ELEC, True), ("21000000000008", Fluid.GAS, False)]},
        ],
    },
    {
        "name": "Logistique Rhône",
        "siren": "408168003",
        "address": "ZI Nord, 69740 Genas",
        "viewer": "client@logistique-rhone.demo",
        "sites": [
            {"name": "Entrepôt Genas", "address": "ZI Nord, 69740 Genas", "surface_m2": 12000,
             "is_tertiary_decret": True,
             "points": [("30001000000004", Fluid.ELEC, True), ("30001000000005", Fluid.ELEC, False)]},
            {"name": "Bureaux Part-Dieu", "address": "20 rue de la Villette, 69003 Lyon", "surface_m2": 1100,
             "is_tertiary_decret": True,
             "points": [("30001000000006", Fluid.ELEC, True)]},
        ],
    },
]
# Point volontairement laissé sans consentement : illustre l'étape d'onboarding (§5).
NO_CONSENT_REFS = {"30001000000004"}


def _create_structure(db: Session) -> list[DeliveryPoint]:
    for fluid, factor, valid_from, label in EMISSION_FACTORS:
        db.add(EmissionFactor(
            fluid=fluid, factor_kgco2_per_kwh=factor, valid_from=valid_from,
            source=f"ADEME Base Empreinte — {label}", version="Base Empreinte 2023 (valeur indicative seed)",
        ))

    auditor = Auditor(name="Cabinet Énergie Conseil", email="contact@energie-conseil.demo")
    db.add(auditor)
    db.flush()
    auditor_user = User(email="auditeur@effismart.demo", password_hash=hash_password(DEMO_PASSWORD),
                        role=Role.AUDITOR, auditor_id=auditor.id)
    db.add(auditor_user)
    db.add(User(email="admin@effismart.demo", password_hash=hash_password(DEMO_PASSWORD), role=Role.ADMIN))

    points: list[DeliveryPoint] = []
    for spec in ORGANIZATIONS:
        org = Organization(name=spec["name"], siren=spec["siren"], address=spec["address"])
        db.add(org)
        db.flush()
        db.add(AuditorClientLink(auditor_id=auditor.id, organization_id=org.id,
                                 start_date=date(2024, 1, 15), active=True))
        db.add(User(email=spec["viewer"], password_hash=hash_password(DEMO_PASSWORD),
                    role=Role.CLIENT_VIEWER, organization_id=org.id))
        for site_spec in spec["sites"]:
            site = Site(organization_id=org.id, name=site_spec["name"], address=site_spec["address"],
                        surface_m2=site_spec["surface_m2"], is_tertiary_decret=site_spec["is_tertiary_decret"])
            db.add(site)
            db.flush()
            for ref, fluid, is_primary in site_spec["points"]:
                dp = DeliveryPoint(site_id=site.id, fluid=fluid, external_ref=ref,
                                   provider=ProviderKind.MOCK, is_primary=is_primary)
                db.add(dp)
                points.append(dp)
            regulatory.ensure_operat_deadline(db, site)
            if site_spec["is_tertiary_decret"]:
                db.add(ActionLog(site_id=site.id, obligation=Obligation.DECRET_TERTIAIRE_OPERAT,
                                 description="Collecte des surfaces et activités pour la déclaration OPERAT",
                                 performed_at=utcnow() - timedelta(days=20), performed_by=auditor_user.id))
        # Autres obligations pour illustrer le calendrier.
        first_site = db.scalar(select(Site).where(Site.organization_id == org.id).order_by(Site.id))
        regulatory.create_deadline(db, first_site, Obligation.AUDIT_EED, today_local() + timedelta(days=45),
                                   "Audit énergétique réglementaire (tous les 4 ans)")
        regulatory.create_deadline(db, first_site, Obligation.VSME, date(today_local().year + 1, 3, 31),
                                   "Rapport de durabilité volontaire VSME (exercice N)")
    db.commit()

    for dp in points:
        if dp.external_ref in NO_CONSENT_REFS:
            continue
        grant_consent(db, dp, scope="Consommations et données contractuelles",
                      proof_ref=f"DEMO-CONSENT-{dp.external_ref}", granted_by=auditor_user.id)
    return points


def seed() -> None:
    with SessionLocal() as db:
        if db.scalar(select(Auditor.id).limit(1)):
            logger.info("Base déjà peuplée : seed ignoré.")
            return
        logger.info("Création du jeu de démonstration…")
        points = _create_structure(db)

        end = yesterday_local()
        start = end - timedelta(days=settings.backfill_days - 1)
        consented = [dp for dp in points if db.scalar(select(Consent.id).where(Consent.delivery_point_id == dp.id))]
        for dp in consented:
            ingest_delivery_point(db, dp, start, end)
        logger.info("Mesures générées du %s au %s pour %d points.", start, end, len(consented))

        day = end - timedelta(days=settings.detection_backfill_days - 1)
        while day <= end:
            run_detection(db, day)
            day += timedelta(days=1)
        regulatory.refresh_statuses(db)
        logger.info("Seed terminé. Connexion : auditeur@effismart.demo / %s", DEMO_PASSWORD)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    seed()
