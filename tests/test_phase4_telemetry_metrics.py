from __future__ import annotations

import time

from idx_digest.extraction_scheduler import BoundedExtractionScheduler, ExtractionJob
from idx_digest.llm_scheduler import GlobalLLMScheduler, ScheduledJob
from idx_digest.provider_gate import AdaptiveProviderGate


def test_llm_scheduler_records_peak_and_queue_wait():
    scheduler = GlobalLLMScheduler(max_workers=1, max_per_group=1)
    try:
        scheduler.submit(ScheduledJob(job_id="a", group_key="a", stage="document", ticker="AAA", func=lambda: time.sleep(0.04)))
        scheduler.submit(ScheduledJob(job_id="b", group_key="b", stage="document", ticker="BBB", func=lambda: None))
        metrics = scheduler.wait()
    finally:
        scheduler.close()
    assert metrics["max_observed_active"] == 1
    assert metrics["max_observed_pending"] >= 1
    assert metrics["queue_wait_samples"] == 2
    assert metrics["max_queue_wait_seconds"] > 0
    assert metrics["peak_utilization"] == 1.0


def test_extraction_scheduler_records_queue_and_backpressure_metrics():
    scheduler = BoundedExtractionScheduler(max_workers=1, max_inflight=2, max_per_ticker=1)
    try:
        scheduler.submit(ExtractionJob(job_id="a", ticker="AAA", announcement_id="1", func=lambda: time.sleep(0.04)))
        scheduler.submit(ExtractionJob(job_id="b", ticker="BBB", announcement_id="2", func=lambda: None))
        metrics = scheduler.wait()
    finally:
        scheduler.close()
    assert metrics["max_observed_active"] == 1
    assert metrics["max_observed_inflight"] >= 1
    assert metrics["queue_wait_samples"] == 2
    assert metrics["peak_utilization"] == 1.0


def test_provider_gate_records_latency_and_live_slot_events():
    events = []

    class Observer:
        def event(self, stage, message, **fields):
            events.append((stage, message, fields))

    gate = AdaptiveProviderGate(configured_max=3, enabled=True, observer=Observer())
    lease = gate.acquire()
    gate.record_success(lease, elapsed_seconds=1.5)
    metrics = gate.metrics
    assert metrics["success_count"] == 1
    assert metrics["average_latency_seconds"] == 1.5
    assert metrics["ewma_latency_seconds"] == 1.5
    assert any(message == "provider request slot acquired" for _, message, _ in events)
    assert any(message == "provider request slot released" for _, message, _ in events)
