from __future__ import annotations

import json
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient

from idx_digest.db import Database
from idx_digest.gui import app


def _summary(ticker: str, start: str, end: str) -> dict:
    return {
        "ticker": ticker,
        "period": {"start": start, "end": end},
        "announcement_count": 1,
        "overview": f"{ticker} saved overview",
        "timeline": [],
        "material_changes": [],
        "key_financial_figures": [],
        "corporate_actions": [],
        "expansion_projects": [],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [],
        "risks_or_uncertainties": [],
        "items_to_monitor": [],
        "limitations": [],
    }


def test_profile_creation_isolates_history_and_autosaves_state(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    Database(tmp_path / "data" / "idx_digest.sqlite3").save_company_summary(
        "ANTM", start, end, _summary("ANTM", start, end), "model", "prompt"
    )

    client = TestClient(app)
    profiles = client.get("/api/profiles").json()
    assert profiles["active_profile_id"] == "main"
    assert profiles["profiles"][0]["company_summaries"] == 1
    assert client.get("/api/library").json()["counts"]["company_summaries"] == 1

    created = client.post(
        "/api/profiles",
        json={
            "name": "Clean room",
            "description": "No inherited research history",
            "copy_current_config": False,
            "copy_current_prompts": False,
        },
    )
    assert created.status_code == 201
    profile_id = created.json()["active_profile_id"]
    assert profile_id != "main"
    clean_library = client.get("/api/library").json()
    assert clean_library["counts"]["company_summaries"] == 0
    assert clean_library["runs"] == []

    saved = client.put(
        f"/api/profiles/{profile_id}/state",
        json={"state": {"ticker": "BBCA", "llm_concurrency": 3, "view": "companies"}},
    )
    assert saved.status_code == 200
    assert saved.json()["state"]["ticker"] == "BBCA"
    assert saved.json()["state"]["autosaved_at"]

    switched = client.post("/api/profiles/main/activate")
    assert switched.status_code == 200
    assert client.get("/api/library").json()["counts"]["company_summaries"] == 1


def test_library_window_opens_saved_summaries_without_run_form_dates(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    db = Database(tmp_path / "data" / "idx_digest.sqlite3")
    db.save_company_summary("ANTM", start, end, _summary("ANTM", start, end), "model", "prompt")
    db.save_company_summary("BBCA", start, end, _summary("BBCA", start, end), "model", "prompt")

    client = TestClient(app)
    library = client.get("/api/library")
    assert library.status_code == 200
    windows = library.json()["windows"]
    assert len(windows) == 1
    assert windows[0]["company_count"] == 2
    assert windows[0]["tickers"] == ["ANTM", "BBCA"]

    opened = client.post(
        "/api/library/window",
        json={"start_at": start, "end_at": end},
    )
    assert opened.status_code == 200
    assert sorted(opened.json()["summaries"]) == ["ANTM", "BBCA"]


def test_run_snapshot_contains_stream_state_and_profile_config(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run123"
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True)
    state = {
        "run_id": run_id,
        "request": {"mode": "scrape", "start": "2026-08-07T00:00", "end": "2026-08-07T23:59", "ticker": "ANTM"},
        "status": "completed",
        "created_at": "2026-08-07T01:00:00+07:00",
        "started_at": "2026-08-07T01:00:01+07:00",
        "finished_at": "2026-08-07T01:01:00+07:00",
        "report": {"company_summaries": 1},
        "summaries": {"ANTM": {"overview": "saved"}},
        "partial_summaries": {"ANTM": [{"announcement_id": "a1"}]},
        "failure": None,
        "artifacts": ["stream"],
        "artifact_paths": {"stream": str(run_dir / "events.jsonl")},
        "last_seq": 1,
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(json.dumps({"seq": 1, "type": "event", "stage": "run", "message": "done"}) + "\n", encoding="utf-8")

    client = TestClient(app)
    client.put("/api/profiles/main/state", json={"state": {"ticker": "ANTM", "view": "activity"}})
    response = client.post(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "state.json" in names
        assert "events.jsonl" in names
        assert "snapshot_manifest.json" in names
        assert "profile_state_snapshot.json" in names
        manifest = json.loads(archive.read("snapshot_manifest.json"))
        assert manifest["company_summary_count"] == 1
        assert manifest["announcement_summary_count"] == 1


def test_profile_delete_removes_isolated_tree_switches_to_main_and_protects_main(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)
    created = client.post(
        "/api/profiles",
        json={
            "name": "Temporary research",
            "description": "delete me",
            "copy_current_config": False,
            "copy_current_prompts": False,
        },
    )
    assert created.status_code == 201
    profile_id = created.json()["active_profile_id"]
    profile_dir = tmp_path / "data" / "profiles" / profile_id
    sentinel = profile_dir / "delete-sentinel.txt"
    sentinel.write_text("profile-local", encoding="utf-8")
    assert sentinel.exists()

    deleted = client.delete(f"/api/profiles/{profile_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted_profile_id"] == profile_id
    assert deleted.json()["active_profile_id"] == "main"
    assert not profile_dir.exists()

    profiles = client.get("/api/profiles").json()
    assert profiles["active_profile_id"] == "main"
    assert profile_id not in {p["id"] for p in profiles["profiles"]}

    protected = client.delete("/api/profiles/main")
    assert protected.status_code == 403
    assert (tmp_path / "data").exists()
