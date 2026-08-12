from __future__ import annotations

import json

from typer.testing import CliRunner

from idx_digest.cli import app as cli_app
from idx_digest.config import Settings
from idx_digest.db import Database
from idx_digest.pipeline import Pipeline
from idx_digest.timeutils import parse_boundary


def _item() -> dict:
    return {
        "pengumuman": {
            "Id2": "20260806120000-RECOVERY_id-id",
            "Kode_Emiten": "TEST",
            "TglPengumuman": "2026-08-06T12:00:00",
            "JudulPengumuman": "Recovery checkpoint",
            "NoPengumuman": "REC-1",
            "JenisPengumuman": "Test",
            "PerihalPengumuman": "Durability",
        },
        "attachments": [],
    }


def test_pipeline_returns_partial_report_after_metadata_disconnect(tmp_path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module

    class FakeIDX:
        browser = None

        def __init__(self, settings, observer=None):
            pass

        def iter_announcements(self, *args, **kwargs):
            yield _item()
            raise ConnectionError("internet disconnected")

        def browser_transport(self):
            raise AssertionError("not used")

        def close(self):
            pass

    class FakeDownloader:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(pipeline_module, "IDXClient", FakeIDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", FakeDownloader)

    settings = Settings(data_dir=tmp_path / "data")
    pipeline = Pipeline(settings, skip_llm=True)
    report = pipeline.run(
        start_at=parse_boundary("2026-08-06", settings.app_timezone, is_end=False),
        end_at=parse_boundary("2026-08-06", settings.app_timezone, is_end=True),
    )

    assert report["status"] == "partial"
    assert report["scrape_complete"] is False
    assert report["processed_announcements"] == 1
    assert report["recovery"]["announcement_count"] == 1
    assert (settings.data_dir / "last_run.json").exists()


def test_recovery_export_preserves_partial_announcement_summary(tmp_path) -> None:
    db = Database(tmp_path / "idx.sqlite3")
    announcement_id, ticker = db.upsert_announcement(_item(), "2026-08-06T12:00:00+07:00")
    db.save_announcement_summary(
        announcement_id,
        ticker,
        {"executive_summary": "Saved before outage"},
        "test-model",
        "test-prompt",
    )

    destination = tmp_path / "recovery"
    snapshot = db.export_recovery(
        destination,
        "2026-08-06T00:00:00+07:00",
        "2026-08-06T23:59:59.999999+07:00",
    )

    assert snapshot["announcement_summary_count"] == 1
    recovery = json.loads((destination / "recovery.json").read_text(encoding="utf-8"))
    assert recovery["partial_announcement_summaries"]["TEST"][0]["summary"]["executive_summary"] == "Saved before outage"
    assert (destination / "TEST" / "announcement_summaries.jsonl").exists()


def test_cli_lists_recover_command() -> None:
    result = CliRunner().invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "recover" in result.stdout


def test_gui_reloads_running_state_as_interrupted(tmp_path, monkeypatch) -> None:
    from idx_digest.gui import RunManager, RunRecord

    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "data" / "runs" / "overnight"
    record = RunRecord(
        run_id="overnight",
        request={
            "start": "2026-08-06",
            "end": "2026-08-06",
            "ticker": None,
            "keyword": "",
            "skip_llm": False,
            "max_announcements": 100,
            "llm_concurrency": 2,
            "trace_browser": True,
            "browser_headless": False,
        },
        status="running",
        created_at="2026-08-06T19:00:00+07:00",
        started_at="2026-08-06T19:00:01+07:00",
        storage_dir=run_dir,
    )
    record.persist()

    manager = RunManager()
    loaded = manager.get("overnight")
    assert loaded.status == "interrupted"
    assert "stopped" in (loaded.failure or "")
    assert (run_dir / "state.json").exists()


def test_gui_recovery_preview_reads_existing_cache(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from idx_digest.gui import app

    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "data" / "idx_digest.sqlite3")
    announcement_id, ticker = db.upsert_announcement(_item(), "2026-08-06T12:00:00+07:00")
    db.save_announcement_summary(
        announcement_id,
        ticker,
        {"executive_summary": "Visible after restart"},
        "old-model",
        "old-prompt",
    )

    response = TestClient(app).post(
        "/api/recovery",
        json={
            "start": "2026-08-06",
            "end": "2026-08-06",
            "ticker": "TEST",
            "keyword": "",
            "skip_llm": False,
            "max_announcements": 100,
            "llm_concurrency": 2,
            "trace_browser": True,
            "browser_headless": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["recovery"]["announcement_summary_count"] == 1
    assert body["partial_summaries"]["TEST"][0]["summary"]["executive_summary"] == "Visible after restart"
