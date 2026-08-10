from idx_digest.provider_gate import AdaptiveProviderGate


def test_provider_gate_halves_on_429_then_ramps_after_healthy_streak():
    gate = AdaptiveProviderGate(configured_max=4, enabled=True, healthy_successes_per_step=2)
    leases = [gate.acquire() for _ in range(4)]
    gate.record_failure(leases[0], status_code=429, error="throttled")
    assert gate.metrics["current_limit"] == 2
    for lease in leases[1:]:
        gate.record_success(lease, elapsed_seconds=0.1)
    # Current limit is 2; threshold is healthy_successes_per_step * current_limit = 4.
    for _ in range(4):
        lease = gate.acquire()
        gate.record_success(lease, elapsed_seconds=0.1)
    assert gate.metrics["current_limit"] == 3
    assert gate.metrics["throttle_events"] == 1
    assert gate.metrics["ramp_events"] == 1


def test_fixed_provider_gate_never_changes_limit():
    gate = AdaptiveProviderGate(configured_max=4, enabled=False)
    lease = gate.acquire()
    gate.record_failure(lease, status_code=429, error="throttled")
    assert gate.metrics["current_limit"] == 4


def test_openrouter_429_reduces_provider_limit_and_retry_can_succeed(monkeypatch, tmp_path):
    import json
    import httpx
    from idx_digest.config import Settings
    from idx_digest.summarizer import ANNOUNCEMENT_SCHEMA, OpenRouterSummarizer

    calls = 0
    payload = {
        "ticker": "ANTM", "announcement_id": "abc",
        "announced_at": "2026-08-05T21:51:10+07:00", "title": "Perubahan Anggaran Dasar",
        "executive_summary": "Perseroan menyampaikan perubahan anggaran dasar.",
        "category": "corporate_governance", "material_facts": [], "financial_figures": [],
        "corporate_actions": [], "expansion_projects": [], "management_or_control_changes": [],
        "capital_structure_events": [], "listing_or_regulatory_events": [], "analytical_scenarios": [],
        "dates_and_deadlines": [], "risks_or_uncertainties": [], "possible_investor_relevance": [],
        "source_files": [], "limitations": [],
    }
    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/", transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", openrouter_api_key="test",
        llm_concurrency=4, llm_adaptive_concurrency=True,
    )
    summarizer = OpenRouterSummarizer(settings, client=client)
    monkeypatch.setattr("idx_digest.summarizer.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("idx_digest.summarizer.random.uniform", lambda _a, _b: 0)
    result = summarizer._json_completion("test", schema_name="idx_announcement_summary", schema=ANNOUNCEMENT_SCHEMA)
    assert result["ticker"] == "ANTM"
    assert calls == 2
    assert summarizer.provider_metrics["throttle_events"] == 1
    assert summarizer.provider_metrics["current_limit"] == 2
