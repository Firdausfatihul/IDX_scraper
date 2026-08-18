from __future__ import annotations

from fastapi.testclient import TestClient

from idx_digest.gui import app
from idx_digest.observability import RunObserver


def test_gui_health_and_index() -> None:
    client = TestClient(app)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["version"] == "0.15.5"

    index = client.get("/")
    assert index.status_code == 200
    assert "IDX Signal Desk" in index.text
    assert "Finish cached company digests" in index.text
    assert "Refine cached financial reports" in index.text
    assert "Listed stocks only" in index.text
    assert "Smart primary attachments" in index.text
    assert any(getattr(route, "path", None) == "/api/refine-financials" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/reduce-cached" for route in app.routes)
    assert "EventSource" in index.text
    assert "Prompt Studio" in index.text
    assert "Open cached checkpoints" in index.text
    assert "Resume interrupted run" in index.text
    assert "Copy / export all" in index.text
    assert "Share company digests" in index.text
    assert "Inspect ticker" in index.text
    assert "Ticker inspector" in index.text
    assert "Saved Intelligence" in index.text
    assert "Company Index" in index.text
    assert "Activity Archive" in index.text
    assert "New profile" in index.text
    assert "Delete profile" in index.text
    assert "Blank = ALL companies" in index.text
    assert "Save run snapshot" in index.text
    assert "Pipeline Observatory" in index.text
    assert "Next Run Settings" in index.text
    assert "Concurrency preset" in index.text
    assert "NETWORK ONLINE" in index.text
    assert "Apply next-run tuning" in index.text
    assert "no analytics" in index.text
    assert any(getattr(route, "path", None) == "/api/share/render" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/share/export" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/company-detail" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/llm-audit/{audit_id}" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/profiles" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/profiles/{profile_id}" and "DELETE" in getattr(route, "methods", set()) for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/library" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/library/window" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/runs/{run_id}/snapshot" for route in app.routes)

    prompts = client.get("/api/prompts")
    assert prompts.status_code == 200
    assert prompts.json()["profile_name"] == "Corporate actions & expansion"
    assert "announcement" in prompts.json()["prompts"]
    assert "public_expose_document" in prompts.json()["prompts"]


def test_prompt_api_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)
    before = client.get("/api/prompts").json()
    custom = before["prompts"]["company"] + "\nPrioritaskan covenant pendanaan."
    saved = client.put(
        "/api/prompts",
        json={"prompts": {"company": custom}, "profile_name": "Research desk"},
    )
    assert saved.status_code == 200
    assert saved.json()["profile_name"] == "Research desk"
    assert saved.json()["hashes"]["company"] != before["hashes"]["company"]
    assert (tmp_path / "data" / "prompts.json").exists()

    reset = client.post("/api/prompts/reset", json={"keys": ["company"]})
    assert reset.status_code == 200
    assert reset.json()["profile_name"] == "Research desk"
    assert reset.json()["prompts"]["company"] == before["defaults"]["company"]


def test_observer_publishes_tasks_without_terminal_progress(tmp_path) -> None:
    events: list[dict] = []
    observer = RunObserver(
        timezone_name="Asia/Jakarta",
        verbose=False,
        show_progress=False,
        console_output=False,
        event_sink=events.append,
        log_file=tmp_path / "events.jsonl",
    )
    try:
        task_id = observer.start_task("Document summaries ANTM", total=2)
        assert task_id is not None
        observer.update_task(task_id, advance=1)
        observer.finish_task(task_id, completed=2)
        observer.event("llm", "OpenRouter request", schema="idx_document_summary")
    finally:
        observer.close()

    task_actions = [event["action"] for event in events if event["type"] == "task"]
    assert task_actions == ["start", "update", "update", "finish"]
    assert any(event.get("type") == "event" and event.get("stage") == "llm" for event in events)


def test_cli_lists_gui_command() -> None:
    from typer.testing import CliRunner
    from idx_digest.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "gui" in result.stdout
    assert "export-all" in result.stdout
    assert "refine-financials" in result.stdout


