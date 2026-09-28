"""Seed de démonstration (brief §9) — idempotent.

1 auditeur, 3 organisations clientes, 5 sites, 8 points de livraison,
~21 mois de courbes mock (plus que les 12 mois du brief : année civile N-1
complète pour l'export OPERAT et période N-1 pour le détecteur climatique),
dont quelques anomalies (cf. DEFAULT_ANOMALIES dans app/providers/mock.py).

Principes ajoutés après le brief :
- graphe physique des équipements pour 3 sites (les Boulangeries Martin restent sans graphe,
  pour montrer une recommandation « identifier la cause ») ;
- un responsable énergie (Clinique du Parc) qui valide les sorties de la plateforme ;
- toutes les sorties (anomalies, prévisions) sont créées « à valider » : aucune validation simulée.

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
    AssetNode,
    AssetNodeKind,
    AssetRelation,
    AssetRelationKind,
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
from app.services import assets, drift_explanations, predictions, recommendations, regulatory
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
# Responsable énergie de démonstration : valide ou écarte les sorties de la plateforme (principe P1).
DEMO_ENERGY_MANAGERS = {"Clinique du Parc": "energie@clinique-du-parc.demo"}

# Graphe physique de démonstration, par organisation et site. Les puissances sont cohérentes avec les
# profils simulés : le groupe froid de la clinique (55 kW) correspond à l'excès du week-end (~50 kW).
# Nœud : (clé, type, catégorie, nom, puissance kW, surface m², occupée 24 h/24).
# Relation : (source, relation, cible) ; « meter:<PRM/PCE> » désigne le compteur d'un point de livraison.
K, R = AssetNodeKind, AssetRelationKind
DEMO_ASSETS = {
    ("Clinique du Parc", "Bâtiment principal"): {
        "nodes": [
            ("chaudiere", K.EQUIPMENT, "BOILER", "Chaudière gaz à condensation", 400, None, False),
            ("groupe_froid", K.EQUIPMENT, "CHILLER", "Groupe froid", 55, None, False),
            ("cta_bloc", K.EQUIPMENT, "AHU", "CTA bloc opératoire", 15, None, False),
            ("cta_admin", K.EQUIPMENT, "AHU", "CTA administration", 7.5, None, False),
            ("radiateurs", K.EQUIPMENT, "TERMINAL", "Radiateurs des chambres", None, None, False),
            ("ballon_ecs", K.EQUIPMENT, "DHW_TANK", "Ballon d'eau chaude sanitaire", None, None, False),
            ("eclairage", K.EQUIPMENT, "LIGHTING", "Éclairage", 20, None, False),
            ("serveurs", K.EQUIPMENT, "IT", "Salle serveurs", 8, None, False),
            ("eau_chaude", K.FLOW, "HOT_WATER", "Eau chaude de chauffage", None, None, False),
            ("eau_ecs", K.FLOW, "DHW", "Eau chaude sanitaire", None, None, False),
            ("eau_glacee", K.FLOW, "CHILLED_WATER", "Eau glacée", None, None, False),
            ("z_bloc", K.ZONE, "CARE", "Bloc opératoire", None, 450, False),
            ("z_chambres", K.ZONE, "CARE", "Chambres", None, 3200, True),
            ("z_admin", K.ZONE, "OFFICE", "Administration", None, 900, False),
            ("z_serveurs", K.ZONE, "TECHNICAL", "Local serveurs", None, 20, True),
            ("u_chauffage", K.USAGE, "HEATING", "Chauffage", None, None, False),
            ("u_froid", K.USAGE, "COOLING", "Refroidissement", None, None, False),
            ("u_ventilation", K.USAGE, "VENTILATION", "Ventilation", None, None, False),
            ("u_ecs", K.USAGE, "DHW", "Eau chaude sanitaire", None, None, False),
            ("u_eclairage", K.USAGE, "LIGHTING", "Éclairage", None, None, False),
            ("u_informatique", K.USAGE, "IT", "Informatique", None, None, False),
        ],
        "relations": [
            ("meter:21000000000008", R.SUPPLIES, "chaudiere"),
            ("chaudiere", R.PRODUCES, "eau_chaude"),
            ("chaudiere", R.PRODUCES, "eau_ecs"),
            ("eau_chaude", R.SUPPLIES, "cta_bloc"),
            ("eau_chaude", R.SUPPLIES, "cta_admin"),
            ("eau_chaude", R.SUPPLIES, "radiateurs"),
            ("eau_ecs", R.SUPPLIES, "ballon_ecs"),
            ("meter:30001000000003", R.SUPPLIES, "groupe_froid"),
            ("meter:30001000000003", R.SUPPLIES, "cta_bloc"),
            ("meter:30001000000003", R.SUPPLIES, "cta_admin"),
            ("meter:30001000000003", R.SUPPLIES, "eclairage"),
            ("meter:30001000000003", R.SUPPLIES, "serveurs"),
            ("groupe_froid", R.PRODUCES, "eau_glacee"),
            ("eau_glacee", R.SUPPLIES, "cta_bloc"),
            ("eau_glacee", R.SUPPLIES, "cta_admin"),
            ("cta_bloc", R.SERVES, "z_bloc"),
            ("cta_admin", R.SERVES, "z_admin"),
            ("radiateurs", R.SERVES, "z_chambres"),
            ("ballon_ecs", R.SERVES, "z_chambres"),
            ("eclairage", R.SERVES, "z_bloc"),
            ("eclairage", R.SERVES, "z_chambres"),
            ("eclairage", R.SERVES, "z_admin"),
            ("serveurs", R.SERVES, "z_serveurs"),
            ("z_bloc", R.HOSTS, "u_chauffage"),
            ("z_bloc", R.HOSTS, "u_froid"),
            ("z_bloc", R.HOSTS, "u_ventilation"),
            ("z_bloc", R.HOSTS, "u_eclairage"),
            ("z_chambres", R.HOSTS, "u_chauffage"),
            ("z_chambres", R.HOSTS, "u_ecs"),
            ("z_chambres", R.HOSTS, "u_eclairage"),
            ("z_admin", R.HOSTS, "u_chauffage"),
            ("z_admin", R.HOSTS, "u_froid"),
            ("z_admin", R.HOSTS, "u_ventilation"),
            ("z_admin", R.HOSTS, "u_eclairage"),
            ("z_serveurs", R.HOSTS, "u_informatique"),
        ],
    },
    ("Logistique Rhône", "Entrepôt Genas"): {
        "nodes": [
            ("chargeurs", K.EQUIPMENT, "CHARGING", "Chargeurs des chariots élévateurs", 140, None, False),
            ("eclairage", K.EQUIPMENT, "LIGHTING", "Éclairage de l'entrepôt", 35, None, False),
            ("aerothermes", K.EQUIPMENT, "TERMINAL", "Aérothermes électriques", 40, None, False),
            ("froid", K.EQUIPMENT, "REFRIGERATION", "Groupes frigorifiques des quais", 50, None, False),
            ("z_stockage", K.ZONE, "STORAGE", "Zone de stockage", None, 10500, False),
            ("z_charge", K.ZONE, "TECHNICAL", "Local de charge", None, 150, False),
            ("z_quais", K.ZONE, "STORAGE", "Quais frigorifiques", None, 1350, True),
            ("u_eclairage", K.USAGE, "LIGHTING", "Éclairage", None, None, False),
            ("u_chauffage", K.USAGE, "HEATING", "Chauffage", None, None, False),
            ("u_recharge", K.USAGE, "PROCESS", "Recharge des chariots", None, None, False),
            ("u_froid", K.USAGE, "REFRIGERATION", "Froid alimentaire", None, None, False),
        ],
        "relations": [
            ("meter:30001000000005", R.SUPPLIES, "chargeurs"),
            ("meter:30001000000005", R.SUPPLIES, "eclairage"),
            ("meter:30001000000005", R.SUPPLIES, "aerothermes"),
            ("meter:30001000000004", R.SUPPLIES, "froid"),
            ("chargeurs", R.SERVES, "z_charge"),
            ("eclairage", R.SERVES, "z_stockage"),
            ("aerothermes", R.SERVES, "z_stockage"),
            ("froid", R.SERVES, "z_quais"),
            ("z_stockage", R.HOSTS, "u_eclairage"),
            ("z_stockage", R.HOSTS, "u_chauffage"),
            ("z_charge", R.HOSTS, "u_recharge"),
            ("z_quais", R.HOSTS, "u_froid"),
        ],
    },
    ("Logistique Rhône", "Bureaux Part-Dieu"): {
        "nodes": [
            ("pac", K.EQUIPMENT, "HEAT_PUMP", "Pompe à chaleur réversible", 60, None, False),
            ("ventilos", K.EQUIPMENT, "TERMINAL", "Ventilo-convecteurs", None, None, False),
            ("cta", K.EQUIPMENT, "AHU", "CTA double flux", 7, None, False),
            ("eclairage", K.EQUIPMENT, "LIGHTING", "Éclairage des bureaux", 15, None, False),
            ("eau", K.FLOW, "HOT_WATER", "Eau de chauffage et de climatisation", None, None, False),
            ("z_bureaux", K.ZONE, "OFFICE", "Plateaux de bureaux", None, 1100, False),
            ("u_chauffage", K.USAGE, "HEATING", "Chauffage", None, None, False),
            ("u_froid", K.USAGE, "COOLING", "Climatisation", None, None, False),
            ("u_ventilation", K.USAGE, "VENTILATION", "Ventilation", None, None, False),
            ("u_eclairage", K.USAGE, "LIGHTING", "Éclairage", None, None, False),
        ],
        "relations": [
            ("meter:30001000000006", R.SUPPLIES, "pac"),
            ("meter:30001000000006", R.SUPPLIES, "cta"),
            ("meter:30001000000006", R.SUPPLIES, "eclairage"),
            ("pac", R.PRODUCES, "eau"),
            ("eau", R.SUPPLIES, "ventilos"),
            ("ventilos", R.SERVES, "z_bureaux"),
            ("cta", R.SERVES, "z_bureaux"),
            ("eclairage", R.SERVES, "z_bureaux"),
            ("z_bureaux", R.HOSTS, "u_chauffage"),
            ("z_bureaux", R.HOSTS, "u_froid"),
            ("z_bureaux", R.HOSTS, "u_ventilation"),
            ("z_bureaux", R.HOSTS, "u_eclairage"),
        ],
    },
}


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
    # Graphe physique avant la détection : les explications des anomalies citent les équipements.
    assets.sync_meter_nodes(db)
    _create_demo_assets(db)

    for dp in points:
        if dp.external_ref in NO_CONSENT_REFS:
            continue
        grant_consent(db, dp, scope="Consommations et données contractuelles",
                      proof_ref=f"DEMO-CONSENT-{dp.external_ref}", granted_by=auditor_user.id)
    return points


def _create_demo_assets(db: Session) -> int:
    """Graphe physique des sites de démonstration qui n'en ont pas encore (idempotent)."""
    created = 0
    for (org_name, site_name), spec in DEMO_ASSETS.items():
        site = db.scalar(select(Site).join(Organization, Site.organization_id == Organization.id)
                         .where(Organization.name == org_name, Site.name == site_name))
        if site is None or db.scalar(select(AssetNode.id).where(
                AssetNode.site_id == site.id, AssetNode.kind != AssetNodeKind.METER).limit(1)):
            continue
        keys = {f"meter:{node.delivery_point.external_ref}": node for node in db.scalars(
            select(AssetNode).where(AssetNode.site_id == site.id, AssetNode.kind == AssetNodeKind.METER))}
        for key, kind, category_code, name, power, surface, always in spec["nodes"]:
            keys[key] = AssetNode(organization_id=site.organization_id, site_id=site.id, kind=kind,
                                  category=category_code, name=name, power_kw=power, surface_m2=surface,
                                  always_occupied=always)
            db.add(keys[key])
        db.flush()
        for source, relation, target in spec["relations"]:
            if source in keys and target in keys:
                db.add(AssetRelation(organization_id=site.organization_id, source_id=keys[source].id,
                                     target_id=keys[target].id, kind=relation))
        created += 1
    db.commit()
    return created


