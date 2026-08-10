from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_performance_summary(
    *,
    processed_announcements: int,
    total_elapsed_seconds: float | None,
    scheduler_metrics: dict[str, Any] | None,
    extraction_metrics: dict[str, Any] | None,
    provider_metrics: dict[str, Any] | None,
    phase3_metrics: dict[str, Any] | None = None,
    stage_timings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create conservative next-run tuning advice from observed run telemetry.

    This never mutates live concurrency. It only describes the observed bottleneck
    and suggests a next-run configuration bounded by the GUI's supported limits.
    """

    scheduler = dict(scheduler_metrics or {})
    extraction = dict(extraction_metrics or {})
    provider = dict(provider_metrics or {})
    phase3 = dict(phase3_metrics or {})
    timings = dict(stage_timings or {})

    elapsed = max(0.0, _safe_float(total_elapsed_seconds))
    throughput = (processed_announcements / elapsed * 60.0) if elapsed > 0 else 0.0

    llm_max = max(1, _safe_int(scheduler.get("max_workers"), _safe_int(provider.get("configured_max"), 1)))
    llm_peak = _safe_int(scheduler.get("max_observed_active"), _safe_int(scheduler.get("active"), 0))
    llm_peak_util = min(1.0, llm_peak / llm_max) if llm_max else 0.0
    llm_avg_wait = _safe_float(scheduler.get("average_queue_wait_seconds"))
    llm_max_wait = _safe_float(scheduler.get("max_queue_wait_seconds"))

    extract_workers = max(1, _safe_int(extraction.get("max_workers"), 1))
    extract_peak = _safe_int(extraction.get("max_observed_active"), _safe_int(extraction.get("active"), 0))
    extract_peak_util = min(1.0, extract_peak / extract_workers) if extract_workers else 0.0
    extract_limit = max(extract_workers, _safe_int(extraction.get("max_inflight"), extract_workers))
    extract_peak_inflight = _safe_int(extraction.get("max_observed_inflight"))
    extract_backlog_ratio = min(1.0, extract_peak_inflight / extract_limit) if extract_limit else 0.0
    extract_avg_wait = _safe_float(extraction.get("average_queue_wait_seconds"))

    provider_configured = max(1, _safe_int(provider.get("configured_max"), llm_max))
    provider_current = max(1, _safe_int(provider.get("current_limit"), provider_configured))
    provider_peak = _safe_int(provider.get("max_observed_active"))
    provider_peak_util = min(1.0, provider_peak / provider_configured) if provider_configured else 0.0
    provider_waited = _safe_int(provider.get("waited_requests"))
    provider_total = max(1, _safe_int(provider.get("success_count")) + _safe_int(provider.get("failure_count")))
    provider_wait_ratio = min(1.0, provider_waited / provider_total)
    provider_avg_latency = _safe_float(provider.get("average_latency_seconds"))
    throttle_events = _safe_int(provider.get("throttle_events"))
    transient_events = _safe_int(provider.get("transient_failure_events"))

    suggested_llm = llm_max
    suggested_extract = extract_workers
    suggested_backlog = extract_limit
    reasons: list[str] = []
    bottleneck = "balanced"
    confidence = "medium"

    if throttle_events > 0 or provider_current < provider_configured:
        bottleneck = "provider"
        suggested_llm = max(1, min(llm_max, provider_current))
        reasons.append(
            f"Provider pressure was observed ({throttle_events} throttles; final adaptive limit {provider_current}/{provider_configured})."
        )
        confidence = "high"
    elif provider_wait_ratio >= 0.25 and llm_peak_util >= 0.75:
        bottleneck = "provider"
        reasons.append(
            f"{provider_wait_ratio:.0%} of provider requests waited for a slot while the LLM scheduler reached {llm_peak}/{llm_max}."
        )
        confidence = "high"
    elif extract_backlog_ratio >= 0.85 and extract_peak_util >= 0.75:
        bottleneck = "extraction"
        if extract_workers < 8:
            suggested_extract = extract_workers + 1
        suggested_backlog = min(32, max(suggested_backlog, suggested_extract * 3))
        reasons.append(
            f"Extraction inflight reached {extract_peak_inflight}/{extract_limit} with {extract_peak}/{extract_workers} workers active."
        )
        confidence = "high"
    elif llm_peak_util >= 0.95 and throttle_events == 0 and provider_wait_ratio < 0.10 and llm_max < 8:
        bottleneck = "llm-capacity"
        suggested_llm = llm_max + 1
        reasons.append(
            f"The LLM scheduler saturated {llm_peak}/{llm_max} slots without throttling or meaningful provider-slot waiting."
        )
    elif llm_peak_util < 0.50 and extract_backlog_ratio < 0.50:
        bottleneck = "preparation-or-source"
        reasons.append(
            "Neither the LLM pool nor extraction pool stayed saturated; browser/source preparation or a small workload likely dominated wall time."
        )
        confidence = "low" if processed_announcements < 8 else "medium"
    else:
        reasons.append("No single stage dominated strongly enough to justify an aggressive concurrency change.")

    if provider_avg_latency >= 60:
        reasons.append(f"Average provider latency was {provider_avg_latency:.1f}s, so adding slots may improve throughput only if the provider remains healthy.")
    if llm_avg_wait >= 10:
        reasons.append(f"Average LLM queue wait was {llm_avg_wait:.1f}s (max {llm_max_wait:.1f}s).")
    if extract_avg_wait >= 1:
        reasons.append(f"Average extraction queue wait was {extract_avg_wait:.1f}s.")

    routine_direct = _safe_int(phase3.get("routine_direct"))
    routine_full = _safe_int(phase3.get("routine_full"))
    dedup = _safe_int(phase3.get("dedup_suppressed"))

    return {
        "throughput_announcements_per_minute": round(throughput, 3),
        "processed_announcements": int(processed_announcements),
        "total_elapsed_seconds": round(elapsed, 3),
        "bottleneck": bottleneck,
        "confidence": confidence,
        "utilization": {
            "llm_peak": llm_peak,
            "llm_limit": llm_max,
            "llm_peak_ratio": round(llm_peak_util, 3),
            "provider_peak": provider_peak,
            "provider_configured_limit": provider_configured,
            "provider_peak_ratio": round(provider_peak_util, 3),
            "extraction_peak": extract_peak,
            "extraction_workers": extract_workers,
            "extraction_peak_ratio": round(extract_peak_util, 3),
            "extraction_peak_inflight": extract_peak_inflight,
            "extraction_backlog_limit": extract_limit,
            "extraction_backlog_ratio": round(extract_backlog_ratio, 3),
        },
        "latency": {
            "provider_average_seconds": round(provider_avg_latency, 3),
            "provider_ewma_seconds": round(_safe_float(provider.get("ewma_latency_seconds")), 3),
            "provider_max_seconds": round(_safe_float(provider.get("max_latency_seconds")), 3),
            "llm_queue_average_seconds": round(llm_avg_wait, 3),
            "llm_queue_max_seconds": round(llm_max_wait, 3),
            "extraction_queue_average_seconds": round(extract_avg_wait, 3),
        },
        "provider_pressure": {
            "wait_ratio": round(provider_wait_ratio, 3),
            "throttle_events": throttle_events,
            "transient_failure_events": transient_events,
            "final_limit": provider_current,
        },
        "triage_savings": {
            "routine_direct": routine_direct,
            "routine_full": routine_full,
            "duplicates_suppressed": dedup,
        },
        "stage_timings": timings,
        "recommendation": {
            "global_llm_slots": suggested_llm,
            "extraction_workers": suggested_extract,
            "extraction_queue_size": suggested_backlog,
            "changed": bool(
                suggested_llm != llm_max
                or suggested_extract != extract_workers
                or suggested_backlog != extract_limit
            ),
            "reasons": reasons,
        },
    }
