from __future__ import annotations

import json

import httpx

from idx_digest.config import Settings
from idx_digest.observability import RunObserver
from idx_digest.summarizer import ANNOUNCEMENT_SCHEMA, OpenRouterSummarizer


def _valid_announcement() -> dict[str, object]:
    return {
        "ticker": "ANTM",
        "announcement_id": "abc",
        "announced_at": "2026-08-05T21:51:10+07:00",
        "title": "Perubahan Anggaran Dasar",
        "executive_summary": "Perseroan menyampaikan perubahan anggaran dasar.",
        "category": "corporate_governance",
        "material_facts": [],
        "financial_figures": [],
        "corporate_actions": [],
        "expansion_projects": [],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [],
        "dates_and_deadlines": [],
        "risks_or_uncertainties": [],
        "possible_investor_relevance": [],
        "source_files": [],
        "limitations": [],
    }


def test_openrouter_emits_correlated_request_lifecycle(tmp_path) -> None:
    events: list[dict] = []
    payload = _valid_announcement()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(payload)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    client = httpx.Client(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    observer = RunObserver(
        timezone_name="Asia/Jakarta",
        event_sink=events.append,
        console_output=False,
        show_progress=False,
    )
    settings = Settings(
        _env_file=None,
        openrouter_api_key="sk-or-v1-test",
        openrouter_provider="deepinfra",
        data_dir=tmp_path,
        llm_concurrency=4,
    )
    summarizer = OpenRouterSummarizer(settings, client=client, observer=observer)

    result = summarizer._json_completion(
        "test",
        schema_name="idx_announcement_summary",
        schema=ANNOUNCEMENT_SCHEMA,
        max_tokens=123,
        audit_context={"ticker": "ANTM", "announcement_id": "abc", "stage": "announcement"},
    )

    assert result == payload
    lifecycle = [e for e in events if e.get("stage") == "llm-request"]
    states = [e.get("fields", {}).get("state") for e in lifecycle]
    assert states == [
        "queued",
        "waiting_provider",
        "sending",
        "generating",
        "response_received",
        "validating",
        "completed",
    ]
    ids = {e["fields"]["request_id"] for e in lifecycle}
    assert len(ids) == 1
    completed = lifecycle[-1]["fields"]
    assert completed["ticker"] == "ANTM"
    assert completed["announcement_id"] == "abc"
    assert completed["total_tokens"] == 30


def test_gui_contains_live_request_progress_instrumentation() -> None:
    from importlib.resources import files

    html = files("idx_digest").joinpath("web/index.html").read_text(encoding="utf-8")
    assert "llmRequests: new Map()" in html
    assert "estimated" in html
    assert "waiting for provider slot" in html
    assert "requestLatencyStats" in html
    assert "generating ·" in html
    assert "waiting provider" in html
    assert "requestLatencyKey" in html
    assert "complete · ${active} generating · ${waiting} waiting" in html
