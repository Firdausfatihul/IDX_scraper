from __future__ import annotations

import json
from typing import Any

import httpx

from idx_digest.config import Settings
from idx_digest.summarizer import ANNOUNCEMENT_SCHEMA, OpenRouterSummarizer


class DummyObserver:
    stream_llm = True

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def begin_stream(self, label: str, **fields: Any) -> None:
        self.events.append(("begin", label, fields))

    def stream_chunk(self, chunk: str) -> None:
        self.chunks.append(chunk)

    def end_stream(self, **fields: Any) -> None:
        self.events.append(("end", "stream", fields))

    def event(self, stage: str, message: str, **fields: Any) -> None:
        self.events.append((stage, message, fields))


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


def test_streaming_completion_collects_and_validates_json() -> None:
    captured: dict[str, Any] = {}
    text = json.dumps(valid_announcement())
    split = len(text) // 2
    events = [
        {"choices": [{"delta": {"content": text[:split]}}]},
        {"choices": [{"delta": {"content": text[split:]}}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
    ]
    sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})

    client = httpx.Client(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    observer = DummyObserver()
    settings = Settings(_env_file=None, openrouter_api_key="sk-or-v1-test")
    summarizer = OpenRouterSummarizer(settings, client=client, observer=observer)  # type: ignore[arg-type]

    result = summarizer._json_completion(
        "test",
        schema_name="idx_announcement_summary",
        schema=ANNOUNCEMENT_SCHEMA,
    )

    assert result == valid_announcement()
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert "".join(observer.chunks) == text
