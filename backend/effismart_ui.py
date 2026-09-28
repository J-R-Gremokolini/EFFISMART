"""EffiSmart — interface locale 100 % Python (Streamlit).

Lancement : `python lancer.py` à la racine du projet
(ou `streamlit run effismart_ui.py` depuis le dossier backend).

L'interface appelle directement les services du backend : mêmes règles métier,
même isolation multi-tenant (`TenantRepository`) et mêmes rôles que l'API.
Un compte « espace client » n'écrit rien, sauf le dépôt de documents et, pour le responsable
énergie, la validation des sorties de la plateforme (principe P1).

Design : « Monochrome Console » (.claude/skills/design-system-effismart/SKILL.md),
implémenté dans `ui_theme.py`.
"""
from app.local import configure_local_environment

configure_local_environment()  # avant tout import de app.config

from datetime import date, timedelta  # noqa: E402

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import ui_theme as ui  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.local import catch_up, prepare_database  # noqa: E402
from app.models import (  # noqa: E402
    AssetNodeKind,
    DeadlineStatus,
    ConnectorAuth,
    DeliveryPoint,
    DeliveryStatus,
    DocumentKind,
    DocumentStatus,
    Drift,
    DriftStatus,
    ExportFormat,
    ExportStatus,
    Fluid,
    MeasurementStep,
    Notification,
    Obligation,
    Organization,
    Prediction,
    ProviderKind,
    ReviewStatus,
    Role,
    Site,
    User,
    ValueUnit,
)
from app.repositories import ResourceNotFound, TenantRepository, sees_unvalidated  # noqa: E402
from app.security import verify_password  # noqa: E402
from app.services import assets, dashboard, integrations, onboarding, regulatory, validation  # noqa: E402
from app.services import documents as documents_service  # noqa: E402
from app.services import drift as drift_service  # noqa: E402
from app.services import predictions as predictions_service  # noqa: E402
from app.services import recommendations as recommendations_service  # noqa: E402
from app.services.consent import ConsentRequiredError, grant_consent, revoke_consent  # noqa: E402
from app.services.delivery_points import active_consent  # noqa: E402
from app.services.exports import ExportEngine, build_zip  # noqa: E402
from app.services.ingestion import backfill_delivery_point  # noqa: E402
from app.timeutils import LOCAL_TZ, local_midnight_utc, to_local, today_local, yesterday_local  # noqa: E402

# --- Libellés (UI en français ; jamais de promesse de « temps réel ») ------------------

APP_NAME = "EffiSmart"
FLUID_LABELS = {Fluid.ELEC: "Électricité", Fluid.GAS: "Gaz"}
FLUID_COLORS = {"Électricité": ui.PRIMARY, "Gaz": ui.SERIES_2}
ROLE_LABELS = {Role.AUDITOR: "Auditeur", Role.CLIENT_VIEWER: "Espace client", Role.ADMIN: "Administrateur"}
PERIOD_LABELS = {"7d": "7 jours", "30d": "30 jours", "12m": "12 mois", "custom": "Personnalisée"}
DRIFT_KIND_LABELS = {
    "THRESHOLD": "Dépassement de seuil",
    "CLIMATE_DEVIATION": "Écart climatique (N-1 / DJU)",
    "BASELOAD": "Talon anormal",
}
# Principe P1 : une sortie de la plateforme est « à valider » tant qu'un humain n'a pas décidé.
DRIFT_STATUS_LABELS = validation.DRIFT_STATUS_LABELS
DRIFT_STATUS_TONES = {DriftStatus.OPEN: "warning", DriftStatus.QUALIFIED: "success", DriftStatus.IGNORED: "neutral"}
REVIEW_STATUS_LABELS = validation.REVIEW_STATUS_LABELS
REVIEW_STATUS_TONES = {ReviewStatus.PROPOSED: "warning", ReviewStatus.VALIDATED: "success",
                       ReviewStatus.REJECTED: "neutral", ReviewStatus.APPLIED: "success",
                       ReviewStatus.SUPERSEDED: "neutral"}
OUTPUT_KIND_LABELS = {"drift": "Anomalie", "recommendation": "Recommandation", "prediction": "Prévision"}
OBLIGATION_LABELS = {
    Obligation.DECRET_TERTIAIRE_OPERAT: "Décret Tertiaire (OPERAT)",
    Obligation.AUDIT_EED: "Audit énergétique (EED)",
    Obligation.VSME: "Rapport VSME",
}
DEADLINE_STATUS_LABELS = {
    DeadlineStatus.UPCOMING: "À venir",
    DeadlineStatus.DUE_SOON: "Échéance proche",
    DeadlineStatus.DONE: "Réalisée",
}
DEADLINE_STATUS_TONES = {DeadlineStatus.UPCOMING: "neutral", DeadlineStatus.DUE_SOON: "warning",
                         DeadlineStatus.DONE: "success"}
EXPORT_LABELS = {ExportFormat.OPERAT: "OPERAT (Décret Tertiaire)", ExportFormat.VSME: "VSME (ESG)"}
EXPORT_STATUS_LABELS = {ExportStatus.PENDING: "En cours", ExportStatus.DONE: "Prêt", ExportStatus.FAILED: "Échec"}
EXPORT_STATUS_TONES = {ExportStatus.PENDING: "neutral", ExportStatus.DONE: "success", ExportStatus.FAILED: "danger"}
DOCUMENT_KIND_LABELS = documents_service.KIND_LABELS
PROVIDER_LABELS = {
    ProviderKind.MOCK: "Simulation (démonstration)",
    ProviderKind.ENEDIS_DATACONNECT: "Enedis Data Connect",
    ProviderKind.ENEDIS_SGE: "Enedis SGE",
    ProviderKind.GRDF_ADICT: "GRDF ADICT",
    ProviderKind.GENERIC_API: "Connecteur",
}


def source_label(dp, connectors) -> str:
    if dp.provider == ProviderKind.GENERIC_API:
        connector = next((c for c in connectors if c.id == dp.connector_id), None)
        return f"connecteur {connector.name}" if connector else "connecteur"
    return PROVIDER_LABELS[dp.provider]


DOCUMENT_STATUS_LABELS = {DocumentStatus.RECEIVED: "À traiter", DocumentStatus.PROCESSED: "Traité",
                          DocumentStatus.REJECTED: "Refusé"}
DOCUMENT_STATUS_TONES = {DocumentStatus.RECEIVED: "neutral", DocumentStatus.PROCESSED: "success",
                         DocumentStatus.REJECTED: "danger"}
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

PAGE_PORTFOLIO = "Portefeuille"
PAGE_DASHBOARD = "Tableau de bord"
PAGE_VALIDATION = "À valider"
PAGE_DRIFTS = "Dérives"
PAGE_RECOMMENDATIONS = "Recommandations"
PAGE_PREDICTIONS = "Prévisions"
PAGE_ASSETS = "Équipements"
PAGE_REGULATORY = "Réglementaire"
PAGE_EXPORTS = "Exports"
PAGE_DOCUMENTS = "Documents"
PAGE_INTEGRATIONS = "Intégrations"
PAGE_MANAGE = "Patrimoine & consentements"
PAGE_NEW_CLIENT = "Nouveau client"
AUDITOR_PAGES = [PAGE_PORTFOLIO, PAGE_DASHBOARD, PAGE_VALIDATION, PAGE_DRIFTS, PAGE_RECOMMENDATIONS,
                 PAGE_PREDICTIONS, PAGE_ASSETS, PAGE_DOCUMENTS, PAGE_REGULATORY, PAGE_EXPORTS, PAGE_MANAGE,
                 PAGE_NEW_CLIENT, PAGE_INTEGRATIONS]
CLIENT_PAGES = [PAGE_DASHBOARD, PAGE_DRIFTS, PAGE_RECOMMENDATIONS, PAGE_PREDICTIONS, PAGE_ASSETS,
                PAGE_DOCUMENTS, PAGE_EXPORTS]
# Le responsable énergie du client a en plus la file de validation (principe P1).
ENERGY_MANAGER_PAGES = [PAGE_DASHBOARD, PAGE_VALIDATION, *CLIENT_PAGES[1:]]


def pages_for(user: User) -> list[str]:
    if user.role != Role.CLIENT_VIEWER:
        return AUDITOR_PAGES
    return ENERGY_MANAGER_PAGES if user.is_energy_manager else CLIENT_PAGES


def role_label(user: User) -> str:
    if user.role == Role.CLIENT_VIEWER:
        return "Responsable énergie, validation" if user.is_energy_manager else "Espace client, lecture seule"
    return ROLE_LABELS[user.role]
# Fenêtre de référence de la barre de progression d'une échéance réglementaire.
DEADLINE_WINDOW_DAYS = 365


# --- Formatage ----------------------------------------------------------------------------


