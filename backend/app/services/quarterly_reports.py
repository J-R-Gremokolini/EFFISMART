"""F10 — Générateur de rapport trimestriel assisté (décision D2, option A).

Le rapport trimestriel n'est pas une fonctionnalité d'IA autonome mais un outil de productivité pour l'auditeur.
La plateforme produit automatiquement un projet de rapport d'analyse trimestrielle : synthèse des consommations,
écarts au prévisionnel, dérives détectées, indicateurs, trajectoire, pistes d'action. L'auditeur le consulte,
l'enrichit de son expertise de terrain, le valide et le délivre à son client sous sa propre identité.
EffiSmart ne délivre jamais d'analyse en direct au client final : le client ne voit que les rapports délivrés,
au nom du cabinet, sans mention de la plateforme.

Projet (DRAFT) → validé par l'auditeur (VALIDATED) → délivré au client (DELIVERED). Le projet est produit dès la
fin du trimestre ; le régénérer met à jour les parties de la plateforme et conserve le texte de l'auditeur.
"""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Auditor,
    AuditorClientLink,
    DeliveryPoint,
    Drift,
    DriftStatus,
    Fluid,
    Notification,
    Organization,
    QuarterlyReport,
    Recommendation,
    RecommendationKind,
    ReportStatus,
    ReviewStatus,
    Role,
    SavingsScenario,
    Site,
    Trajectory,
    User,
)
from app.repositories import TenantRepository, active_links_clause
from app.services import alert_groups, anomaly_context, consumption_model, ipe, tariffs
from app.services.consent import has_active_consent
from app.services.drift import DRIFT_LABELS, daily_kwh
from app.services.validation import fr
from app.timeutils import add_months, to_local, today_local, utcnow

logger = logging.getLogger(__name__)

SECTIONS = [
    ("consumption", "Synthèse des consommations"),
    ("forecast", "Écarts au prévisionnel"),
    ("drifts", "Dérives détectées"),
    ("performance", "Indicateurs de performance énergétique"),
    ("trajectory", "Trajectoire Décret Tertiaire"),
    ("actions", "Pistes d'action"),
]
STATUS_LABELS = {ReportStatus.DRAFT: "Projet à relire", ReportStatus.VALIDATED: "Validé par l'auditeur",
                 ReportStatus.DELIVERED: "Délivré au client"}
FLUID_NAMES = {Fluid.ELEC: "Électricité", Fluid.GAS: "Gaz"}


class ReportError(ValueError):
    pass


class ReportPermissionError(PermissionError):
    pass


def quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start = date(year, 3 * (quarter - 1) + 1, 1)
    return start, add_months(start, 3) - timedelta(days=1)


def previous_quarter(today: date) -> tuple[int, int]:
    quarter = (today.month - 1) // 3 + 1
    return (today.year - 1, 4) if quarter == 1 else (today.year, quarter - 1)


def label(year: int, quarter: int) -> str:
    return f"T{quarter} {year}"


def _pct(value: float) -> str:
    if abs(value) < 0.0005:
        return "stable"
    return f"{'+' if value > 0 else ''}{fr(value * 100, 1)} %"


# --- Sections produites par la plateforme -------------------------------------------------------------


