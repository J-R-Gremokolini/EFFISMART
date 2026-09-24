"""F3 — Échéances réglementaires et journal d'actions."""
from datetime import date, timedelta

from app.config import settings
from app.models import DeadlineStatus, Obligation, RegulatoryDeadline
from app.services.regulatory import compute_status, next_operat_due_date
from tests.conftest import login


def test_tertiary_site_creation_generates_operat_deadline(client, world, db):
    headers = login(client, world.auditor_a)
    response = client.post(
        f"/api/organizations/{world.org_a.id}/sites", headers=headers,
        json={"name": "Nouveau siège", "surface_m2": 2500, "is_tertiary_decret": True},
    )
    assert response.status_code == 201
    site_id = response.json()["id"]
    deadlines = db.query(RegulatoryDeadline).filter_by(site_id=site_id).all()
    assert len(deadlines) == 1
    assert deadlines[0].obligation == Obligation.DECRET_TERTIAIRE_OPERAT
    assert (deadlines[0].due_date.month, deadlines[0].due_date.day) == (settings.operat_due_month,
                                                                          settings.operat_due_day)


def test_non_tertiary_site_has_no_operat_deadline(client, world, db):
    headers = login(client, world.auditor_a)
    site_id = client.post(
        f"/api/organizations/{world.org_a.id}/sites", headers=headers, json={"name": "Dépôt"}
    ).json()["id"]
    assert db.query(RegulatoryDeadline).filter_by(site_id=site_id).count() == 0


def test_due_soon_threshold_is_parametrable():
    today = date(2026, 1, 1)
    assert compute_status(today + timedelta(days=60), today=today) == DeadlineStatus.DUE_SOON
    assert compute_status(today + timedelta(days=61), today=today) == DeadlineStatus.UPCOMING
    assert compute_status(today - timedelta(days=5), DeadlineStatus.DONE, today) == DeadlineStatus.DONE
    original = settings.due_soon_days
    settings.due_soon_days = 90
    try:
        assert compute_status(today + timedelta(days=80), today=today) == DeadlineStatus.DUE_SOON
    finally:
        settings.due_soon_days = original


def test_next_operat_due_date_rolls_over():
    assert next_operat_due_date(date(2026, 9, 1)) == date(2026, 9, 30)
    assert next_operat_due_date(date(2026, 10, 1)) == date(2027, 9, 30)


def test_auditor_logs_action_attached_to_obligation(client, world):
    headers = login(client, world.auditor_a)
    response = client.post(
        f"/api/sites/{world.site_a.id}/actions", headers=headers,
        json={"obligation": "DECRET_TERTIAIRE_OPERAT", "description": "Saisie des surfaces sur OPERAT"},
    )
    assert response.status_code == 201
    actions = client.get(f"/api/sites/{world.site_a.id}/actions", headers=headers).json()
    assert actions[0]["obligation"] == "DECRET_TERTIAIRE_OPERAT"


def test_completing_operat_creates_next_year_deadline(client, world, db):
    from app.services.regulatory import ensure_operat_deadline

    deadline = ensure_operat_deadline(db, world.site_a)
    db.commit()
    headers = login(client, world.auditor_a)
    response = client.patch(f"/api/deadlines/{deadline.id}", headers=headers, json={"status": "DONE"})
    assert response.status_code == 200
    assert response.json()["status"] == "DONE"
    dues = sorted(d.due_date for d in db.query(RegulatoryDeadline).filter_by(site_id=world.site_a.id))
    assert dues == [deadline.due_date, deadline.due_date.replace(year=deadline.due_date.year + 1)]
