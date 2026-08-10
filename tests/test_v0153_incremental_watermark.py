from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from idx_digest.config import Settings
from idx_digest.db import Database
from idx_digest.idx_client import IDXClient


def _item(aid: str, stamp: str = "2026-08-09T10:00:00", ticker: str = "ANTM") -> dict:
    return {
        "pengumuman": {
            "Id2": aid,
            "Kode_Emiten": ticker,
            "TglPengumuman": stamp,
            "JudulPengumuman": "Update",
            "NoPengumuman": aid,
            "JenisPengumuman": "General",
            "PerihalPengumuman": "General",
        },
        "attachments": [],
    }


def test_scrape_watermark_schema_migrates_and_round_trips(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    assert db.scrape_watermark("stocks:ALL") is None
    db.save_scrape_watermark(
        "stocks:ALL",
        last_successful_poll_end="2026-08-09T23:59:00+07:00",
        last_seen_announcement_at="2026-08-09T20:00:00+07:00",
        baseline_source="runtime",
    )
    row = db.scrape_watermark("stocks:ALL")
    assert row is not None
    assert row["last_successful_poll_end"] == "2026-08-09T23:59:00+07:00"
    assert row["last_seen_announcement_at"] == "2026-08-09T20:00:00+07:00"


def test_incremental_collection_can_refuse_stock_master_fanout(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        idx_page_size=50,
        idx_wide_page_probe_size=200,
        stock_master_enabled=True,
    )
    client = IDXClient(settings)
    master_called = False

    def master():
        nonlocal master_called
        master_called = True
        return frozenset({"ANTM", "BBCA"})

    monkeypatch.setattr(client, "stock_master_tickers", master)

    def fake(params):
        # One busy day that cannot be proven complete by normal pagination or a
        # wide first page. Incremental mode must return partial instead of
        # exploding into one request per listed ticker.
        if params["indexFrom"] == 0:
            return {"ResultCount": 250, "Replies": [_item(f"a{i}") for i in range(50)]}
        return {"ResultCount": 250, "Replies": []}

    monkeypatch.setattr(client, "_get_json", fake)
    tz = ZoneInfo("Asia/Jakarta")
    _items, diag = client.collect_announcements(
        datetime(2026, 8, 9, tzinfo=tz),
        datetime(2026, 8, 9, 23, 59, tzinfo=tz),
        allow_ticker_fallback=False,
    )
    client.close()
    assert diag["complete"] is False
    assert master_called is False
    assert diag["incomplete_shards"][0]["ticker_fallback_skipped"] is True


def test_existing_archive_without_proven_start_is_rechecked_once(tmp_path: Path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        stock_master_enabled=False,
        idx_incremental_overlap_days=1,
    )
    db = Database(settings.database_path)
    tz = ZoneInfo("Asia/Jakarta")
    existing = _item("old", "2026-08-08T20:00:00")
    db.upsert_announcement(existing, "2026-08-08T20:00:00+07:00")

    calls = []

    class IDX:
        def __init__(self, *a, **k): self.browser = None
        def collect_announcements(self, start_at, end_at, **kwargs):
            calls.append((start_at, end_at, kwargs))
            return [], {"complete": True, "strategy": "pagination", "collected": 0}
        def stock_master_tickers(self): return None
        def browser_transport(self): raise AssertionError
        def close(self): pass

    class Downloader:
        def __init__(self, *a, **k): pass
        def close(self): pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)

    p = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(p, "_refresh_share_exports", lambda *a, **k: {})
    monkeypatch.setattr(p, "_export_company_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(p, "_metadata_poll_snapshot", lambda: datetime(2026, 8, 10, tzinfo=tz))
    report = p.run(
        start_at=datetime(2026, 7, 6, tzinfo=tz),
        end_at=datetime(2026, 8, 9, 23, 59, tzinfo=tz),
        metadata_mode="incremental",
    )
    p.close()

    assert len(calls) == 1
    # An announcement timestamp proves that one row exists, not that every
    # earlier calendar day was completely polled. v0.15.5 conservatively checks
    # the requested interval once, then persists exact coverage.
    assert calls[0][0].date().isoformat() == "2026-07-06"
    assert calls[0][2]["allow_ticker_fallback"] is False
    assert report["metadata_effective_start"].startswith("2026-07-06T00:00:00")
    assert report["metadata_coverage_after"] == [{
        "start_at": "2026-07-06T00:00:00+07:00",
        "end_at": "2026-08-09T23:59:00+07:00",
    }]
    wm = Database(settings.database_path).scrape_watermark("stocks:ALL")
    assert wm is not None
    assert wm["last_successful_poll_end"].startswith("2026-08-09T23:59:00")


def test_historical_audit_keeps_full_requested_range_and_ticker_fallback(tmp_path: Path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "data", stock_master_enabled=False)
    calls = []

    class IDX:
        def __init__(self, *a, **k): self.browser = None
        def collect_announcements(self, start_at, end_at, **kwargs):
            calls.append((start_at, end_at, kwargs))
            return [], {"complete": True, "strategy": "pagination", "collected": 0}
        def stock_master_tickers(self): return None
        def browser_transport(self): raise AssertionError
        def close(self): pass

    class Downloader:
        def __init__(self, *a, **k): pass
        def close(self): pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)
    tz = ZoneInfo("Asia/Jakarta")
    start = datetime(2026, 7, 6, tzinfo=tz)
    end = datetime(2026, 8, 9, 23, 59, tzinfo=tz)
    p = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(p, "_refresh_share_exports", lambda *a, **k: {})
    monkeypatch.setattr(p, "_export_company_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(p, "_metadata_poll_snapshot", lambda: datetime(2026, 8, 10, tzinfo=tz))
    p.run(start_at=start, end_at=end, metadata_mode="historical_audit")
    p.close()
    assert calls[0][0] == start
    assert calls[0][2]["allow_ticker_fallback"] is True


def test_empty_archive_partial_run_does_not_create_a_trusted_watermark(tmp_path: Path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "data", stock_master_enabled=False)
    tz = ZoneInfo("Asia/Jakarta")
    start = datetime(2026, 8, 1, tzinfo=tz)
    end = datetime(2026, 8, 9, 23, 59, tzinfo=tz)

    class IDX:
        def __init__(self, *a, **k): self.browser = None
        def collect_announcements(self, start_at, end_at, **kwargs):
            # Deliberately partial. An empty profile must remain watermark-free
            # until a complete run actually proves the requested poll boundary.
            return [], {"complete": False, "strategy": "date-shards-partial", "collected": 0}
        def stock_master_tickers(self): return None
        def browser_transport(self): raise AssertionError
        def close(self): pass

    class Downloader:
        def __init__(self, *a, **k): pass
        def close(self): pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)
    p = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(p, "_refresh_share_exports", lambda *a, **k: {})
    monkeypatch.setattr(p, "_export_company_checkpoint", lambda *a, **k: None)
    report = p.run(start_at=start, end_at=end, metadata_mode="incremental")
    p.close()
    assert report["status"] == "partial"
    wm = Database(settings.database_path).scrape_watermark("stocks:ALL")
    assert wm is None


