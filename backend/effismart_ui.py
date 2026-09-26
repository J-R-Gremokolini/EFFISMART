"""EffiSmart — interface locale 100 % Python (Streamlit).

Lancement : `python lancer.py` à la racine du projet
(ou `streamlit run effismart_ui.py` depuis le dossier backend).

L'interface appelle directement les services du backend : mêmes règles métier,
même isolation multi-tenant (`TenantRepository`) et mêmes rôles que l'API.
Aucune écriture n'est proposée ni acceptée pour un compte « espace client ».
"""
from app.local import configure_local_environment

configure_local_environment()  # avant tout import de app.config

from datetime import date, timedelta  # noqa: E402

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.local import catch_up, prepare_database  # noqa: E402
from app.models import (  # noqa: E402
    DeadlineStatus,
    DriftStatus,
    ExportFormat,
    ExportStatus,
    Fluid,
    Notification,
    Obligation,
    Organization,
    Role,
    User,
)
from app.repositories import ResourceNotFound, TenantRepository  # noqa: E402
from app.security import verify_password  # noqa: E402
from app.services import dashboard, onboarding, regulatory  # noqa: E402
from app.services import drift as drift_service  # noqa: E402
from app.services.consent import ConsentRequiredError, grant_consent, revoke_consent  # noqa: E402
from app.services.delivery_points import active_consent  # noqa: E402
from app.services.exports import ExportEngine, build_zip  # noqa: E402
from app.services.ingestion import backfill_delivery_point  # noqa: E402
from app.timeutils import LOCAL_TZ, to_local, today_local, yesterday_local  # noqa: E402

# --- Libellés (UI en français ; jamais de promesse de « temps réel ») ------------------

APP_NAME = "EffiSmart"
FLUID_LABELS = {Fluid.ELEC: "Électricité", Fluid.GAS: "Gaz"}
FLUID_COLORS = {"Électricité": "#2563eb", "Gaz": "#d97706"}
ROLE_LABELS = {Role.AUDITOR: "Auditeur", Role.CLIENT_VIEWER: "Espace client", Role.ADMIN: "Administrateur"}
PERIOD_LABELS = {"7d": "7 jours", "30d": "30 jours", "12m": "12 mois", "custom": "Personnalisée"}
DRIFT_KIND_LABELS = {
    "THRESHOLD": "Dépassement de seuil",
    "CLIMATE_DEVIATION": "Écart climatique (N-1 / DJU)",
    "BASELOAD": "Talon anormal",
}
DRIFT_STATUS_LABELS = {DriftStatus.OPEN: "Ouverte", DriftStatus.QUALIFIED: "Qualifiée", DriftStatus.IGNORED: "Ignorée"}
OBLIGATION_LABELS = {
    Obligation.DECRET_TERTIAIRE_OPERAT: "Décret Tertiaire — OPERAT",
    Obligation.AUDIT_EED: "Audit énergétique (EED)",
    Obligation.VSME: "Rapport VSME",
}
DEADLINE_STATUS_LABELS = {
    DeadlineStatus.UPCOMING: "À venir",
    DeadlineStatus.DUE_SOON: "Échéance proche",
    DeadlineStatus.DONE: "Réalisée",
}
EXPORT_LABELS = {ExportFormat.OPERAT: "OPERAT (Décret Tertiaire)", ExportFormat.VSME: "VSME (ESG)"}
EXPORT_STATUS_LABELS = {ExportStatus.PENDING: "En cours", ExportStatus.DONE: "Prêt", ExportStatus.FAILED: "Échec"}
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

PAGE_PORTFOLIO = "Portefeuille"
PAGE_DASHBOARD = "Tableau de bord"
PAGE_DRIFTS = "Dérives"
PAGE_REGULATORY = "Réglementaire"
PAGE_EXPORTS = "Exports"
PAGE_MANAGE = "Patrimoine & consentements"
PAGE_NEW_CLIENT = "Nouveau client"
AUDITOR_PAGES = [PAGE_PORTFOLIO, PAGE_DASHBOARD, PAGE_DRIFTS, PAGE_REGULATORY, PAGE_EXPORTS, PAGE_MANAGE,
                 PAGE_NEW_CLIENT]
