import json

from idx_digest.summarizer import OpenRouterSummarizer


def test_malformed_json_retry_changes_prompt_and_increases_budget(monkeypatch):
    summarizer = OpenRouterSummarizer.__new__(OpenRouterSummarizer)
    summarizer.observer = None
    calls = []

    def fake_once(prompt, **kwargs):
        calls.append((prompt, kwargs["max_tokens"]))
        if len(calls) == 1:
            raise json.JSONDecodeError("unterminated", '"abc', 1)
        return {"ok": True}

    summarizer._completion_once = fake_once
    monkeypatch.setattr("idx_digest.summarizer.time.sleep", lambda _: None)
    monkeypatch.setattr("idx_digest.summarizer.random.uniform", lambda a, b: 0)

    result = summarizer._json_completion(
        "ORIGINAL",
        schema_name="test",
        schema={"type": "object"},
        max_tokens=5000,
    )
    assert result == {"ok": True}
    assert calls[0] == ("ORIGINAL", 5000)
    assert calls[1][1] == 7000
    assert "RETRY JSON" in calls[1][0]


def test_document_chunk_override():
    summarizer = OpenRouterSummarizer.__new__(OpenRouterSummarizer)
    class Settings:
        llm_chunk_chars = 45000
    summarizer.settings = Settings()
    text = "a" * 50000
    assert len(summarizer._chunks(text)) == 2
    assert len(summarizer._chunks(text, chunk_chars=22000)) == 3


def test_retry_event_includes_chunk_context_and_reason(monkeypatch):
    from idx_digest.observability import RunObserver

    events = []
    summarizer = OpenRouterSummarizer.__new__(OpenRouterSummarizer)
    summarizer.observer = RunObserver(
        timezone_name="Asia/Jakarta", event_sink=events.append,
        console_output=False, show_progress=False,
    )
    calls = 0

    def fake_once(_prompt, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise json.JSONDecodeError("unterminated", '"abc', 1)
        return {"ok": True}

    summarizer._completion_once = fake_once
    monkeypatch.setattr("idx_digest.summarizer.time.sleep", lambda _: None)
    monkeypatch.setattr("idx_digest.summarizer.random.uniform", lambda a, b: 0)

    result = summarizer._json_completion(
        "ORIGINAL",
        schema_name="test",
        schema={"type": "object"},
        max_tokens=5000,
        audit_context={
            "ticker": "SRSN", "announcement_id": "ann", "filename": "financial.pdf",
            "attachment_url": "https://example.test/financial.pdf", "chunk_index": 12, "chunk_count": 19,
        },
    )
    assert result == {"ok": True}
    retry = next(e for e in events if e.get("message") == "summary request failed; retrying")
    assert retry["fields"]["retry_reason"] == "malformed_json"
    assert retry["fields"]["chunk_index"] == 12
    assert retry["fields"]["chunk_count"] == 19
