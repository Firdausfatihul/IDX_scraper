from idx_digest.performance_advisor import build_performance_summary


def test_advisor_reduces_llm_slots_after_provider_throttle():
    summary = build_performance_summary(
        processed_announcements=20,
        total_elapsed_seconds=600,
        scheduler_metrics={"max_workers": 4, "max_observed_active": 4},
        extraction_metrics={"max_workers": 3, "max_inflight": 8, "max_observed_active": 2, "max_observed_inflight": 4},
        provider_metrics={
            "configured_max": 4, "current_limit": 2, "max_observed_active": 4,
            "throttle_events": 1, "success_count": 20, "failure_count": 1,
        },
    )
    assert summary["bottleneck"] == "provider"
    assert summary["recommendation"]["global_llm_slots"] == 2
    assert summary["recommendation"]["changed"] is True


def test_advisor_increases_extraction_workers_when_backlog_saturates():
    summary = build_performance_summary(
        processed_announcements=30,
        total_elapsed_seconds=600,
        scheduler_metrics={"max_workers": 4, "max_observed_active": 3},
        extraction_metrics={
            "max_workers": 3, "max_inflight": 8, "max_observed_active": 3,
            "max_observed_inflight": 8, "average_queue_wait_seconds": 1.2,
        },
        provider_metrics={"configured_max": 4, "current_limit": 4, "max_observed_active": 3, "success_count": 20},
    )
    assert summary["bottleneck"] == "extraction"
    assert summary["recommendation"]["extraction_workers"] == 4
    assert summary["recommendation"]["extraction_queue_size"] >= 12


def test_advisor_can_recommend_one_more_llm_slot_only_when_provider_is_healthy():
    summary = build_performance_summary(
        processed_announcements=40,
        total_elapsed_seconds=600,
        scheduler_metrics={"max_workers": 4, "max_observed_active": 4, "average_queue_wait_seconds": 5},
        extraction_metrics={"max_workers": 3, "max_inflight": 8, "max_observed_active": 2, "max_observed_inflight": 4},
        provider_metrics={
            "configured_max": 4, "current_limit": 4, "max_observed_active": 4,
            "throttle_events": 0, "waited_requests": 1, "success_count": 40, "failure_count": 0,
            "average_latency_seconds": 20,
        },
    )
    assert summary["bottleneck"] == "llm-capacity"
    assert summary["recommendation"]["global_llm_slots"] == 5
    assert summary["throughput_announcements_per_minute"] == 4.0