CLIENT_PAGES = [PAGE_DASHBOARD, PAGE_DRIFTS, PAGE_EXPORTS]
ORG_PAGES = {PAGE_DASHBOARD, PAGE_DRIFTS, PAGE_EXPORTS, PAGE_MANAGE}


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
    _, center, _ = st.columns([1, 1.2, 1])
    with center:
        st.title(f"⚡ {APP_NAME}")
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
        st.info(
            "Comptes de démonstration (mot de passe `demo1234`) :  \n"
            "`auditeur@effismart.demo` — auditeur  \n"
            "`client@clinique-du-parc.demo` — espace client"
        )


def data_as_of_banner(as_of: date | None) -> None:
    if as_of is None:
        st.warning("Aucune donnée disponible : vérifiez le consentement des points de livraison.")
    else:
        st.info(
            f"Données arrêtées au **{fmt_date(as_of)}**. "
            "Les gestionnaires de réseau publient les consommations à J+1."
        )


# --- Pages -----------------------------------------------------------------------------------


def page_portfolio(db: Session, repo: TenantRepository, user: User) -> None:
    st.title("Portefeuille clients")
    rows = dashboard.portfolio(db, repo)
    if not rows:
        st.info("Aucun client pour l'instant. Créez-en un depuis la page « Nouveau client ».")
        return
    month = fmt_month(rows[0]["month"])
    table = pd.DataFrame(
        {
            "Organisation": [r["name"] for r in rows],
            "Sites": [r["sites_count"] for r in rows],
            f"Conso. {month} (kWh)": [r["last_month_kwh"] for r in rows],
            "Variation M-1 (%)": [r["variation_pct"] for r in rows],
            "Dérives ouvertes": [r["open_drifts"] for r in rows],
            "Données au": [fmt_date(r["data_as_of"]) for r in rows],
        }
    )
    st.dataframe(
        table,
        hide_index=True,
        column_config={
            f"Conso. {month} (kWh)": st.column_config.NumberColumn(format="localized"),
            "Variation M-1 (%)": st.column_config.NumberColumn(format="%+.1f %%"),
        },
    )
    st.caption("Ouvrir le tableau de bord d'un client :")
    columns = st.columns(len(rows))
    for column, row in zip(columns, rows):
        if column.button(row["name"], key=f"open-{row['organization_id']}", width="stretch"):
            goto(PAGE_DASHBOARD, row["organization_id"])

    st.subheader("Alertes récentes")
    notifications = db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id, repo.org_clause(Notification.organization_id))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(15)
    ).all()
    if not notifications:
        st.caption("Aucune alerte.")
    for notification in notifications:
        st.markdown(f"- {notification.message}")


