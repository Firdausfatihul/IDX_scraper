from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from idx_digest.pipeline import Pipeline, PreparedAttachment


class FakeDatabase:
    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}

    def get_document_summary(self, url: str, **_: Any) -> dict[str, Any] | None:
        return self.saved.get(url)

    def save_document_summary(
        self,
        url: str,
        ticker: str,
        payload: dict[str, Any],
        model: str,
        prompt_version: str = "legacy-document",
    ) -> None:
        self.saved[url] = payload


class FakeSummarizer:
    model = "fake-model"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    @staticmethod
    def is_valid_document_summary(payload: dict[str, Any] | None) -> bool:
        return bool(payload and payload.get("summary"))

    def summarize_document(
        self,
        *,
        ticker: str,
        filename: str,
        text: str,
        stream: bool | None = None,
        source_url: str | None = None,
        announcement_id: str | None = None,
    ) -> dict[str, Any]:
        assert stream is False
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.04)
        with self._lock:
            self.active -= 1
        return {"summary": f"{ticker}:{filename}:{text}", "chunk_count": 1}


def test_document_summaries_use_bounded_parallel_workers(tmp_path: Path) -> None:
    documents: list[PreparedAttachment] = []
    for index in range(3):
        text_path = tmp_path / f"document-{index}.txt"
        text_path.write_text(f"text-{index}", encoding="utf-8")
        documents.append(
            PreparedAttachment(
                url=f"https://example.test/{index}.pdf",
                filename=f"document-{index}.pdf",
                text_path=text_path,
            )
        )

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.settings = SimpleNamespace(llm_concurrency=2)
    pipeline.observer = None
    pipeline.db = FakeDatabase()
    pipeline.summarizer = FakeSummarizer()

    errors, completed = pipeline._summarize_documents_parallel(
        documents,
        announcement_id="announcement-1",
        ticker="ANTM",
        title="Example disclosure",
    )

    assert errors == []
    assert completed == 3
    assert pipeline.summarizer.max_active == 2
    assert len(pipeline.db.saved) == 3