def _ensure_demo_energy_managers(db: Session) -> None:
    for org_name, email in DEMO_ENERGY_MANAGERS.items():
        org = db.scalar(select(Organization).where(Organization.name == org_name))
        if org is not None and not db.scalar(select(User.id).where(User.email == email)):
            db.add(User(email=email, password_hash=hash_password(DEMO_PASSWORD), role=Role.CLIENT_VIEWER,
                        organization_id=org.id, is_energy_manager=True))
    db.commit()


def upgrade(db: Session) -> None:
    """Mise à niveau idempotente d'une base existante (principes P1 et graphe physique)."""
    created = assets.sync_meter_nodes(db)
    if created:
        logger.info("%d compteur(s) ajouté(s) au graphe physique.", created)
    if db.scalar(select(Organization.id).where(Organization.name.in_([o["name"] for o in ORGANIZATIONS])).limit(1)):
        _create_demo_assets(db)
        _ensure_demo_energy_managers(db)
    explained = drift_explanations.backfill_explanations(db)
    if explained:
        logger.info("%d anomalie(s) existante(s) expliquée(s).", explained)
    proposed = recommendations.propose_missing(db)
    if proposed:
        logger.info("%d recommandation(s) proposée(s) pour des anomalies déjà validées.", proposed)
    predictions.refresh_predictions(db)


def seed() -> None:
    with SessionLocal() as db:
        if db.scalar(select(Auditor.id).limit(1)):
            logger.info("Base déjà peuplée : seed ignoré, mise à niveau seulement.")
            upgrade(db)
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
        upgrade(db)
        logger.info("Seed terminé. Connexion : auditeur@effismart.demo / %s", DEMO_PASSWORD)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    seed()