def page_dashboard(db: Session, repo: TenantRepository, org: Organization) -> None:
    st.title(org.name)
    period = st.radio(
        "Période", list(PERIOD_LABELS), format_func=PERIOD_LABELS.get, index=1, horizontal=True, key="period"
    )
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
    st.caption(f"Période : {fmt_date(p_start)} → {fmt_date(p_end)}")

    totals = data["totals"]
    versions = ", ".join(sorted({f["version"] for f in data["emission_factors"]}))
    for column, (label, value, help_text) in zip(
        st.columns(5),
        [
            ("Consommation totale", fmt_energy(totals["total_kwh"]), None),
            ("Électricité", fmt_energy(totals["elec_kwh"]), None),
            ("Gaz", fmt_energy(totals["gas_kwh"]), None),
            ("Coût estimé", fmt_eur(totals["cost_eur"]), "Estimation sur prix moyens indicatifs"),
            ("Émissions", fmt_emissions(totals["emissions_kgco2e"]), versions or None),
        ],
    ):
        column.metric(label, value, help=help_text, border=True)

    # Courbe de charge
    points = data["delivery_points"]
    consented = [p for p in points if p["has_active_consent"]]
    missing = [p for p in points if not p["has_active_consent"]]
    st.subheader("Courbe de charge")
    if consented:
        labels = {p["id"]: f"{p['site_name']} — {FLUID_LABELS[p['fluid']]} {p['external_ref']}" for p in consented}
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
            chart = (
                alt.Chart(frame)
                .mark_line(strokeWidth=1.5, color=FLUID_COLORS[fluid_label])
                .encode(
                    x=alt.X("t:T", title=None, axis=alt.Axis(format="%d/%m")),
                    y=alt.Y("value:Q", title=y_title),
                    tooltip=[alt.Tooltip("t:T", title="Date", format="%d/%m/%Y %H:%M"),
                             alt.Tooltip("value:Q", title=curve["unit"], format=",.1f")],
                )
                .properties(height=300)
            )
            st.altair_chart(chart, width="stretch")
            if curve["aggregated"]:
                st.caption("Période longue : courbe agrégée au jour (pas 30 min affiché jusqu'à 31 jours).")
        else:
            st.caption("Aucune mesure sur la période.")
    if missing:
        st.warning(
            "Consentement requis, aucune donnée collectée pour : "
            + ", ".join(f"{p['site_name']} ({p['external_ref']})" for p in missing)
        )

    left, right = st.columns(2)
    with left:
        st.subheader("Consommation mensuelle (12 mois)")
        monthly = pd.DataFrame(data["monthly"])
        monthly["Mois"] = monthly["month"].map(fmt_month)
        long = monthly.melt(id_vars=["month", "Mois"], value_vars=["elec_kwh", "gas_kwh"],
                            var_name="fluid", value_name="kWh")
        long["Énergie"] = long["fluid"].map({"elec_kwh": "Électricité", "gas_kwh": "Gaz"})
        bars = (
            alt.Chart(long)
            .mark_bar()
            .encode(
                x=alt.X("Mois:N", sort=list(monthly["Mois"]), title=None),
                y=alt.Y("kWh:Q", stack=True, title="kWh"),
                color=alt.Color("Énergie:N", scale=alt.Scale(domain=list(FLUID_COLORS),
                                                             range=list(FLUID_COLORS.values()))),
                tooltip=["Mois", "Énergie", alt.Tooltip("kWh:Q", format=",.0f")],
            )
            .properties(height=280)
        )
        st.altair_chart(bars, width="stretch")
    with right:
        st.subheader("Répartition par énergie")
        split = pd.DataFrame(
            {"Énergie": ["Électricité", "Gaz"], "kWh": [totals["elec_kwh"], totals["gas_kwh"]]}
        )
        split = split[split["kWh"] > 0]
        if split.empty:
            st.caption("Aucune donnée.")
        else:
            pie = (
                alt.Chart(split)
                .mark_arc(innerRadius=60)
                .encode(
                    theta="kWh:Q",
                    color=alt.Color("Énergie:N", scale=alt.Scale(domain=list(FLUID_COLORS),
                                                                 range=list(FLUID_COLORS.values()))),
                    tooltip=["Énergie", alt.Tooltip("kWh:Q", format=",.0f")],
                )
                .properties(height=280)
            )
            st.altair_chart(pie, width="stretch")

    st.subheader(f"Dérives ouvertes ({data['open_drifts']})")
    for drift in repo.list_drifts(org.id, DriftStatus.OPEN)[:5]:
        st.markdown(
            f"- **{fmt_date(drift.day)}** · {DRIFT_KIND_LABELS[drift.kind.value]} · "
            f"{drift.delivery_point.site.name} — {drift.details} (**{fmt_pct(drift.deviation_pct)}**)"
        )
    if data["open_drifts"] == 0:
        st.caption("Aucune dérive ouverte.")

    st.caption(
        "Facteurs d'émission : "
        + " · ".join(
            f"{FLUID_LABELS[f['fluid']]} {fmt_number(f['factor_kgco2_per_kwh'], 3)} kgCO₂e/kWh ({f['version']})"
            for f in data["emission_factors"]
        )
    )


