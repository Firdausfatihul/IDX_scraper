from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from idx_digest.config import Settings
from idx_digest.db import Database
from idx_digest.incremental import CoverageRange, normalize_coverage_ranges, subtract_coverage


TZ = ZoneInfo("Asia/Jakarta")


def _dt(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=TZ)


def _coverage(start: datetime, end: datetime) -> CoverageRange:
    return CoverageRange(start, end)


def _serialized(*ranges: CoverageRange) -> list[dict[str, str]]:
    return [item.as_dict() for item in ranges]


def _seed_coverage(db: Database, *ranges: CoverageRange, scope: str = "stocks:ALL") -> None:
    for item in ranges:
        db.save_scrape_coverage(
            scope,
            covered_start=item.start.isoformat(),
            covered_end=item.end.isoformat(),
            baseline_source="test",
        )


def _item(announcement_id: str, announced_at: str) -> dict:
    return {
        "pengumuman": {
            "Id2": announcement_id,
            "Kode_Emiten": "ANTM",
            "TglPengumuman": announced_at,
            "JudulPengumuman": "Coverage regression",
            "NoPengumuman": announcement_id,
            "JenisPengumuman": "General",
            "PerihalPengumuman": "General",
        },
        "attachments": [],
    }


def _run_incremental(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start: datetime,
    end: datetime,
    completeness: list[bool] | None = None,
    batches: list[list[dict]] | None = None,
    ticker: str | None = None,
    instrument_scope: str = "stocks",
    poll_snapshot: datetime | None = None,
) -> tuple[dict, list[tuple[datetime, datetime, dict]], Settings]:
    """Run the metadata-only path with no network, downloads, or LLM work."""

    import idx_digest.pipeline as pipeline_module
    from idx_digest.pipeline import Pipeline

    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        idx_incremental_overlap_days=0,
        stock_master_enabled=False,
    )
    calls: list[tuple[datetime, datetime, dict]] = []
    outcomes = list(completeness or [])
    collected_batches = list(batches or [])

    class IDX:
        def __init__(self, *args, **kwargs):
            self.browser = None

        def collect_announcements(self, start_at, end_at, **kwargs):
            index = len(calls)
            calls.append((start_at, end_at, dict(kwargs)))
            complete = outcomes[index] if index < len(outcomes) else True
            collected = collected_batches[index] if index < len(collected_batches) else []
            return collected, {
                "complete": complete,
                "strategy": "offline-test",
                "collected": len(collected),
                "reported_total": len(collected),
            }

        def stock_master_tickers(self):
            return None

        def browser_transport(self):
            raise AssertionError("the offline downloader must not request a browser")

        def close(self):
            pass

    class Downloader:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(pipeline_module, "IDXClient", IDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", Downloader)
    monkeypatch.setattr(
        Pipeline,
        "_metadata_poll_snapshot",
        lambda self: poll_snapshot or datetime(2026, 8, 31, 23, 59, tzinfo=TZ),
    )

    pipeline = Pipeline(settings, skip_llm=True)
    monkeypatch.setattr(pipeline, "_refresh_share_exports", lambda *args, **kwargs: {})
    monkeypatch.setattr(pipeline, "_export_company_checkpoint", lambda *args, **kwargs: None)
    try:
        report = pipeline.run(
            start_at=start,
            end_at=end,
            ticker=ticker,
            instrument_scope=instrument_scope,
            metadata_mode="incremental",
        )
    finally:
        pipeline.close()
    return report, calls, settings


def test_normalize_coverage_ranges_sorts_and_merges_only_touching_blocks() -> None:
    ranges = [
        _coverage(_dt(6), _dt(8)),
        _coverage(_dt(1), _dt(3)),
        _coverage(_dt(5), _dt(6)),
        _coverage(_dt(3), _dt(5)),
        _coverage(_dt(10), _dt(11)),
    ]

    assert normalize_coverage_ranges(ranges) == [
        _coverage(_dt(1), _dt(8)),
        _coverage(_dt(10), _dt(11)),
    ]
    with pytest.raises(ValueError, match="coverage range end"):
        CoverageRange(_dt(2), _dt(1))


def test_subtract_coverage_returns_each_uncovered_gap_and_clips_to_request() -> None:
    requested = _coverage(_dt(2), _dt(10))
    covered = [
        _coverage(_dt(1), _dt(3)),
        _coverage(_dt(7), _dt(8)),
        _coverage(_dt(5), _dt(6)),
        _coverage(_dt(12), _dt(13)),
    ]

    assert subtract_coverage(requested, covered) == [
        _coverage(_dt(3), _dt(5)),
        _coverage(_dt(6), _dt(7)),
        _coverage(_dt(8), _dt(10)),
    ]
    assert subtract_coverage(requested, [_coverage(_dt(1), _dt(11))]) == []
    assert subtract_coverage(requested, []) == [requested]


def test_coverage_table_migrates_additively_merges_and_isolates_scopes(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE scrape_watermarks (
                scope_key TEXT PRIMARY KEY,
                last_successful_poll_end TEXT NOT NULL,
                last_seen_announcement_at TEXT,
                baseline_source TEXT NOT NULL DEFAULT 'runtime',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO scrape_watermarks VALUES (?,?,?,?,?)",
            (
                "stocks:ALL",
                _dt(9, 23, 59).isoformat(),
                _dt(9, 20).isoformat(),
                "legacy",
                _dt(9, 23, 59).isoformat(),
            ),
        )

    db = Database(path)
    assert db.scrape_watermark("stocks:ALL")["baseline_source"] == "legacy"
    assert db.scrape_coverage("stocks:ALL") == []

    _seed_coverage(db, _coverage(_dt(5), _dt(7)))
    _seed_coverage(db, _coverage(_dt(1), _dt(3)))
    _seed_coverage(db, _coverage(_dt(3), _dt(5)))
    _seed_coverage(
        db,
        _coverage(_dt(8), _dt(9)),
        scope="stocks:ANTM",
    )

    all_scope = db.scrape_coverage("stocks:ALL")
    assert [(row["covered_start"], row["covered_end"]) for row in all_scope] == [
        (_dt(1).isoformat(), _dt(7).isoformat()),
    ]
    antm_scope = db.scrape_coverage("stocks:ANTM")
    assert [(row["covered_start"], row["covered_end"]) for row in antm_scope] == [
        (_dt(8).isoformat(), _dt(9).isoformat()),
    ]
    assert db.scrape_coverage("stocks:BBCA") == []


def test_pipeline_backward_extension_queries_only_the_missing_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    _seed_coverage(Database(settings.database_path), _coverage(_dt(7), _dt(9, 23, 59)))

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(6),
        end=_dt(9, 23, 59),
    )

    assert [(start, end) for start, end, _ in calls] == [(_dt(6), _dt(7))]
    assert report["metadata_missing_ranges"] == _serialized(_coverage(_dt(6), _dt(7)))
    assert report["metadata_coverage_after"] == _serialized(
        _coverage(_dt(6), _dt(9, 23, 59)),
    )
    assert report["metadata_noop"] is False


def test_backward_gap_rows_are_accepted_while_covered_boundary_rows_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    _seed_coverage(Database(settings.database_path), _coverage(_dt(7), _dt(9, 23, 59)))

    report, _calls, final_settings = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(6),
        end=_dt(9, 23, 59),
        batches=[[
            _item("backward-missing", "2026-08-06T12:00:00"),
            _item("covered-boundary", "2026-08-07T12:00:00"),
        ]],
    )

    db = Database(final_settings.database_path)
    assert db.announcement_exists("backward-missing") is True
    assert db.announcement_exists("covered-boundary") is False
    assert report["metadata_trusted_history_skipped"] == 1
    assert report["metadata_announcements_scheduled"] == 1


