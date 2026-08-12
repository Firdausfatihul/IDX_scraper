from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from idx_digest.db import Database
from idx_digest.gui import app
from idx_digest.config import Settings
from idx_digest.summarizer import ANNOUNCEMENT_SCHEMA, OpenRouterSummarizer


def _announcement_item() -> dict:
    return {
        "pengumuman": {
            "Id2": "ann-1",
            "Kode_Emiten": "TEST",
            "JudulPengumuman": "Ekspansi pabrik",
            "NoPengumuman": "001",
            "JenisPengumuman": "Keterbukaan",
            "PerihalPengumuman": "Ekspansi",
        },
        "attachments": [],
    }


def _announcement_summary(url: str) -> dict:
    return {
        "ticker": "TEST",
        "announcement_id": "ann-1",
        "announced_at": "2026-08-01T10:00:00+07:00",
        "title": "Ekspansi pabrik",
        "executive_summary": "Perseroan menambah kapasitas produksi.",
        "category": "expansion",
        "material_facts": ["Kapasitas meningkat menjadi 500.000 ton per tahun."],
        "financial_figures": [],
        "corporate_actions": [],
        "expansion_projects": ["Kapasitas meningkat menjadi 500.000 ton per tahun."],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [],
        "dates_and_deadlines": [],
        "risks_or_uncertainties": [],
        "possible_investor_relevance": [],
        "source_files": [{"filename": "source.pdf", "url": url}],
        "limitations": [],
    }


def _company_summary(start: str, end: str) -> dict:
    return {
        "ticker": "TEST",
        "period": {"start": start, "end": end},
        "announcement_count": 1,
        "overview": "TEST meningkatkan kapasitas produksi.",
        "timeline": [{"announced_at": "2026-08-01T10:00:00+07:00", "title": "Ekspansi pabrik", "summary": "Kapasitas meningkat."}],
        "material_changes": ["Kapasitas meningkat menjadi 500.000 ton per tahun."],
        "key_financial_figures": [],
        "corporate_actions": [],
        "expansion_projects": ["Kapasitas meningkat menjadi 500.000 ton per tahun."],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [],
        "risks_or_uncertainties": [],
        "items_to_monitor": [],
        "limitations": [],
    }


def test_company_detail_traces_saved_attachment_and_pdf_page(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "data" / "idx_digest.sqlite3")
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    db.upsert_announcement(_announcement_item(), "2026-08-01T10:00:00+07:00")
    url = "https://example.test/source.pdf"
    db.upsert_attachment("ann-1", {"FullSavePath": url, "OriginalFilename": "source.pdf", "IsAttachment": False})
    raw = tmp_path / "data" / "raw" / "source.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"%PDF fake")
    text = tmp_path / "data" / "text" / "TEST" / "source.txt"
    text.parent.mkdir(parents=True)
    text.write_text("===== PAGE 1 =====\nIntro\n===== PAGE 2 =====\nKapasitas meningkat menjadi 500.000 ton per tahun.\n", encoding="utf-8")
    db.update_attachment_file(url, local_path=str(raw), sha256="abc", content_type="application/pdf")
    db.update_extraction(url, text_path=str(text), method="pdf-native", error=None)
    db.save_document_summary(url, "TEST", {
        "document_type": "disclosure", "summary": "Expansion", "material_facts": [], "financial_figures": [],
        "dates_and_deadlines": [], "parties": [], "corporate_action_signals": [],
        "expansion_or_capex": ["Kapasitas meningkat menjadi 500.000 ton per tahun."],
        "management_or_control_changes": [], "capital_structure_or_ownership": [],
        "listing_or_regulatory_events": [], "analytical_observations": [], "risks_or_uncertainties": [],
        "explicit_market_relevance": [],
        "source_evidence": [{"fact": "Capacity", "evidence": "Kapasitas meningkat menjadi 500.000 ton per tahun."}],
        "missing_or_unclear": [], "chunk_count": 1,
    }, "model")
    db.save_announcement_summary("ann-1", "TEST", _announcement_summary(url), "legacy-model", "legacy-ann")
    db.save_company_summary("TEST", start, end, _company_summary(start, end), "model", "legacy-company")

    body = TestClient(app).post("/api/company-detail", json={"start": "2026-07-06", "end": "2026-08-06", "ticker": "TEST"}).json()
    assert body["ticker"] == "TEST"
    assert body["claim_traces"][0]["attribution"] == "deterministic_single_announcement"
    evidence = body["announcements"][0]["attachments"][0]["source_evidence_located"][0]
    assert evidence["locator"]["page"] == 2
    assert evidence["locator"]["match"] == "exact_text"


def test_llm_audit_persists_exact_prompt_and_response(tmp_path) -> None:
    result = _announcement_summary("https://example.test/source.pdf")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})

    settings = Settings(_env_file=None, openrouter_api_key="test", data_dir=tmp_path / "data")
    client = httpx.Client(base_url="https://openrouter.ai/api/v1/", transport=httpx.MockTransport(handler))
    summarizer = OpenRouterSummarizer(settings, client=client)
    output = summarizer._json_completion(
        "EXACT USER PROMPT",
        schema_name="idx_announcement_summary",
        schema=ANNOUNCEMENT_SCHEMA,
        max_tokens=100,
        audit_context={"stage": "announcement", "ticker": "TEST", "announcement_id": "ann-1"},
    )
    assert output["ticker"] == "TEST"
    db = Database(settings.database_path)
    with db.connect() as conn:
        row = conn.execute("SELECT audit_id FROM llm_audits ORDER BY audit_id DESC LIMIT 1").fetchone()
    audit = db.llm_audit(int(row["audit_id"]))
    assert audit is not None
    assert audit["user_prompt"] == "EXACT USER PROMPT"
    assert "Perseroan menambah kapasitas" in audit["raw_response"]
    assert audit["prompt_tokens"] == 10
    assert audit["status"] == "succeeded"