def test_successful_historical_audit_advances_incremental_watermark(tmp_path: Path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "data", stock_master_enabled=False)
    tz = ZoneInfo("Asia/Jakarta")
    start = datetime(2026, 8, 1, tzinfo=tz)
    end = datetime(2026, 8, 9, 23, 59, tzinfo=tz)
    db = Database(settings.database_path)
    db.save_scrape_watermark(
        "stocks:ALL",
        last_successful_poll_end="2026-08-08T23:59:00+07:00",
        last_seen_announcement_at=None,
        baseline_source="runtime",
    )

    class IDX:
        def __init__(self, *a, **k): self.browser = None
        def collect_announcements(self, start_at, end_at, **kwargs):
            return [], {"complete": True, "strategy": "pagination", "collected": 0}
        def stock_master_tickers(self): return None
        def browser_transport(self): raise AssertionError
        def close(self): pass

    class Downloader:
        def __init__(self, *a, **k): pass
        def close(self): pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)
    p = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(p, "_refresh_share_exports", lambda *a, **k: {})
    monkeypatch.setattr(p, "_export_company_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(p, "_metadata_poll_snapshot", lambda: datetime(2026, 8, 10, tzinfo=tz))
    p.run(start_at=start, end_at=end, metadata_mode="historical_audit")
    p.close()
    wm = Database(settings.database_path).scrape_watermark("stocks:ALL")
    assert wm is not None
    assert wm["last_successful_poll_end"] == end.isoformat()


def test_same_or_earlier_incremental_window_is_metadata_noop(tmp_path: Path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "data", stock_master_enabled=False)
    tz = ZoneInfo("Asia/Jakarta")
    start = datetime(2026, 8, 6, tzinfo=tz)
    end = datetime(2026, 8, 7, 23, 59, tzinfo=tz)
    db = Database(settings.database_path)
    db.save_scrape_watermark(
        "stocks:ALL",
        last_successful_poll_end=end.isoformat(),
        last_seen_announcement_at="2026-08-07T23:51:56+07:00",
        baseline_source="runtime",
    )
    db.save_scrape_coverage(
        "stocks:ALL",
        covered_start=start.isoformat(),
        covered_end=end.isoformat(),
        baseline_source="runtime",
    )
    collect_called = False

    class IDX:
        def __init__(self, *a, **k): self.browser = None
        def collect_announcements(self, *a, **k):
            nonlocal collect_called
            collect_called = True
            raise AssertionError("same completed window should not hit IDX metadata")
        def stock_master_tickers(self): return None
        def browser_transport(self): raise AssertionError
        def close(self): pass

    class Downloader:
        def __init__(self, *a, **k): pass
        def close(self): pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)
    p = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(p, "_refresh_share_exports", lambda *a, **k: {})
    monkeypatch.setattr(p, "_export_company_checkpoint", lambda *a, **k: None)
    report = p.run(start_at=start, end_at=end, metadata_mode="incremental")
    p.close()

    assert collect_called is False
    assert report["status"] == "completed"
    assert report["metadata_noop"] is True
    assert report["metadata_announcements_scheduled"] == 0
    wm = Database(settings.database_path).scrape_watermark("stocks:ALL")
    assert wm is not None
    assert wm["last_successful_poll_end"] == end.isoformat()


