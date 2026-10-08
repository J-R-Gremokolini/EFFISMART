"""Périmètre F1–F5 confirmé par l'étude de marché : données N0 (factures) et mode dégradé, avant / après
recommandations, notification immédiate par e-mail, rappels d'échéances, données RSE produites automatiquement."""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import (
    Consent,
    DeadlineStatus,
    DeliveryPoint,
    DeliveryStatus,
    Drift,
    DriftStatus,
    ExportFormat,
    Fluid,
    Notification,
    Obligation,
    OutgoingEmail,
    ProviderKind,
    Recommendation,
    RecommendationKind,
    ReviewStatus,
    Role,
    User,
)
from app.repositories import ResourceNotFound, TenantRepository
from app.security import hash_password
from app.services import dashboard, energy_data, exports, integrations, mailer, regulatory, savings, validation
from app.services.drift import detect_baseload
from app.services.ingestion import ingest_delivery_point
from app.timeutils import LOCAL_TZ, today_local, utcnow, yesterday_local
from tests.conftest import PASSWORD

# --- Données N0 et mode dégradé ---------------------------------------------------------------------


@pytest.fixture()
def invoiced_point(db, world) -> DeliveryPoint:
    """Point de gaz sans compteur communicant ni consentement : seules ses factures sont connues."""
    dp = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.GAS, external_ref="21009000000001",
                       provider=ProviderKind.MOCK)
    db.add(dp)
    db.commit()
    return dp


