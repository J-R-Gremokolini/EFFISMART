"""F4 — Génération des données énergie pour les rapports RSE (OPERAT + VSME).

EffiSmart fournit la brique énergie (consommations, coûts indicatifs, émissions des scopes 1 et 2) ;
il ne produit pas le rapport de durabilité complet, dont les volets social et gouvernance sont hors
de son périmètre. Chaque jeu le dit, fichier par fichier :
- les fichiers s'appellent « donnees_energie_… », jamais « rapport » ;
- chaque JSON porte le périmètre et ce qui reste à compléter par l'entreprise (modules VSME autres que
  B3, compléments de la déclaration OPERAT) ;
- chaque archive contient un LISEZMOI.txt qui l'explique en clair.

Architecture :
- `DataAssembler` : socle commun, agrège conso, coûts et émissions sur la période, à partir des
  compteurs (N1) et, à défaut, des factures et relevés (N0) ;
- `Formatter`     : gabarits de sortie interchangeables (`OperatFormatter`, `VsmeFormatter`) ;
- `ExportEngine`  : assemble UNE fois, puis rend chaque format demandé ;
- `produce_automatic` : jeux de données produits sans intervention (année écoulée, année en cours
  mise à jour chaque mois), téléchargeables par le client.

Ajouter un format = ajouter un `Formatter` dans `FORMATTERS`, sans toucher au socle.

Les gabarits officiels OPERAT/VSME seront précisés plus tard : en V1, chaque
formatter produit un JSON structuré + un CSV (séparateur « ; », UTF-8 BOM pour Excel).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    DeliveryPoint,
    ExportFormat,
    ExportJob,
    ExportStatus,
    Fluid,
    Measurement,
    Organization,
    Site,
)
from app.services.consent import active_consent_clause
from app.services.dashboard import emission_factors_at, estimated_price
from app.services.energy_data import declared_daily
from app.timeutils import local_day_bounds, month_start, to_local, today_local, utcnow

logger = logging.getLogger(__name__)

FLUID_LABELS = {Fluid.ELEC: "ELECTRICITE", Fluid.GAS: "GAZ_NATUREL"}
FLUID_NAMES = {Fluid.ELEC: "électricité", Fluid.GAS: "gaz naturel"}
# Version du gabarit : un jeu automatique produit avec une version antérieure est renouvelé.
FORMAT_VERSION = "effismart-v1.1-provisoire"
README_NAME = "LISEZMOI.txt"
PERIMETER = ("Données énergie pour les rapports RSE : consommations, coûts indicatifs et émissions des scopes 1 "
             "et 2. EffiSmart fournit la brique énergie ; le rapport de durabilité complet, avec ses volets social "
             "et de gouvernance, reste à établir par l'entreprise.")
# Module de base de la norme VSME (EFRAG) : EffiSmart remplit le module B3, l'entreprise les autres.
VSME_PROVIDED = {"B3": "Énergie et émissions de gaz à effet de serre"}
VSME_TO_COMPLETE = {
    "B1": "Base de préparation",
    "B2": "Pratiques, politiques et initiatives de transition",
    "B4": "Pollution de l'air, de l'eau et des sols",
    "B5": "Biodiversité",
    "B6": "Eau",
    "B7": "Utilisation des ressources, économie circulaire et gestion des déchets",
    "B8": "Effectifs : caractéristiques générales",
    "B9": "Effectifs : santé et sécurité",
    "B10": "Effectifs : rémunération, négociation collective et formation",
    "B11": "Condamnations et amendes pour corruption",
}
OPERAT_TO_COMPLETE = ("Sur la plateforme OPERAT (ADEME), l'assujetti déclare lui-même ses entités fonctionnelles : "
                      "catégories d'activité et indicateurs d'intensité d'usage s'ajoutent aux consommations "
                      "fournies ici.")
FILE_DESCRIPTIONS = {
    "donnees_energie_operat.json": "consommations annuelles par site assujetti et par énergie (JSON)",
    "donnees_energie_operat.csv": "les mêmes consommations, une ligne par site et par énergie (tableur)",
    "donnees_energie_vsme_b3.json": "indicateurs du module B3 du VSME, détail par site (JSON)",
    "donnees_energie_vsme_b3.csv": "indicateurs du module B3, une ligne par indicateur (tableur)",
}


# --- Socle commun ------------------------------------------------------------


@dataclass
class FluidTotals:
    kwh: float = 0.0
    n0_kwh: float = 0.0  # dont : factures et relevés (N0)
    cost_eur: float = 0.0
    kgco2e: float = 0.0
    monthly_kwh: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    delivery_points: list[str] = field(default_factory=list)
    data_sources: set[str] = field(default_factory=set)


@dataclass
class SiteEnergy:
    site_id: int
    name: str
    address: str | None
    surface_m2: float | None
    is_tertiary_decret: bool
    by_fluid: dict[Fluid, FluidTotals]

    @property
    def total_kwh(self) -> float:
        return sum(t.kwh for t in self.by_fluid.values())


@dataclass
class AssembledData:
    organization_id: int
    organization_name: str
    siren: str | None
    period_start: date
    period_end: date
    sites: list[SiteEnergy]
    emission_factors_used: list[dict]
    assembled_at: datetime

    def total_kwh(self, fluid: Fluid | None = None) -> float:
        return sum(
            t.kwh for s in self.sites for f, t in s.by_fluid.items() if fluid is None or f == fluid
        )

    def total_kgco2e(self, fluid: Fluid | None = None) -> float:
        return sum(
            t.kgco2e for s in self.sites for f, t in s.by_fluid.items() if fluid is None or f == fluid
        )


class DataAssembler:
    def assemble(self, db: Session, org: Organization, start: date, end: date) -> AssembledData:
        factors = emission_factors_at(db, end)
        assembled_at = utcnow()
        t0, t1 = local_day_bounds(start, end)
        sites: list[SiteEnergy] = []
        consented = set(db.scalars(select(DeliveryPoint.id).where(active_consent_clause())))
        for site in db.scalars(select(Site).where(Site.organization_id == org.id).order_by(Site.id)):
            by_fluid: dict[Fluid, FluidTotals] = {}
            for dp in db.scalars(select(DeliveryPoint).where(DeliveryPoint.site_id == site.id)):
                measured_days: set[date] = set()
                n1_kwh, n1_monthly = 0.0, defaultdict(float)
                if dp.id in consented:  # N1 : compteurs, uniquement avec consentement actif
                    rows = db.execute(
                        select(Measurement.time, Measurement.value_kwh).where(
                            Measurement.delivery_point_id == dp.id,
                            Measurement.time >= t0,
                            Measurement.time < t1,
                        )
                    )
                    for t, kwh in rows:
                        local_day = to_local(t).date()
                        measured_days.add(local_day)
                        n1_kwh += kwh
                        n1_monthly[local_day.strftime("%Y-%m")] += kwh
                n0 = declared_daily(db, dp, start, end, measured_days)  # N0 : factures, jours sans mesure
                if not measured_days and not n0:
                    continue
                totals = by_fluid.setdefault(dp.fluid, FluidTotals())
                totals.delivery_points.append(dp.external_ref)
                if measured_days:
                    totals.data_sources.add(f"N1 compteurs ({dp.provider.value})")
                if n0:
                    totals.data_sources.add("N0 factures et relevés")
                totals.kwh += n1_kwh
                for month, kwh in n1_monthly.items():
                    totals.monthly_kwh[month] += kwh
                for day, kwh in n0.items():
                    totals.kwh += kwh
                    totals.n0_kwh += kwh
                    totals.monthly_kwh[day.strftime("%Y-%m")] += kwh
            for fluid, totals in by_fluid.items():
                totals.cost_eur = totals.kwh * estimated_price(fluid)
                if fluid in factors:
                    totals.kgco2e = totals.kwh * factors[fluid].factor_kgco2_per_kwh
            sites.append(
                SiteEnergy(site.id, site.name, site.address, site.surface_m2, site.is_tertiary_decret, by_fluid)
            )

        factors_used = [
            {
                "fluid": fluid.value,
                "factor_kgco2e_per_kwh": f.factor_kgco2_per_kwh,
                "valid_from": f.valid_from.isoformat(),
                "version": f.version,
                "source": f.source,
                "applied_at": assembled_at.isoformat(),
            }
            for fluid, f in factors.items()
        ]
        return AssembledData(
            organization_id=org.id,
            organization_name=org.name,
            siren=org.siren,
            period_start=start,
            period_end=end,
            sites=sites,
            emission_factors_used=factors_used,
            assembled_at=assembled_at,
        )


# --- Formatters ------------------------------------------------------------------


class Formatter(Protocol):
    format: ExportFormat

    def render(self, data: AssembledData) -> dict[str, bytes]:
        """Renvoie {nom de fichier: contenu}."""
        ...


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _common_header(data: AssembledData, fmt: str) -> dict:
    total = data.total_kwh()
    n0 = sum(t.n0_kwh for s in data.sites for t in s.by_fluid.values())
    return {
        "format": fmt,
        "format_version": FORMAT_VERSION,
        "avertissement": "Gabarit provisoire V1 : structure à aligner sur le gabarit officiel.",
        "perimetre": PERIMETER,
        "organisation": {"nom": data.organization_name, "siren": data.siren},
        "periode": {"debut": data.period_start, "fin": data.period_end},
        "genere_le": data.assembled_at,
        "niveau_des_donnees": {
            "N1_compteurs_kwh": round(total - n0, 1),
            "N0_factures_et_releves_kwh": round(n0, 1),
            "part_N0": round(n0 / total, 4) if total else 0,
        },
        "facteurs_emission_utilises": data.emission_factors_used,
    }


class OperatFormatter:
    """Déclaration Décret Tertiaire : consommations par site assujetti et par fluide."""

    format = ExportFormat.OPERAT

    def render(self, data: AssembledData) -> dict[str, bytes]:
        sites = [s for s in data.sites if s.is_tertiary_decret]
        payload = _common_header(data, "OPERAT")
        payload["a_completer_par_l_entreprise"] = OPERAT_TO_COMPLETE
        payload["annee_consommations"] = data.period_end.year
        payload["entites_fonctionnelles_assujetties"] = [
            {
                "site": s.name,
                "adresse": s.address,
                "surface_m2": s.surface_m2,
                "consommations": [
                    {
                        "fluide": FLUID_LABELS[fluid],
                        "conso_kwh": round(t.kwh, 1),
                        "points_de_livraison": t.delivery_points,
                        "source_donnees": sorted(t.data_sources),
                    }
                    for fluid, t in s.by_fluid.items()
                ],
                "conso_totale_kwh": round(s.total_kwh, 1),
                "ratio_kwh_m2": round(s.total_kwh / s.surface_m2, 1) if s.surface_m2 else None,
            }
            for s in sites
        ]
        rows = [
            [
                s.name, s.address or "", s.surface_m2 or "", data.period_start.isoformat(),
                data.period_end.isoformat(), FLUID_LABELS[fluid], round(t.kwh, 1),
                ",".join(t.delivery_points), ",".join(sorted(t.data_sources)),
            ]
            for s in sites
            for fluid, t in s.by_fluid.items()
        ]
        csv_content = _csv_bytes(
            ["site", "adresse", "surface_m2", "periode_debut", "periode_fin", "fluide", "conso_kwh",
             "points_de_livraison", "source_donnees"],
            rows,
        )
        return {"donnees_energie_operat.json": _json_bytes(payload), "donnees_energie_operat.csv": csv_content}


class VsmeFormatter:
    """Gabarit ESG européen VSME — module de base B3 « Énergie et émissions de GES »."""

    format = ExportFormat.VSME

    def render(self, data: AssembledData) -> dict[str, bytes]:
        elec_mwh = data.total_kwh(Fluid.ELEC) / 1000
        gas_mwh = data.total_kwh(Fluid.GAS) / 1000
        scope1_t = data.total_kgco2e(Fluid.GAS) / 1000  # combustion de gaz sur site
        scope2_t = data.total_kgco2e(Fluid.ELEC) / 1000  # électricité achetée (méthode « location-based »)
        payload = _common_header(data, "VSME")
        payload["couverture_vsme"] = {
            "modules_fournis_par_effismart": VSME_PROVIDED,
            "modules_a_completer_par_l_entreprise": VSME_TO_COMPLETE,
        }
        payload["B3_energie_et_ges"] = {
            "consommation_energie_totale_mwh": round(elec_mwh + gas_mwh, 3),
            "electricite_mwh": round(elec_mwh, 3),
            "combustibles_gaz_naturel_mwh": round(gas_mwh, 3),
            "part_renouvelable": "non renseignée (contrat de fourniture non connu en V1)",
            "emissions_scope1_tco2e": round(scope1_t, 3),
            "emissions_scope2_location_based_tco2e": round(scope2_t, 3),
            "emissions_scope1_2_tco2e": round(scope1_t + scope2_t, 3),
        }
        payload["detail_par_site"] = [
            {
                "site": s.name,
                "electricite_mwh": round(s.by_fluid[Fluid.ELEC].kwh / 1000, 3) if Fluid.ELEC in s.by_fluid else 0,
                "gaz_mwh": round(s.by_fluid[Fluid.GAS].kwh / 1000, 3) if Fluid.GAS in s.by_fluid else 0,
                "emissions_tco2e": round(sum(t.kgco2e for t in s.by_fluid.values()) / 1000, 3),
            }
            for s in data.sites
        ]
        rows = [
            ["B3", "consommation_energie_totale", round(elec_mwh + gas_mwh, 3), "MWh"],
            ["B3", "electricite", round(elec_mwh, 3), "MWh"],
            ["B3", "combustibles_gaz_naturel", round(gas_mwh, 3), "MWh"],
            ["B3", "emissions_scope1", round(scope1_t, 3), "tCO2e"],
            ["B3", "emissions_scope2_location_based", round(scope2_t, 3), "tCO2e"],
        ]
        csv_content = _csv_bytes(["module", "indicateur", "valeur", "unite"], rows)
        return {"donnees_energie_vsme_b3.json": _json_bytes(payload), "donnees_energie_vsme_b3.csv": csv_content}


def _fr(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def readme(data: AssembledData, fmt: ExportFormat, names: list[str]) -> bytes:
    """LISEZMOI.txt : ce que contient l'archive, d'où viennent les données, ce qui reste à l'entreprise."""
    total = data.total_kwh()
    n0 = sum(t.n0_kwh for s in data.sites for t in s.by_fluid.values())
    title = "déclaration OPERAT (Décret Tertiaire)" if fmt == ExportFormat.OPERAT else "VSME, module B3"
    lines = [
        f"Données énergie pour les rapports RSE : {title}",
        f"Organisation : {data.organization_name}" + (f" (SIREN {data.siren})" if data.siren else ""),
        f"Période : du {data.period_start:%d/%m/%Y} au {data.period_end:%d/%m/%Y}",
        f"Produit par EffiSmart le {to_local(data.assembled_at):%d/%m/%Y à %H:%M} (gabarit {FORMAT_VERSION})",
        "",
        "Périmètre",
        PERIMETER,
        "",
        "Fichiers",
        *(f"- {name} : {FILE_DESCRIPTIONS.get(name, '')}" for name in names),
        "",
        "Origine des données",
        f"- N1, compteurs communicants : {_fr(total - n0)} kWh",
        f"- N0, factures et relevés saisis : {_fr(n0)} kWh ({_fr(n0 / total * 100 if total else 0, 1)} %)",
        *(f"- Facteur d'émission {FLUID_NAMES[Fluid(f['fluid'])]} : "
          f"{_fr(f['factor_kgco2e_per_kwh'], 3)} kgCO2e/kWh ({f['version']})" for f in data.emission_factors_used),
        "",
        "À compléter par l'entreprise",
    ]
    if fmt == ExportFormat.VSME:
        lines += [f"- {code} : {label}" for code, label in VSME_TO_COMPLETE.items()]
    else:
        lines.append(OPERAT_TO_COMPLETE)
    lines += ["", "Gabarit provisoire V1 : structure à aligner sur le gabarit officiel."]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def archive_name(job: ExportJob) -> str:
    return f"effismart_donnees_energie_{job.format.value.lower()}_{job.period_start}_{job.period_end}.zip"