def test_pipeline_forward_extension_queries_only_the_missing_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    _seed_coverage(Database(settings.database_path), _coverage(_dt(7), _dt(9, 23, 59)))

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(7),
        end=_dt(10, 23, 59),
    )

    assert [(start, end) for start, end, _ in calls] == [
        (_dt(9, 23, 59), _dt(10, 23, 59)),
    ]
    assert report["metadata_missing_ranges"] == _serialized(
        _coverage(_dt(9, 23, 59), _dt(10, 23, 59)),
    )
    assert report["metadata_coverage_after"] == _serialized(
        _coverage(_dt(7), _dt(10, 23, 59)),
    )


def test_pipeline_extension_at_both_ends_makes_two_gap_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    _seed_coverage(Database(settings.database_path), _coverage(_dt(7), _dt(9, 23, 59)))

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(6),
        end=_dt(10, 23, 59),
    )

    expected = [
        _coverage(_dt(6), _dt(7)),
        _coverage(_dt(9, 23, 59), _dt(10, 23, 59)),
    ]
    assert [(start, end) for start, end, _ in calls] == [
        (item.start, item.end) for item in expected
    ]
    assert report["metadata_missing_ranges"] == _serialized(*expected)
    assert report["metadata_coverage_after"] == _serialized(
        _coverage(_dt(6), _dt(10, 23, 59)),
    )