def _consumption(db: Session, org: Organization, start: date, end: date, quarter_label: str) -> dict:
    current = tariffs.monthly_triaxis(db, org, start, end)
    previous = tariffs.monthly_triaxis(db, org, date(start.year - 1, start.month, 1),
                                       date(end.year - 1, end.month, end.day))
    totals: dict[tuple[str, Fluid], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for row in current:
        t = totals[(row.site, row.fluid)]
        t[0] += row.costing.kwh
        t[1] += row.costing.eur
        t[2] += row.kgco2e
    for row in previous:
        totals[(row.site, row.fluid)][3] += row.costing.kwh
    rows = [[site, FLUID_NAMES[fluid], f"{fr(t[0])} kWh", f"{fr(t[1])} €", f"{fr(t[2] / 1000, 2)} tCO₂e",
             _pct((t[0] - t[3]) / t[3]) if t[3] else "—"]
            for (site, fluid), t in sorted(totals.items(), key=lambda item: (item[0][0], item[0][1].value)) if t[0]]
    kwh, eur, co2, before = (sum(t[i] for t in totals.values()) for i in range(4))
    if not kwh:
        text = f"Aucune consommation connue au {quarter_label}."
    else:
        text = (f"Au {quarter_label}, la consommation totale s'élève à {fr(kwh / 1000, 1)} MWh"
                + (f" ({_pct((kwh - before) / before)} par rapport au même trimestre de l'an dernier, écart brut non "
                   "corrigé du climat)" if before else "")
                + f", pour un coût de {fr(eur)} € et {fr(co2 / 1000, 1)} tCO₂e. Coûts : grille tarifaire des contrats "
                "de fourniture, à défaut prix indicatifs.")
    return {"text": text, "headers": ["Site", "Énergie", "Consommation", "Coût", "Émissions", "Évolution N-1"],
            "rows": rows}


def _forecast(db: Session, org: Organization, start: date, end: date, weather) -> dict:
    rows, expected_total, actual_total, degraded = [], 0.0, 0.0, []
    for site in org.sites:
        for fluid in Fluid:
            points = [dp for dp in site.delivery_points if dp.fluid == fluid and has_active_consent(db, dp.id)]
            if not points:
                continue
            model = consumption_model.train(db, points, start - timedelta(days=1), weather)
            if model is None:
                degraded.append(f"{site.name} ({FLUID_NAMES[fluid].lower()})")
                continue
            actual_days = defaultdict(float)
            for dp in points:
                for day, kwh in daily_kwh(db, dp.id, start, end).items():
                    actual_days[day] += kwh
            if not actual_days:
                continue
            climate = consumption_model.observed(model, weather, start, max(actual_days))
            expected = sum(model.predict(day, climate) or 0.0 for day in actual_days)
            actual = sum(actual_days.values())
            expected_total += expected
            actual_total += actual
            rows.append([site.name, FLUID_NAMES[fluid], f"{fr(expected)} kWh", f"{fr(actual)} kWh",
                         _pct((actual - expected) / expected) if expected else "—"])
    if expected_total:
        text = (f"À météo réelle, la consommation du trimestre est {_pct((actual_total - expected_total) / expected_total)} "
                "par rapport à ce qu'attendaient les modèles de consommation appris sur les 12 à 24 mois précédents "
                "(écart corrigé du climat).")
    else:
        text = "Prévisionnel indisponible : il faut au moins 12 mois d'historique avant le trimestre."
    if degraded:
        text += f" Mode dégradé (moins de 12 mois d'historique) : {', '.join(degraded)}."
    return {"text": text, "headers": ["Site", "Énergie", "Attendu", "Mesuré", "Écart"], "rows": rows}


def _drifts(db: Session, org: Organization, start: date, end: date) -> dict:
    drifts = list(db.scalars(select(Drift).join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id)
                             .join(Site, DeliveryPoint.site_id == Site.id)
                             .where(Site.organization_id == org.id, Drift.day >= start, Drift.day <= end)
                             .order_by(Drift.day)))
    leads = [d for d in drifts if d.grouped_with_id is None]
    sizes = alert_groups.group_sizes(db, [d.id for d in leads])
    status = defaultdict(int)
    for d in leads:
        status[d.status] += 1
    rows = [[f"{d.day:%d/%m/%Y}", DRIFT_LABELS[d.kind], d.delivery_point.site.name, f"{d.deviation_pct:+.0f} %",
             str(sizes[d.id]), {DriftStatus.OPEN: "À valider", DriftStatus.QUALIFIED: "Validée",
                                DriftStatus.IGNORED: "Écartée"}[d.status],
             anomaly_context.short_text(d.context) or "non localisée"]
            for d in sorted(leads, key=lambda d: -(d.gain_eur or 0))[:8]]
    if not leads:
        text = "Aucune dérive détectée sur le trimestre."
    else:
        text = (f"{len(leads)} alerte(s) sur le trimestre, regroupant {len(drifts)} anomalie(s) de même cause : "
                f"{status[DriftStatus.QUALIFIED]} validée(s), {status[DriftStatus.IGNORED]} écartée(s), "
                f"{status[DriftStatus.OPEN]} encore à valider. Les plus importantes, par gain estimé :")
    return {"text": text, "headers": ["Date", "Type", "Site", "Écart", "Occurrences", "Statut", "Localisation"],
            "rows": rows}


def _performance(db: Session, org: Organization, end: date, weather) -> dict:
    rows = []
    for site in org.sites:
        report = ipe.site_report(db, site, end.replace(day=1), weather)
        values = report.current.values
        if not values.get("kwh"):
            continue

        def cell(key: str, digits: int = 1) -> str:
            value = values.get(key)
            if value is None:
                return "—"
            change = report.variation(key)
            return fr(value, digits) + (f" ({_pct(change)})" if change is not None else "")

        rows.append([site.name, cell("kwh_m2"), cell("kwh_unit", 2), cell("kwh_fte", 0), cell("kwh_dju", 1)])
    text = ("Indicateurs de performance énergétique (ISO 50001) sur les 12 mois qui finissent avec le trimestre, entre "
            "parenthèses l'évolution par rapport à la situation de référence de chaque site." if rows
            else "Indicateurs indisponibles : pas de consommation connue.")
    return {"text": text, "headers": ["Site", "kWh/m²", "kWh/unité produite", "kWh/ETP", "kWh/DJU"], "rows": rows}


