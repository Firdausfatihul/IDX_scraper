from __future__ import annotations

import threading
import time
from collections import defaultdict

from idx_digest.extraction_scheduler import BoundedExtractionScheduler, ExtractionJob


def test_extraction_scheduler_is_bounded_and_ticker_fair():
    lock = threading.Lock()
    active = 0
    max_active = 0
    active_by_ticker = defaultdict(int)
    max_by_ticker = defaultdict(int)
    starts: list[str] = []

    def work(ticker: str) -> str:
        nonlocal active, max_active
        with lock:
            active += 1
            active_by_ticker[ticker] += 1
            max_active = max(max_active, active)
            max_by_ticker[ticker] = max(max_by_ticker[ticker], active_by_ticker[ticker])
            starts.append(ticker)
        time.sleep(0.035)
        with lock:
            active -= 1
            active_by_ticker[ticker] -= 1
        return ticker

    scheduler = BoundedExtractionScheduler(max_workers=3, max_inflight=6, max_per_ticker=2)
    try:
        # Submit an attachment-heavy ticker first, then peers. Backpressure keeps the
        # producer bounded while round-robin dispatch prevents >2 active from AAAA.
        for i in range(4):
            scheduler.submit(ExtractionJob(job_id=f"A-{i}", ticker="AAAA", announcement_id="a", func=lambda: work("AAAA")))
        for ticker in ("BBBB", "CCCC"):
            for i in range(2):
                scheduler.submit(ExtractionJob(job_id=f"{ticker}-{i}", ticker=ticker, announcement_id=ticker, func=lambda t=ticker: work(t)))
        metrics = scheduler.wait()
    finally:
        scheduler.close()

    assert max_active == 3
    assert max_by_ticker["AAAA"] <= 2
    assert metrics["completed"] == 8
    assert metrics["max_observed_inflight"] <= 6
    assert "BBBB" in starts[:6]
    assert "CCCC" in starts