def fmt_number(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def fmt_energy(kwh: float) -> str:
    return f"{fmt_number(kwh / 1000, 1)} MWh" if abs(kwh) >= 10_000 else f"{fmt_number(kwh)} kWh"


def fmt_emissions(kg: float) -> str:
    return f"{fmt_number(kg / 1000, 1)} tCO₂e" if abs(kg) >= 1000 else f"{fmt_number(kg)} kgCO₂e"


def fmt_eur(value: float) -> str:
    return f"{fmt_number(value)} €"


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{'+' if value > 0 else ''}{fmt_number(value, 1)} %"


def fmt_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def fmt_month(yyyy_mm: str) -> str:
    year, month = yyyy_mm.split("-")
    return f"{MONTHS[int(month) - 1]} {year[2:]}"


def variation(current: float, previous: float) -> float | None:
    return (current - previous) / previous * 100 if previous > 0 else None


def style_chart(chart: alt.Chart) -> alt.Chart:
    """Graphiques du design system : fond transparent, grille discrète, libellés atténués."""
    return (
        chart.configure(background="transparent")
        .configure_view(strokeWidth=0)
        .configure_axis(labelFont="Inter", titleFont="Inter", labelColor=ui.MUTED, titleColor=ui.MUTED,
                        gridColor=ui.BORDER, domain=False, ticks=False, labelFontSize=12)
        .configure_legend(labelFont="Inter", labelColor=ui.MUTED, labelFontSize=13, orient="top", symbolType="circle")
    )


def daily_series(db: Session, points: list[DeliveryPoint], start: date, end: date) -> dict[Fluid, list[float]]:
    """Consommation journalière par fluide (pour les mini-courbes des cartes KPI)."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    series = {fluid: [0.0] * len(days) for fluid in Fluid}
    index = {day: i for i, day in enumerate(days)}
    for dp in points:
        for day, kwh in drift_service.daily_kwh(db, dp.id, start, end).items():
            if day in index:
                series[dp.fluid][index[day]] += kwh
    return series


# --- Navigation et messages ----------------------------------------------------------------


def goto(page: str, org_id: int | None = None) -> None:
    """Navigation différée : appliquée au début du prochain passage, avant les widgets."""
    st.session_state["_goto"] = (page, org_id)
    st.rerun()


def flash(message: str) -> None:
    st.session_state["_flash"] = message


def show_flash() -> None:
    message = st.session_state.pop("_flash", None)
    if message:
        st.success(message)


def can_write(user: User) -> bool:
    return user.role in (Role.AUDITOR, Role.ADMIN)


def guard_write(user: User) -> None:
    """Garde-fou F5 : aucune écriture pour un compte espace client, quelle que soit la page."""
    if not can_write(user):
        st.error("Espace client en lecture seule.")
        st.stop()


# --- Initialisation ------------------------------------------------------------------------


@st.cache_resource(show_spinner="Préparation des données (premier lancement : 1 à 2 minutes)…")
def initialize() -> bool:
    prepare_database()
    catch_up()
    return True


def current_user(db: Session) -> User | None:
    user_id = st.session_state.get("user_id")
    return db.get(User, user_id) if user_id else None


def login_page(db: Session) -> None:
    art, center = st.columns([1.05, 1], gap="large", vertical_alignment="center")
    with art:
        ui.render(ui.artwork(
            "connexion.png",
            "Illustration : seize semaines de courbe de charge d'un bâtiment, chaque jour en colonne, "
            "chaque demi-heure en ligne ; la journée ressort en vert, la nuit et le week-end en sombre.",
        ))
    with center:
        ui.render(ui.logo_html(large=True))
        st.title("Connexion")
        st.caption("Suivi énergétique & conformité réglementaire")
        with st.form("login"):
            email = st.text_input("E-mail")
            password = st.text_input("Mot de passe", type="password")
            submitted = st.form_submit_button("Se connecter", type="primary", width="stretch")
        if submitted:
            user = db.scalar(select(User).where(User.email == email.strip().lower()))
            if user and verify_password(password, user.password_hash):
                st.session_state["user_id"] = user.id
                st.rerun()
            st.error("Identifiants invalides")
        st.caption(
            "Comptes de démonstration (mot de passe `demo1234`) :  \n"
            "`auditeur@effismart.demo` (auditeur)  \n"
            "`energie@clinique-du-parc.demo` (responsable énergie : valide les sorties)  \n"
            "`client@clinique-du-parc.demo` (espace client, lecture seule)"
        )


def data_as_of_banner(as_of: date | None) -> None:
    if as_of is None:
        st.warning("Aucune donnée disponible : vérifiez le consentement des points de livraison.")
    else:
        ui.banner(
            f"<span>Données arrêtées au <b>{fmt_date(as_of)}</b>. "
            "Les gestionnaires de réseau publient les consommations le lendemain.</span>"
        )


# --- Pages -----------------------------------------------------------------------------------


def page_portfolio(db: Session, repo: TenantRepository, user: User) -> None:
    ui.page_header("", "Portefeuille clients",
                   "Consommations, dérives et conformité de vos clients.")
    rows = dashboard.portfolio(db, repo)
    if not rows:
        st.info("Aucun client pour l'instant. Créez-en un depuis la page « Nouveau client ».")
        return
    month = fmt_month(rows[0]["month"])
    last_total = sum(r["last_month_kwh"] for r in rows)
    previous_total = sum(r["previous_month_kwh"] for r in rows)
    pending = {r["organization_id"]: validation.count_pending(db, r["organization_id"]) for r in rows}
    ui.kpi_grid(
        [
            {"label": "Clients suivis", "value": str(len(rows)), "icon": "clients",
             "note": f"{sum(r['sites_count'] for r in rows)} sites"},
            {"label": f"Consommation {month}", "value": fmt_energy(last_total), "icon": "total",
             "pct": variation(last_total, previous_total), "note": "vs mois précédent"},
            {"label": "Sorties à valider", "value": str(sum(pending.values())), "icon": "check",
             "note": "anomalies, recommandations, prévisions"},
        ]
    )

    left, right = st.columns([5, 2])
    with left:
        st.header("Clients")
        ui.table(
            ["Organisation", "Sites", f"Conso. {month}", "Variation", "À valider"],
            [
                [
                    f"<b>{ui.e(r['name'])}</b><span class='sub'>{r['delivery_points_count']} points consentis, "
                    f"données au {fmt_date(r['data_as_of'])}</span>",
                    str(r["sites_count"]),
                    fmt_energy(r["last_month_kwh"]),
                    ui.trend(r["variation_pct"])[0],
                    ui.badge(str(pending[r["organization_id"]]),
                             "warning" if pending[r["organization_id"]] else "success"),
                ]
                for r in rows
            ],
            numeric={1, 2},
        )
        st.caption("Ouvrir le tableau de bord d'un client :")
        for column, row in zip(st.columns(len(rows)), rows):
            if column.button(row["name"], key=f"open-{row['organization_id']}", width="stretch"):
                goto(PAGE_DASHBOARD, row["organization_id"])
    with right:
        st.header("Décisions humaines")
        org_ids = [r["organization_id"] for r in rows]
        counts = dict(
            db.execute(
                select(Drift.status, func.count(Drift.id))
                .join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id)
                .join(Site, DeliveryPoint.site_id == Site.id)
                .where(Site.organization_id.in_(org_ids))
                .group_by(Drift.status)
            ).all()
        )
        total = sum(counts.values())
        handled = total - counts.get(DriftStatus.OPEN, 0)
        with st.container(border=True):
            ui.gauge(handled / total * 100 if total else 100,
                     f"{handled} anomalie(s) validée(s) ou écartée(s) sur {total}")

    st.header("Alertes récentes")
    notifications = db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id, repo.org_clause(Notification.organization_id))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(10)
    ).all()
    if not notifications:
        st.caption("Aucune alerte.")
    else:
        with st.container(border=True):
            ui.feed([(to_local(n.created_at).strftime("%d/%m %H:%M"), ui.e(n.message)) for n in notifications])


def page_dashboard(db: Session, repo: TenantRepository, org: Organization) -> None:
    ui.page_header(org.name, "Tableau de bord",
                   "Consommations, coûts et émissions de tous les points de livraison consentis.")
    period = st.session_state.get("period", "30d")  # choisie dans la barre du haut
    start = end = None
    if period == "custom":
        selection = st.date_input(
            "Du … au …",
            value=(yesterday_local() - timedelta(days=29), yesterday_local()),
            max_value=yesterday_local(),
            format="DD/MM/YYYY",
        )
        if len(selection) != 2:
            st.info("Choisissez la date de fin de la période.")
            st.stop()
        start, end = selection

    data = dashboard.organization_dashboard(db, org, period, start, end)
    data_as_of_banner(data["data_as_of"])
    p_start, p_end = data["period"]["start"], data["period"]["end"]

    # Période précédente de même durée, pour les variations des cartes KPI.
    length = (p_end - p_start).days + 1
    previous = dashboard.organization_dashboard(
        db, org, "custom", p_start - timedelta(days=length), p_start - timedelta(days=1)
    )["totals"]
    totals = data["totals"]
    note = f"vs {length} j préc."
    series = daily_series(db, dashboard.consented_delivery_points(db, org.id), p_start, p_end)
    elec, gas = series[Fluid.ELEC], series[Fluid.GAS]
    total = [a + b for a, b in zip(elec, gas)]
    prices = data["estimated_prices_eur_kwh"]
    factors = {f["fluid"]: f["factor_kgco2_per_kwh"] for f in data["emission_factors"]}
    cost = [a * prices["ELEC"] + b * prices["GAS"] for a, b in zip(elec, gas)]
    co2 = [a * factors.get(Fluid.ELEC, 0) + b * factors.get(Fluid.GAS, 0) for a, b in zip(elec, gas)]
    ui.kpi_grid(
        [
            {"label": "Consommation totale", "value": fmt_energy(totals["total_kwh"]), "note": note, "icon": "total", "hero": True,
             "pct": variation(totals["total_kwh"], previous["total_kwh"]), "spark": total},
            {"label": "Électricité", "value": fmt_energy(totals["elec_kwh"]), "icon": "elec",
             "pct": variation(totals["elec_kwh"], previous["elec_kwh"]), "spark": elec},
            {"label": "Gaz", "value": fmt_energy(totals["gas_kwh"]), "icon": "gas",
             "pct": variation(totals["gas_kwh"], previous["gas_kwh"]), "spark": gas},
            {"label": "Coût estimé", "value": fmt_eur(totals["cost_eur"]), "note": "prix indicatifs", "icon": "cost",
             "pct": variation(totals["cost_eur"], previous["cost_eur"]), "spark": cost},
            {"label": "Émissions", "value": fmt_emissions(totals["emissions_kgco2e"]), "icon": "co2",
             "pct": variation(totals["emissions_kgco2e"], previous["emissions_kgco2e"]), "spark": co2},
        ]
    )
    st.caption(f"Période du {fmt_date(p_start)} au {fmt_date(p_end)}")

    # Courbe de charge
    points = data["delivery_points"]
    consented = [p for p in points if p["has_active_consent"]]
    missing = [p for p in points if not p["has_active_consent"]]
    with st.container(border=True):
        st.header("Courbe de charge")
        if consented:
            labels = {p["id"]: f"{p['site_name']}, {FLUID_LABELS[p['fluid']].lower()} {p['external_ref']}" for p in consented}
            dp_id = st.selectbox("Point de livraison", list(labels), format_func=labels.get)
            curve = dashboard.load_curve(db, repo.get_delivery_point(dp_id), p_start, p_end)
            fluid_label = FLUID_LABELS[next(p["fluid"] for p in consented if p["id"] == dp_id)]
            if curve["points"]:
                frame = pd.DataFrame(curve["points"])
                if curve["step"] == "PT30M":
                    frame["t"] = pd.to_datetime(frame["t"], utc=True).dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)
                    y_title = "Puissance moyenne (kW)"
                else:
                    frame["t"] = pd.to_datetime(frame["t"])
                    y_title = "Consommation (kWh/jour)"
                color = FLUID_COLORS[fluid_label]
                base = alt.Chart(frame).encode(
                    x=alt.X("t:T", title=None, axis=alt.Axis(format="%d/%m")),
                    y=alt.Y("value:Q", title=y_title),
                    tooltip=[alt.Tooltip("t:T", title="Date", format="%d/%m/%Y %H:%M"),
                             alt.Tooltip("value:Q", title=curve["unit"], format=",.1f")],
                )
                # Dégradé sous la courbe (design system) : purement décoratif.
                area = base.mark_area(
                    interpolate="monotone",
                    color=alt.Gradient(gradient="linear", x1=0, x2=0, y1=0, y2=1,
                                       stops=[alt.GradientStop(color=color, offset=0),
                                              alt.GradientStop(color="rgba(0,0,0,0)", offset=1)]),
                    opacity=0.35,
                )
                line = base.mark_line(strokeWidth=2.5, color=color, interpolate="monotone")
                chart = (area + line).properties(height=300)
                st.altair_chart(style_chart(chart), width="stretch")
                # Résumé textuel et tableau équivalent : le graphique seul n'est pas accessible.
                peak = frame.loc[frame["value"].idxmax()]
                when = "%d/%m/%Y %H:%M" if curve["step"] == "PT30M" else "%d/%m/%Y"
                st.caption(
                    f"Résumé : maximum {fmt_number(peak['value'], 1)} {curve['unit']} le {peak['t'].strftime(when)}, "
                    f"moyenne {fmt_number(frame['value'].mean(), 1)} {curve['unit']} sur {len(frame)} mesures."
                )
                with st.expander("Voir les données de la courbe"):
                    st.dataframe(
                        frame.rename(columns={"t": "Date", "value": curve["unit"]}), hide_index=True,
                        column_config={"Date": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm")},
                    )
                if curve["aggregated"]:
                    st.caption("Période longue : courbe agrégée au jour (pas 30 min affiché jusqu'à 31 jours).")
            else:
                st.caption("Aucune mesure sur la période.")
        if missing:
            st.warning(
                "Consentement requis, aucune donnée collectée pour : "
                + ", ".join(f"{p['site_name']} ({p['external_ref']})" for p in missing)
            )

    left, right = st.columns([2, 1])
    with left, st.container(border=True):
        st.header("Consommation mensuelle")
        monthly = pd.DataFrame(data["monthly"])
        monthly["Mois"] = monthly["month"].map(fmt_month)
        long = monthly.melt(id_vars=["month", "Mois"], value_vars=["elec_kwh", "gas_kwh"],
                            var_name="fluid", value_name="kWh")
        long["Énergie"] = long["fluid"].map({"elec_kwh": "Électricité", "gas_kwh": "Gaz"})
        bars = (
            alt.Chart(long)
            .mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2)
            .encode(
                x=alt.X("Mois:N", sort=list(monthly["Mois"]), title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("kWh:Q", stack=True, title="kWh", axis=alt.Axis(format="~s")),
                color=alt.Color("Énergie:N", title=None, scale=alt.Scale(domain=list(FLUID_COLORS),
                                                                         range=list(FLUID_COLORS.values()))),
                tooltip=["Mois", "Énergie", alt.Tooltip("kWh:Q", format=",.0f")],
            )
            .properties(height=280)
        )
        st.altair_chart(style_chart(bars), width="stretch")
        with st.expander("Voir les données mensuelles"):
            st.dataframe(
                monthly[["Mois", "elec_kwh", "gas_kwh"]].rename(
                    columns={"elec_kwh": "Électricité (kWh)", "gas_kwh": "Gaz (kWh)"}),
                hide_index=True,
            )
    with right, st.container(border=True):
        st.header("Part de l'électricité")
        total = totals["elec_kwh"] + totals["gas_kwh"]
        if total > 0:
            ui.gauge(totals["elec_kwh"] / total * 100,
                     f"Électricité {fmt_energy(totals['elec_kwh'])}, gaz {fmt_energy(totals['gas_kwh'])}")
        else:
            st.caption("Aucune donnée.")

    if sees_unvalidated(repo.user):
        pending = validation.pending_outputs(repo, org.id)
        st.header(f"À valider ({len(pending)})")
        if not pending:
            ui.empty_state("Rien à valider", "Toutes les sorties de la plateforme ont été examinées.")
        else:
            with st.container(border=True):
                ui.feed([(fmt_date(to_local(output_created(i.kind, i.output)).date()),
                          f"{ui.badge(OUTPUT_KIND_LABELS[i.kind])} {confidence_badge(i.output.confidence)} "
                          f"{ui.e(output_title(i.kind, i.output))}")
                         for i in pending[:5]])
            if st.button("Ouvrir la file de validation", icon=":material/fact_check:"):
                goto(PAGE_VALIDATION, org.id)
    else:
        drifts = repo.list_drifts(org.id, DriftStatus.QUALIFIED)[:5]
        st.header("Anomalies confirmées")
        if not drifts:
            ui.empty_state("Aucune anomalie confirmée", "La consommation suit son rythme habituel.")
        else:
            with st.container(border=True):
                ui.feed([
                    (fmt_date(d.day),
                     f"{ui.badge(DRIFT_KIND_LABELS[d.kind.value], 'danger')} {ui.e(d.delivery_point.site.name)} : "
                     f"{ui.e(d.details)} <b>{ui.e(fmt_pct(d.deviation_pct))}</b>")
                    for d in drifts
                ])

    st.caption(
        "Facteurs d'émission : "
        + " ; ".join(
            f"{FLUID_LABELS[f['fluid']].lower()} {fmt_number(f['factor_kgco2_per_kwh'], 3)} kgCO₂e/kWh ({f['version']})"
            for f in data["emission_factors"]
        )
    )


# --- Principe P1 : sorties de la plateforme expliquées, validées par un humain --------------------------

# Décisions possibles selon le type de sortie et son statut : (libellé du bouton, statut visé).
ACTIONS = {
    ("drift", DriftStatus.OPEN): [("Valider", DriftStatus.QUALIFIED), ("Écarter", DriftStatus.IGNORED)],
    ("drift", DriftStatus.QUALIFIED): [("Rouvrir", DriftStatus.OPEN)],
    ("drift", DriftStatus.IGNORED): [("Rouvrir", DriftStatus.OPEN)],
    ("recommendation", ReviewStatus.PROPOSED): [("Valider", ReviewStatus.VALIDATED), ("Écarter", ReviewStatus.REJECTED)],
    ("recommendation", ReviewStatus.VALIDATED): [("Déclarer appliquée", ReviewStatus.APPLIED),
                                                 ("Écarter", ReviewStatus.REJECTED)],
    ("recommendation", ReviewStatus.APPLIED): [("Annuler la déclaration", ReviewStatus.VALIDATED)],
    ("recommendation", ReviewStatus.REJECTED): [("Rouvrir", ReviewStatus.PROPOSED)],
    ("prediction", ReviewStatus.PROPOSED): [("Valider", ReviewStatus.VALIDATED), ("Écarter", ReviewStatus.REJECTED)],
    ("prediction", ReviewStatus.VALIDATED): [("Retirer la validation", ReviewStatus.PROPOSED)],
    ("prediction", ReviewStatus.REJECTED): [("Rouvrir", ReviewStatus.PROPOSED)],
}
# (explication, libellé du commentaire ou None)
ACTION_HELP = {
    "Valider": ("Vous confirmez cette sortie après examen de son raisonnement. Elle devient visible par tout "
                "le client et peut être transmise aux outils partenaires.", "Commentaire (facultatif)"),
    "Écarter": ("La sortie est conservée avec votre motif, qui aide à fiabiliser la plateforme.",
                "Motif (obligatoire)"),
    "Déclarer appliquée": ("À déclarer une fois l'intervention réalisée sur site : la plateforme ne pilote "
                           "aucun équipement.", "Ce qui a été fait, par qui (facultatif)"),
    "Annuler la déclaration": ("La recommandation redevient « validée, à mettre en œuvre ».", None),
    "Rouvrir": ("La sortie redevient « à valider ».", None),
    "Retirer la validation": ("La prévision redevient « à valider » et n'est plus visible par le client.", None),
}
DECISION_MESSAGES = {
    DriftStatus.QUALIFIED: "Anomalie validée.", DriftStatus.IGNORED: "Anomalie écartée.",
    DriftStatus.OPEN: "Anomalie remise à valider.", ReviewStatus.VALIDATED: "Décision enregistrée : validée.",
    ReviewStatus.REJECTED: "Décision enregistrée : écartée.", ReviewStatus.APPLIED: "Recommandation déclarée appliquée.",
    ReviewStatus.PROPOSED: "Remise à valider.",
}


def output_created(kind: str, output):
    return output.detected_at if kind == "drift" else output.created_at


def output_title(kind: str, output) -> str:
    if kind == "drift":
        dp = output.delivery_point
        return (f"{DRIFT_KIND_LABELS[output.kind.value]} : {dp.site.name}, {dp.external_ref}, le "
                f"{fmt_date(output.day)} ({fmt_pct(output.deviation_pct)})")
    if kind == "recommendation":
        return output.title
    return f"Projection {output.year} : {output.site.name}, {FLUID_LABELS[output.fluid].lower()}"


def confidence_badge(score: float | None) -> str:
    label, tone = validation.confidence_level(score)
    value = "" if score is None else f" {fmt_number(score * 100)} %"
    return ui.badge(f"Confiance {label.lower()}{value}", tone)


def gain_html(output, kind: str) -> str:
    """Gain estimé écrit en clair : énergie, euros, CO₂."""
    if output.gain_kwh is None:
        return "<div class='es-output-gain'>Gain estimé : non chiffré.</div>"
    parts = [fmt_energy(abs(output.gain_kwh)), fmt_eur(abs(output.gain_eur or 0))]
    if output.gain_kgco2e:
        parts.append(fmt_emissions(abs(output.gain_kgco2e)))
    values = ", ".join(parts)
    if kind == "prediction":
        label = ("Économie projetée par rapport à l'année précédente" if output.gain_kwh >= 0
                 else "Surconsommation projetée par rapport à l'année précédente")
        return f"<div class='es-output-gain'>{label} : <b>{ui.e(values)}</b></div>"
    label = "Gain estimé si corrigée" if kind == "drift" else "Gain estimé si appliquée"
    return f"<div class='es-output-gain'>{label} : <b>{ui.e(values)} par an</b></div>"


def decision_html(db: Session, created_at, decided_by: int | None, decided_at, comment: str | None,
                  status_label: str, pending: bool) -> str:
    parts = [f"Proposée par la plateforme le {to_local(created_at):%d/%m/%Y}."]
    if decided_by and not pending:
        who = db.get(User, decided_by)
        role = "auditeur" if who is not None and who.role == Role.AUDITOR else "responsable énergie"
        parts.append(f"{status_label} par {ui.e(who.email if who else 'compte supprimé')} ({role}) le "
                     f"{to_local(decided_at):%d/%m/%Y}" + (f" : « {ui.e(comment)} »" if comment else "") + ".")
    elif pending:
        parts.append("En attente de la décision de l'auditeur ou du responsable énergie.")
    elif comment:
        parts.append(ui.e(comment))
    return f"<div class='es-output-meta'>{' '.join(parts)}</div>"


def explanation_block(output) -> None:
    """Raisonnement, facteurs du niveau de confiance et hypothèses du gain."""
    label, _ = validation.confidence_level(output.confidence)
    with st.expander("Raisonnement, niveau de confiance et hypothèses"):
        st.markdown("**Raisonnement**")
        ui.render("<ol class='es-steps'>" + "".join(f"<li>{ui.e(step)}</li>" for step in output.reasoning or [])
                  + "</ol>")
        score = f" ({fmt_number(output.confidence * 100)} %)" if output.confidence is not None else ""
        st.markdown(f"**Niveau de confiance : {label.lower()}{score}**")
        rows = []
        for factor in output.confidence_factors or []:
            up = factor["delta"] >= 0
            points = fmt_number(abs(factor["delta"]) * 100)
            rows.append(f"<li><span class='delta {'up' if up else 'down'}'>{'▲ +' if up else '▼ −'}{points} pts</span>"
                        f"<span>{ui.e(factor['label'])}</span></li>")
        ui.render("<ul class='es-factors'>" + "".join(rows) + "</ul>")
        st.caption("Départ à 50 points, ajustés par chaque facteur, bornés entre 5 et 95 : la plateforme n'est "
                   "jamais certaine, la décision reste humaine.")
        st.markdown("**Hypothèses du gain estimé**")
        st.caption(output.gain_basis or "—")
        st.caption(f"Algorithme : {output.algorithm or '—'}")


def _decide(db: Session, repo: TenantRepository, user: User, kind: str, output_id: int, status, comment) -> None:
    try:
        if kind == "drift":
            _, rec = validation.review_drift(db, repo, user, output_id, status, comment)
            message = DECISION_MESSAGES[status]
            if rec is not None and rec.drift_id == output_id:
                message += f" Recommandation proposée, à valider à son tour : « {rec.title} »."
            elif rec is not None:
                message += f" Elle appuie la recommandation existante « {rec.title} »."
        elif kind == "recommendation":
            validation.review_recommendation(db, repo, user, output_id, status, comment)
            message = DECISION_MESSAGES[status]
        else:
            validation.review_prediction(db, repo, user, output_id, status, comment)
            message = DECISION_MESSAGES[status]
    except validation.ValidationError as exc:
        st.error(str(exc))
        return
    integrations.dispatch_in_background()
    flash(message)
    st.rerun()


def review_actions(db: Session, repo: TenantRepository, user: User, kind: str, output, key: str) -> None:
    """Boutons de décision, réservés à l'auditeur et au responsable énergie."""
    if not validation.can_validate(user):
        return
    for label, target in ACTIONS.get((kind, output.status), []):
        explanation, comment_label = ACTION_HELP[label]
        if kind == "drift" and label == "Valider":
            explanation += " Une recommandation ciblée sera proposée, elle aussi à valider."
        with st.popover(label, width="stretch", key=f"{key}-pop-{target.value}",
                        type="primary" if label == "Valider" else "secondary"):
            st.markdown(f"**{label} ?**")
            st.caption(explanation)
            comment = (st.text_input(comment_label, key=f"{key}-comment-{target.value}", max_chars=2000)
                       if comment_label else None)
            if st.button("Confirmer", key=f"{key}-confirm-{target.value}", type="primary"):
                _decide(db, repo, user, kind, output.id, target, comment)


def prediction_chart(prediction: Prediction) -> None:
    rows = []
    for month in prediction.monthly or []:
        label = fmt_month(month["month"])
        rows.append({"Mois": label, "Série": "Mesuré", "kWh": month["measured"]})
        rows.append({"Mois": label, "Série": "Projeté", "kWh": month["predicted"]})
    if not rows:
        return
    order = [fmt_month(m["month"]) for m in prediction.monthly]
    bars = alt.Chart(pd.DataFrame(rows)).mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=alt.X("Mois:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("kWh:Q", stack=True, title="kWh", axis=alt.Axis(format="~s")),
        color=alt.Color("Série:N", title=None, scale=alt.Scale(domain=["Mesuré", "Projeté"],
                                                               range=[ui.PRIMARY, ui.SERIES_2])),
        tooltip=["Mois", "Série", alt.Tooltip("kWh:Q", format=",.0f")],
    )
    chart = bars
    if prediction.reference_kwh is not None:
        reference = pd.DataFrame([{"Mois": fmt_month(m["month"]), "Année précédente": m["reference"]}
                                  for m in prediction.monthly])
        line = alt.Chart(reference).mark_line(point=True, strokeDash=[4, 3], color=ui.MUTED).encode(
            x=alt.X("Mois:N", sort=order), y="Année précédente:Q",
            tooltip=["Mois", alt.Tooltip("Année précédente:Q", format=",.0f")],
        )
        chart = bars + line
    st.altair_chart(style_chart(chart.properties(height=220)), width="stretch")
    st.caption("Barres : mesuré puis projeté, par mois ; pointillés : même mois de l'année précédente.")


def output_card(db: Session, repo: TenantRepository, user: User, kind: str, output, key: str) -> None:
    """Carte d'une sortie de la plateforme : quoi, pourquoi (raisonnement), combien (gain), avec quelle confiance."""
    with st.container(border=True):
        info, actions = st.columns([4, 1])
        with info:
            if kind == "drift":
                dp = output.delivery_point
                status_html = ui.status(DRIFT_STATUS_LABELS[output.status], DRIFT_STATUS_TONES[output.status])
                head = [ui.badge("Anomalie"), ui.badge(DRIFT_KIND_LABELS[output.kind.value])]
                title = f"{dp.site.name}, {FLUID_LABELS[dp.fluid].lower()} {dp.external_ref}, le {fmt_date(output.day)}"
                body = f"<div>{ui.e(output.details)} <b>({ui.e(fmt_pct(output.deviation_pct))})</b></div>"
                meta = decision_html(db, output.detected_at, output.qualified_by, output.qualified_at, output.comment,
                                     DRIFT_STATUS_LABELS[output.status], output.status == DriftStatus.OPEN)
            else:
                status_html = ui.status(REVIEW_STATUS_LABELS[output.status], REVIEW_STATUS_TONES[output.status])
                pending = output.status == ReviewStatus.PROPOSED
                decided = (REVIEW_STATUS_LABELS[ReviewStatus.VALIDATED] if output.status == ReviewStatus.APPLIED
                           else REVIEW_STATUS_LABELS[output.status])
                meta = decision_html(db, output.created_at, output.reviewed_by, output.reviewed_at,
                                     output.review_comment, decided, pending)
                if kind == "recommendation":
                    head = [ui.badge("Recommandation"), ui.badge(recommendations_service.KIND_LABELS[output.kind])]
                    title = output.title
                    body = f"<div class='es-output-action'><b>Action proposée</b> : {ui.e(output.action)}</div>"
                    if output.drift_id:
                        try:
                            origin = repo.get_drift(output.drift_id)
                            others = len(output.supporting_drift_ids or [])
                            body += (f"<div class='es-output-meta'>Anomalie d'origine : "
                                     f"{ui.e(DRIFT_KIND_LABELS[origin.kind.value].lower())} du {fmt_date(origin.day)}"
                                     + (f", appuyée par {others} autre(s) anomalie(s) validée(s)" if others else "")
                                     + ".</div>")
                        except ResourceNotFound:
                            pass
                    if output.applied_at:
                        who = db.get(User, output.applied_by) if output.applied_by else None
                        meta += (f"<div class='es-output-meta'>Déclarée appliquée par "
                                 f"{ui.e(who.email if who else 'compte supprimé')} le {to_local(output.applied_at):%d/%m/%Y}"
                                 + (f" : « {ui.e(output.applied_comment)} »" if output.applied_comment else "")
                                 + ".</div>")
                else:
                    head = [ui.badge("Prévision")]
                    fluid = FLUID_LABELS[output.fluid].lower()
                    title = f"Consommation {output.year} projetée, {output.site.name}, {fluid}"
                    body = (f"<div><b>{ui.e(fmt_energy(output.predicted_kwh))}</b> (intervalle "
                            f"{ui.e(fmt_energy(output.low_kwh))} – {ui.e(fmt_energy(output.high_kwh))}), dont "
                            f"{ui.e(fmt_energy(output.measured_kwh))} déjà mesurés au {fmt_date(output.data_as_of)}.")
                    if output.reference_kwh:
                        body += (f" Année {output.year - 1} : {ui.e(fmt_energy(output.reference_kwh))} "
                                 f"({ui.e(fmt_pct(variation(output.predicted_kwh, output.reference_kwh)))}).")
                    body += "</div>"
            ui.render(
                f"<div class='es-output-head'>{' '.join(head)} {status_html} {confidence_badge(output.confidence)}</div>"
                f"<div class='es-output-title'>{ui.e(title)}</div>{body}{gain_html(output, kind)}{meta}"
            )
            if kind == "prediction":
                prediction_chart(output)
            explanation_block(output)
        with actions:
            review_actions(db, repo, user, kind, output, key)


def pending_count(repo: TenantRepository, org_ids: list[int]) -> int:
    return sum(validation.count_pending(repo.db, org_id) for org_id in org_ids)


def page_validation(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    ui.page_header(org.name, "À valider",
                   "La plateforme propose, un humain décide : chaque anomalie, recommandation et prévision attend "
                   "la validation de l'auditeur ou du responsable énergie avant d'être appliquée ou montrée au client.")
    items = validation.pending_outputs(repo, org.id)
    counts = {kind: sum(1 for i in items if i.kind == kind) for kind in OUTPUT_KIND_LABELS}
    at_stake = sum(i.output.gain_eur or 0 for i in items if i.kind != "prediction" and (i.output.gain_eur or 0) > 0)
    scores = [i.output.confidence for i in items if i.output.confidence is not None]
    ui.kpi_grid([
        {"label": "Sorties à valider", "value": str(len(items)), "icon": "check",
         "note": f"{counts['drift']} anomalie(s), {counts['recommendation']} recommandation(s), "
                 f"{counts['prediction']} prévision(s)"},
        {"label": "Gain estimé en jeu", "value": fmt_eur(at_stake), "icon": "cost",
         "note": "par an, anomalies et recommandations"},
        {"label": "Confiance moyenne", "value": f"{fmt_number(sum(scores) / len(scores) * 100)} %" if scores else "—",
         "icon": "drifts", "note": "jamais 100 % : la décision reste humaine"},
    ])
    if not validation.can_validate(user):
        st.info("Lecture seule : la validation revient à l'auditeur partenaire ou au responsable énergie du client, "
                "jamais à la plateforme.")
    filters = {"Tout": None, "Anomalies": "drift", "Recommandations": "recommendation", "Prévisions": "prediction"}
    choice = st.radio("Type de sortie", list(filters), horizontal=True, key="validation_filter")
    shown = [i for i in items if filters[choice] in (None, i.kind)]
    if not shown:
        ui.empty_state("Rien à valider", "Toutes les sorties de la plateforme ont été examinées par un humain.")
        return
    st.caption("Classement : gain estimé × niveau de confiance, du plus utile au moins utile. "
               "Chaque sortie se valide une à une, après lecture de son raisonnement.")
    limit = st.session_state.get("validation_limit", 10)
    for item in shown[:limit]:
        output_card(db, repo, user, item.kind, item.output, key=f"v-{item.kind}-{item.output.id}")
    if len(shown) > limit and st.button(f"Afficher 10 de plus ({len(shown) - limit} restante(s))"):
        st.session_state["validation_limit"] = limit + 10
        st.rerun()


def page_drifts(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    ui.page_header(org.name, "Dérives de consommation",
                   "Anomalies détectées par la plateforme, chacune avec son raisonnement, son gain estimé et son "
                   "niveau de confiance, puis validées ou écartées par un humain. Aucune action automatique.")
    if sees_unvalidated(user):
        filters = {"À valider": DriftStatus.OPEN, "Validées": DriftStatus.QUALIFIED,
                   "Écartées": DriftStatus.IGNORED, "Toutes": None}
        choice = st.radio("Statut", list(filters), horizontal=True, key="drift_filter")
        drifts = repo.list_drifts(org.id, filters[choice])
    else:
        st.caption("Seules les anomalies validées par votre auditeur ou votre responsable énergie apparaissent ici.")
        drifts = repo.list_drifts(org.id)
    if not drifts:
        ui.empty_state("Aucune dérive", "Rien pour ce filtre : la consommation suit son rythme habituel.")
        return
    ui.table(
        ["Date", "Type", "Point de livraison", "Écart", "Confiance", "Gain estimé", "Statut"],
        [
            [
                fmt_date(d.day),
                ui.e(DRIFT_KIND_LABELS[d.kind.value]),
                f"{ui.e(d.delivery_point.site.name)}<span class='sub'>"
                f"{ui.e(FLUID_LABELS[d.delivery_point.fluid])} {ui.e(d.delivery_point.external_ref)}</span>",
                f"<b>{ui.e(fmt_pct(d.deviation_pct))}</b>",
                confidence_badge(d.confidence),
                ui.e(f"{fmt_eur(d.gain_eur)}/an") if d.gain_eur else "—",
                ui.status(DRIFT_STATUS_LABELS[d.status], DRIFT_STATUS_TONES[d.status]),
            ]
            for d in drifts
        ],
        numeric={3, 5},
    )
    st.header("Détail d'une anomalie")
    labels = {
        d.id: f"{fmt_date(d.day)}, {DRIFT_KIND_LABELS[d.kind.value].lower()}, {d.delivery_point.site.name} "
              f"({fmt_pct(d.deviation_pct)}), {DRIFT_STATUS_LABELS[d.status].lower()}"
        for d in drifts
    }
    drift_id = st.selectbox("Anomalie", list(labels), format_func=labels.get, key="drift_detail")
    output_card(db, repo, user, "drift", repo.get_drift(drift_id), key=f"d-{drift_id}")


def page_recommendations(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    ui.page_header(org.name, "Recommandations d'optimisation",
                   "Proposées à partir d'anomalies validées et du graphe physique des équipements. Un humain les "
                   "valide, puis déclare leur mise en œuvre : la plateforme ne pilote aucun équipement.")
    recs = repo.list_recommendations(org.id)

    def total(status: ReviewStatus) -> tuple[int, float]:
        chosen = [r for r in recs if r.status == status]
        return len(chosen), sum(r.gain_eur or 0 for r in chosen)

    proposed, validated, applied = (total(s) for s in (ReviewStatus.PROPOSED, ReviewStatus.VALIDATED,
                                                        ReviewStatus.APPLIED))
    cards = [
        {"label": "Validées, à mettre en œuvre", "value": str(validated[0]), "icon": "check",
         "note": f"{fmt_eur(validated[1])} par an estimés"},
        {"label": "Appliquées", "value": str(applied[0]), "icon": "cost", "note": f"{fmt_eur(applied[1])} par an estimés"},
    ]
    if sees_unvalidated(user):
        cards.insert(0, {"label": "À valider", "value": str(proposed[0]), "icon": "drifts",
                         "note": f"{fmt_eur(proposed[1])} par an en jeu"})
        filters = {"À valider": [ReviewStatus.PROPOSED], "Validées": [ReviewStatus.VALIDATED],
                   "Appliquées": [ReviewStatus.APPLIED],
                   "Écartées ou retirées": [ReviewStatus.REJECTED, ReviewStatus.SUPERSEDED], "Toutes": None}
    else:
        filters = {"Validées": [ReviewStatus.VALIDATED], "Appliquées": [ReviewStatus.APPLIED], "Toutes": None}
    ui.kpi_grid(cards)
    choice = st.radio("Statut", list(filters), horizontal=True, key="rec_filter")
    shown = [r for r in recs if filters[choice] is None or r.status in filters[choice]]
    if not shown:
        ui.empty_state("Aucune recommandation", "Les recommandations naissent des anomalies validées : validez une "
                       "anomalie pour que la plateforme en propose une." if sees_unvalidated(user)
                       else "Aucune recommandation validée pour l'instant.")
        return
    for rec in shown:
        output_card(db, repo, user, "recommendation", rec, key=f"r-{rec.id}")


def page_predictions(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    ui.page_header(org.name, "Prévisions de consommation",
                   "Projection de l'année en cours par site et par énergie (signature énergétique × météo normale). "
                   "Chaque projection est expliquée et doit être validée avant d'être montrée au client.")
    if can_write(user) and st.button("Recalculer les projections", icon=":material/refresh:",
                                     help="Nouvelle projection avec les dernières données, proposée à la validation"):
        with st.spinner("Calcul des projections…"):
            created = predictions_service.refresh_predictions(db, org.id, force=True)
        flash(f"{len(created)} projection(s) proposée(s), à valider." if created
              else "Historique insuffisant : aucune projection calculée.")
        st.rerun()
    preds = repo.list_predictions(org.id)
    latest: dict[tuple, Prediction] = {}
    for prediction in preds:
        if prediction.status != ReviewStatus.SUPERSEDED:
            latest.setdefault((prediction.site_id, prediction.fluid, prediction.year), prediction)
    if not latest:
        ui.empty_state("Aucune prévision", "Aucune projection validée pour l'instant." if not sees_unvalidated(user)
                       else "Au moins 60 jours d'historique consenti sont nécessaires par site et par énergie.")
        return
    for prediction in latest.values():
        output_card(db, repo, user, "prediction", prediction, key=f"p-{prediction.id}")
    history = [p for p in preds if p not in latest.values()]
    if history:
        with st.expander(f"Historique des projections ({len(history)})"):
            ui.table(
                ["Calculée le", "Site", "Énergie", "Données au", "Projection", "Statut"],
                [[fmt_date(to_local(p.created_at).date()), ui.e(p.site.name), ui.e(FLUID_LABELS[p.fluid]),
                  fmt_date(p.data_as_of), ui.e(fmt_energy(p.predicted_kwh)),
                  ui.status(REVIEW_STATUS_LABELS[p.status], REVIEW_STATUS_TONES[p.status])] for p in history],
                numeric={4},
            )


# --- Graphe physique des équipements -----------------------------------------------------------------

ASSET_KIND_ORDER = [AssetNodeKind.METER, AssetNodeKind.EQUIPMENT, AssetNodeKind.FLOW, AssetNodeKind.ZONE,
                    AssetNodeKind.USAGE]


def node_label(node) -> str:
    return f"{node.name} ({assets.KIND_LABELS[node.kind].lower()})"


def asset_editor(db: Session, repo: TenantRepository, user: User, site_id: int, graph: assets.SiteGraph) -> None:
    nodes = sorted(graph.nodes.values(), key=lambda n: (ASSET_KIND_ORDER.index(n.kind), n.name.lower()))
    by_id = {n.id: n for n in nodes}
    st.header("Modifier le graphe")
    tab_add, tab_link, tab_edit = st.tabs(["Ajouter un élément", "Relier deux éléments", "Modifier ou supprimer"],
                                          key="asset_tabs")
    with tab_add:
        kinds = ASSET_KIND_ORDER[1:]  # les compteurs naissent des points de livraison
        kind = st.selectbox("Type d'élément", kinds, format_func=assets.KIND_LABELS.get, key="asset_new_kind")
        categories = assets.CATEGORIES[kind]
        with st.form(f"asset-new-{kind.value}", clear_on_submit=True):
            category = st.selectbox("Catégorie", list(categories), format_func=lambda c: categories[c].label)
            name = st.text_input("Nom", max_chars=120, placeholder={
                AssetNodeKind.EQUIPMENT: "ex. Chaudière gaz n° 2", AssetNodeKind.FLOW: "ex. Eau chaude de chauffage",
                AssetNodeKind.ZONE: "ex. Bureaux du 2e étage", AssetNodeKind.USAGE: "ex. Chauffage"}[kind])
            power = surface = None
            always = False
            if kind == AssetNodeKind.EQUIPMENT:
                power = st.number_input("Puissance nominale (kW)", min_value=0.0, step=1.0, value=None,
                                        help="Sert à relier un excès de consommation mesuré à l'équipement probable.")
            if kind == AssetNodeKind.ZONE:
                surface = st.number_input("Surface (m²)", min_value=0.0, step=10.0, value=None)
                always = st.checkbox("Occupée 24 h/24 (chambres, local serveurs…)",
                                     help="Ses équipements fonctionnent légitimement la nuit et le week-end.")
            if st.form_submit_button("Ajouter au graphe", type="primary"):
                try:
                    assets.create_node(db, repo, user, site_id, kind=kind, category_code=category, name=name,
                                       power_kw=power or None, surface_m2=surface or None, always_occupied=always)
                except assets.AssetGraphError as exc:
                    st.error(str(exc))
                else:
                    flash(f"« {name.strip()} » ajouté au graphe.")
                    st.rerun()
    with tab_link:
        if len(nodes) < 2:
            st.caption("Ajoutez d'abord au moins deux éléments.")
        else:
            source_id = st.selectbox("Élément de départ", list(by_id), format_func=lambda i: node_label(by_id[i]),
                                     key="asset_link_source")
            source = by_id[source_id]
            options = assets.allowed_relations_from(source.kind)
            if not options:
                st.caption("Un usage est l'aboutissement d'une chaîne physique : il ne mène à aucun autre élément.")
            else:
                relation, target_kind = st.selectbox(
                    "Relation", options, key=f"asset_link_relation_{source.kind.value}",
                    format_func=lambda o: f"{assets.RELATION_LABELS[o[0]]} → {assets.KIND_LABELS[o[1]].lower()}")
                targets = [n for n in nodes if n.kind == target_kind and n.id != source.id]
                if not targets:
                    st.caption(f"Aucun élément de type « {assets.KIND_LABELS[target_kind].lower()} » sur ce site.")
                else:
                    target_id = st.selectbox("Élément d'arrivée", [n.id for n in targets],
                                             format_func=lambda i: node_label(by_id[i]), key="asset_link_target")
                    st.caption(f"Relation créée : {source.name} → {assets.RELATION_LABELS[relation]} → "
                               f"{by_id[target_id].name}")
                    if st.button("Relier", type="primary", key="asset_link_submit"):
                        try:
                            assets.create_relation(db, repo, user, source.id, relation, target_id)
                        except assets.AssetGraphError as exc:
                            st.error(str(exc))
                        else:
                            flash("Relation ajoutée au graphe.")
                            st.rerun()
    with tab_edit:
        node_id = st.selectbox("Élément", list(by_id), format_func=lambda i: node_label(by_id[i]), key="asset_edit_node")
        node = by_id[node_id]
        categories = assets.CATEGORIES[node.kind]
        with st.form(f"asset-edit-{node.id}"):
            name = st.text_input("Nom", value=node.name, max_chars=120)
            codes = list(categories)
            category = node.category
            if node.kind != AssetNodeKind.METER:
                category = st.selectbox("Catégorie", codes, index=codes.index(node.category) if node.category in codes else 0,
                                        format_func=lambda c: categories[c].label)
            power, surface, always = node.power_kw, node.surface_m2, node.always_occupied
            if node.kind == AssetNodeKind.EQUIPMENT:
                power = st.number_input("Puissance nominale (kW)", min_value=0.0, step=1.0, value=node.power_kw)
            if node.kind == AssetNodeKind.ZONE:
                surface = st.number_input("Surface (m²)", min_value=0.0, step=10.0, value=node.surface_m2)
                always = st.checkbox("Occupée 24 h/24", value=node.always_occupied)
            if st.form_submit_button("Enregistrer", type="primary"):
                try:
                    assets.update_node(db, repo, user, node.id, name=name, category_code=category,
                                       power_kw=power or None, surface_m2=surface or None, always_occupied=always)
                except assets.AssetGraphError as exc:
                    st.error(str(exc))
                else:
                    flash("Élément mis à jour.")
                    st.rerun()
        links = [(rel, node, other) for rel, other in graph.successors(node.id)] + \
                [(rel, other, node) for rel, other in graph.predecessors(node.id)]
        if links:
            st.markdown("**Relations de cet élément**")
            for rel, source, target in links:
                text_col, button_col = st.columns([4, 1], vertical_alignment="center")
                text_col.caption(f"{source.name} → {assets.RELATION_LABELS[rel.kind]} → {target.name}")
                if button_col.button("Supprimer", key=f"asset-rel-rm-{rel.id}", width="stretch"):
                    assets.delete_relation(db, repo, user, rel.id)
                    flash("Relation supprimée.")
                    st.rerun()
        if node.kind != AssetNodeKind.METER:
            with st.popover("Supprimer l'élément", key=f"asset-rm-pop-{node.id}"):
                st.markdown(f"**Supprimer « {node.name} » ?**")
                st.caption(f"Ses {len(links)} relation(s) seront supprimées. Les recommandations qui le visaient "
                           "gardent leur texte.")
                if st.button("Confirmer la suppression", key=f"asset-rm-{node.id}", type="primary"):
                    assets.delete_node(db, repo, user, node.id)
                    flash(f"« {node.name} » supprimé du graphe.")
                    st.rerun()
        else:
            st.caption("Un compteur suit son point de livraison : il se gère dans « Patrimoine & consentements ».")


def page_assets(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    ui.page_header(org.name, "Équipements",
                   "Graphe physique : ce qui alimente quoi, ce qui produit quoi, quelles zones et quels usages sont "
                   "desservis. Ce n'est pas une arborescence de rangement : c'est le chemin réel de l'énergie, "
                   "utilisé pour cibler les recommandations.")
    sites = {s.id: s for s in repo.list_sites(org.id)}
    if not sites:
        st.info("Aucun site pour ce client : créez-en un dans « Patrimoine & consentements ».")
        return
    site_id = st.selectbox("Site", list(sites), format_func=lambda i: sites[i].name, key="asset_site")
    graph = assets.site_graph(db, repo, site_id)
    active = [r for r in repo.list_recommendations(org.id, [ReviewStatus.PROPOSED, ReviewStatus.VALIDATED])
              if r.site_id == site_id and r.equipment_id]
    with st.container(border=True):
        st.header("Graphe physique")
        if graph.relations:
            st.graphviz_chart(graph.to_dot({r.equipment_id for r in active}), width="stretch")
        else:
            st.caption("Aucune relation pour l'instant : seuls les compteurs sont connus.")
        ui.render(
            "<div class='es-legend'><span><i class='k-meter'></i>Compteur</span><span><i class='k-equipment'></i>"
            "Équipement</span><span><i class='k-flow'></i>Fluide produit</span><span><i class='k-zone'></i>Zone</span>"
            "<span><i class='k-usage'></i>Usage</span><span><i class='k-target'></i>Visé par une recommandation "
            "en cours</span></div>"
        )
    left, right = st.columns([3, 2])
    with left:
        st.header("Chaînes physiques")
        meters = [n for n in graph.nodes.values() if n.kind == AssetNodeKind.METER]
        chains = [text for meter in meters for text in assets.chain_summaries(graph.physical_paths(meter.id))]
        if not chains:
            st.caption("Aucune chaîne complète : reliez les compteurs à leurs équipements.")
        else:
            st.caption("Du compteur à l'usage final ; seuls les usages que chaque équipement peut réellement "
                       "servir sont retenus (un groupe froid ne mène pas au chauffage).")
        for text in chains:
            ui.render(f"<div class='es-chain'>{ui.e(text)}</div>")
    with right:
        st.header("Maillons manquants")
        warnings = graph.warnings()
        if warnings:
            st.caption("Sans ces maillons, la plateforme ne peut pas localiser la cause d'une anomalie.")
            ui.render("<ul class='es-warnings'>" + "".join(f"<li>{ui.e(w)}</li>" for w in warnings) + "</ul>")
        else:
            ui.render(ui.status("Graphe complet : chaque compteur mène à un usage", "success"))
    if assets.can_edit(user):
        asset_editor(db, repo, user, site_id, graph)
    else:
        st.caption("Ce graphe est tenu à jour par votre auditeur énergétique.")


def page_regulatory(db: Session, repo: TenantRepository, user: User) -> None:
    guard_write(user)
    ui.page_header("", "Suivi réglementaire",
                   "Échéances Décret Tertiaire, audit EED et VSME, et journal des actions par site.")
    orgs = {o.id: o.name for o in repo.list_organizations()}
    org_filter = st.selectbox("Organisation", [None, *orgs],
                              format_func=lambda i: "Toutes les organisations" if i is None else orgs[i])
    deadlines = repo.list_deadlines(org_filter)
    today = today_local()

    def status_cell(deadline) -> str:
        if deadline.status != DeadlineStatus.DONE and deadline.due_date < today:
            return ui.badge("En retard", "danger")
        return ui.badge(DEADLINE_STATUS_LABELS[deadline.status], DEADLINE_STATUS_TONES[deadline.status])

    def time_cell(deadline) -> str:
        if deadline.status == DeadlineStatus.DONE:
            return ui.progress(100, "fait")
        days = (deadline.due_date - today).days
        elapsed = (DEADLINE_WINDOW_DAYS - max(days, 0)) / DEADLINE_WINDOW_DAYS * 100
        return ui.progress(elapsed, "échue" if days < 0 else f"J-{days}",
                           amber=deadline.status == DeadlineStatus.DUE_SOON or days < 0)

    if deadlines:
        ui.table(
            ["Échéance", "Obligation", "Organisation / site", "Statut", "Délai", "Notes"],
            [
                [
                    f"<b>{fmt_date(d.due_date)}</b>",
                    ui.e(OBLIGATION_LABELS[d.obligation]),
                    f"{ui.e(d.site.organization.name)}<span class='sub'>{ui.e(d.site.name)}</span>",
                    status_cell(d),
                    time_cell(d),
                    f"<span class='sub'>{ui.e(d.notes or '')}</span>",
                ]
                for d in deadlines
            ],
        )
    else:
        st.info("Aucune échéance.")

    sites = {s.id: f"{s.organization.name}, {s.name}" for s in repo.list_sites(org_filter)}
    left, right = st.columns(2)
    with left:
        st.header("Mettre à jour une échéance")
        if deadlines:
            with st.form("deadline_status"):
                labels = {d.id: f"{fmt_date(d.due_date)}, {OBLIGATION_LABELS[d.obligation]}, {d.site.name}"
                          for d in deadlines}
                deadline_id = st.selectbox("Échéance", list(labels), format_func=labels.get)
                done = st.radio("Action", ["Marquer réalisée", "Rouvrir"], horizontal=True)
                if st.form_submit_button("Enregistrer", type="primary"):
                    status = DeadlineStatus.DONE if done == "Marquer réalisée" else DeadlineStatus.UPCOMING
                    regulatory.update_deadline(db, repo.get_deadline(deadline_id), user_id=user.id, status=status)
                    flash("Échéance mise à jour.")
                    st.rerun()
    with right:
        st.header("Ajouter une échéance")
        if sites:
            with st.form("deadline_add", clear_on_submit=True):
                site_id = st.selectbox("Site", list(sites), format_func=sites.get)
                obligation = st.selectbox("Obligation", list(Obligation), format_func=OBLIGATION_LABELS.get)
                due = st.date_input("Échéance", value=today + timedelta(days=90), format="DD/MM/YYYY")
                notes = st.text_input("Notes")
                if st.form_submit_button("Créer"):
                    regulatory.create_deadline(db, repo.get_site(site_id), obligation, due, notes or None)
                    db.commit()
                    flash("Échéance créée.")
                    st.rerun()

    st.header("Journal des actions")
    if not sites:
        return
    site_id = st.selectbox("Site", list(sites), format_func=sites.get, key="log_site")
    with st.form("action_log", clear_on_submit=True):
        obligation = st.selectbox("Obligation", list(Obligation), format_func=OBLIGATION_LABELS.get, key="log_obl")
        description = st.text_input("Action menée")
        if st.form_submit_button("Journaliser"):
            try:
                regulatory.log_action(db, repo.get_site(site_id), obligation=obligation,
                                      description=description, user_id=user.id)
                flash("Action journalisée.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    actions = repo.list_action_logs(site_id)
    if actions:
        with st.container(border=True):
            ui.feed([
                (to_local(a.performed_at).strftime("%d/%m/%Y"),
                 f"{ui.badge(OBLIGATION_LABELS[a.obligation])} {ui.e(a.description)}")
                for a in actions
            ])


def fmt_size(size: int) -> str:
    return f"{fmt_number(size / 1024 / 1024, 1)} Mo" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} Ko"


def page_documents(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    """Dépôt de factures et de relevés : le client dépose, l'auditeur traite."""
    is_client = user.role == Role.CLIENT_VIEWER
    ui.page_header(
        org.name, "Documents",
        "Déposez vos factures d'énergie et vos relevés de compteur : votre auditeur les reçoit et les traite."
        if is_client else "Factures et relevés déposés par le client ou par vous.",
    )
    sites = {s.id: s.name for s in repo.list_sites(org.id)}

    if documents_service.can_deposit(user, org.id):
        with st.form("deposit", clear_on_submit=True):
            st.header("Déposer des documents")
            files = st.file_uploader(
                "Fichiers", type=documents_service.ALLOWED_EXTENSIONS, accept_multiple_files=True,
                help=f"PDF, photo (PNG, JPEG), CSV ou Excel. {settings.document_max_mb:g} Mo maximum par fichier.",
            )
            left, right = st.columns(2)
            kind = left.selectbox("Type de document", list(DocumentKind), format_func=DOCUMENT_KIND_LABELS.get)
            site_id = right.selectbox("Site concerné", [None, *sites],
                                      format_func=lambda i: "Non précisé" if i is None else sites[i])
            left, right = st.columns(2)
            start = left.date_input("Début de la période facturée ou relevée", value=None, format="DD/MM/YYYY")
            end = right.date_input("Fin de la période", value=None, format="DD/MM/YYYY")
            comment = st.text_area("Commentaire pour l'auditeur (facultatif)", max_chars=1000,
                                   placeholder="Ex. : facture rectificative, relevé fait sur place…")
            if st.form_submit_button("Déposer", type="primary"):
                if not files:
                    st.error("Ajoutez au moins un fichier avant de déposer.")
                else:
                    deposited, errors = 0, []
                    for upload in files:
                        try:
                            documents_service.deposit(
                                db, repo, user, org.id, filename=upload.name, content=upload.getvalue(),
                                kind=kind, site_id=site_id, period_start=start, period_end=end, comment=comment,
                            )
                            deposited += 1
                        except documents_service.DocumentError as exc:
                            errors.append(str(exc))
                    for message in errors:
                        st.error(message)
                    if deposited:
                        integrations.dispatch_in_background()
                        flash(f"{deposited} document(s) déposé(s)."
                              + (" Votre auditeur est prévenu." if is_client else ""))
                        if not errors:
                            st.rerun()

    st.header("Documents déposés")
    filters = {"À traiter": DocumentStatus.RECEIVED, "Traités": DocumentStatus.PROCESSED,
               "Refusés": DocumentStatus.REJECTED, "Tous": None}
    choice = st.radio("Statut", list(filters), index=3 if is_client else 0, horizontal=True, key="doc_filter")
    docs = repo.list_documents(org.id, filters[choice])
    if not docs:
        ui.empty_state("Aucun document", "Déposez une facture ou un relevé avec le formulaire ci-dessus."
                       if documents_service.can_deposit(user, org.id) else "Aucun document pour ce filtre.")
        return

    uploaders = {u.id: u.email for u in db.scalars(select(User).where(User.id.in_({d.uploaded_by for d in docs})))}
    for doc in docs:
        with st.container(border=True):
            info, actions = st.columns([4, 1], vertical_alignment="center")
            details = [DOCUMENT_KIND_LABELS[doc.kind], sites.get(doc.site_id, "site non précisé")]
            if doc.period_start or doc.period_end:
                details.append(f"du {fmt_date(doc.period_start)} au {fmt_date(doc.period_end)}")
            note = ""
            if doc.review_note:
                note = f"<div style='margin-top:8px'>Note de l'auditeur : {ui.e(doc.review_note)}</div>"
            if doc.comment:
                note += f"<div style='margin-top:4px;color:var(--color-muted)'>Commentaire : {ui.e(doc.comment)}</div>"
            with info:
                ui.render(
                    f"<div><b>{ui.e(doc.original_name)}</b> &nbsp;"
                    f"{ui.status(DOCUMENT_STATUS_LABELS[doc.status], DOCUMENT_STATUS_TONES[doc.status])}</div>"
                    f"<div style='margin-top:6px;font-size:14px;color:var(--color-muted)'>{ui.e(', '.join(details))}. "
                    f"Déposé le {to_local(doc.uploaded_at):%d/%m/%Y à %H:%M} par "
                    f"{ui.e(uploaders.get(doc.uploaded_by, 'compte supprimé'))}, {fmt_size(doc.size_bytes)}.</div>"
                    f"{note}"
                )
            with actions:
                try:
                    content = documents_service.read_content(doc)
                except FileNotFoundError:
                    st.caption("Fichier indisponible")
                else:
                    st.download_button("Télécharger", content, file_name=doc.original_name, mime=doc.content_type,
                                       key=f"doc-dl-{doc.id}", on_click="ignore", width="stretch")
                if documents_service.can_withdraw(user, doc):
                    with st.popover("Retirer", width="stretch"):
                        st.markdown(f"**Retirer « {doc.original_name} » ?**")
                        st.caption("Le fichier est supprimé. Vous pourrez le déposer à nouveau.")
                        if st.button("Confirmer le retrait", key=f"doc-rm-{doc.id}", type="primary"):
                            documents_service.withdraw(db, user, doc)
                            flash("Document retiré.")
                            st.rerun()
                if can_write(user):
                    with st.popover("Traiter", width="stretch"):
                        with st.form(f"doc-review-{doc.id}"):
                            decision = st.radio(
                                "Décision",
                                [DocumentStatus.PROCESSED, DocumentStatus.REJECTED, DocumentStatus.RECEIVED],
                                format_func={DocumentStatus.PROCESSED: "Marquer traité",
                                             DocumentStatus.REJECTED: "Refuser",
                                             DocumentStatus.RECEIVED: "Remettre à traiter"}.get,
                            )
                            reason = st.text_input("Note pour le client (obligatoire en cas de refus)",
                                                   value=doc.review_note or "")
                            if st.form_submit_button("Enregistrer", type="primary"):
                                try:
                                    documents_service.review(db, user, doc, decision, reason)
                                    flash("Document mis à jour.")
                                    st.rerun()
                                except documents_service.DocumentError as exc:
                                    st.error(str(exc))


CONNECTOR_AUTH_LABELS = {
    ConnectorAuth.NONE: "Aucune",
    ConnectorAuth.API_KEY_HEADER: "Clé d'API dans un en-tête",
    ConnectorAuth.BEARER: "Jeton (Authorization: Bearer)",
    ConnectorAuth.BASIC: "Identifiant et mot de passe",
}
VALUE_UNIT_LABELS = {ValueUnit.KWH: "kWh (énergie)", ValueUnit.WH: "Wh (énergie)",
                     ValueUnit.KW: "kW (puissance moyenne)", ValueUnit.W: "W (puissance moyenne)"}
STEP_LABELS = {MeasurementStep.PT30M: "30 minutes", MeasurementStep.P1D: "Journalier"}
DELIVERY_STATUS_LABELS = {DeliveryStatus.PENDING: ("En attente", "neutral"), DeliveryStatus.SENT: ("Envoyé", "success"),
                          DeliveryStatus.FAILED: ("Échec", "danger")}
PARTNER_API_URL = "http://localhost:8000/api/v1"


def show_once(key: str, title: str, value: str) -> None:
    """Affiche une seule fois une valeur secrète venant d'être créée (clé d'API, clé de signature)."""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption("Copiez-la maintenant : elle ne sera plus jamais affichée. En cas de perte, créez-en une autre.")
        st.code(value, language=None)
        if st.button("J'ai copié la valeur", key=f"ack-{key}"):
            st.session_state.pop(key, None)
            st.rerun()


def integrations_sources(db: Session, user: User) -> None:
    is_admin = user.role == Role.ADMIN
    if not is_admin:
        st.caption("Ces sources sont réglées par l'administrateur de la plateforme : EffiSmart signe les contrats "
                   "Enedis et GRDF. Vous voyez leur état.")
    for kind, spec in integrations.PLATFORM_SPECS.items():
        row = integrations.get_platform(db, kind)
        with st.container(border=True):
            state = ui.status("Active", "success") if row.enabled else ui.status("Inactive", "neutral")
            ui.render(f"<div><b>{ui.e(spec['label'])}</b> &nbsp;{state}</div>"
                      f"<div style='margin-top:6px;font-size:14px;color:var(--color-muted)'>{ui.e(spec['description'])}</div>")
            if row.last_test_at:
                tone = "success" if row.last_test_ok else "danger"
                ui.render(f"<div style='margin-top:8px'>{ui.status('Dernier test', tone)} "
                          f"<span style='font-size:14px'>{to_local(row.last_test_at):%d/%m/%Y à %H:%M} : "
                          f"{ui.e(row.last_test_message or '')}</span></div>")
            if not is_admin:
                continue
            current = integrations.platform_settings(row)
            stored = integrations.secret_status(row)
            with st.form(f"platform-{kind.value}"):
                enabled = st.toggle("Activer cette source", value=row.enabled)
                values, secrets_values = {}, {}
                for name, field in spec["settings"].items():
                    if "options" in field:
                        values[name] = st.selectbox(field["label"], field["options"],
                                                    index=field["options"].index(current[name]))
                    else:
                        values[name] = st.number_input(field["label"], value=float(current[name]), format="%.4f")
                for name, label in spec["secrets"].items():
                    secrets_values[name] = st.text_input(
                        label, type="password", autocomplete="off",
                        placeholder="Enregistré. Laissez vide pour conserver." if stored.get(name) else "",
                    )
                if st.form_submit_button("Enregistrer", type="primary"):
                    try:
                        integrations.save_platform(db, user, kind, enabled=enabled, settings_values=values,
                                                   secrets_values=secrets_values)
                        flash(f"{spec['label']} : réglages enregistrés.")
                        st.rerun()
                    except integrations.IntegrationError as exc:
                        st.error(str(exc))
            if st.button("Tester la connexion", key=f"test-{kind.value}"):
                with st.spinner("Test en cours…"):
                    ok, message = integrations.test_platform(db, user, kind)
                (st.success if ok else st.error)(message)


def integrations_connectors(db: Session, user: User) -> None:
    st.caption("Un connecteur transforme n'importe quelle API REST en source de relevés. Rattachez-le ensuite à un "
               "point de livraison (page « Patrimoine & consentements ») : il suit les mêmes règles que les autres "
               "sources, consentement du client compris.")
    connectors = integrations.list_connectors(db, user)
    usage = dict(db.execute(select(DeliveryPoint.connector_id, func.count(DeliveryPoint.id))
                            .where(DeliveryPoint.connector_id.is_not(None)).group_by(DeliveryPoint.connector_id)).all())
    for connector in connectors:
        with st.container(border=True):
            info, actions = st.columns([4, 1], vertical_alignment="center")
            with info:
                ui.render(
                    f"<div><b>{ui.e(connector.name)}</b> &nbsp;{ui.status(f'{usage.get(connector.id, 0)} point(s)', 'neutral')}</div>"
                    f"<div style='margin-top:6px;font-size:14px;color:var(--color-muted);word-break:break-all'>"
                    f"{ui.e(connector.url_template)}</div>"
                    f"<div style='font-size:14px;color:var(--color-muted)'>Authentification : "
                    f"{ui.e(CONNECTOR_AUTH_LABELS[connector.auth_type].lower())} ; relevés "
                    f"« {ui.e(connector.records_path or 'racine')} », date « {ui.e(connector.time_field)} », valeur "
                    f"« {ui.e(connector.value_field)} » en {ui.e(VALUE_UNIT_LABELS[connector.value_unit])}, pas "
                    f"{ui.e(STEP_LABELS[connector.step].lower())}.</div>"
                )
            with actions:
                with st.popover("Tester", width="stretch"):
                    with st.form(f"connector-test-{connector.id}"):
                        ref = st.text_input("Référence du point (remplace {ref})")
                        day = yesterday_local()
                        period = st.date_input("Période", value=(day - timedelta(days=1), day), max_value=day,
                                               format="DD/MM/YYYY")
                        if st.form_submit_button("Lancer le test", type="primary"):
                            if not ref.strip() or len(period) != 2:
                                st.error("Indiquez une référence et une période.")
                            else:
                                try:
                                    readings = integrations.test_connector(db, user, connector.id, ref, *period)
                                    st.success(f"{len(readings)} relevé(s) reconnu(s).")
                                    if readings:
                                        st.dataframe(pd.DataFrame([
                                            {"Début": to_local(r.time).strftime("%d/%m/%Y %H:%M"), "kWh": r.value_kwh}
                                            for r in readings[:20]
                                        ]), hide_index=True)
                                except Exception as exc:  # message de l'API, sans secret
                                    st.error(f"Échec : {exc}")
                with st.popover("Supprimer", width="stretch"):
                    st.markdown(f"**Supprimer « {connector.name} » ?**")
                    if st.button("Confirmer la suppression", key=f"connector-rm-{connector.id}", type="primary"):
                        try:
                            integrations.delete_connector(db, user, connector.id)
                            flash("Connecteur supprimé.")
                            st.rerun()
                        except integrations.IntegrationError as exc:
                            st.error(str(exc))
    if not connectors:
        st.caption("Aucun connecteur pour l'instant.")

    with st.form("connector-new", clear_on_submit=True):
        st.header("Nouveau connecteur")
        name = st.text_input("Nom", placeholder="Ex. : Capteurs de l'usine de Genas")
        url_template = st.text_input(
            "Adresse de l'API (HTTPS)", placeholder="https://api.exemple.fr/v1/compteurs/{ref}/releves?du={start}&au={end}",
            help="{ref} est remplacé par la référence du point, {start} et {end} par les dates (AAAA-MM-JJ).",
        )
        left, right = st.columns(2)
        auth_type = left.selectbox("Authentification", list(ConnectorAuth), format_func=CONNECTOR_AUTH_LABELS.get)
        auth_header = right.text_input("Nom de l'en-tête (clé d'API)", value="X-API-Key")
        left, right = st.columns(2)
        username = left.text_input("Identifiant (authentification par mot de passe)")
        secret = right.text_input("Clé, jeton ou mot de passe", type="password", autocomplete="off")
        left, mid, right = st.columns(3)
        records_path = left.text_input("Chemin de la liste des relevés", placeholder="data.readings (vide = racine)")
        time_field = mid.text_input("Champ de date", value="timestamp")
        value_field = right.text_input("Champ de valeur", value="value")
        left, right = st.columns(2)
        value_unit = left.selectbox("Unité de la valeur", list(ValueUnit), format_func=VALUE_UNIT_LABELS.get)
        step = right.selectbox("Pas des relevés", list(MeasurementStep), format_func=STEP_LABELS.get)
        if st.form_submit_button("Créer le connecteur", type="primary"):
            try:
                integrations.save_connector(
                    db, user, name=name, url_template=url_template, auth_type=auth_type, auth_header=auth_header,
                    username=username, secret=secret, records_path=records_path, time_field=time_field,
                    value_field=value_field, value_unit=value_unit, step=step,
                )
                flash("Connecteur créé. Testez-le, puis rattachez-le à un point de livraison.")
                st.rerun()
            except integrations.IntegrationError as exc:
                st.error(str(exc))


def integrations_api_keys(db: Session, repo: TenantRepository, user: User) -> None:
    st.caption(f"L'API partenaires permet à d'autres logiciels (ERP, GMAO, outil de reporting…) de lire les "
               f"consommations, les sites et les dérives de vos clients. Adresse : {PARTNER_API_URL} ; documentation "
               f"interactive : http://localhost:8000/docs. Lecture seule.")
    if st.session_state.get("new_api_key"):
        show_once("new_api_key", "Nouvelle clé d'API", st.session_state["new_api_key"])
    orgs = {o.id: o.name for o in repo.list_organizations()}
    keys = integrations.list_api_keys(db, user)
    if keys:
        ui.table(
            ["Nom", "Clé", "Accès", "Créée le", "Dernière utilisation", "État"],
            [
                [ui.e(k.name), f"<code>{ui.e(k.prefix)}…</code>",
                 ui.e(orgs.get(k.organization_id, "client retiré") if k.organization_id else "Tous mes clients"),
                 f"{to_local(k.created_at):%d/%m/%Y}",
                 f"{to_local(k.last_used_at):%d/%m/%Y à %H:%M}" if k.last_used_at else "Jamais",
                 ui.status("Révoquée", "danger") if k.revoked_at else ui.status("Active", "success")]
                for k in keys
            ],
        )
        active = {k.id: f"{k.name} ({k.prefix}…)" for k in keys if not k.revoked_at}
        if active:
            with st.popover("Révoquer une clé"):
                key_id = st.selectbox("Clé", list(active), format_func=active.get)
                st.caption("Les logiciels qui l'utilisent perdront immédiatement l'accès.")
                if st.button("Confirmer la révocation", type="primary", key="revoke-key"):
                    integrations.revoke_api_key(db, user, key_id)
                    flash("Clé révoquée.")
                    st.rerun()
    with st.form("api-key-new", clear_on_submit=True):
        st.header("Créer une clé d'API")
        name = st.text_input("Nom", placeholder="Ex. : ERP de la Clinique du Parc")
        organization_id = st.selectbox("Accès", [None, *orgs],
                                       format_func=lambda i: "Tous mes clients" if i is None else f"Seulement {orgs[i]}")
        if st.form_submit_button("Créer la clé", type="primary"):
            try:
                _, raw = integrations.create_api_key(db, user, repo, name=name, organization_id=organization_id)
                st.session_state["new_api_key"] = raw
                st.rerun()
            except integrations.IntegrationError as exc:
                st.error(str(exc))
    with st.expander("Exemple d'appel"):
        st.code(f'curl -H "X-API-Key: esk_…" "{PARTNER_API_URL}/organizations"\n'
                f'curl -H "X-API-Key: esk_…" "{PARTNER_API_URL}/organizations/1/consumption?granularity=month"',
                language="bash")


def integrations_webhooks(db: Session, repo: TenantRepository, user: User) -> None:
    st.caption("Un webhook envoie automatiquement un événement à l'adresse d'un autre outil (nouvelle dérive, "
               "nouveau document déposé). Chaque envoi est signé ; en cas d'échec, il est réessayé plusieurs fois.")
    if st.session_state.get("new_webhook_secret"):
        show_once("new_webhook_secret", "Clé de signature du webhook", st.session_state["new_webhook_secret"])
    orgs = {o.id: o.name for o in repo.list_organizations()}
    for webhook in integrations.list_webhooks(db, user):
        with st.container(border=True):
            info, actions = st.columns([4, 1], vertical_alignment="center")
            events = ", ".join(integrations.WEBHOOK_EVENTS.get(e, e).lower() for e in webhook.events)
            scope = orgs.get(webhook.organization_id, "client retiré") if webhook.organization_id else "tous mes clients"
            with info:
                ui.render(
                    f"<div><b>{ui.e(webhook.name)}</b> &nbsp;"
                    f"{ui.status('Actif', 'success') if webhook.enabled else ui.status('En pause', 'neutral')}</div>"
                    f"<div style='margin-top:6px;font-size:14px;color:var(--color-muted);word-break:break-all'>"
                    f"{ui.e(webhook.url)}</div>"
                    f"<div style='font-size:14px;color:var(--color-muted)'>Événements : {ui.e(events)} ; "
                    f"{ui.e(scope)}.</div>"
                )
                deliveries = integrations.recent_deliveries(db, user, webhook.id, 5)
                if deliveries:
                    ui.feed([
                        (to_local(d.created_at).strftime("%d/%m %H:%M"),
                         f"{ui.status(*DELIVERY_STATUS_LABELS[d.status])} {ui.e(d.event)}"
                         + (f" : {ui.e(d.last_error)}" if d.last_error and d.status != DeliveryStatus.SENT else ""))
                        for d in deliveries
                    ])
            with actions:
                if st.button("Envoyer un test", key=f"wh-test-{webhook.id}", width="stretch"):
                    delivery = integrations.send_test(db, user, webhook.id)
                    if delivery.status == DeliveryStatus.SENT:
                        flash(f"Test reçu par « {webhook.name} » (réponse {delivery.last_status_code}).")
                    else:
                        flash(f"Échec du test : {delivery.last_error}")
                    st.rerun()
                if st.button("Mettre en pause" if webhook.enabled else "Réactiver", key=f"wh-toggle-{webhook.id}",
                             width="stretch"):
                    integrations.set_webhook_enabled(db, user, webhook.id, not webhook.enabled)
                    st.rerun()
                with st.popover("Supprimer", width="stretch"):
                    st.markdown(f"**Supprimer « {webhook.name} » ?**")
                    if st.button("Confirmer la suppression", key=f"wh-rm-{webhook.id}", type="primary"):
                        integrations.delete_webhook(db, user, webhook.id)
                        flash("Webhook supprimé.")
                        st.rerun()
    with st.form("webhook-new", clear_on_submit=True):
        st.header("Nouveau webhook")
        name = st.text_input("Nom", placeholder="Ex. : Tickets GMAO")
        url = st.text_input("Adresse de réception (HTTPS)", placeholder="https://outil.exemple.fr/hooks/effismart")
        events = st.multiselect("Événements", list(integrations.WEBHOOK_EVENTS),
                                default=list(integrations.WEBHOOK_EVENTS), format_func=integrations.WEBHOOK_EVENTS.get)
        organization_id = st.selectbox("Clients concernés", [None, *orgs],
                                       format_func=lambda i: "Tous mes clients" if i is None else f"Seulement {orgs[i]}")
        if st.form_submit_button("Créer le webhook", type="primary"):
            try:
                _, secret = integrations.create_webhook(db, user, repo, name=name, url=url, events=events,
                                                        organization_id=organization_id)
                st.session_state["new_webhook_secret"] = secret
                st.rerun()
            except integrations.IntegrationError as exc:
                st.error(str(exc))
    with st.expander("Vérifier la signature côté réception"):
        st.caption("Chaque envoi porte les en-têtes X-EffiSmart-Event, X-EffiSmart-Timestamp et X-EffiSmart-Signature. "
                   "Refusez les envois dont la signature ne correspond pas ou dont l'horodatage a plus de 5 minutes.")
        st.code(
            "import hashlib, hmac, time\n\n"
            "def verifier(corps: bytes, horodatage: str, signature: str, cle: str) -> bool:\n"
            "    if abs(time.time() - int(horodatage)) > 300:\n"
            "        return False\n"
            "    attendu = hmac.new(cle.encode(), horodatage.encode() + b\".\" + corps, hashlib.sha256).hexdigest()\n"
            "    return hmac.compare_digest(f\"sha256={attendu}\", signature)",
            language="python",
        )


def page_integrations(db: Session, repo: TenantRepository, user: User) -> None:
    ui.page_header("", "Intégrations",
                   "Sources de données, connecteurs, accès des logiciels partenaires et webhooks.")
    tabs = st.tabs(["Sources de données", "Connecteurs", "API partenaires", "Webhooks"])
    with tabs[0]:
        integrations_sources(db, user)
    if user.role != Role.AUDITOR:
        for tab in tabs[1:]:
            with tab:
                st.info("Les connecteurs, clés d'API et webhooks appartiennent à chaque cabinet : "
                        "connectez-vous avec un compte auditeur.")
        return
    with tabs[1]:
        integrations_connectors(db, user)
    with tabs[2]:
        integrations_api_keys(db, repo, user)
    with tabs[3]:
        integrations_webhooks(db, repo, user)


def page_exports(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    ui.page_header(org.name, "Exports des données énergie", "Déclaration OPERAT et reporting ESG VSME.")
    if can_write(user):
        st.caption("Un même calcul alimente les deux gabarits. Formats provisoires V1 : JSON + CSV.")
        last_year = today_local().year - 1
        with st.form("export"):
            left, right = st.columns(2)
            start = left.date_input("Début de période", value=date(last_year, 1, 1), format="DD/MM/YYYY")
            end = right.date_input("Fin de période", value=date(last_year, 12, 31), format="DD/MM/YYYY")
            formats = st.multiselect("Formats", list(ExportFormat), default=list(ExportFormat),
                                     format_func=EXPORT_LABELS.get)
            if st.form_submit_button("Générer les exports", type="primary"):
                if not formats:
                    st.error("Choisissez au moins un format.")
                elif start > end:
                    st.error("La date de début doit précéder la date de fin.")
                else:
                    with st.spinner("Génération…"):
                        ExportEngine().run(db, org, start, end, formats, user.id)
                    flash("Exports générés.")
                    st.rerun()
    else:
        st.caption("Les exports sont générés par votre auditeur ; vous pouvez les télécharger ici.")

    st.header("Exports disponibles")
    jobs = repo.list_export_jobs(org.id)
    if not jobs:
        st.info("Aucun export pour l'instant.")
    for job in jobs:
        with st.container(border=True):
            info, action = st.columns([4, 1], vertical_alignment="center")
            factors = " ; ".join(
                f"{FLUID_LABELS[Fluid(f['fluid'])]} {fmt_number(f['factor_kgco2e_per_kwh'], 3)} kgCO₂e/kWh "
                f"({f['version']}, valide dès {f['valid_from']})"
                for f in job.factors_used or []
            )
            with info:
                ui.render(
                    f"<div><b>{ui.e(EXPORT_LABELS[job.format])}</b> &nbsp;"
                    f"{ui.badge(EXPORT_STATUS_LABELS[job.status], EXPORT_STATUS_TONES[job.status])}</div>"
                    f"<div style='margin-top:6px;font-size:14px;color:var(--color-muted)'>"
                    f"Du {fmt_date(job.period_start)} au {fmt_date(job.period_end)}, "
                    f"créé le {to_local(job.created_at).strftime('%d/%m/%Y à %H:%M')}</div>"
                    f"<div style='font-size:13px;color:var(--color-muted);margin-top:4px'>"
                    f"Facteurs : {ui.e(factors)}</div>"
                )
            if job.status == ExportStatus.DONE:
                try:
                    content = build_zip(job)
                except FileNotFoundError:
                    action.caption("Fichier indisponible")
                else:
                    action.download_button(
                        "Télécharger", content,
                        file_name=f"effismart_{job.format.value.lower()}_{job.period_start}_{job.period_end}.zip",
                        mime="application/zip", key=f"dl-{job.id}", on_click="ignore", width="stretch",
                    )


def page_manage(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    guard_write(user)
    ui.page_header(org.name, "Patrimoine & consentements",
                   "Sites, points de livraison et recueil du consentement RGPD.")
    connectors = integrations.list_connectors(db, user) if user.role == Role.AUDITOR else []
    connectors_by_id = {c.id: c for c in connectors}

    for site in repo.list_sites(org.id):
        tags = ", assujetti au Décret Tertiaire" if site.is_tertiary_decret else ""
        with st.expander(f"{site.name}{tags}", expanded=True):
            surface = f", {fmt_number(site.surface_m2)} m²" if site.surface_m2 else ""
            st.caption(f"{site.address or 'Adresse non renseignée'}{surface}")
            for dp in site.delivery_points:
                consent = active_consent(db, dp.id)
                details = [FLUID_LABELS[dp.fluid], f"source : {source_label(dp, connectors)}"]
                if dp.is_primary:
                    details.append("principal")
                if dp.subscribed_power_kva:
                    details.append(f"{fmt_number(dp.subscribed_power_kva)} kVA souscrits")
                head = (f"<b style='font-family:var(--font-mono)'>{ui.e(dp.external_ref)}</b> "
                        f"<span class='sub' style='display:inline;color:var(--color-muted)'>"
                        f"{ui.e(', '.join(details))}</span>")
                if consent:
                    left, right = st.columns([4, 1], vertical_alignment="center")
                    with left:
                        ui.render(
                            f"{head}<br>{ui.badge('Consentement actif', 'success')} "
                            f"<span style='font-size:13px;color:var(--color-muted)'>depuis le "
                            f"{to_local(consent.granted_at).strftime('%d/%m/%Y')}, preuve {ui.e(consent.proof_ref)}</span>"
                        )
                    with right.popover("Révoquer", width="stretch"):
                        st.markdown(f"**Révoquer le consentement de {dp.external_ref} ?**")
                        st.caption("La collecte des données de ce point s'arrête immédiatement ; "
                                   "un nouveau consentement du client sera nécessaire.")
                        if st.button("Confirmer la révocation", key=f"revoke-{consent.id}", type="primary"):
                            revoke_consent(db, consent)
                            flash(f"Consentement révoqué pour {dp.external_ref}.")
                            st.rerun()
                else:
                    ui.render(f"{head}<br>{ui.badge('Aucun consentement : aucune donnée collectée', 'warning')}")
                    with st.form(f"consent-{dp.id}"):
                        st.markdown("**Recueil du consentement**")
                        authorized = st.checkbox(
                            "Le client a autorisé EffiSmart à accéder à ses données depuis son espace Enedis / GRDF"
                        )
                        proof = st.text_input("Référence de preuve", placeholder="ex. mandat signé n°…")
                        if st.form_submit_button("Enregistrer le consentement", type="primary"):
                            if not authorized or not proof.strip():
                                st.error("L'autorisation du client et une référence de preuve sont requises.")
                            else:
                                grant_consent(db, dp, scope="Consommations et données contractuelles",
                                              proof_ref=proof.strip(), granted_by=user.id)
                                with st.spinner("Récupération de l'historique et analyse…"):
                                    backfill_delivery_point(dp.id)
                                flash(f"Consentement enregistré, historique récupéré pour {dp.external_ref}.")
                                st.rerun()

            with st.form(f"dp-{site.id}", clear_on_submit=True):
                st.markdown("**Ajouter un point de livraison**")
                a, b, c = st.columns([2, 1, 1], vertical_alignment="bottom")
                ref = a.text_input("PRM / PCE (14 chiffres), ou référence du capteur pour un connecteur")
                fluid = b.selectbox("Énergie", list(Fluid), format_func=FLUID_LABELS.get)
                primary = c.checkbox("Point principal")
                sources = [(ProviderKind.MOCK, None), (ProviderKind.ENEDIS_DATACONNECT, None),
                           (ProviderKind.GRDF_ADICT, None)] + [(ProviderKind.GENERIC_API, c.id) for c in connectors]
                source = st.selectbox(
                    "Source des données", sources,
                    format_func=lambda s: PROVIDER_LABELS[s[0]] if s[1] is None else f"Connecteur : {connectors_by_id[s[1]].name}",
                    help="Enedis et GRDF ne fournissent des données qu'une fois l'intégration activée par "
                         "l'administrateur. Les connecteurs se créent dans la page « Intégrations ».",
                )
                if st.form_submit_button("Ajouter"):
                    try:
                        onboarding.create_delivery_point(
                            db, site, fluid=fluid, external_ref=ref, is_primary=primary, provider=source[0],
                            connector=connectors_by_id.get(source[1]),
                        )
                        flash("Point de livraison ajouté. Recueillez maintenant le consentement.")
                        st.rerun()
                    except (ValueError, onboarding.ConflictError) as exc:
                        st.error(str(exc))

    left, right = st.columns(2)
    with left, st.form("site", clear_on_submit=True):
        st.header("Ajouter un site")
        name = st.text_input("Nom du site")
        address = st.text_input("Adresse")
        surface = st.number_input("Surface (m²)", min_value=0.0, step=10.0)
        tertiary = st.checkbox("Assujetti au Décret Tertiaire")
        if st.form_submit_button("Créer le site", type="primary"):
            try:
                onboarding.create_site(db, org, name=name, address=address or None,
                                       surface_m2=surface or None, is_tertiary_decret=tertiary)
                flash("Site créé." + (" Échéance OPERAT générée." if tertiary else ""))
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with right, st.form("viewer", clear_on_submit=True):
        st.header("Accès espace client")
        email = st.text_input("E-mail du client")
        password = st.text_input("Mot de passe initial (8 caractères min.)", type="password")
        manager = st.checkbox("Responsable énergie du client",
                              help="Il pourra valider ou écarter les anomalies, recommandations et prévisions de "
                                   "la plateforme. Les autres comptes client ne voient que ce qui a été validé.")
        if st.form_submit_button("Créer l'accès"):
            try:
                onboarding.create_viewer(db, org, email=email, password=password, energy_manager=manager)
                flash(f"Accès client créé pour {email.strip().lower()}"
                      + (", responsable énergie." if manager else "."))
                st.rerun()
            except (ValueError, onboarding.ConflictError) as exc:
                st.error(str(exc))

    accounts = onboarding.list_client_users(db, org)
    if accounts:
        st.header("Comptes client")
        st.caption("Le responsable énergie valide ou écarte les sorties de la plateforme au même titre que "
                   "l'auditeur ; les autres comptes ne voient que ce qui a été validé.")
        for account in accounts:
            text_col, button_col = st.columns([4, 1], vertical_alignment="center")
            with text_col:
                ui.render(f"<b>{ui.e(account.email)}</b> &nbsp;"
                          + (ui.status("Responsable énergie", "success") if account.is_energy_manager
                             else ui.status("Lecture seule", "neutral")))
            label = "Retirer le rôle" if account.is_energy_manager else "Nommer responsable énergie"
            if button_col.button(label, key=f"manager-{account.id}", width="stretch"):
                onboarding.set_energy_manager(db, org, account.id, not account.is_energy_manager)
                flash(f"{account.email} : " + ("rôle de responsable énergie retiré." if account.is_energy_manager
                                                else "désormais responsable énergie."))
                st.rerun()


def page_new_client(db: Session, user: User) -> None:
    guard_write(user)
    ui.page_header("", "Nouveau client",
                   "Créez l'organisation, puis ajoutez ses sites et points de livraison.")
    with st.form("new_client"):
        name = st.text_input("Raison sociale")
        siren = st.text_input("SIREN (9 chiffres, facultatif)")
        address = st.text_input("Adresse")
        if st.form_submit_button("Créer puis ajouter sites et points de livraison", type="primary"):
            try:
                org = onboarding.create_organization(db, user, name=name, siren=siren.strip() or None,
                                                     address=address or None)
            except ValueError as exc:
                st.error(str(exc))
            else:
                flash(f"Client « {org.name} » créé. Ajoutez ses sites et points de livraison.")
                goto(PAGE_MANAGE, org.id)


# --- Application ------------------------------------------------------------------------------


def sidebar(repo: TenantRepository, user: User) -> str:
    pages = pages_for(user)
    pending = st.session_state.pop("_goto", None)
    if pending:
        st.session_state["page"], target_org = pending
        if target_org is not None:
            st.session_state["org_id"] = target_org
    if st.session_state.get("page") not in pages:
        st.session_state["page"] = pages[0]
    # Nombre de sorties en attente d'une décision humaine, tous clients du périmètre.
    waiting = pending_count(repo, [o.id for o in repo.list_organizations()]) if PAGE_VALIDATION in pages else 0

    with st.sidebar:
        ui.render(ui.logo_html())
        ui.section_label("Menu")
        page = st.radio("Navigation", pages, key="page", label_visibility="collapsed",
                        format_func=lambda p: f"{p} ({waiting})" if p == PAGE_VALIDATION and waiting else p)
        st.caption(f"Base locale : {settings.database_url.rsplit('/', 1)[-1]}")
    return page


def _on_search() -> None:
    """Recherche de la barre du haut : ouvre la page ou le tableau de bord du client choisi."""
    choice = st.session_state.get("es_search")
    if not choice:
        return
    kind, value = choice
    if kind == "org":
        st.session_state["org_id"] = value
        st.session_state["page"] = PAGE_DASHBOARD
    else:
        st.session_state["page"] = value
    st.session_state["es_search"] = None


def top_bar(db: Session, repo: TenantRepository, user: User, page: str) -> Organization | None:
    """Barre horizontale : client, recherche, période, alertes, aide, nouvel export, compte.

    Chaque contrôle est fonctionnel (pas de bouton décoratif, cf. design system).
    Renvoie l'organisation courante.
    """
    is_client = user.role == Role.CLIENT_VIEWER
    orgs = {o.id: o for o in repo.list_organizations()}
    org = None
    with st.container(key="es-topbar", horizontal=True, vertical_alignment="center", horizontal_alignment="distribute"):
        with st.container(key="es-topbar-left", horizontal=True, vertical_alignment="center"):
            # Sélecteur de client (l'espace client est limité à sa propre organisation).
            if is_client:
                org = repo.get_organization(user.organization_id)
                ui.render(f'<div class="es-org-pill"><span class="es-org-mark">{ui.e(org.name[:1].upper())}</span>'
                          f"{ui.e(org.name)}</div>")
            elif orgs:
                if st.session_state.get("org_id") not in orgs:
                    st.session_state["org_id"] = next(iter(orgs))
                org_id = st.selectbox("Client", list(orgs), format_func=lambda i: orgs[i].name, key="org_id",
                                      label_visibility="collapsed", width=210)
                org = orgs[org_id]

            # Recherche : pages accessibles et clients du périmètre.
            pages = pages_for(user)
            options = [("page", p) for p in pages] + ([] if is_client else [("org", i) for i in orgs])
            st.selectbox(
                "Rechercher", options, index=None, key="es_search", on_change=_on_search,
                placeholder="Rechercher un client, une page…", label_visibility="collapsed", width="stretch",
                format_func=lambda o: f"Client : {orgs[o[1]].name}" if o[0] == "org" else f"Page : {o[1]}",
            )

        with st.container(key="es-topbar-right", horizontal=True, vertical_alignment="center",
                          horizontal_alignment="right", width="content"):
            # Période (tableau de bord uniquement).
            if page == PAGE_DASHBOARD:
                if st.session_state.get("period") not in PERIOD_LABELS:
                    st.session_state["period"] = "30d"
                st.selectbox("Période", list(PERIOD_LABELS), format_func=PERIOD_LABELS.get, key="period",
                             label_visibility="collapsed", width=160)

            # Alertes récentes (7 derniers jours) — le nombre est écrit, la couleur n'est pas seule.
            recent = db.scalars(
                select(Notification)
                .where(Notification.user_id == user.id, repo.org_clause(Notification.organization_id),
                       Notification.created_at >= local_midnight_utc(today_local() - timedelta(days=7)))
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .limit(8)
            ).all()
            bell_label = f"Alertes, {len(recent)} nouvelle(s)" if recent else "Alertes, aucune nouvelle"
            # Libellés dynamiques affichés en CSS : seuls des chiffres et lettres A-Z y sont insérés.
            avatar_text = "".join(c for c in ui.initials(user.email) if c.isascii() and c.isalnum())
            ui.render(f'<style>.st-key-es-avatar button::before{{content:"{avatar_text}"}}'
                      f'.st-key-es-bell-on button::before{{content:"{len(recent)}"}}</style>')
            with st.container(key="es-bell" + ("-on" if recent else "")):
                with st.popover(bell_label, icon=":material/notifications:", help="Alertes des 7 derniers jours"):
                    st.markdown("**Alertes des 7 derniers jours**")
                    if not recent:
                        st.caption("Aucune nouvelle alerte.")
                    for n in recent:
                        st.caption(f"{to_local(n.created_at).strftime('%d/%m à %H:%M')} : {n.message}")

            with st.container(key="es-help"):
                with st.popover("Aide", icon=":material/support:", help="Aide"):
                    st.markdown("**Aide**")
                    st.caption("Les gestionnaires de réseau (Enedis, GRDF) publient les consommations à J+1 : "
                               "les données s'arrêtent toujours à la veille.")
                    st.caption("La plateforme propose (anomalies, recommandations, prévisions), chaque fois avec "
                               "son raisonnement, son gain estimé et son niveau de confiance. L'auditeur ou le "
                               "responsable énergie décide ; aucune action n'est automatique.")
                    st.caption("Page « Équipements » : le graphe physique relie compteurs, équipements, fluides, "
                               "zones et usages ; il sert à cibler les recommandations.")
                    st.caption("Données de démonstration : fournisseur simulé (MockDataProvider).")

            # Action principale : nouvel export (écriture réservée à l'auditeur).
            if can_write(user) and org is not None:
                if st.button("Créer un export", type="primary", icon=":material/add:", key="es-new-export"):
                    goto(PAGE_EXPORTS, org.id)

            # Compte : initiales, rôle, mise à jour des données, déconnexion.
            with st.container(key="es-avatar"):
                with st.popover("Mon compte", help="Mon compte"):
                    ui.render(ui.user_html(user.email, role_label(user)))
                    if can_write(user) and st.button("Mettre à jour les données", type="primary", width="stretch",
                                                     help="Récupère les jours manquants jusqu'à la veille"):
                        with st.spinner("Mise à jour…"):
                            days = catch_up()
                        flash("Données à jour." if days == 0 else f"{days} jour(s) récupéré(s) et analysé(s).")
                        st.rerun()
                    if st.button("Se déconnecter", width="stretch", key="es-logout"):
                        st.session_state.clear()
                        st.rerun()
    return org


def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon=ui.ICON_PATH, layout="wide")
    ui.inject_css()
    initialize()
    with SessionLocal() as db:
        user = current_user(db)
        if user is None:
            login_page(db)
            return
        repo = TenantRepository(db, user)
        page = sidebar(repo, user)
        org = top_bar(db, repo, user, page)
        show_flash()
        try:
            if page == PAGE_PORTFOLIO:
                page_portfolio(db, repo, user)
            elif page == PAGE_REGULATORY:
                page_regulatory(db, repo, user)
            elif page == PAGE_NEW_CLIENT:
                page_new_client(db, user)
            elif page == PAGE_INTEGRATIONS:
                page_integrations(db, repo, user)
            elif org is None:
                st.info("Aucun client pour l'instant. Créez-en un depuis la page « Nouveau client ».")
            elif page == PAGE_DASHBOARD:
                page_dashboard(db, repo, org)
            elif page == PAGE_VALIDATION:
                page_validation(db, repo, user, org)
            elif page == PAGE_DRIFTS:
                page_drifts(db, repo, user, org)
            elif page == PAGE_RECOMMENDATIONS:
                page_recommendations(db, repo, user, org)
            elif page == PAGE_PREDICTIONS:
                page_predictions(db, repo, user, org)
            elif page == PAGE_ASSETS:
                page_assets(db, repo, user, org)
            elif page == PAGE_EXPORTS:
                page_exports(db, repo, user, org)
            elif page == PAGE_DOCUMENTS:
                page_documents(db, repo, user, org)
            elif page == PAGE_MANAGE:
                page_manage(db, repo, user, org)
        except ResourceNotFound:
            st.error("Ressource introuvable ou hors de votre périmètre.")
        except ConsentRequiredError:
            st.error("Consentement actif requis pour accéder aux données de ce point de livraison.")
        except (documents_service.DocumentPermissionError, integrations.IntegrationPermissionError,
                validation.ValidationPermissionError, assets.AssetPermissionError) as exc:
            st.error(str(exc))


main()