def _month_end(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def test_invoices_feed_the_dashboard_in_degraded_mode(db, world, invoiced_point):
    repo = TenantRepository(db, world.auditor_a)
    first = (yesterday_local().replace(day=1) - timedelta(days=1)).replace(day=1)  # mois précédent complet
    energy_data.add_declared(db, repo, world.auditor_a, invoiced_point.id, period_start=first,
                             period_end=_month_end(first), kwh=3100, amount_eur=420)
    data = dashboard.organization_dashboard(db, world.org_a, "custom", first, _month_end(first))
    assert data["totals"]["gas_kwh"] == pytest.approx(3100, rel=1e-6)
    assert data["totals"]["n0_kwh"] == pytest.approx(3100, rel=1e-6)
    level = {p["external_ref"]: p["data_level"] for p in data["delivery_points"]}
    assert level["21009000000001"] == "N0" and level[world.dp_a.external_ref] == "N1"


def test_meter_data_take_priority_over_invoices(db, world):
    """Sur un point consenti, la facture ne compte que pour les jours sans mesure du compteur."""
    repo = TenantRepository(db, world.auditor_a)
    end = yesterday_local()
    start = end - timedelta(days=59)  # 20 jours avant la première mesure de la fixture (40 jours)
    energy_data.add_declared(db, repo, world.auditor_a, world.dp_a.id, period_start=start, period_end=end, kwh=6000)
    combined = energy_data.combined_daily(db, world.dp_a, start, end)
    n0_days = [d for d, (_, level) in combined.items() if level == "N0"]
    assert len(n0_days) == 19 and all(d < end - timedelta(days=40) for d in n0_days)
    assert sum(v for v, level in combined.values() if level == "N0") == pytest.approx(6000 * 19 / 60)


def test_declared_consumptions_are_checked(db, world, invoiced_point):
    repo = TenantRepository(db, world.auditor_a)
    day = date(2025, 1, 1)
    energy_data.add_declared(db, repo, world.auditor_a, invoiced_point.id, period_start=day,
                             period_end=date(2025, 1, 31), kwh=100)
    for kwargs in (
        {"period_start": date(2025, 1, 15), "period_end": date(2025, 2, 14), "kwh": 100},  # chevauchement
        {"period_start": date(2025, 3, 31), "period_end": date(2025, 3, 1), "kwh": 100},  # fin avant début
        {"period_start": today_local(), "period_end": today_local() + timedelta(days=30), "kwh": 100},  # futur
        {"period_start": date(2025, 4, 1), "period_end": date(2025, 4, 30), "kwh": -5},  # négatif
    ):
        with pytest.raises(energy_data.DeclaredError):
            energy_data.add_declared(db, repo, world.auditor_a, invoiced_point.id, **kwargs)
    with pytest.raises(energy_data.DeclaredPermissionError):  # le client dépose, l'auditeur saisit
        energy_data.add_declared(db, TenantRepository(db, world.client_a), world.client_a, invoiced_point.id,
                                 period_start=date(2025, 5, 1), period_end=date(2025, 5, 31), kwh=1)
    with pytest.raises(ResourceNotFound):  # isolation entre cabinets
        energy_data.add_declared(db, TenantRepository(db, world.auditor_b), world.auditor_b, invoiced_point.id,
                                 period_start=date(2025, 5, 1), period_end=date(2025, 5, 31), kwh=1)


def test_csv_import_reports_rejected_lines(db, world, invoiced_point):
    repo = TenantRepository(db, world.auditor_a)
    content = ("Point;Début;Fin;kWh;Montant\n"
               "21009000000001;01/02/2025;28/02/2025;2 450,5;310,20\n"
               "21009000000001;01/03/2025;31/03/2025;1980;\n"
               "99999999999999;01/03/2025;31/03/2025;10;\n"  # point inconnu
               "21009000000001;15/03/2025;30/04/2025;500;\n").encode("utf-8")  # chevauchement
    created, errors = energy_data.import_csv(db, repo, world.auditor_a, world.org_a.id, content)
    assert created == 2 and len(errors) == 2
    entries = energy_data.list_declared(db, repo, world.org_a.id)
    assert {e.kwh for e in entries} == {2450.5, 1980.0}


def test_rse_dataset_includes_invoices_and_states_its_sources(db, world, invoiced_point):
    repo = TenantRepository(db, world.auditor_a)
    energy_data.add_declared(db, repo, world.auditor_a, invoiced_point.id, period_start=date(2025, 1, 1),
                             period_end=date(2025, 12, 31), kwh=36500)
    data = exports.DataAssembler().assemble(db, world.org_a, date(2025, 1, 1), date(2025, 12, 31))
    gas = data.sites[0].by_fluid[Fluid.GAS]
    assert gas.kwh == pytest.approx(36500) and gas.n0_kwh == pytest.approx(36500)
    assert gas.data_sources == {"N0 factures et relevés"}
    header = exports._common_header(data, "VSME")
    assert header["niveau_des_donnees"]["N0_factures_et_releves_kwh"] == pytest.approx(36500)
    assert "volets social et de gouvernance" in header["perimetre"]  # brique énergie, pas le rapport complet


def test_rse_datasets_are_produced_automatically_once(db, world):
    today = today_local()
    created = exports.produce_automatic(db, today)
    assert created and all(job.automatic and job.created_by is None for job in created)
    assert {job.format for job in created} == set(ExportFormat)
    assert exports.produce_automatic(db, today) == []  # déjà produits : rien de nouveau
    client_jobs = TenantRepository(db, world.client_a).list_export_jobs(world.org_a.id)
    assert {job.id for job in client_jobs} >= {job.id for job in created if job.organization_id == world.org_a.id}


def test_automatic_datasets_are_renewed_when_the_template_changes(db, world):
    """Nouvelle version des fichiers : les jeux automatiques sont produits de nouveau, sans ressaisie. L'ancienne
    version reste conservée ; seule la plus récente est proposée au téléchargement."""
    today = today_local()
    first = exports.produce_automatic(db, today)
    for job in first:  # jeux produits avec le gabarit précédent
        for path in (Path(settings.export_dir) / job.file_ref).glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["format_version"] = "effismart-v1-provisoire"
            path.write_text(json.dumps(payload), encoding="utf-8")
    renewed = exports.produce_automatic(db, today)
    assert len(renewed) == len(first) and all(exports.is_current(job) for job in renewed)
    shown = exports.latest_jobs(TenantRepository(db, world.auditor_a).list_export_jobs(world.org_a.id))
    assert {job.id for job in shown} == {job.id for job in renewed if job.organization_id == world.org_a.id}


def test_partner_api_consumption_flags_invoice_share(client, db, world, invoiced_point):
    repo = TenantRepository(db, world.auditor_a)
    end = yesterday_local()
    energy_data.add_declared(db, repo, world.auditor_a, invoiced_point.id, period_start=end - timedelta(days=9),
                             period_end=end, kwh=1000)
    _, raw = integrations.create_api_key(db, world.auditor_a, repo, name="BI")
    data = client.get(f"/api/v1/organizations/{world.org_a.id}/consumption?start={end - timedelta(days=9)}&end={end}",
                      headers={"X-API-Key": raw}).json()["data"]
    assert sum(row["n0_kwh"] for row in data) == pytest.approx(1000) and sum(row["gas_kwh"] for row in data) == pytest.approx(1000)


# --- Avant / après recommandations -------------------------------------------------------------------


def test_savings_are_measured_then_validated_before_the_client_sees_them(db, world):
    """Entrepôt de démonstration : éclairage allumé la nuit du 3/11/2025 au 5/04/2026, corrigé le 6/04."""
    dp = DeliveryPoint(site_id=world.site_a.id, fluid=Fluid.ELEC, external_ref="30001000000005",
                       provider=ProviderKind.MOCK)
    db.add(dp)
    db.flush()
    db.add(Consent(delivery_point_id=dp.id, granted_at=utcnow() - timedelta(days=1), scope="t", proof_ref="t"))
    db.commit()
    ingest_delivery_point(db, dp, date(2025, 9, 1), date(2026, 7, 31))
    from app.services import integrations as integ

    candidate = detect_baseload(db, dp, date(2025, 11, 8), integ.weather_provider(db))
    assert candidate is not None  # l'éclairage allumé est bien vu comme un talon de nuit anormal
    drift = Drift(delivery_point_id=dp.id, kind=candidate.kind, day=date(2025, 11, 8), measured_value=candidate.measured,
                  reference_value=candidate.reference, deviation_pct=candidate.deviation_pct, unit="kW",
                  details=candidate.details, status=DriftStatus.QUALIFIED, qualified_by=world.auditor_a.id)
    db.add(drift)
    db.flush()
    rec = Recommendation(organization_id=world.org_a.id, site_id=world.site_a.id, delivery_point_id=dp.id,
                         drift_id=drift.id, kind=RecommendationKind.SCHEDULE_OFF_HOURS, title="Éteindre l'éclairage",
                         action="LED et détecteurs", status=ReviewStatus.APPLIED, reviewed_by=world.auditor_a.id,
                         applied_by=world.auditor_a.id,
                         applied_at=datetime.combine(date(2026, 4, 6), time(8), tzinfo=LOCAL_TZ))
    db.add(rec)
    db.commit()

    measurement = savings.measure(db, rec)
    assert measurement is not None and measurement.baseline_start == date(2025, 11, 8)
    assert measurement.saved_kwh > 3 * measurement.uncertainty_kwh  # économie nette, au-delà de l'incertitude
    assert 0.1 < measurement.saved_pct < 0.6 and measurement.weekly and measurement.reasoning

    with pytest.raises(validation.ValidationPermissionError):
        validation.validate_savings(db, TenantRepository(db, world.client_a), world.client_a, rec.id)
    validation.validate_savings(db, TenantRepository(db, world.auditor_a), world.auditor_a, rec.id)
    db.refresh(rec)
    assert rec.savings_snapshot["saved_kwh"] == pytest.approx(measurement.saved_kwh)
    assert rec.savings_validated_by == world.auditor_a.id
    # Retour à « validée » : la déclaration d'application et la mesure tombent ensemble.
    validation.review_recommendation(db, TenantRepository(db, world.auditor_a), world.auditor_a, rec.id,
                                     ReviewStatus.VALIDATED)
    assert rec.savings_snapshot is None and rec.applied_at is None


def test_savings_need_two_weeks_of_follow_up(db, world):
    rec = Recommendation(organization_id=world.org_a.id, site_id=world.site_a.id, delivery_point_id=world.dp_a.id,
                         kind=RecommendationKind.INVESTIGATE, title="t", action="a", status=ReviewStatus.APPLIED,
                         applied_at=utcnow() - timedelta(days=3))
    db.add(rec)
    db.commit()
    assert savings.measure(db, rec) is None
    assert savings.availability(rec) > today_local()


# --- Notification immédiate par e-mail ----------------------------------------------------------------


def test_alerts_are_emailed_to_their_recipients_only(db, world, tmp_path, monkeypatch):
    monkeypatch.setattr(mailer.settings, "mail_outbox_dir", str(tmp_path))
    manager = User(email="energie-a@test.fr", password_hash=hash_password(PASSWORD), role=Role.CLIENT_VIEWER,
                   organization_id=world.org_a.id, is_energy_manager=True, email_notifications=False)
    db.add(manager)
    db.commit()
    validation.notify(db, world.org_a.id, "À valider : talon anormal", validators_only=True)
    db.commit()
    emails = db.scalars(select(OutgoingEmail)).all()
    assert [e.to_address for e in emails] == [world.auditor_a.email]  # responsable énergie : e-mails coupés
    assert db.scalar(select(Notification.id).where(Notification.user_id == manager.id))  # alerte quand même
    assert mailer.dispatch_pending(db) == (1, 0)
    written = list(tmp_path.glob("*.eml"))
    assert len(written) == 1 and "talon anormal" in written[0].read_text(encoding="utf-8")
    assert emails[0].status == DeliveryStatus.SENT and emails[0].channel == "outbox"


def test_email_without_any_server_is_retried_then_abandoned(db, world, monkeypatch):
    monkeypatch.setattr(mailer.settings, "mail_outbox_dir", "")
    monkeypatch.setattr(mailer.settings, "mail_max_attempts", 2)
    email = mailer.enqueue(db, world.auditor_a, "Rappel")
    db.commit()
    assert mailer.dispatch_pending(db) == (0, 0) and email.status == DeliveryStatus.PENDING
    email.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert mailer.dispatch_pending(db) == (0, 1) and "aucun serveur" in email.last_error


def test_smtp_settings_are_admin_only_and_checked(db, world):
    admin = User(email="admin@test.fr", password_hash=hash_password(PASSWORD), role=Role.ADMIN)
    db.add(admin)
    db.commit()
    from app.models import IntegrationKind

    with pytest.raises(integrations.IntegrationPermissionError):
        integrations.save_platform(db, world.auditor_a, IntegrationKind.SMTP, enabled=False, settings_values={},
                                   secrets_values={})
    with pytest.raises(integrations.IntegrationError):  # activer sans serveur
        integrations.save_platform(db, admin, IntegrationKind.SMTP, enabled=True, settings_values={"host": ""},
                                   secrets_values={})
    row = integrations.save_platform(db, admin, IntegrationKind.SMTP, enabled=True, secrets_values={},
                                     settings_values={"host": "smtp.exemple.fr", "port": "465", "security": "ssl",
                                                      "sender": "alertes@exemple.fr", "username": ""})
    assert row.settings["port"] == 465 and mailer.smtp_config(db)["host"] == "smtp.exemple.fr"


# --- Rappels d'échéances -----------------------------------------------------------------------------------


def test_deadline_reminders_once_per_threshold(db, world):
    today = today_local()
    deadline = regulatory.create_deadline(db, world.site_a, Obligation.AUDIT_EED, today + timedelta(days=25))
    db.commit()
    assert regulatory.send_reminders(db, today) >= 1 and deadline.reminder_level == 30
    messages = [n.message for n in db.scalars(select(Notification).where(Notification.user_id == world.auditor_a.id))]
    assert any("dans 25 jour(s)" in m for m in messages)
    assert not db.scalar(select(Notification.id).where(Notification.user_id == world.auditor_b.id))  # autre cabinet
    assert not db.scalar(select(Notification.id).where(Notification.user_id == world.client_a.id))  # F3 : auditeur
    before = db.query(Notification).count()
    regulatory.send_reminders(db, today)
    assert db.query(Notification).count() == before  # pas de doublon
    regulatory.send_reminders(db, today + timedelta(days=20))
    assert deadline.reminder_level == 7
    regulatory.send_reminders(db, today + timedelta(days=27))
    assert deadline.reminder_level == -1  # en retard
    regulatory.update_deadline(db, deadline, user_id=world.auditor_a.id, due_date=today + timedelta(days=90))
    assert deadline.reminder_level is None and deadline.status == DeadlineStatus.UPCOMING