FORMATTERS: dict[ExportFormat, Formatter] = {
    ExportFormat.OPERAT: OperatFormatter(),
    ExportFormat.VSME: VsmeFormatter(),
}


# --- Moteur ------------------------------------------------------------------------


class ExportEngine:
    def __init__(
        self, assembler: DataAssembler | None = None, formatters: dict[ExportFormat, Formatter] | None = None
    ) -> None:
        self.assembler = assembler or DataAssembler()
        self.formatters = formatters or FORMATTERS

    @staticmethod
    def _ordered(formats: list[ExportFormat]) -> list[ExportFormat]:
        priority = settings.export_formats_priority
        return sorted(set(formats), key=lambda f: priority.index(f.value) if f.value in priority else len(priority))

    def run(
        self,
        db: Session,
        org: Organization,
        start: date,
        end: date,
        formats: list[ExportFormat],
        user_id: int | None = None,
        automatic: bool = False,
        data: AssembledData | None = None,
    ) -> list[ExportJob]:
        data = data or self.assembler.assemble(db, org, start, end)  # socle calculé une seule fois
        jobs = []
        for fmt in self._ordered(formats):
            job = ExportJob(
                organization_id=org.id,
                format=fmt,
                period_start=start,
                period_end=end,
                status=ExportStatus.PENDING,
                factors_used=data.emission_factors_used,
                created_by=user_id,
                automatic=automatic,
            )
            db.add(job)
            db.flush()
            try:
                files = self.formatters[fmt].render(data)
                files[README_NAME] = readme(data, fmt, list(files))
                job_dir = Path(settings.export_dir) / str(job.id)
                job_dir.mkdir(parents=True, exist_ok=True)
                for name, content in files.items():
                    (job_dir / name).write_bytes(content)
                job.file_ref = str(job.id)
                job.status = ExportStatus.DONE
            except Exception as exc:
                logger.exception("Échec de l'export #%s", job.id)
                job.status = ExportStatus.FAILED
                job.error = str(exc)
            jobs.append(job)
        db.commit()
        return jobs