def test_pipeline_queries_an_internal_gap_between_two_coverage_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    db = Database(settings.database_path)
    _seed_coverage(
        db,
        _coverage(_dt(1), _dt(3, 23, 59)),
        _coverage(_dt(5), _dt(9, 23, 59)),
    )

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(1),
        end=_dt(9, 23, 59),
    )

    gap = _coverage(_dt(3, 23, 59), _dt(5))
    assert [(start, end) for start, end, _ in calls] == [(gap.start, gap.end)]
    assert report["metadata_missing_ranges"] == _serialized(gap)
    assert report["metadata_coverage_after"] == _serialized(
        _coverage(_dt(1), _dt(9, 23, 59)),
    )


def test_pipeline_is_a_fast_noop_only_when_the_full_request_is_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    _seed_coverage(Database(settings.database_path), _coverage(_dt(7), _dt(9, 23, 59)))

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(8),
        end=_dt(9, 12),
    )

    assert calls == []
    assert report["status"] == "completed"
    assert report["metadata_noop"] is True
    assert report["metadata_missing_ranges"] == []
    assert report["metadata_query_ranges"] == []
    assert report["metadata_announcements_scheduled"] == 0


def test_legacy_bare_high_watermark_does_not_prove_any_start_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    db = Database(settings.database_path)
    db.save_scrape_watermark(
        "stocks:ALL",
        last_successful_poll_end=_dt(9, 23, 59).isoformat(),
        last_seen_announcement_at=_dt(9, 20).isoformat(),
        baseline_source="legacy",
    )

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(6),
        end=_dt(9, 23, 59),
    )

    assert [(start, end) for start, end, _ in calls] == [
        (_dt(6), _dt(9, 23, 59)),
    ]
    assert report["metadata_noop"] is False
    assert report["metadata_coverage_before"] == []
    assert report["metadata_diagnostics"]["baseline_source"] == "legacy-watermark-without-start"
    assert report["metadata_coverage_after"] == _serialized(
        _coverage(_dt(6), _dt(9, 23, 59)),
    )