def _trajectory(db: Session, org: Organization) -> dict:
    rows = []
    for site in org.sites:
        trajectory = db.scalar(select(Trajectory).where(Trajectory.site_id == site.id,
                                                        Trajectory.status == ReviewStatus.VALIDATED)
                               .order_by(Trajectory.created_at.desc()).limit(1))
        if trajectory is None:
            continue
        rows.append([site.name, f"{trajectory.reference_year} : {fr(trajectory.reference_kwh / 1000, 1)} MWh",
                     f"{fr(-trajectory.reduction_2030 * 100)} %", "−40 %",
                     f"{fr(-trajectory.reduction_2030_with_actions * 100)} %"
                     if trajectory.reduction_2030_with_actions is not None else "—"])
    text = ("Trajectoires validées : réduction projetée en 2030 au rythme actuel et avec le plan d'actions retenu, "
            "face à l'objectif de −40 % du Décret Tertiaire." if rows
            else "Aucune trajectoire Décret Tertiaire validée pour ce client.")
    return {"text": text, "headers": ["Site", "Référence", "2030 au rythme actuel", "Objectif 2030", "2030 avec actions"],
            "rows": rows}


def _actions(db: Session, org: Organization, start: date, end: date) -> dict:
    recs = list(db.scalars(select(Recommendation).where(
        Recommendation.organization_id == org.id,
        Recommendation.status.in_([ReviewStatus.VALIDATED, ReviewStatus.APPLIED])).order_by(Recommendation.id)))
    rows = []
    for rec in recs:
        if rec.status == ReviewStatus.APPLIED and rec.applied_at and to_local(rec.applied_at).date() < start:
            snapshot = rec.savings_snapshot
            if not snapshot:
                continue
            state = f"Appliquée ; économie mesurée et validée : {fr(snapshot['annualized_kwh'])} kWh/an"
        elif rec.status == ReviewStatus.APPLIED:
            state = f"Appliquée le {to_local(rec.applied_at):%d/%m/%Y}"
        else:
            state = "Validée, à mettre en œuvre"
        gain = (f"{fr(rec.gain_eur)} €/an (gain financier)" if rec.kind == RecommendationKind.LOAD_SHIFT
                else f"{fr(rec.gain_kwh or 0)} kWh, {fr(rec.gain_eur or 0)} €/an")
        rows.append([rec.title, state, gain])
    scenarios = [s for site in org.sites for s in site_scenarios(db, site.id)]
    for scenario in scenarios:
        result = scenario.results
        rows.append([f"Plan d'actions « {scenario.name} » ({scenario.site.name})", "Scénario retenu (simulation)",
                     f"{fr(result.get('saved_kwh', 0))} kWh, {fr(result.get('saved_eur', 0))} €/an"])
    text = ("Recommandations validées par l'auditeur ou le responsable énergie, actions appliquées et plan d'actions "
            "simulé. Gains estimés, sauf mention « mesurée »." if rows else "Aucune piste d'action validée.")
    return {"text": text, "headers": ["Action", "État", "Gain"], "rows": rows}


def site_scenarios(db: Session, site_id: int) -> list[SavingsScenario]:
    return list(db.scalars(select(SavingsScenario).where(SavingsScenario.site_id == site_id,
                                                         SavingsScenario.retained.is_(True))))


def build_sections(db: Session, org: Organization, year: int, quarter: int) -> list[dict]:
    from app.services import integrations

    weather = integrations.weather_provider(db)
    start, end = quarter_bounds(year, quarter)
    built = {
        "consumption": _consumption(db, org, start, end, label(year, quarter)),
        "forecast": _forecast(db, org, start, end, weather),
        "drifts": _drifts(db, org, start, end),
        "performance": _performance(db, org, end, weather),
        "trajectory": _trajectory(db, org),
        "actions": _actions(db, org, start, end),
    }
    return [{"key": key, "title": title, "platform_text": built[key]["text"], "headers": built[key]["headers"],
             "rows": built[key]["rows"], "auditor_text": ""} for key, title in SECTIONS]


# --- Cycle de vie ---------------------------------------------------------------------------------------


