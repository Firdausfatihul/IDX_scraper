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
