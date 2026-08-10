from __future__ import annotations

import json

from idx_digest.observability import RunObserver


def test_chatty_events_are_hidden_but_written(tmp_path) -> None:
    log_path = tmp_path / "events.jsonl"
    observer = RunObserver(
        timezone_name="Asia/Jakarta",
        verbose=True,
        trace_browser=True,
        browser_network=False,
        show_cache_events=False,
        show_page_events=False,
        show_progress=False,
        log_file=log_path,
    )

    assert observer._should_print(
        stage="browser", message="network response", level="INFO", always=False
    ) is False
    assert observer._should_print(
        stage="cache", message="cache hit", level="INFO", always=False
    ) is False
    assert observer._should_print(
        stage="extract", message="PDF page extracted", level="INFO", always=False
    ) is False
    assert observer._should_print(
        stage="llm", message="OpenRouter request", level="INFO", always=False
    ) is True

    observer.event("cache", "cache hit", filename="example.pdf")
    observer.event("extract", "PDF page extracted", page=1)
    observer.event("browser", "network response", url="https://www.idx.co.id/x.js")
    observer.close()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [row["stage"] for row in rows] == ["cache", "extract", "browser"]


def test_explicit_noisy_modes_print() -> None:
    observer = RunObserver(
        timezone_name="Asia/Jakarta",
        verbose=True,
        browser_network=True,
        show_cache_events=True,
        show_page_events=True,
        show_progress=False,
    )
    assert observer._should_print(
        stage="browser", message="network response", level="INFO", always=False
    ) is True
    assert observer._should_print(
        stage="cache", message="cache hit", level="INFO", always=False
    ) is True
    assert observer._should_print(
        stage="extract", message="PDF page extracted", level="INFO", always=False
    ) is True
    observer.close()
