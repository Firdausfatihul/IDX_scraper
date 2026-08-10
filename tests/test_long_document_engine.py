from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.db import Database
from idx_digest.observability import RunObserver
from idx_digest.summarizer import OpenRouterSummarizer, SummaryError


def _document_payload(label: str = "ok") -> dict:
    return {
        "document_type": "disclosure",
        "summary": label,
        "material_facts": [],
        "financial_figures": [],
        "dates_and_deadlines": [],
        "parties": [],
        "corporate_action_signals": [],
        "expansion_or_capex": [],
        "management_or_control_changes": [],
        "capital_structure_or_ownership": [],
        "listing_or_regulatory_events": [],
        "analytical_observations": [],
        "risks_or_uncertainties": [],
        "explicit_market_relevance": [],
        "source_evidence": [],
        "missing_or_unclear": [],
    }


def _seed_attachment(db: Database, url: str) -> None:
    item = {
        "pengumuman": {
            "Id2": "ann-1",
            "Kode_Emiten": "TEST",
            "JudulPengumuman": "Penyampaian Laporan Keuangan",
            "NoPengumuman": "1",
            "JenisPengumuman": "test",
            "PerihalPengumuman": "test",
        },
        "attachments": [],
    }
    db.upsert_announcement(item, "2026-08-07T10:00:00+07:00")
    db.upsert_attachment(
        "ann-1",
        {"FullSavePath": url, "OriginalFilename": "FinancialStatement.pdf", "IsAttachment": False},
    )


def test_long_document_chunks_obey_disclosure_cap_and_emit_exact_progress(tmp_path: Path) -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()
    events: list[dict] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(_document_payload())}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    client = httpx.Client(base_url="https://openrouter.ai/api/v1/", transport=httpx.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        openrouter_api_key="test",
        llm_concurrency=4,
        llm_per_announcement_concurrency=2,
        llm_document_chunk_concurrency=4,
        llm_adaptive_concurrency=False,
    )
    observer = RunObserver(
        timezone_name="Asia/Jakarta", event_sink=events.append,
        console_output=False, show_progress=False,
    )
    summarizer = OpenRouterSummarizer(settings, client=client, observer=observer)
    summarizer._chunks = lambda _text, **_kwargs: ["one", "two", "three", "four"]  # type: ignore[method-assign]

    result = summarizer.summarize_document(
        ticker="TEST", filename="FinancialStatement.pdf", text="ignored",
        source_url=None, announcement_id="ann-1", stream=False,
    )

    assert result["chunk_count"] == 4
    assert max_active == 2
    chunk_events = [e for e in events if e.get("stage") == "llm-chunk"]
    plan = next(e for e in chunk_events if e["message"] == "document chunk plan")
    assert plan["fields"]["chunk_count"] == 4
    assert plan["fields"]["chunk_concurrency"] == 2
    assert plan["fields"]["straggler"] is True
    assert len([e for e in chunk_events if e["message"] == "document chunk completed"]) == 4
    assert any(e["message"] == "document combine started" for e in chunk_events)
    assert any(e["message"] == "document combine completed" for e in chunk_events)


def test_chunk_checkpoint_resume_reuses_completed_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        openrouter_api_key="test",
        llm_concurrency=2,
        llm_per_announcement_concurrency=1,
        llm_document_chunk_concurrency=1,
    )
    url = "https://example.test/financial.pdf"
    db = Database(settings.database_path)
    _seed_attachment(db, url)

    first = OpenRouterSummarizer(settings, client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500))))
    first._chunks = lambda _text, **_kwargs: ["one", "two", "three"]  # type: ignore[method-assign]
    calls: list[int | str] = []

    def first_completion(_prompt: str, **kwargs):
        context = kwargs.get("audit_context") or {}
        if context.get("stage") == "document_combine":
            calls.append("combine")
            return _document_payload("combined")
        index = int(context["chunk_index"])
        calls.append(index)
        if index == 2:
            raise SummaryError("synthetic interruption")
        return _document_payload(f"chunk-{index}")

    monkeypatch.setattr(first, "_json_completion", first_completion)
    with pytest.raises(SummaryError, match="synthetic interruption"):
        first.summarize_document(
            ticker="TEST", filename="FinancialStatement.pdf", text="ignored",
            source_url=url, announcement_id="ann-1", stream=False,
        )
    progress = db.document_chunk_progress(url, model=first.model, prompt_version=first.document_prompt_version)
    assert [row["chunk_index"] for row in progress] == [1]

    second = OpenRouterSummarizer(settings, client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500))))
    second._chunks = lambda _text, **_kwargs: ["one", "two", "three"]  # type: ignore[method-assign]
    resumed_calls: list[int | str] = []

    def second_completion(_prompt: str, **kwargs):
        context = kwargs.get("audit_context") or {}
        if context.get("stage") == "document_combine":
            resumed_calls.append("combine")
            return _document_payload("combined")
        index = int(context["chunk_index"])
        resumed_calls.append(index)
        return _document_payload(f"chunk-{index}")

    monkeypatch.setattr(second, "_json_completion", second_completion)
    result = second.summarize_document(
        ticker="TEST", filename="FinancialStatement.pdf", text="ignored",
        source_url=url, announcement_id="ann-1", stream=False,
    )
    assert result["chunk_count"] == 3
    assert resumed_calls == [2, 3, "combine"]
    progress = db.document_chunk_progress(url, model=second.model, prompt_version=second.document_prompt_version)
    assert [row["chunk_index"] for row in progress] == [1, 2, 3]