def test_report_migration_recovers_real_poll_but_excludes_legacy_false_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    false_noop = {
        "status": "completed",
        "scrape_complete": True,
        "start_at": _dt(1).isoformat(),
        "end_at": _dt(9, 23, 59).isoformat(),
        "ticker_filter": None,
        "metadata_mode": "incremental",
        "metadata_noop": True,
    }
    (settings.data_dir / "last_run.json").write_text(json.dumps(false_noop), encoding="utf-8")

    genuine_dir = settings.data_dir / "runs" / "genuine"
    genuine_dir.mkdir(parents=True)
    genuine_poll = {
        "status": "completed",
        "scrape_complete": True,
        "start_at": _dt(7).isoformat(),
        "end_at": _dt(9, 23, 59).isoformat(),
        "ticker_filter": None,
        "metadata_mode": "historical_audit",
        "metadata_noop": False,
    }
    (genuine_dir / "report.json").write_text(json.dumps(genuine_poll), encoding="utf-8")

    db = Database(settings.database_path)
    db.save_scrape_watermark(
        "stocks:ALL",
        last_successful_poll_end=_dt(9, 23, 59).isoformat(),
        last_seen_announcement_at=None,
        baseline_source="legacy",
    )

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(6),
        end=_dt(9, 23, 59),
    )

    # The real Aug 7-9 poll is reusable. The old false no-op must not claim
    # Aug 1-6, so the backward prefix still receives a metadata request.
    assert report["metadata_coverage_before"] == _serialized(
        _coverage(_dt(7), _dt(9, 23, 59)),
    )
    assert [(start, end) for start, end, _ in calls] == [(_dt(6), _dt(7))]
    assert report["metadata_noop"] is False


def test_partial_multi_gap_run_does_not_merge_any_unproven_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    original = _coverage(_dt(7), _dt(9, 23, 59))
    _seed_coverage(Database(settings.database_path), original)

    report, calls, final_settings = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(6),
        end=_dt(10, 23, 59),
        completeness=[True, False],
    )

    assert len(calls) == 2
    assert report["status"] == "partial"
    assert report["scrape_complete"] is False
    assert report["metadata_coverage_added"] == []
    assert report["metadata_coverage_after"] == _serialized(original)
    persisted = Database(final_settings.database_path).scrape_coverage("stocks:ALL")
    assert [(row["covered_start"], row["covered_end"]) for row in persisted] == [
        (original.start.isoformat(), original.end.isoformat()),
    ]


def test_coverage_stops_at_poll_snapshot_and_later_rerun_fetches_new_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_end = _dt(9, 23, 59)
    first_snapshot = _dt(9, 23, 2)
    first, first_calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(9),
        end=requested_end,
        poll_snapshot=first_snapshot,
    )

    assert [(start, end) for start, end, _ in first_calls] == [(_dt(9), first_snapshot)]
    assert first["metadata_coverage_after"] == _serialized(_coverage(_dt(9), first_snapshot))
    assert first["metadata_deferred_ranges"] == _serialized(
        _coverage(first_snapshot, requested_end),
    )

    second_snapshot = _dt(9, 23, 30)
    second, second_calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(9),
        end=requested_end,
        poll_snapshot=second_snapshot,
    )

    assert [(start, end) for start, end, _ in second_calls] == [
        (first_snapshot, second_snapshot),
    ]
    assert second["metadata_noop"] is False
    assert second["metadata_coverage_after"] == _serialized(
        _coverage(_dt(9), second_snapshot),
    )
    assert second["metadata_deferred_ranges"] == _serialized(
        _coverage(second_snapshot, requested_end),
    )


def test_scope_less_legacy_stock_report_cannot_seed_all_instrument_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    legacy_report = {
        "status": "completed",
        "scrape_complete": True,
        "start_at": _dt(7).isoformat(),
        "end_at": _dt(9, 23, 59).isoformat(),
        "ticker_filter": None,
        "metadata_mode": "historical_audit",
        "metadata_noop": False,
    }
    (settings.data_dir / "last_run.json").write_text(json.dumps(legacy_report), encoding="utf-8")

    report, calls, _ = _run_incremental(
        tmp_path,
        monkeypatch,
        start=_dt(7),
        end=_dt(9, 23, 59),
        instrument_scope="all",
    )

    assert report["metadata_coverage_before"] == []
    assert report["metadata_noop"] is False
    assert [(start, end) for start, end, _ in calls] == [
        (_dt(7), _dt(9, 23, 59)),
    ]
