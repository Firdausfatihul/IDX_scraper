import json

import httpx

from idx_digest.config import Settings
from idx_digest.summarizer import (
    ANNOUNCEMENT_SCHEMA,
    OpenRouterSummarizer,
    SummaryError,
)


def valid_announcement() -> dict[str, object]:
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


def test_completion_pins_provider_and_uses_json_schema() -> None:
    captured: dict[str, object] = {}
    result_payload = valid_announcement()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(result_payload)}}]},
        )

    client = httpx.Client(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        openrouter_api_key="sk-or-v1-test",
        openrouter_provider="deepinfra",
    )
    summarizer = OpenRouterSummarizer(settings, client=client)

    result = summarizer._json_completion(
        "test",
        schema_name="idx_announcement_summary",
        schema=ANNOUNCEMENT_SCHEMA,
        max_tokens=123,
    )

    assert result == result_payload
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "deepseek/deepseek-v4-flash-0731"
    assert body["provider"] == {
        "only": ["deepinfra"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning"] == {"enabled": False}


def test_empty_object_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.Client(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(_env_file=None, openrouter_api_key="sk-or-v1-test")
    summarizer = OpenRouterSummarizer(settings, client=client)

    try:
        summarizer._json_completion(
            "test",
            schema_name="idx_announcement_summary",
            schema=ANNOUNCEMENT_SCHEMA,
        )
    except SummaryError as exc:
        assert "missing keys" in str(exc)
    else:
        raise AssertionError("empty JSON object must not be accepted")