def _has_energy_data(data: AssembledData) -> bool:
    return data.total_kwh() > 0


def is_current(job: ExportJob) -> bool:
    """Jeu produit avec le gabarit actuel ? Sinon, la production automatique le renouvelle."""
    job_dir = Path(settings.export_dir) / (job.file_ref or "")
    for path in sorted(job_dir.glob("*.json")) if job.file_ref else []:
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("format_version") == FORMAT_VERSION
        except (OSError, ValueError):
            return False
    return False


def latest_jobs(jobs: list[ExportJob]) -> list[ExportJob]:
    """Jeux à afficher : pour la production automatique, le plus récent de chaque format et période ; les
    versions remplacées restent conservées."""
    seen: set[tuple] = set()
    shown = []
    for job in sorted(jobs, key=lambda j: (j.created_at, j.id), reverse=True):
        key = (job.format, job.period_start, job.period_end)
        if job.automatic and key in seen:
            continue
        if job.automatic:
            seen.add(key)
        shown.append(job)
    return shown


def produce_automatic(db: Session, today: date | None = None) -> list[ExportJob]:
    """F4 « sans ressaisie » : pour chaque organisation, la plateforme produit d'elle-même
    - le jeu de l'année civile écoulée, une fois (dès le 1er janvier) ;
    - le jeu de l'année en cours jusqu'au dernier mois complet, renouvelé chaque mois ;
    - de nouveau, un jeu produit avec un gabarit antérieur (nouvelle version des fichiers).
    Le client les télécharge depuis son espace, sans écrire quoi que ce soit (lecture seule F5)."""
    today = today or today_local()
    current_month = month_start(today)
    previous_year = (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    year_to_date = (date(today.year, 1, 1), current_month - timedelta(days=1))  # jusqu'au dernier mois complet
    periods = [previous_year] + ([year_to_date] if year_to_date[1] >= year_to_date[0] else [])
    created: list[ExportJob] = []
    engine = ExportEngine()
    for org in db.scalars(select(Organization).order_by(Organization.id)):
        for start, end in periods:
            done = db.scalars(select(ExportJob).where(
                ExportJob.organization_id == org.id, ExportJob.automatic.is_(True),
                ExportJob.period_start == start, ExportJob.period_end == end,
                ExportJob.status == ExportStatus.DONE)).all()
            if {j.format for j in done if is_current(j)} >= set(ExportFormat):
                continue
            data = engine.assembler.assemble(db, org, start, end)
            if not _has_energy_data(data):
                continue
            created += engine.run(db, org, start, end, list(ExportFormat), automatic=True, data=data)
    if created:
        logger.info("%d jeu(x) de données RSE produit(s) automatiquement.", len(created))
    return created


def build_zip(job: ExportJob) -> bytes:
    job_dir = Path(settings.export_dir) / (job.file_ref or "")
    if not job.file_ref or not job_dir.is_dir():
        raise FileNotFoundError(job.id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(job_dir.iterdir()):
            archive.write(path, arcname=path.name)
    return buffer.getvalue()