def test_share_api_renders_saved_company_digests(tmp_path, monkeypatch) -> None:
    from idx_digest.db import Database

    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "data" / "idx_digest.sqlite3")
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    summary = {
        "ticker": "ANTM",
        "period": {"start": start, "end": end},
        "announcement_count": 1,
        "overview": "ANTM overview",
        "timeline": [],
        "material_changes": [],
        "key_financial_figures": [],
        "corporate_actions": ["Corporate action signal"],
        "expansion_projects": [],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [],
        "risks_or_uncertainties": [],
        "items_to_monitor": [],
        "limitations": [],
    }
    db.save_company_summary("ANTM", start, end, summary, "model")
    client = TestClient(app)
    response = client.post(
        "/api/share/render",
        json={
            "start": "2026-07-06T00:00",
            "end": "2026-08-06T23:59",
            "format": "txt",
            "sections": ["overview", "corporate_actions"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["company_count"] == 1
    assert body["llm_calls"] == 0
    assert "ANTM overview" in body["text"]
    assert "Corporate action signal" in body["text"]


def _seed_share_windows(tmp_path):
    from idx_digest.db import Database

    def summary(ticker: str, marker: str) -> dict:
        return {
            "ticker": ticker,
            "announcement_count": 1,
            "overview": f"{ticker} {marker} overview",
            "timeline": [],
            "material_changes": [],
            "key_financial_figures": [],
            "corporate_actions": [f"{marker} corporate action"],
            "expansion_projects": [],
            "management_or_control_changes": [],
            "capital_structure_events": [],
            "listing_or_regulatory_events": [],
            "analytical_scenarios": [],
            "risks_or_uncertainties": [],
            "items_to_monitor": [],
            "limitations": [],
        }

    db = Database(tmp_path / "data" / "idx_digest.sqlite3")
    db.save_company_summary(
        "ANTM", "2026-07-06T00:00:00+07:00", "2026-08-06T23:59:59.999999+07:00",
        summary("ANTM", "July"), "model",
    )
    db.save_company_summary(
        "ANTM", "2026-08-10T00:00:00+07:00", "2026-08-15T19:59:00+07:00",
        summary("ANTM", "August"), "model",
    )
    db.save_company_summary(
        "BIRD", "2026-08-10T00:00:00+07:00", "2026-08-15T19:59:00+07:00",
        summary("BIRD", "August"), "model",
    )
    return db


def test_share_windows_endpoint_lists_pickable_saved_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_share_windows(tmp_path)
    body = TestClient(app).get("/api/share/windows").json()

    assert body["window_count"] == 2
    assert [window["label"] for window in body["windows"]] == [
        "2026-07-06 → 2026-08-06",
        "2026-08-10 → 2026-08-15 19:59",
    ]
    assert body["windows"][0]["company_count"] == 1
    assert body["windows"][1]["company_count"] == 2
    assert body["span"] == {
        "start_at": "2026-07-06T00:00:00+07:00",
        "end_at": "2026-08-15T19:59:00+07:00",
        "start_date": "2026-07-06",
        "end_date": "2026-08-15",
    }


def test_share_render_supports_picked_range_and_all_dates(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_share_windows(tmp_path)
    client = TestClient(app)
    payload = {"format": "txt", "sections": ["overview", "corporate_actions"]}

    picked = client.post(
        "/api/share/render",
        json={**payload, "date_mode": "range", "start": "2026-08-12", "end": "2026-08-20"},
    ).json()
    assert picked["company_count"] == 2
    assert picked["date_mode"] == "range" and picked["window_count"] == 1
    assert "August corporate action" in picked["text"]
    assert "July corporate action" not in picked["text"]

    everything = client.post("/api/share/render", json={**payload, "date_mode": "all"}).json()
    assert everything["company_count"] == 2
    assert everything["date_mode"] == "all"
    assert everything["digest_count"] == 2
    # Both companies resolve to the August window, so July contributes nothing.
    assert everything["window_count"] == 1
    assert "July corporate action" not in everything["text"]

    every_window = client.post(
        "/api/share/render", json={**payload, "date_mode": "all", "per_ticker": "all"}
    ).json()
    assert every_window["digest_count"] == 3
    assert every_window["window_count"] == 2
    assert "July corporate action" in every_window["text"]


def test_share_render_exports_exactly_the_chosen_saved_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_share_windows(tmp_path)
    client = TestClient(app)
    payload = {"format": "txt", "sections": ["overview", "corporate_actions"]}
    july = ["2026-07-06T00:00:00+07:00", "2026-08-06T23:59:59.999999+07:00"]

    # The two windows overlap in the calendar, so no date range can isolate July —
    # naming the window key can.
    only_july = client.post(
        "/api/share/render", json={**payload, "date_mode": "range", "window_keys": [july]}
    ).json()
    assert only_july["window_count"] == 1
    assert only_july["company_count"] == 1
    assert "July corporate action" in only_july["text"]
    assert "August corporate action" not in only_july["text"]
    assert only_july["covered_window"] == "2026-07-06 → 2026-08-06"

    empty = client.post(
        "/api/share/render", json={**payload, "date_mode": "range", "window_keys": []}
    )
    assert empty.status_code == 422

    unknown = client.post(
        "/api/share/render",
        json={**payload, "date_mode": "range", "window_keys": [["2020-01-01T00:00:00+07:00", "2020-01-02T00:00:00+07:00"]]},
    )
    assert unknown.status_code == 404


def test_range_export_states_the_period_of_every_digest(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_share_windows(tmp_path)
    body = TestClient(app).post(
        "/api/share/render",
        json={
            "format": "md",
            "sections": ["overview"],
            "date_mode": "range",
            "start": "2026-08-01",
            "end": "2026-08-31",
            "per_ticker": "all",
        },
    ).json()

    # A picked range that catches the July window must not be labelled as August only.
    assert "**Covered:** 2026-07-06 → 2026-08-15 19:59" in body["text"]
    assert "**Dates picked:** 2026-08-01 → 2026-08-31" in body["text"]
    assert body["text"].count("**Window:** 2026-07-06 → 2026-08-06") == 1
    assert body["text"].count("**Window:** 2026-08-10 → 2026-08-15 19:59") == 2


def test_share_range_rejects_empty_and_inverted_selections(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_share_windows(tmp_path)
    client = TestClient(app)
    payload = {"format": "txt", "sections": ["overview"]}

    missing = client.post("/api/share/render", json={**payload, "date_mode": "range", "start": "2026-09-01", "end": "2026-09-30"})
    assert missing.status_code == 404
    assert "All saved dates" in missing.json()["detail"]

    inverted = client.post("/api/share/render", json={**payload, "date_mode": "range", "start": "2026-08-20", "end": "2026-08-12"})
    assert inverted.status_code == 422

    blank = client.post("/api/share/render", json={**payload, "date_mode": "range", "start": "", "end": ""})
    assert blank.status_code == 422


def test_share_modal_exposes_date_pickers() -> None:
    html = TestClient(app).get("/").text
    assert 'id="shareDatesScope"' in html and "Current scope" in html
    assert 'id="shareDatesRange"' in html and "Pick dates" in html
    assert 'id="shareDatesAll"' in html and "All saved dates" in html
    assert 'id="shareStartDate" type="date"' in html
    assert 'id="shareEndDate" type="date"' in html
    # The saved-window list must not be hidden behind a mode, and each entry must be
    # individually clickable — a calendar range alone cannot isolate overlapping windows.
    assert '<div class="share-windows" id="shareWindows">' in html
    assert "Saved windows · click to include or exclude" in html
    assert 'role="checkbox"' in html and "toggleShareWindow" in html
    assert "window_keys:chosen.map" in html
    assert "onShareRangeChanged" in html
    assert 'input[type="date"]' in html
    assert "[hidden]{display:none!important}" in html
    assert any(getattr(route, "path", None) == "/api/share/windows" for route in app.routes)


def test_gui_responsive_containment_contract() -> None:
    client = TestClient(app)
    html = client.get("/").text
    assert "v0.14.1 responsive containment" in html
    assert "Long-document chunks" in html
    assert "documentChunks: new Map()" in html
    assert "chunkAwareRemaining" in html
    assert "llmRequests: new Map()" in html
    assert "requestLatencyStats" in html
    assert "html,body{width:100%;max-width:100%;overflow-x:hidden}" in html
    assert ".desk-grid{grid-template-columns:330px minmax(0,1fr)}" in html
    assert ".activity-grid{grid-template-columns:300px minmax(0,1fr)}" in html
    assert ".activity-event{grid-template-columns:72px 90px minmax(0,1fr)}" in html
    assert "overflow-wrap:anywhere" in html
    assert ".ledger-wrap" in html
    assert "@media(max-width:520px)" in html
    assert ".modal,.share-modal,.profile-modal,.audit-modal{width:100%;max-width:100%}" in html