def _linked_auditors(db: Session, organization_id: int) -> list[User]:
    linked = select(AuditorClientLink.auditor_id).where(AuditorClientLink.organization_id == organization_id,
                                                        *active_links_clause())
    return list(db.scalars(select(User).where(User.role == Role.AUDITOR, User.auditor_id.in_(linked))))


def generate(db: Session, org: Organization, year: int, quarter: int) -> QuarterlyReport:
    """Produit le projet, ou le met à jour s'il est encore à relire (le texte de l'auditeur est conservé)."""
    report = db.scalar(select(QuarterlyReport).where(QuarterlyReport.organization_id == org.id,
                                                     QuarterlyReport.year == year, QuarterlyReport.quarter == quarter))
    if report is not None and report.status != ReportStatus.DRAFT:
        return report
    sections = build_sections(db, org, year, quarter)
    if report is None:
        auditors = _linked_auditors(db, org.id)
        report = QuarterlyReport(organization_id=org.id, year=year, quarter=quarter,
                                 auditor_id=auditors[0].auditor_id if auditors else None)
        db.add(report)
    else:
        kept = {s["key"]: s.get("auditor_text", "") for s in report.sections or []}
        for section in sections:
            section["auditor_text"] = kept.get(section["key"], "")
    report.sections, report.generated_at = sections, utcnow()
    db.commit()
    return report


def generate_due(db: Session, today: date | None = None) -> list[QuarterlyReport]:
    """Dès la fin d'un trimestre : un projet par client suivi par un auditeur ; l'auditeur est prévenu."""
    from app.services import mailer

    year, quarter = previous_quarter(today or today_local())
    created = []
    for org in db.scalars(select(Organization).order_by(Organization.id)).all():
        auditors = _linked_auditors(db, org.id)
        if not auditors or db.scalar(select(QuarterlyReport.id).where(
                QuarterlyReport.organization_id == org.id, QuarterlyReport.year == year,
                QuarterlyReport.quarter == quarter)):
            continue
        report = generate(db, org, year, quarter)
        message = (f"Projet de rapport trimestriel {label(year, quarter)} prêt pour {org.name} : à relire, enrichir de "
                   "votre analyse et valider avant de le délivrer à votre client.")
        for user in auditors:
            db.add(Notification(user_id=user.id, organization_id=org.id, message=message))
            mailer.enqueue(db, user, message, org.name)
        created.append(report)
    db.commit()
    return created


def _require_auditor(user: User) -> None:
    if user.role != Role.AUDITOR or user.auditor_id is None:
        raise ReportPermissionError("Le rapport trimestriel est relu, validé et délivré par l'auditeur, sous son "
                                    "identité ; ni la plateforme ni le client ne le délivrent.")


def save_texts(db: Session, repo: TenantRepository, user: User, report_id: int, *, introduction: str | None,
               conclusion: str | None, section_texts: dict[str, str]) -> QuarterlyReport:
    """Texte de l'auditeur : synthèse, commentaire de chaque section, conclusion."""
    _require_auditor(user)
    report = repo.get_report(report_id)
    if report.status == ReportStatus.DELIVERED:
        raise ReportError("Rapport déjà délivré : il n'est plus modifiable.")
    report.introduction = (introduction or "").strip() or None
    report.conclusion = (conclusion or "").strip() or None
    report.sections = [{**s, "auditor_text": (section_texts.get(s["key"], s.get("auditor_text", "")) or "").strip()}
                       for s in report.sections]
    if report.status == ReportStatus.VALIDATED:  # modifié après validation : à valider de nouveau
        report.status, report.validated_by, report.validated_at = ReportStatus.DRAFT, None, None
    db.commit()
    return report


def validate_report(db: Session, repo: TenantRepository, user: User, report_id: int) -> QuarterlyReport:
    _require_auditor(user)
    report = repo.get_report(report_id)
    if report.status != ReportStatus.DRAFT:
        raise ReportError("Seul un projet à relire peut être validé.")
    if not report.introduction:
        raise ReportError("Ajoutez votre synthèse avant de valider : le rapport porte votre analyse.")
    report.status, report.validated_by, report.validated_at = ReportStatus.VALIDATED, user.id, utcnow()
    report.auditor_id = user.auditor_id
    db.commit()
    return report


def reopen(db: Session, repo: TenantRepository, user: User, report_id: int) -> QuarterlyReport:
    _require_auditor(user)
    report = repo.get_report(report_id)
    if report.status != ReportStatus.VALIDATED:
        raise ReportError("Seul un rapport validé, pas encore délivré, peut être rouvert.")
    report.status, report.validated_by, report.validated_at = ReportStatus.DRAFT, None, None
    db.commit()
    return report