def page_drifts(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    st.title(f"Dérives — {org.name}")
    st.caption("Chaque dérive est une alerte à qualifier : aucune action automatique n'est déclenchée.")
    filters = {"Ouvertes": DriftStatus.OPEN, "Qualifiées": DriftStatus.QUALIFIED,
               "Ignorées": DriftStatus.IGNORED, "Toutes": None}
    choice = st.radio("Statut", list(filters), horizontal=True, key="drift_filter")
    drifts = repo.list_drifts(org.id, filters[choice])
    if not drifts:
        st.info("Aucune dérive.")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "Date": [fmt_date(d.day) for d in drifts],
                "Type": [DRIFT_KIND_LABELS[d.kind.value] for d in drifts],
                "Site": [d.delivery_point.site.name for d in drifts],
                "Point": [f"{FLUID_LABELS[d.delivery_point.fluid]} {d.delivery_point.external_ref}" for d in drifts],
                "Écart (%)": [d.deviation_pct for d in drifts],
                "Détail": [d.details for d in drifts],
                "Statut": [DRIFT_STATUS_LABELS[d.status] for d in drifts],
                "Commentaire": [d.comment or "" for d in drifts],
            }
        ),
        hide_index=True,
        column_config={"Écart (%)": st.column_config.NumberColumn(format="%+.0f %%")},
    )

    if not can_write(user):
        return
    st.subheader("Qualifier une dérive")
    labels = {
        d.id: f"{fmt_date(d.day)} · {DRIFT_KIND_LABELS[d.kind.value]} · {d.delivery_point.site.name} "
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
    st.title("Suivi réglementaire")
    orgs = {o.id: o.name for o in repo.list_organizations()}
    org_filter = st.selectbox("Organisation", [None, *orgs],
                              format_func=lambda i: "Toutes les organisations" if i is None else orgs[i])
    deadlines = repo.list_deadlines(org_filter)
    today = today_local()

    def days_left(deadline) -> str:
        if deadline.status == DeadlineStatus.DONE:
            return "—"
        days = (deadline.due_date - today).days
        return "En retard" if days < 0 else str(days)

    if deadlines:
        st.dataframe(
            pd.DataFrame(
                {
                    "Échéance": [fmt_date(d.due_date) for d in deadlines],
                    "Obligation": [OBLIGATION_LABELS[d.obligation] for d in deadlines],
                    "Organisation": [d.site.organization.name for d in deadlines],
                    "Site": [d.site.name for d in deadlines],
                    "Jours restants": [days_left(d) for d in deadlines],
                    "Statut": [DEADLINE_STATUS_LABELS[d.status] for d in deadlines],
                    "Notes": [d.notes or "" for d in deadlines],
                }
            ),
            hide_index=True,
        )
    else:
        st.info("Aucune échéance.")

    sites = {s.id: f"{s.organization.name} — {s.name}" for s in repo.list_sites(org_filter)}
    left, right = st.columns(2)
    with left:
        st.subheader("Mettre à jour une échéance")
        if deadlines:
            with st.form("deadline_status"):
                labels = {d.id: f"{fmt_date(d.due_date)} · {OBLIGATION_LABELS[d.obligation]} · {d.site.name}"
                          for d in deadlines}
                deadline_id = st.selectbox("Échéance", list(labels), format_func=labels.get)
                done = st.radio("Action", ["Marquer réalisée", "Rouvrir"], horizontal=True)
                if st.form_submit_button("Enregistrer", type="primary"):
                    status = DeadlineStatus.DONE if done == "Marquer réalisée" else DeadlineStatus.UPCOMING
                    regulatory.update_deadline(db, repo.get_deadline(deadline_id), user_id=user.id, status=status)
                    flash("Échéance mise à jour.")
                    st.rerun()
    with right:
        st.subheader("Ajouter une échéance")
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

    st.subheader("Journal des actions")
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
    for action in repo.list_action_logs(site_id):
        st.markdown(
            f"- **{to_local(action.performed_at).strftime('%d/%m/%Y')}** · {OBLIGATION_LABELS[action.obligation]} — "
            f"{action.description}"
        )


def page_exports(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    st.title(f"Exports — {org.name}")
    if can_write(user):
        st.caption("Un même calcul alimente les deux gabarits. Formats provisoires V1 : JSON + CSV.")
        last_year = today_local().year - 1
        with st.form("export"):
            left, right = st.columns(2)
            start = left.date_input("Début de période", value=date(last_year, 1, 1), format="DD/MM/YYYY")
            end = right.date_input("Fin de période", value=date(last_year, 12, 31), format="DD/MM/YYYY")
            formats = st.multiselect("Formats", list(ExportFormat), default=list(ExportFormat),
                                     format_func=EXPORT_LABELS.get)
            if st.form_submit_button("Générer", type="primary"):
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

    st.subheader("Exports disponibles")
    jobs = repo.list_export_jobs(org.id)
    if not jobs:
        st.info("Aucun export pour l'instant.")
    for job in jobs:
        with st.container(border=True):
            info, action = st.columns([4, 1])
            factors = " · ".join(
                f"{FLUID_LABELS[Fluid(f['fluid'])]} {fmt_number(f['factor_kgco2e_per_kwh'], 3)} kgCO₂e/kWh "
                f"({f['version']}, valide dès {f['valid_from']})"
                for f in job.factors_used or []
            )
            info.markdown(
                f"**{EXPORT_LABELS[job.format]}** — {fmt_date(job.period_start)} → {fmt_date(job.period_end)} "
                f"· {EXPORT_STATUS_LABELS[job.status]}  \n"
                f"<small>Créé le {to_local(job.created_at).strftime('%d/%m/%Y %H:%M')} · Facteurs : {factors}</small>",
                unsafe_allow_html=True,
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
                        mime="application/zip", key=f"dl-{job.id}", on_click="ignore",
                    )


def page_manage(db: Session, repo: TenantRepository, user: User, org: Organization) -> None:
    guard_write(user)
    st.title(f"Patrimoine & consentements — {org.name}")

    for site in repo.list_sites(org.id):
        tags = " · assujetti au Décret Tertiaire" if site.is_tertiary_decret else ""
        with st.expander(f"{site.name}{tags}", expanded=True):
            surface = f" · {fmt_number(site.surface_m2)} m²" if site.surface_m2 else ""
            st.caption(f"{site.address or 'Adresse non renseignée'}{surface}")
            for dp in site.delivery_points:
                consent = active_consent(db, dp.id)
                label = f"**{FLUID_LABELS[dp.fluid]} · {dp.external_ref}**" + (" · principal" if dp.is_primary else "")
                if dp.subscribed_power_kva:
                    label += f" · {fmt_number(dp.subscribed_power_kva)} kVA souscrits"
                if consent:
                    left, right = st.columns([4, 1])
                    left.markdown(f"{label}  \n✅ Consentement actif depuis le "
                                  f"{to_local(consent.granted_at).strftime('%d/%m/%Y')} (preuve : {consent.proof_ref})")
                    if right.button("Révoquer", key=f"revoke-{consent.id}"):
                        revoke_consent(db, consent)
                        flash(f"Consentement révoqué pour {dp.external_ref}.")
                        st.rerun()
                else:
                    st.markdown(f"{label}  \n⚠️ Aucun consentement actif : aucune donnée n'est collectée.")
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
                a, b, c = st.columns([2, 1, 1])
                ref = a.text_input("PRM / PCE (14 chiffres)")
                fluid = b.selectbox("Énergie", list(Fluid), format_func=FLUID_LABELS.get)
                primary = c.checkbox("Point principal")
                if st.form_submit_button("Ajouter"):
                    try:
                        onboarding.create_delivery_point(db, site, fluid=fluid, external_ref=ref, is_primary=primary)
                        flash("Point de livraison ajouté. Recueillez maintenant le consentement.")
                        st.rerun()
                    except (ValueError, onboarding.ConflictError) as exc:
                        st.error(str(exc))

    left, right = st.columns(2)
    with left, st.form("site", clear_on_submit=True):
        st.subheader("Ajouter un site")
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
        st.subheader("Accès espace client")
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
    st.title("Nouveau client")
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


def sidebar(repo: TenantRepository, user: User) -> tuple[str, Organization | None]:
    pages = CLIENT_PAGES if user.role == Role.CLIENT_VIEWER else AUDITOR_PAGES
    pending = st.session_state.pop("_goto", None)
    if pending:
        st.session_state["page"], target_org = pending
        if target_org is not None:
            st.session_state["org_id"] = target_org
    if st.session_state.get("page") not in pages:
        st.session_state["page"] = pages[0]

    with st.sidebar:
        st.markdown(f"## ⚡ {APP_NAME}")
        st.caption(f"{user.email}  \n{ROLE_LABELS[user.role]}"
                   + (" · lecture seule" if user.role == Role.CLIENT_VIEWER else ""))
        page = st.radio("Navigation", pages, key="page", label_visibility="collapsed")

        org = None
        if user.role == Role.CLIENT_VIEWER:
            org = repo.get_organization(user.organization_id)
        elif page in ORG_PAGES:
            orgs = {o.id: o for o in repo.list_organizations()}
            if orgs:
                if st.session_state.get("org_id") not in orgs:
                    st.session_state["org_id"] = next(iter(orgs))
                org_id = st.selectbox("Client", list(orgs), format_func=lambda i: orgs[i].name, key="org_id")
                org = orgs[org_id]

        st.divider()
        if can_write(user) and st.button("Mettre à jour les données", width="stretch",
                                         help="Récupère les jours manquants jusqu'à la veille et analyse les dérives"):
            with st.spinner("Mise à jour…"):
                days = catch_up()
            flash("Données à jour." if days == 0 else f"{days} jour(s) récupéré(s) et analysé(s).")
            st.rerun()
        if st.button("Se déconnecter", width="stretch"):
            st.session_state.clear()
            st.rerun()
        st.caption(f"Base locale : {settings.database_url.rsplit('/', 1)[-1]}")
    return page, org


def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="⚡", layout="wide")
    initialize()
    with SessionLocal() as db:
        user = current_user(db)
        if user is None:
            login_page(db)
            return
        repo = TenantRepository(db, user)
        page, org = sidebar(repo, user)
        show_flash()
        try:
            if page == PAGE_PORTFOLIO:
                page_portfolio(db, repo, user)
            elif page == PAGE_REGULATORY:
                page_regulatory(db, repo, user)
            elif page == PAGE_NEW_CLIENT:
                page_new_client(db, user)
            elif org is None:
                st.info("Aucun client pour l'instant. Créez-en un depuis la page « Nouveau client ».")
            elif page == PAGE_DASHBOARD:
                page_dashboard(db, repo, org)
            elif page == PAGE_DRIFTS:
                page_drifts(db, repo, user, org)
            elif page == PAGE_EXPORTS:
                page_exports(db, repo, user, org)
            elif page == PAGE_MANAGE:
                page_manage(db, repo, user, org)
        except ResourceNotFound:
            st.error("Ressource introuvable ou hors de votre périmètre.")
        except ConsentRequiredError:
            st.error("Consentement actif requis pour accéder aux données de ce point de livraison.")


main()
