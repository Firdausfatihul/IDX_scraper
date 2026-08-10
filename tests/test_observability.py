from __future__ import annotations

import json
import time

from idx_digest.observability import RunObserver


def test_timestamped_jsonl_and_slowdown_report(tmp_path) -> None:
    log_path = tmp_path / "run.jsonl"
    observer = RunObserver(
        timezone_name="Asia/Jakarta",
        verbose=True,
        show_progress=False,
        log_file=log_path,
    )
    observer.event("test", "hello", value=7)
    with observer.timed("stage", "small operation"):
        time.sleep(0.001)
    report = observer.slowdown_report()
    observer.close()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["timestamp"].endswith("+07:00")
    assert rows[0]["stage"] == "test"
    assert report["total_elapsed_seconds"] >= 0
    assert report["slowest_stages"][0]["stage"] == "stage"
