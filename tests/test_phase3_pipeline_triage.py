from __future__ import annotations
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import idx_digest.pipeline as pipeline_module
from idx_digest.config import Settings
from idx_digest.pipeline import Pipeline, PreparedAttachment


def _item(title: str) -> dict[str, Any]:
    return {
        "pengumuman": {
            "Id2": "20260807120000-ROUTINE",
            "Kode_Emiten": "TEST",
            "TglPengumuman": "2026-08-07T12:00:00",
            "JudulPengumuman": title,
            "NoPengumuman": "1",
            "JenisPengumuman": "General",
            "PerihalPengumuman": "General",
        },
        "attachments": [
            {"FullSavePath": "https://example.test/main.pdf", "OriginalFilename": "main.pdf", "IsAttachment": False},
            {"FullSavePath": "https://example.test/lamp.pdf", "OriginalFilename": "lamp.pdf", "IsAttachment": True},
        ],
    }


class FakeIDX:
    def __init__(self, *_a, **_k): self.browser = None
    def iter_announcements(self, *_a, **_k): yield _item("Laporan Bulanan Registrasi Pemegang Efek")
    def browser_transport(self): raise AssertionError
    def close(self): pass


class FakeDownloader:
    def __init__(self, *_a, **_k): pass
    def close(self): pass


class FakeSummarizer:
    model = "fake-model"
    document_prompt_version = "doc-v1"
    announcement_prompt_version = "ann-v1"
    company_prompt_version = "company-v1"
    provider_metrics = {"enabled": True, "current_limit": 2}
    def __init__(self):
        self.prompts = SimpleNamespace(profile_name="fake", hashes={})
        self.document_calls = 0
        self.routine_calls = 0
    @staticmethod
    def is_valid_document_summary(v): return bool(v and v.get("summary"))
    @staticmethod
    def is_valid_announcement_summary(v): return bool(v and v.get("executive_summary"))
    def summarize_document(self, **_k):
        self.document_calls += 1
        return {"summary": "doc", "chunk_count": 1}
    def summarize_routine_announcement(self, *, announcement, raw_documents, triage, **_k):
        self.routine_calls += 1
        return {
            "ticker": announcement["ticker"], "announcement_id": announcement["id2"],
            "announced_at": announcement["announced_at"], "title": announcement["title"],
            "executive_summary": f"routine direct from {len(raw_documents)} files",
        }
    def summarize_announcement(self, **_k): raise AssertionError("full reducer should not run")
    def summarize_company_window(self, *, ticker, **_k): return {"ticker": ticker, "overview": "company"}
    def close(self): pass


def test_clean_routine_skips_document_fanout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "IDXClient", FakeIDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", FakeDownloader)
    settings = Settings(
        data_dir=tmp_path / "data", openrouter_api_key="unused",
        llm_concurrency=2, llm_per_announcement_concurrency=1,
        routine_triage_enabled=True, attachment_dedup_enabled=False,
    )
    pipeline = Pipeline(settings, skip_llm=True)
    fake = FakeSummarizer(); pipeline.summarizer = fake
    text = " ".join(["Posisi pemegang saham per 31 Juli 2026 sesuai daftar registrasi efek."] * 100)
    def prepare(_d, *, announcement_id, ticker, attachment):
        path = tmp_path / attachment["OriginalFilename"]
        path.write_text(text)
        return PreparedAttachment(url=attachment["FullSavePath"], filename=attachment["OriginalFilename"], text_path=path)
    monkeypatch.setattr(pipeline, "_download_or_cached_attachment", prepare)
    monkeypatch.setattr(pipeline, "_refresh_share_exports", lambda *_a, **_k: {})
    monkeypatch.setattr(pipeline, "_export_company_checkpoint", lambda *_a, **_k: None)
    tz = ZoneInfo("Asia/Jakarta")
    report = pipeline.run(start_at=datetime(2026,8,7,0,0,tzinfo=tz), end_at=datetime(2026,8,7,23,59,59,tzinfo=tz))
    assert fake.document_calls == 0
    assert fake.routine_calls == 1
    assert report["diagnostics"]["phase3_metrics"]["routine_direct"] == 1
    with pipeline.db.connect() as conn:
        row = conn.execute("SELECT analysis_mode, triage_json FROM announcement_summaries").fetchone()
    assert row["analysis_mode"] == "routine_direct"
    assert "routine_direct" in row["triage_json"]


def test_suspicious_routine_keeps_full_document_pipeline(tmp_path: Path, monkeypatch):
    class FullFakeSummarizer(FakeSummarizer):
        def __init__(self):
            super().__init__(); self.full_calls = 0
        def summarize_announcement(self, *, announcement, documents, **_k):
            self.full_calls += 1
            return {
                "ticker": announcement["ticker"], "announcement_id": announcement["id2"],
                "announced_at": announcement["announced_at"], "title": announcement["title"],
                "executive_summary": "full path",
            }
        def summarize_routine_announcement(self, **_k):
            raise AssertionError("suspicious routine filing must not use direct route")

    monkeypatch.setattr(pipeline_module, "IDXClient", FakeIDX)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", FakeDownloader)
    settings = Settings(
        data_dir=tmp_path / "data", openrouter_api_key="unused",
        llm_concurrency=2, llm_per_announcement_concurrency=1,
        routine_triage_enabled=True, attachment_dedup_enabled=False,
    )
    pipeline = Pipeline(settings, skip_llm=True)
    fake = FullFakeSummarizer(); pipeline.summarizer = fake
    text = ("Perubahan pemegang saham pengendali terjadi setelah pengalihan saham. " * 80)
    def prepare(_d, *, announcement_id, ticker, attachment):
        path = tmp_path / attachment["OriginalFilename"]
        path.write_text(text)
        return PreparedAttachment(url=attachment["FullSavePath"], filename=attachment["OriginalFilename"], text_path=path)
    monkeypatch.setattr(pipeline, "_download_or_cached_attachment", prepare)
    monkeypatch.setattr(pipeline, "_refresh_share_exports", lambda *_a, **_k: {})
    monkeypatch.setattr(pipeline, "_export_company_checkpoint", lambda *_a, **_k: None)
    tz = ZoneInfo("Asia/Jakarta")
    report = pipeline.run(start_at=datetime(2026,8,7,0,0,tzinfo=tz), end_at=datetime(2026,8,7,23,59,59,tzinfo=tz))
    assert fake.document_calls == 2
    assert fake.full_calls == 1
    assert report["diagnostics"]["phase3_metrics"]["routine_full"] == 1
