"""Seed de démonstration (brief §9) — idempotent.

1 auditeur, 3 organisations clientes, 5 sites, 8 points de livraison,
~21 mois de courbes mock (plus que les 12 mois du brief : année civile N-1
complète pour l'export OPERAT et période N-1 pour le détecteur climatique),
dont quelques anomalies (cf. DEFAULT_ANOMALIES dans app/providers/mock.py).

Principes ajoutés après le brief :
- graphe physique des équipements pour 3 sites (les Boulangeries Martin restent sans graphe,
  pour montrer une recommandation « identifier la cause ») ;
- un responsable énergie (Clinique du Parc) qui valide les sorties de la plateforme ;
- toutes les sorties (anomalies, prévisions) sont créées « à valider ». Seule exception, signalée comme
  « Historique de démonstration » : l'éclairage de l'entrepôt de Genas, resté allumé la nuit de novembre
  2025 à avril 2026, dont l'anomalie a été validée et la recommandation appliquée le 6 avril 2026 ; il
  sert à montrer la mesure des économies avant / après (F1).

Usage : ``python -m app.seed``
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import (
    ActionLog,
    AdjustmentKind,
    AdjustmentVariable,
    AssetNode,
    AssetNodeKind,
    AssetRelation,
    AssetRelationKind,
    Auditor,
    AuditorClientLink,
    Consent,
    DeliveryPoint,
    Drift,
    DriftStatus,
    EmissionFactor,
    Fluid,
    Notification,
    Obligation,
    Organization,
    OutgoingEmail,
    ProviderKind,
    ReviewStatus,
    Role,
    Site,
    TariffOption,
    User,
)
from app.repositories import TenantRepository
from app.security import hash_password
from app.services import (
    alert_groups,
    anomaly_context,
    assets,
    drift_explanations,
    exports,
    integrations,
    load_shift,
    predictions,
    quarterly_reports,
    recommendations,
    regulatory,
    simulator,
    tariffs,
    trajectory,
)
from app.services.consent import grant_consent, has_active_consent
from app.services.dashboard import data_as_of
from app.services.drift import detect_baseload, run_detection
from app.services.ingestion import ingest_delivery_point
from app.timeutils import LOCAL_TZ, add_months, month_start, today_local, utcnow, yesterday_local

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


DEMO_HISTORY_REF = "30001000000005"
DEMO_HISTORY_ANOMALY_DAY = date(2025, 11, 8)
DEMO_HISTORY_NOTE = "Historique de démonstration : "


def _local_dt(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=LOCAL_TZ)


def _create_demo_history(db: Session) -> None:
    """Décisions humaines passées, simulées et signalées comme telles, pour la mesure avant / après (F1)."""
    dp = db.scalar(select(DeliveryPoint).where(DeliveryPoint.external_ref == DEMO_HISTORY_REF))
    auditor = db.scalar(select(User).where(User.email == "auditeur@effismart.demo"))
    if dp is None or auditor is None or db.scalar(select(Drift.id).where(
            Drift.delivery_point_id == dp.id, Drift.day == DEMO_HISTORY_ANOMALY_DAY)):
        return
    last_notification = db.scalar(select(func.max(Notification.id))) or 0
    candidate = detect_baseload(db, dp, DEMO_HISTORY_ANOMALY_DAY, integrations.weather_provider(db))
    if candidate is None:
        logger.warning("Historique de démonstration : talon de nuit non détecté le %s.", DEMO_HISTORY_ANOMALY_DAY)
        return
    drift = Drift(delivery_point_id=dp.id, kind=candidate.kind, day=DEMO_HISTORY_ANOMALY_DAY,
                  measured_value=round(candidate.measured, 2), reference_value=round(candidate.reference, 2),
                  deviation_pct=round(candidate.deviation_pct, 1), unit=candidate.unit, details=candidate.details,
                  detected_at=_local_dt(date(2025, 11, 9), 6))
    db.add(drift)
    db.flush()
    drift_explanations.explain_drift(db, dp, drift, candidate)
    drift.status, drift.qualified_by = DriftStatus.QUALIFIED, auditor.id
    drift.qualified_at = _local_dt(date(2025, 11, 10), 9)
    drift.comment = DEMO_HISTORY_NOTE + "éclairage de l'entrepôt resté allumé la nuit et le week-end."
    rec = recommendations.propose_for_drift(db, drift)
    rec.created_at = drift.qualified_at
    rec.status, rec.reviewed_by = ReviewStatus.APPLIED, auditor.id
    rec.reviewed_at = _local_dt(date(2025, 11, 12), 10)
    rec.review_comment = DEMO_HISTORY_NOTE + "passage aux LED avec détection de présence, travaux prévus fin mars."
    rec.applied_by, rec.applied_at = auditor.id, _local_dt(date(2026, 4, 6), 8)
    rec.applied_comment = DEMO_HISTORY_NOTE + "LED et détecteurs de présence installés, horloge reprogrammée."
    # Ces décisions sont passées : pas d'alerte « à valider » dans la cloche d'aujourd'hui.
    for notification in db.scalars(select(Notification).where(Notification.id > last_notification)):
        db.delete(notification)
    db.commit()
    logger.info("Historique de démonstration créé : « %s ».", rec.title)


# --- Version 2 : données de démonstration (F6 à F12) ----------------------------------------------------

# F6 : contrats de fourniture de démonstration (prix complets indicatifs, en €/kWh ; fournisseurs fictifs).
TEMPO_DEMO = {"BLEU": {"HP": 0.1612, "HC": 0.1325}, "BLANC": {"HP": 0.1871, "HC": 0.1499},
              "ROUGE": {"HP": 0.7060, "HC": 0.1575}}
DEMO_CONTRACTS = {
    "30001000000001": dict(supplier="Volt Pro (démo)", option=TariffOption.HPHC, price_hp=0.2280, price_hc=0.1720,
                           subscription_eur_month=85.0),
    "30001000000002": dict(supplier="Volt Pro (démo)", option=TariffOption.BASE, price_base=0.2150,
                           subscription_eur_month=38.0),
    "30001000000003": dict(supplier="Watt Santé (démo)", option=TariffOption.TEMPO, tempo_prices=TEMPO_DEMO,
                           subscription_eur_month=210.0),
    "30001000000005": dict(supplier="Logi Énergie (démo)", option=TariffOption.HPHC, price_hp=0.2210,
                           price_hc=0.1640, subscription_eur_month=160.0),
    "30001000000006": dict(supplier="Spot Énergie (démo)", option=TariffOption.DYNAMIC,
                           dynamic_margin_eur_kwh=0.095, subscription_eur_month=120.0),
    "21000000000007": dict(supplier="Gaz Ouest (démo)", option=TariffOption.BASE, price_base=0.1050,
                           subscription_eur_month=45.0),
    "21000000000008": dict(supplier="Gaz Ouest (démo)", option=TariffOption.BASE, price_base=0.0980,
                           subscription_eur_month=140.0),
}
# F7 : variables d'ajustement (site : unité de production, production mensuelle moyenne, effectif en ETP).
DEMO_ACTIVITY = {
    "Atelier central": ("tonnes de pain et viennoiseries", 44.0, 38.0),
    "Boutique Bellecour": (None, None, 6.0),
    "Bâtiment principal": ("journées d'hospitalisation", 2900.0, 310.0),
    "Entrepôt Genas": ("palettes expédiées", 8500.0, 52.0),
    "Bureaux Part-Dieu": (None, None, 88.0),
}
# F11 : consommation actuelle / référence 2019 (fictive) : trajectoires contrastées pour la démonstration.
DEMO_DT_RATIOS = {"Atelier central": 0.86, "Bâtiment principal": 0.90, "Entrepôt Genas": 0.66,
                  "Bureaux Part-Dieu": 0.80}
DEMO_ISO50001 = {"Clinique du Parc": date(2028, 6, 30)}
# F8 : positions sur le plan 2D (en % ; zone : x, y, largeur, hauteur ; équipement ou compteur : x, y).
DEMO_PLANS = {
    "Bâtiment principal": {
        "Bloc opératoire": (4, 6, 40, 38), "Chambres": (50, 6, 46, 52), "Administration": (4, 50, 40, 40),
        "Local serveurs": (50, 64, 16, 26), "CTA bloc opératoire": (24, 30), "CTA administration": (24, 76),
        "Radiateurs des chambres": (73, 34), "Ballon d'eau chaude sanitaire": (88, 66), "Éclairage": (46, 47),
        "Salle serveurs": (58, 76), "Chaudière gaz à condensation": (88, 80), "Groupe froid": (75, 80),
        "Compteur élec. 30001000000003": (75, 92), "Compteur gaz 21000000000008": (88, 92),
    },
    "Entrepôt Genas": {
        "Zone de stockage": (4, 6, 64, 86), "Local de charge": (72, 6, 24, 30), "Quais frigorifiques": (72, 42, 24, 44),
        "Chargeurs des chariots élévateurs": (84, 22), "Éclairage de l'entrepôt": (30, 30),
        "Aérothermes électriques": (30, 66), "Groupes frigorifiques des quais": (84, 66),
        "Compteur élec. 30001000000005": (50, 90), "Compteur élec. 30001000000004": (84, 90),
    },
    "Bureaux Part-Dieu": {
        "Plateaux de bureaux": (4, 6, 68, 86), "Éclairage des bureaux": (36, 26), "Ventilo-convecteurs": (36, 62),
        "Pompe à chaleur réversible": (86, 22), "CTA double flux": (86, 50), "Compteur élec. 30001000000006": (86, 82),
    },
}
DEMO_SCENARIO = ("Bureaux Part-Dieu", "Plan d'actions 2027 (démonstration)",
                 ["ECL_LED", "ECL_DETECTION", "VENT_PROG", "CHAUF_CONSIGNE"])


def _create_demo_v2(db: Session) -> None:
    """Données de démonstration de la version 2, pour les clients de démonstration (idempotent)."""
    auditor = db.scalar(select(User).where(User.email == "auditeur@effismart.demo"))
    demo_orgs = {o.name: o for o in db.scalars(select(Organization).where(
        Organization.name.in_([spec["name"] for spec in ORGANIZATIONS])))}
    if auditor is None or not demo_orgs:
        return
    repo = TenantRepository(db, auditor)
    for ref, spec in DEMO_CONTRACTS.items():
        dp = db.scalar(select(DeliveryPoint).where(DeliveryPoint.external_ref == ref))
        if dp is not None and not tariffs.contracts_of(db, dp.id):
            tariffs.save_contract(db, repo, auditor, dp.id, valid_from=date(2024, 1, 1), **spec)
    today = today_local()
    sites = {s.name: s for s in db.scalars(select(Site).where(Site.organization_id.in_([o.id for o in demo_orgs.values()])))}
    for name, (unit, production, headcount) in DEMO_ACTIVITY.items():
        site = sites.get(name)
        if site is None or db.scalar(select(AdjustmentVariable.id).where(AdjustmentVariable.site_id == site.id).limit(1)):
            continue
        site.production_unit = site.production_unit or unit
        site.energy_baseline_year = site.energy_baseline_year or today.year - 1
        month = add_months(month_start(today), -24)
        while month < month_start(today):
            wave = math.sin(2 * math.pi * (month.month - 3) / 12)
            if production:
                db.add(AdjustmentVariable(organization_id=site.organization_id, site_id=site.id,
                                          kind=AdjustmentKind.PRODUCTION, month=month,
                                          value=round(production * (1 + 0.08 * wave + 0.03 * (month.month == 12)), 1)))
            db.add(AdjustmentVariable(organization_id=site.organization_id, site_id=site.id,
                                      kind=AdjustmentKind.HEADCOUNT, month=month,
                                      value=round(headcount * (1 - 0.06 * (month.month == 8)), 1)))
            month = add_months(month, 1)
        db.commit()
    for org_name, until in DEMO_ISO50001.items():
        if org_name in demo_orgs and demo_orgs[org_name].iso50001_certified_until is None:
            demo_orgs[org_name].iso50001_certified_until = until
    for site_name, positions in DEMO_PLANS.items():
        site = sites.get(site_name)
        if site is None:
            continue
        nodes = {n.name: n for n in db.scalars(select(AssetNode).where(AssetNode.site_id == site.id))}
        if any(n.plan_x is not None for n in nodes.values()):
            continue
        for node_name, place in positions.items():
            node = nodes.get(node_name)
            if node is not None:
                node.plan_x, node.plan_y = float(place[0]), float(place[1])
                if len(place) == 4:
                    node.plan_w, node.plan_h = float(place[2]), float(place[3])
    db.commit()
    weather = integrations.weather_provider(db)
    for site_name, ratio in DEMO_DT_RATIOS.items():
        site = sites.get(site_name)
        if site is None or site.dt_reference_kwh:
            continue
        points = [dp for dp in site.delivery_points if has_active_consent(db, dp.id)]
        as_of = data_as_of(db, [dp.id for dp in points])
        parts = trajectory.normalized_consumption(db, site, as_of, weather) if as_of else None
        if parts:
            site.dt_reference_year = 2019
            site.dt_reference_kwh = round(sum(p.kwh for p in parts) / ratio, -2)
    db.commit()
    site_name, scenario_name, codes = DEMO_SCENARIO
    site = sites.get(site_name)
    if site is not None and not repo.list_scenarios(site.id):
        templates = [t for t in simulator.library(db, auditor) if t.code in codes]
        breakdown = simulator.breakdown(db, site, weather)
        if breakdown and templates:
            results = simulator.simulate(site, breakdown, templates, [])
            simulator.save_scenario(db, repo, auditor, site.id, name=scenario_name,
                                    template_ids=[t.id for t in templates], recommendation_ids=[], results=results,
                                    retained=True)


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
    contextualized = anomaly_context.backfill(db)
    if contextualized:
        logger.info("%d anomalie(s) existante(s) replacée(s) dans le graphe des équipements (F2b).", contextualized)
    grouped = alert_groups.regroup_existing(db)
    if grouped:
        logger.info("%d anomalie(s) à valider regroupée(s) avec une alerte de même cause.", grouped)
    simulator.ensure_library(db)
    if db.scalar(select(Organization.id).where(Organization.name.in_([o["name"] for o in ORGANIZATIONS])).limit(1)):
        _create_demo_v2(db)
    proposed = recommendations.propose_missing(db)
    if proposed:
        logger.info("%d recommandation(s) proposée(s) pour des anomalies déjà validées.", proposed)
    predictions.refresh_predictions(db)
    trajectory.refresh_trajectories(db)
    load_shift.propose_load_shifts(db)
    regulatory.send_reminders(db)
    exports.produce_automatic(db)
    quarterly_reports.generate_due(db)


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
        _create_demo_history(db)
        upgrade(db)
        # Jeu de démonstration : pas d'e-mails pour des alertes « historiques » créées par le seed.
        db.execute(delete(OutgoingEmail))
        db.commit()
        logger.info("Seed terminé. Connexion : auditeur@effismart.demo / %s", DEMO_PASSWORD)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    seed()
