"""F4 — Moteur d'export OPERAT + VSME."""
import io
import json
import zipfile
from datetime import timedelta

from app.services.exports import DataAssembler
from app.timeutils import yesterday_local
from tests.conftest import login


def _period():
    end = yesterday_local()
    return (end - timedelta(days=30)).isoformat(), end.isoformat()


def test_both_formats_from_a_single_assembly(client, world, monkeypatch):
    calls = []
    original = DataAssembler.assemble

    def spy(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DataAssembler, "assemble", spy)
    start, end = _period()
    headers = login(client, world.auditor_a)
    response = client.post(
        f"/api/organizations/{world.org_a.id}/exports", headers=headers,
        json={"period_start": start, "period_end": end, "formats": ["VSME", "OPERAT"]},
    )
    assert response.status_code == 201, response.text
    jobs = response.json()
    assert [j["format"] for j in jobs] == ["OPERAT", "VSME"]  # OPERAT livré en premier
    assert all(j["status"] == "DONE" for j in jobs)
    assert len(calls) == 1  # socle non recalculé


def test_exports_timestamp_emission_factors(client, world):
    start, end = _period()
    headers = login(client, world.auditor_a)
    job = client.post(
        f"/api/organizations/{world.org_a.id}/exports", headers=headers,
        json={"period_start": start, "period_end": end, "formats": ["OPERAT"]},
    ).json()[0]
    factor = job["factors_used"][0]
    assert {"version", "valid_from", "source", "applied_at"} <= factor.keys()

    archive = client.get(f"/api/exports/{job['id']}/download", headers=headers)
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert set(z.namelist()) == {"donnees_energie_operat.json", "donnees_energie_operat.csv", "LISEZMOI.txt"}
        payload = json.loads(z.read("donnees_energie_operat.json"))
        notice = z.read("LISEZMOI.txt").decode("utf-8-sig")
    assert "brique énergie" in notice and "volets social et de gouvernance" in notice
    assert "indicateurs d'intensité d'usage" in payload["a_completer_par_l_entreprise"]
    assert payload["facteurs_emission_utilises"][0]["version"] == "test-v1"
    assert payload["entites_fonctionnelles_assujetties"][0]["conso_totale_kwh"] > 0


def test_vsme_contains_b3_indicators(client, world):
    start, end = _period()
    headers = login(client, world.auditor_a)
    job = client.post(
        f"/api/organizations/{world.org_a.id}/exports", headers=headers,
        json={"period_start": start, "period_end": end, "formats": ["VSME"]},
    ).json()[0]
    archive = client.get(f"/api/exports/{job['id']}/download", headers=headers)
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        payload = json.loads(z.read("donnees_energie_vsme_b3.json"))
        notice = z.read("LISEZMOI.txt").decode("utf-8-sig")
    b3 = payload["B3_energie_et_ges"]
    # Brique énergie seulement : le module B3 ; les volets social (B8 à B10) et gouvernance (B11) restent à
    # l'entreprise, comme les autres modules environnementaux.
    coverage = payload["couverture_vsme"]
    assert list(coverage["modules_fournis_par_effismart"]) == ["B3"]
    assert {"B8", "B9", "B10", "B11"} <= coverage["modules_a_completer_par_l_entreprise"].keys()
    assert "B11 : Condamnations et amendes pour corruption" in notice
    assert b3["electricite_mwh"] > 0
    assert b3["emissions_scope2_location_based_tco2e"] > 0


def test_client_downloads_but_cannot_generate(client, world):
    start, end = _period()
    auditor = login(client, world.auditor_a)
    job = client.post(
        f"/api/organizations/{world.org_a.id}/exports", headers=auditor,
        json={"period_start": start, "period_end": end, "formats": ["OPERAT"]},
    ).json()[0]
    viewer = login(client, world.client_a)
    assert client.get(f"/api/exports/{job['id']}/download", headers=viewer).status_code == 200
    response = client.post(
        f"/api/organizations/{world.org_a.id}/exports", headers=viewer,
        json={"period_start": start, "period_end": end, "formats": ["OPERAT"]},
    )
    assert response.status_code == 403


def test_other_auditor_cannot_download(client, world):
    start, end = _period()
    job = client.post(
        f"/api/organizations/{world.org_a.id}/exports", headers=login(client, world.auditor_a),
        json={"period_start": start, "period_end": end, "formats": ["OPERAT"]},
    ).json()[0]
    assert client.get(f"/api/exports/{job['id']}/download", headers=login(client, world.auditor_b)).status_code == 404
