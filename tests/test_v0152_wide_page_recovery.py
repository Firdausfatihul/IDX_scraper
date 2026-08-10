from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from idx_digest.config import Settings
from idx_digest.idx_client import IDXClient


def _item(aid: str, ticker: str = "TEST", stamp: str = "2026-08-06T10:00:00") -> dict:
    return {
        "pengumuman": {
            "Id2": aid,
            "Kode_Emiten": ticker,
            "TglPengumuman": stamp,
            "JudulPengumuman": "x",
        },
        "attachments": [],
    }


def test_busy_day_uses_one_wide_page_before_ticker_fanout(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        idx_page_size=50,
        idx_wide_page_probe_size=200,
        stock_master_enabled=True,
    )
    client = IDXClient(settings)
    calls: list[dict] = []
    master_called = False

    def stock_master():
        nonlocal master_called
        master_called = True
        return frozenset({"ANTM", "BBCA", "TLKM"})

    monkeypatch.setattr(client, "stock_master_tickers", stock_master)

    def fake(params):
        calls.append(dict(params))
        date_from, date_to = params["dateFrom"], params["dateTo"]
        offset, page_size = params["indexFrom"], params["pageSize"]

        # Primary two-day query reproduces the real IDX symptom: 100 rows are
        # reachable through offset paging although ResultCount is much larger.
        if date_from != date_to:
            if offset == 0:
                return {"ResultCount": 4374, "Replies": [_item(f"primary-{i}") for i in range(50)]}
            if offset == 50:
                return {"ResultCount": 4374, "Replies": [_item(f"primary-b-{i}") for i in range(50)]}
            return {"ResultCount": 4374, "Replies": []}

        # Busy day: normal 50-row pagination truncates, but a larger first page
        # returns the complete 183-row shard in one call.
        if date_from == "20260806":
            if page_size >= 183 and offset == 0:
                return {"ResultCount": 183, "Replies": [_item(f"busy-{i}") for i in range(183)]}
            if offset == 0:
                return {"ResultCount": 183, "Replies": [_item(f"busy-first-{i}") for i in range(50)]}
            return {"ResultCount": 183, "Replies": []}

        return {"ResultCount": 1, "Replies": [_item("quiet-day", stamp="2026-08-07T12:00:00")]}

    monkeypatch.setattr(client, "_get_json", fake)
    tz = ZoneInfo("Asia/Jakarta")
    items, diag = client.collect_announcements(
        datetime(2026, 8, 6, tzinfo=tz),
        datetime(2026, 8, 7, 23, 59, tzinfo=tz),
    )
    client.close()

    assert diag["complete"] is True
    assert diag["strategy"] == "date-shards"
    assert len(items) == 184
    assert master_called is False
    busy_wide_calls = [
        c for c in calls
        if c["dateFrom"] == "20260806" and c["dateTo"] == "20260806" and c["pageSize"] == 200
    ]
    assert len(busy_wide_calls) == 1
    assert busy_wide_calls[0]["indexFrom"] == 0
    assert not any(c["kodeEmiten"] for c in calls)


def test_small_primary_range_can_recover_without_date_shards(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        idx_page_size=50,
        idx_wide_page_probe_size=200,
        stock_master_enabled=False,
    )
    client = IDXClient(settings)
    calls: list[dict] = []

    def fake(params):
        calls.append(dict(params))
        offset, page_size = params["indexFrom"], params["pageSize"]
        if page_size == 200 and offset == 0:
            return {"ResultCount": 120, "Replies": [_item(f"wide-{i}") for i in range(120)]}
        if offset == 0:
            return {"ResultCount": 120, "Replies": [_item(f"first-{i}") for i in range(50)]}
        return {"ResultCount": 120, "Replies": []}

    monkeypatch.setattr(client, "_get_json", fake)
    tz = ZoneInfo("Asia/Jakarta")
    items, diag = client.collect_announcements(
        datetime(2026, 8, 6, tzinfo=tz),
        datetime(2026, 8, 6, 23, 59, tzinfo=tz),
    )
    client.close()

    assert diag["complete"] is True
    assert diag["strategy"] == "wide-page-probe"
    assert len(items) == 120
    assert [c["pageSize"] for c in calls] == [50, 50, 200]


def test_wide_probe_skips_pointless_request_when_reported_total_exceeds_capacity(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        idx_page_size=50,
        idx_wide_page_probe_size=200,
        idx_wide_page_probe_max_size=200,
        stock_master_enabled=True,
        idx_ticker_shard_delay_seconds=0,
        idx_ticker_shard_jitter_seconds=0,
        idx_ticker_shard_burst_cooldown_seconds=0,
    )
    client = IDXClient(settings)
    calls: list[dict] = []
    monkeypatch.setattr(client, "stock_master_tickers", lambda: frozenset({"ANTM"}))

    def fake(params):
        calls.append(dict(params))
        ticker = params["kodeEmiten"]
        offset = params["indexFrom"]
        if ticker == "ANTM":
            return {"ResultCount": 1, "Replies": [_item("ANTM-only", "ANTM")]}
        if offset == 0:
            return {"ResultCount": 250, "Replies": [_item(f"first-{i}") for i in range(50)]}
        return {"ResultCount": 250, "Replies": []}

    monkeypatch.setattr(client, "_get_json", fake)
    tz = ZoneInfo("Asia/Jakarta")
    items, diag = client.collect_announcements(
        datetime(2026, 8, 6, tzinfo=tz),
        datetime(2026, 8, 6, 23, 59, tzinfo=tz),
    )
    client.close()

    assert diag["complete"] is True
    assert {x["pengumuman"]["Id2"] for x in items} == {"ANTM-only"}
    # No 200-row probe is wasted because 250 cannot fit in that probe.
    assert not any(c["pageSize"] == 200 for c in calls)
    assert any(c["kodeEmiten"] == "ANTM" for c in calls)


def test_adaptive_wide_probe_recovers_321_row_day_without_ticker_fanout(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        idx_page_size=50,
        idx_wide_page_probe_size=200,
        idx_wide_page_probe_max_size=1000,
        stock_master_enabled=True,
    )
    client = IDXClient(settings)
    calls: list[dict] = []
    master_called = False

    def stock_master():
        nonlocal master_called
        master_called = True
        return frozenset({"ANTM"})

    monkeypatch.setattr(client, "stock_master_tickers", stock_master)

    def fake(params):
        calls.append(dict(params))
        offset, page_size = params["indexFrom"], params["pageSize"]
        if page_size >= 321 and offset == 0:
            return {"ResultCount": 321, "Replies": [_item(f"wide-{i}") for i in range(321)]}
        if offset == 0:
            return {"ResultCount": 321, "Replies": [_item(f"first-{i}") for i in range(50)]}
        return {"ResultCount": 321, "Replies": []}

    monkeypatch.setattr(client, "_get_json", fake)
    tz = ZoneInfo("Asia/Jakarta")
    items, diag = client.collect_announcements(
        datetime(2026, 8, 7, tzinfo=tz),
        datetime(2026, 8, 7, 23, 59, tzinfo=tz),
    )
    client.close()

    assert diag["complete"] is True
    assert diag["strategy"] == "wide-page-probe"
    assert len(items) == 321
    assert master_called is False
    assert any(c["pageSize"] == 321 and c["indexFrom"] == 0 for c in calls)
