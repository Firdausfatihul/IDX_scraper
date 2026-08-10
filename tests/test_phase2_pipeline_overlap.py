from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import idx_digest.pipeline as pipeline_module
from idx_digest.config import Settings
from idx_digest.pipeline import DownloadedAttachment, Pipeline, PreparedAttachment


def _item(ticker: str, idx: int) -> dict[str, Any]:
    return {
        "pengumuman": {
            "Id2": f"20260807130{idx:02d}-{ticker}", "Kode_Emiten": ticker,
            "TglPengumuman": f"2026-08-07T13:{idx:02d}:00", "JudulPengumuman": "Informasi material",
            "NoPengumuman": str(idx), "JenisPengumuman": "General", "PerihalPengumuman": "General",
        },
        "attachments": [{"FullSavePath": f"https://example.test/{ticker}.pdf", "OriginalFilename": f"{ticker}.pdf", "IsAttachment": False}],
    }


class FakeIDXClient:
    def __init__(self, *_a, **_k): self.browser = None
    def iter_announcements(self, *_a, **_k):
        yield _item("AAAA", 1)
        yield _item("BBBB", 2)
        yield _item("CCCC", 3)
    def browser_transport(self): raise AssertionError
    def close(self): pass


class FakeDownloader:
    def __init__(self, *_a, **_k): pass
    def close(self): pass


def test_download_producer_overlaps_background_extraction(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "IDXClient", FakeIDXClient)
    monkeypatch.setattr(pipeline_module, "AttachmentDownloader", FakeDownloader)
    settings = Settings(
        data_dir=tmp_path / "data", extraction_workers=2, extraction_queue_size=4,
        idx_request_delay_seconds=0, _env_file=None,
    )
    pipeline = Pipeline(settings, skip_llm=True)
    extraction_started = threading.Event()
    producer_observed_overlap = {"value": False}
    download_calls = {"n": 0}

    def localize(_downloader, *, announcement_id, ticker, attachment):
        download_calls["n"] += 1
        if download_calls["n"] >= 2 and extraction_started.wait(timeout=.2):
            producer_observed_overlap["value"] = True
        path = tmp_path / f"{ticker}.pdf"
        path.write_bytes(b"pdf")
        return DownloadedAttachment(
            url=str(attachment["FullSavePath"]), filename=str(attachment["OriginalFilename"]),
            path=path, content_type="application/pdf", sha256=ticker.lower(),
        )

    def extract(downloaded: DownloadedAttachment, *, announcement_id: str, ticker: str):
        extraction_started.set()
        time.sleep(.06)
        text = tmp_path / f"{ticker}.txt"
        text.write_text(f"text {ticker}")
        return PreparedAttachment(url=downloaded.url, filename=downloaded.filename, text_path=text)

    monkeypatch.setattr(pipeline, "_download_or_cached_attachment", localize)
    monkeypatch.setattr(pipeline, "_extract_downloaded_attachment", extract)
    monkeypatch.setattr(pipeline, "_export_company_checkpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "_refresh_share_exports", lambda *_a, **_k: {})
    tz = ZoneInfo("Asia/Jakarta")
    report = pipeline.run(
        start_at=datetime(2026, 8, 7, 0, 0, tzinfo=tz),
        end_at=datetime(2026, 8, 7, 23, 59, 59, tzinfo=tz),
    )

    assert producer_observed_overlap["value"] is True
    assert report["processed_announcements"] == 3
    assert report["diagnostics"]["extraction_metrics"]["completed"] == 3
    assert report["diagnostics"]["extraction_metrics"]["max_workers"] == 2