def deliver(db: Session, repo: TenantRepository, user: User, report_id: int) -> QuarterlyReport:
    """Délivre le rapport validé au client, sous l'identité du cabinet ; le client en est prévenu."""
    from app.services import mailer

    _require_auditor(user)
    report = repo.get_report(report_id)
    if report.status != ReportStatus.VALIDATED:
        raise ReportError("Validez le rapport avant de le délivrer.")
    report.status, report.delivered_by, report.delivered_at = ReportStatus.DELIVERED, user.id, utcnow()
    report.auditor_id = user.auditor_id
    cabinet = db.get(Auditor, user.auditor_id)
    message = (f"Rapport d'analyse trimestrielle {label(report.year, report.quarter)} délivré par "
               f"{cabinet.name if cabinet else 'votre auditeur'}.")
    for client in db.scalars(select(User).where(User.role == Role.CLIENT_VIEWER,
                                                User.organization_id == report.organization_id)):
        db.add(Notification(user_id=client.id, organization_id=report.organization_id, message=message))
        mailer.enqueue(db, client, message, report.organization.name)
    db.commit()
    return report


# --- Document délivré -------------------------------------------------------------------------------


def render_html(db: Session, report: QuarterlyReport) -> str:
    """Document autonome au nom du cabinet (imprimable en PDF depuis le navigateur), sans mention de la plateforme."""
    e = html.escape
    cabinet = db.get(Auditor, report.auditor_id) if report.auditor_id else None
    signer = db.get(User, report.validated_by) if report.validated_by else None
    org = report.organization
    start, end = quarter_bounds(report.year, report.quarter)
    paragraphs = "".join(f"<p>{e(p)}</p>" for p in (report.introduction or "").split("\n") if p.strip())
    body = []
    for section in report.sections:
        table = ""
        if section["rows"]:
            head = "".join(f"<th>{e(h)}</th>" for h in section["headers"])
            rows = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in row) + "</tr>" for row in section["rows"])
            table = f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
        comment = "".join(f"<p>{e(p)}</p>" for p in section.get("auditor_text", "").split("\n") if p.strip())
        body.append(f"<section><h2>{e(section['title'])}</h2><p>{e(section['platform_text'])}</p>{table}"
                    + (f'<div class="comment"><h3>Analyse</h3>{comment}</div>' if comment else "") + "</section>")
    conclusion = "".join(f"<p>{e(p)}</p>" for p in (report.conclusion or "").split("\n") if p.strip())
    cabinet_name = e(cabinet.name) if cabinet else "Votre auditeur énergétique"
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rapport d'analyse trimestrielle {e(label(report.year, report.quarter))} — {e(org.name)}</title>
<style>
body {{ font-family: Georgia, 'Times New Roman', serif; color: #1f2937; max-width: 860px; margin: 32px auto;
       padding: 0 24px; line-height: 1.55; }}
header {{ border-bottom: 3px solid #1f2937; padding-bottom: 12px; margin-bottom: 24px; }}
header .cabinet {{ font-size: 15px; letter-spacing: .08em; text-transform: uppercase; color: #4b5563; }}
h1 {{ font-size: 28px; margin: 6px 0; }} h2 {{ font-size: 20px; margin-top: 32px; border-bottom: 1px solid #d1d5db; }}
h3 {{ font-size: 15px; margin: 12px 0 4px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 12px 0; }}
th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f3f4f6; }}
.comment {{ background: #f9fafb; border-left: 4px solid #1f2937; padding: 8px 14px; margin-top: 10px; }}
.synthese {{ background: #f9fafb; padding: 12px 18px; border-radius: 6px; }}
footer {{ margin-top: 40px; border-top: 1px solid #d1d5db; padding-top: 10px; font-size: 13px; color: #4b5563; }}
@media print {{ body {{ margin: 0; }} section {{ page-break-inside: avoid; }} }}
</style></head><body>
<header><div class="cabinet">{cabinet_name}</div>
<h1>Rapport d'analyse trimestrielle — {e(label(report.year, report.quarter))}</h1>
<div>{e(org.name)}, du {start:%d/%m/%Y} au {end:%d/%m/%Y}</div></header>
<section class="synthese"><h2>Synthèse</h2>{paragraphs}</section>
{''.join(body)}
{f'<section><h2>Conclusion</h2>{conclusion}</section>' if conclusion else ''}
<footer>Analyse établie et délivrée par {cabinet_name}{f', {e(signer.email)}' if signer else ''}
{f'le {to_local(report.delivered_at):%d/%m/%Y}' if report.delivered_at else ''}.</footer>
</body></html>"""
