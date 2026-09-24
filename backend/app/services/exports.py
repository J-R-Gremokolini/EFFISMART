"""F4 — Moteur d'export multi-format (OPERAT + VSME).

Architecture :
- `DataAssembler` : socle commun, agrège conso, coûts et émissions sur la période ;
- `Formatter`     : gabarits de sortie interchangeables (`OperatFormatter`, `VsmeFormatter`) ;
- `ExportEngine`  : assemble UNE fois, puis rend chaque format demandé.

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
from datetime import date, datetime
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
from app.timeutils import local_day_bounds, to_local, utcnow

logger = logging.getLogger(__name__)

FLUID_LABELS = {Fluid.ELEC: "ELECTRICITE", Fluid.GAS: "GAZ_NATUREL"}


# --- Socle commun ------------------------------------------------------------


@dataclass
class FluidTotals:
    kwh: float = 0.0
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
        for site in db.scalars(select(Site).where(Site.organization_id == org.id).order_by(Site.id)):
            by_fluid: dict[Fluid, FluidTotals] = {}
            points = db.scalars(
                select(DeliveryPoint).where(DeliveryPoint.site_id == site.id, active_consent_clause())
            )
            for dp in points:
                totals = by_fluid.setdefault(dp.fluid, FluidTotals())
                totals.delivery_points.append(dp.external_ref)
                totals.data_sources.add(dp.provider.value)
                rows = db.execute(
                    select(Measurement.time, Measurement.value_kwh).where(
                        Measurement.delivery_point_id == dp.id,
                        Measurement.time >= t0,
                        Measurement.time < t1,
                    )
                )
                for t, kwh in rows:
                    totals.kwh += kwh
                    totals.monthly_kwh[to_local(t).strftime("%Y-%m")] += kwh
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
    return {
        "format": fmt,
        "format_version": "effismart-v1-provisoire",
        "avertissement": "Gabarit provisoire V1 : structure à aligner sur le gabarit officiel.",
        "organisation": {"nom": data.organization_name, "siren": data.siren},
        "periode": {"debut": data.period_start, "fin": data.period_end},
        "genere_le": data.assembled_at,
        "facteurs_emission_utilises": data.emission_factors_used,
    }


class OperatFormatter:
    """Déclaration Décret Tertiaire : consommations par site assujetti et par fluide."""

    format = ExportFormat.OPERAT

    def render(self, data: AssembledData) -> dict[str, bytes]:
        sites = [s for s in data.sites if s.is_tertiary_decret]
        payload = _common_header(data, "OPERAT")
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
        return {"operat_export.json": _json_bytes(payload), "operat_consommations.csv": csv_content}


class VsmeFormatter:
    """Gabarit ESG européen VSME — module de base B3 « Énergie et émissions de GES »."""

    format = ExportFormat.VSME

    def render(self, data: AssembledData) -> dict[str, bytes]:
        elec_mwh = data.total_kwh(Fluid.ELEC) / 1000
        gas_mwh = data.total_kwh(Fluid.GAS) / 1000
        scope1_t = data.total_kgco2e(Fluid.GAS) / 1000  # combustion de gaz sur site
        scope2_t = data.total_kgco2e(Fluid.ELEC) / 1000  # électricité achetée (méthode « location-based »)
        payload = _common_header(data, "VSME")
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
        return {"vsme_export.json": _json_bytes(payload), "vsme_b3_energie_ges.csv": csv_content}


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
    ) -> list[ExportJob]:
        data = self.assembler.assemble(db, org, start, end)  # socle calculé une seule fois
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
            )
            db.add(job)
            db.flush()
            try:
                files = self.formatters[fmt].render(data)
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


def build_zip(job: ExportJob) -> bytes:
    job_dir = Path(settings.export_dir) / (job.file_ref or "")
    if not job.file_ref or not job_dir.is_dir():
        raise FileNotFoundError(job.id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(job_dir.iterdir()):
            archive.write(path, arcname=path.name)
    return buffer.getvalue()
