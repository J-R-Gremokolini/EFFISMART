"""EffiSmart — interface locale 100 % Python (Streamlit).

Lancement : `python lancer.py` à la racine du projet
(ou `streamlit run effismart_ui.py` depuis le dossier backend).

L'interface appelle directement les services du backend : mêmes règles métier,
même isolation multi-tenant (`TenantRepository`) et mêmes rôles que l'API.
Aucune écriture n'est proposée ni acceptée pour un compte « espace client ».

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
    ProviderKind,
    Role,
    Site,
    User,
    ValueUnit,
)
from app.repositories import ResourceNotFound, TenantRepository  # noqa: E402
from app.security import verify_password  # noqa: E402
from app.services import dashboard, integrations, onboarding, regulatory  # noqa: E402
from app.services import documents as documents_service  # noqa: E402
from app.services import drift as drift_service  # noqa: E402
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
DRIFT_STATUS_LABELS = {DriftStatus.OPEN: "Ouverte", DriftStatus.QUALIFIED: "Qualifiée", DriftStatus.IGNORED: "Ignorée"}
DRIFT_STATUS_TONES = {DriftStatus.OPEN: "danger", DriftStatus.QUALIFIED: "success", DriftStatus.IGNORED: "neutral"}
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
PAGE_DRIFTS = "Dérives"
PAGE_REGULATORY = "Réglementaire"
PAGE_EXPORTS = "Exports"
PAGE_DOCUMENTS = "Documents"
PAGE_INTEGRATIONS = "Intégrations"
PAGE_MANAGE = "Patrimoine & consentements"
PAGE_NEW_CLIENT = "Nouveau client"
AUDITOR_PAGES = [PAGE_PORTFOLIO, PAGE_DASHBOARD, PAGE_DRIFTS, PAGE_DOCUMENTS, PAGE_REGULATORY, PAGE_EXPORTS,
                 PAGE_MANAGE, PAGE_NEW_CLIENT, PAGE_INTEGRATIONS]
CLIENT_PAGES = [PAGE_DASHBOARD, PAGE_DRIFTS, PAGE_DOCUMENTS, PAGE_EXPORTS]
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
            "`client@clinique-du-parc.demo` (espace client)"
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
    open_total = sum(r["open_drifts"] for r in rows)
    ui.kpi_grid(
        [
            {"label": "Clients suivis", "value": str(len(rows)), "icon": "clients",
             "note": f"{sum(r['sites_count'] for r in rows)} sites"},
            {"label": f"Consommation {month}", "value": fmt_energy(last_total), "icon": "total",
             "pct": variation(last_total, previous_total), "note": "vs mois précédent"},
            {"label": "Dérives ouvertes", "value": str(open_total), "note": "à qualifier", "icon": "drifts"},
        ]
    )

    left, right = st.columns([5, 2])
    with left:
        st.header("Clients")
        ui.table(
            ["Organisation", "Sites", f"Conso. {month}", "Variation", "Dérives"],
            [
                [
                    f"<b>{ui.e(r['name'])}</b><span class='sub'>{r['delivery_points_count']} points consentis, "
                    f"données au {fmt_date(r['data_as_of'])}</span>",
                    str(r["sites_count"]),
                    fmt_energy(r["last_month_kwh"]),
                    ui.trend(r["variation_pct"])[0],
                    ui.badge(str(r["open_drifts"]), "danger" if r["open_drifts"] else "success"),
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
        st.header("Traitement des dérives")
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
            ui.gauge(handled / total * 100 if total else 100, f"{handled} dérive(s) qualifiée(s) ou ignorée(s) sur {total}")

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

    st.header(f"Dérives ouvertes ({data['open_drifts']})")
    drifts = repo.list_drifts(org.id, DriftStatus.OPEN)[:5]
    if not drifts:
        ui.empty_state("Aucune dérive ouverte", "La consommation suit son rythme habituel.")
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


def page_drifts(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    ui.page_header(org.name, "Dérives de consommation",
                   "Chaque dérive est une alerte à qualifier : aucune action automatique n'est déclenchée.")
    filters = {"Ouvertes": DriftStatus.OPEN, "Qualifiées": DriftStatus.QUALIFIED,
               "Ignorées": DriftStatus.IGNORED, "Toutes": None}
    choice = st.radio("Statut", list(filters), horizontal=True, key="drift_filter")
    drifts = repo.list_drifts(org.id, filters[choice])
    if not drifts:
        ui.empty_state("Aucune dérive", "Rien à qualifier pour ce filtre : la consommation suit son rythme habituel.")
        return
    ui.table(
        ["Date", "Type", "Point de livraison", "Écart", "Détail", "Statut", "Commentaire"],
        [
            [
                fmt_date(d.day),
                ui.e(DRIFT_KIND_LABELS[d.kind.value]),
                f"{ui.e(d.delivery_point.site.name)}<span class='sub'>"
                f"{ui.e(FLUID_LABELS[d.delivery_point.fluid])} {ui.e(d.delivery_point.external_ref)}</span>",
                f"<b>{ui.e(fmt_pct(d.deviation_pct))}</b>",
                ui.e(d.details),
                ui.status(DRIFT_STATUS_LABELS[d.status], DRIFT_STATUS_TONES[d.status]),
                ui.e(d.comment or "—"),
            ]
            for d in drifts
        ],
        numeric={3},
    )

    if not can_write(user):
        return
    st.header("Qualifier une dérive")
    labels = {
        d.id: f"{fmt_date(d.day)}, {DRIFT_KIND_LABELS[d.kind.value].lower()}, {d.delivery_point.site.name} "
              f"({fmt_pct(d.deviation_pct)})"
        for d in drifts
    }
    with st.form("qualify"):
        drift_id = st.selectbox("Dérive", list(labels), format_func=labels.get)
        status = st.radio("Décision", [DriftStatus.QUALIFIED, DriftStatus.IGNORED, DriftStatus.OPEN],
                          format_func=DRIFT_STATUS_LABELS.get, horizontal=True)
        comment = st.text_input("Commentaire", placeholder="ex. groupe froid laissé en marche")
        if st.form_submit_button("Enregistrer", type="primary"):
            guard_write(user)
            drift_service.qualify_drift(db, repo.get_drift(drift_id), status=status,
                                        comment=comment or None, user_id=user.id)
            flash("Dérive mise à jour.")
            st.rerun()


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
        if st.form_submit_button("Créer l'accès"):
            try:
                onboarding.create_viewer(db, org, email=email, password=password)
                flash(f"Accès client créé pour {email.strip().lower()}.")
                st.rerun()
            except (ValueError, onboarding.ConflictError) as exc:
                st.error(str(exc))


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


def sidebar(user: User) -> str:
    pages = CLIENT_PAGES if user.role == Role.CLIENT_VIEWER else AUDITOR_PAGES
    pending = st.session_state.pop("_goto", None)
    if pending:
        st.session_state["page"], target_org = pending
        if target_org is not None:
            st.session_state["org_id"] = target_org
    if st.session_state.get("page") not in pages:
        st.session_state["page"] = pages[0]

    with st.sidebar:
        ui.render(ui.logo_html())
        ui.section_label("Menu")
        page = st.radio("Navigation", pages, key="page", label_visibility="collapsed")
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
            pages = CLIENT_PAGES if is_client else AUDITOR_PAGES
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
                    st.caption("Les dérives sont des alertes à qualifier ; aucune action automatique n'est déclenchée.")
                    st.caption("Données de démonstration : fournisseur simulé (MockDataProvider).")

            # Action principale : nouvel export (écriture réservée à l'auditeur).
            if can_write(user) and org is not None:
                if st.button("Créer un export", type="primary", icon=":material/add:", key="es-new-export"):
                    goto(PAGE_EXPORTS, org.id)

            # Compte : initiales, rôle, mise à jour des données, déconnexion.
            with st.container(key="es-avatar"):
                with st.popover("Mon compte", help="Mon compte"):
                    role = ROLE_LABELS[user.role] + (", lecture seule" if is_client else "")
                    ui.render(ui.user_html(user.email, role))
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
        page = sidebar(user)
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
            elif page == PAGE_DRIFTS:
                page_drifts(db, repo, user, org)
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
        except (documents_service.DocumentPermissionError, integrations.IntegrationPermissionError) as exc:
            st.error(str(exc))


main()