def test_incremental_overlap_never_backfills_rows_at_or_before_trusted_poll_end(tmp_path: Path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "data", stock_master_enabled=False, idx_incremental_overlap_days=1)
    tz = ZoneInfo("Asia/Jakarta")
    requested_start = datetime(2026, 8, 1, tzinfo=tz)
    watermark = datetime(2026, 8, 7, 23, 59, tzinfo=tz)
    requested_end = datetime(2026, 8, 8, 23, 59, tzinfo=tz)
    db = Database(settings.database_path)
    db.save_scrape_watermark(
        "stocks:ALL",
        last_successful_poll_end=watermark.isoformat(),
        last_seen_announcement_at="2026-08-07T23:50:00+07:00",
        baseline_source="runtime",
    )
    db.save_scrape_coverage(
        "stocks:ALL",
        covered_start=requested_start.isoformat(),
        covered_end=watermark.isoformat(),
        baseline_source="runtime",
    )
    old = _item("old-missing", "2026-08-07T12:00:00")
    new = _item("new", "2026-08-08T10:00:00")

    class IDX:
        def __init__(self, *a, **k): self.browser = None
        def collect_announcements(self, start_at, end_at, **kwargs):
            assert start_at < watermark  # calendar overlap is still queried
            return [old, new], {"complete": True, "strategy": "pagination", "reported_total": 2, "collected": 2}
        def stock_master_tickers(self): return None
        def browser_transport(self): raise AssertionError
        def close(self): pass

    class Downloader:
        def __init__(self, *a, **k): pass
        def close(self): pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)
    p = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(p, "_refresh_share_exports", lambda *a, **k: {})
    monkeypatch.setattr(p, "_export_company_checkpoint", lambda *a, **k: None)
    report = p.run(start_at=requested_start, end_at=requested_end, metadata_mode="incremental")
    p.close()

    db = Database(settings.database_path)
    assert db.announcement_exists("old-missing") is False
    assert db.announcement_exists("new") is True
    assert report["metadata_trusted_history_skipped"] == 1
    assert report["metadata_announcements_scheduled"] == 1


def test_completed_run_report_bootstraps_poll_end_not_last_announcement_time(tmp_path: Path, monkeypatch) -> None:
    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(data_dir=tmp_path / "data", stock_master_enabled=False)
    tz = ZoneInfo("Asia/Jakarta")
    db = Database(settings.database_path)
    db.upsert_announcement(_item("old", "2026-08-07T23:51:56"), "2026-08-07T23:51:56+07:00")
    run_dir = settings.data_dir / "runs" / "legacy-complete"
    run_dir.mkdir(parents=True, exist_ok=True)
    completed_end = datetime(2026, 8, 7, 23, 59, tzinfo=tz)
    (run_dir / "report.json").write_text(
        __import__("json").dumps({
            "status": "completed", "scrape_complete": True,
            "start_at": "2026-07-06T00:00:00+07:00",
            "end_at": completed_end.isoformat(), "ticker_filter": None,
        }),
        encoding="utf-8",
    )
    calls = 0

    class IDX:
        def __init__(self, *a, **k): self.browser = None
        def collect_announcements(self, *a, **k):
            nonlocal calls
            calls += 1
            return [], {"complete": True, "strategy": "pagination", "collected": 0}
        def stock_master_tickers(self): return None
        def browser_transport(self): raise AssertionError
        def close(self): pass

    class Downloader:
        def __init__(self, *a, **k): pass
        def close(self): pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)
    p = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(p, "_refresh_share_exports", lambda *a, **k: {})
    monkeypatch.setattr(p, "_export_company_checkpoint", lambda *a, **k: None)
    report = p.run(
        start_at=datetime(2026, 7, 6, tzinfo=tz),
        end_at=completed_end,
        metadata_mode="incremental",
    )
    p.close()

    assert calls == 0
    assert report["metadata_noop"] is True
    wm = Database(settings.database_path).scrape_watermark("stocks:ALL")
    assert wm is not None
    assert wm["last_successful_poll_end"] == completed_end.isoformat()
    assert wm["last_seen_announcement_at"] == "2026-08-07T23:51:56+07:00"
