from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from idx_digest.browser_transport import IDXBrowserTransport
from idx_digest.config import Settings
from idx_digest.idx_client import IDXClient


class _FakePage:
    url = "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi"

    def __init__(self) -> None:
        self.waits: list[int] = []
        self.front_calls = 0

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)

    def bring_to_front(self) -> None:
        self.front_calls += 1


def _item(aid: str, ticker: str = "ANTM") -> dict:
    return {
        "pengumuman": {
            "Id2": aid,
            "Kode_Emiten": ticker,
            "TglPengumuman": "2026-08-07T10:00:00",
            "JudulPengumuman": "x",
        },
        "attachments": [],
    }


def test_browser_429_uses_cooldown_not_two_second_verification_loop(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_browser_verification_timeout_seconds=5,
        idx_429_cooldown_initial_seconds=1,
        idx_429_cooldown_max_seconds=5,
        idx_429_jitter_seconds=0,
    )
    transport = IDXBrowserTransport(settings)
    page = _FakePage()
    transport.page = page
    responses = iter(
        [
            {"status": 429, "contentType": "text/html", "retryAfter": "", "body": "rate limited"},
            {"status": 200, "contentType": "application/json", "retryAfter": "", "body": '{"ResultCount": 0, "Replies": []}'},
        ]
    )
    monkeypatch.setattr(transport, "_fetch_once", lambda *a, **k: next(responses))
    payload = transport.get_json({"indexFrom": 0, "pageSize": 50})
    assert payload["Replies"] == []
    assert page.waits == [1000]
    # A 429 is rate limiting, not an interactive verification challenge.
    assert page.front_calls == 0


def test_retry_after_header_wins_over_default_backoff(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_429_cooldown_initial_seconds=12,
        idx_429_cooldown_max_seconds=90,
        idx_429_jitter_seconds=0,
    )
    transport = IDXBrowserTransport(settings)
    assert transport._rate_limit_cooldown_seconds(attempt=1, retry_after="45", remaining_seconds=120) == 45
    assert transport._rate_limit_cooldown_seconds(attempt=2, retry_after=None, remaining_seconds=120) == 24


def test_ticker_shard_pacer_adds_interval_and_burst_rest(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_ticker_shard_delay_seconds=1.0,
        idx_ticker_shard_jitter_seconds=0,
        idx_ticker_shard_burst_size=25,
        idx_ticker_shard_burst_cooldown_seconds=12,
    )
    client = IDXClient(settings)
    waits: list[float] = []
    monkeypatch.setattr("idx_digest.idx_client.time.sleep", lambda seconds: waits.append(seconds))
    client._last_ticker_shard_request_at = time.monotonic()
    client._pace_ticker_shard(ticker="ANTM", day="2026-08-07")
    assert waits and waits[-1] > 0.9
    client._ticker_shard_requests = 25
    client._last_ticker_shard_request_at = time.monotonic()
    client._pace_ticker_shard(ticker="BBCA", day="2026-08-07")
    assert waits[-1] >= 12
    client.close()


def test_incomplete_daily_shard_uses_paced_ticker_fallback(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        idx_request_delay_seconds=0,
        idx_page_size=50,
        stock_master_enabled=True,
    )
    client = IDXClient(settings)
    paced: list[str] = []
    monkeypatch.setattr(client, "stock_master_tickers", lambda: frozenset({"ANTM", "BBCA", "TLKM"}))
    monkeypatch.setattr(client, "_pace_ticker_shard", lambda *, ticker, day: paced.append(ticker))

    def fake(params):
        date_from, date_to = params["dateFrom"], params["dateTo"]
        ticker = params["kodeEmiten"]
        offset = params["indexFrom"]
        if ticker:
            return {"ResultCount": 1, "Replies": [_item(f"{ticker}-{date_from}", ticker)]}
        if date_from != date_to:
            if offset == 0:
                return {"ResultCount": 4374, "Replies": [_item(f"p{i}") for i in range(50)]}
            if offset == 50:
                return {"ResultCount": 4374, "Replies": [_item(f"q{i}") for i in range(50)]}
            return {"ResultCount": 4374, "Replies": []}
        if date_from.endswith("06"):
            if offset == 0:
                return {"ResultCount": 200, "Replies": [_item(f"d{i}") for i in range(50)]}
            if offset == 50:
                return {"ResultCount": 200, "Replies": [_item(f"e{i}") for i in range(50)]}
            return {"ResultCount": 200, "Replies": []}
        return {"ResultCount": 1, "Replies": [_item(f"day-{date_from}")]}

    monkeypatch.setattr(client, "_get_json", fake)
    tz = ZoneInfo("Asia/Jakarta")
    items, diag = client.collect_announcements(
        datetime(2026, 8, 6, tzinfo=tz), datetime(2026, 8, 7, 23, 59, tzinfo=tz)
    )
    client.close()
    assert diag["complete"] is True
    assert sorted(paced) == ["ANTM", "BBCA", "TLKM"]
    ids = {x["pengumuman"]["Id2"] for x in items}
    assert {"ANTM-20260806", "BBCA-20260806", "TLKM-20260806", "day-20260807"}.issubset(ids)
