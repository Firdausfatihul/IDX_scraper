from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn


def _format_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(max(value, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


@dataclass(frozen=True)
class TimingRecord:
    stage: str
    label: str
    elapsed_seconds: float
    fields: dict[str, Any]


class RunObserver:
    """Timestamped diagnostics, progress bars, streaming output, and timing data.

    Console output is written to stderr so the final JSON report on stdout stays
    machine-readable. When enabled, the same events are appended as JSON Lines
    to a timestamped log file.
    """

    def __init__(
        self,
        *,
        timezone_name: str,
        verbose: bool = False,
        trace_browser: bool = False,
        browser_network: bool = False,
        stream_llm: bool = False,
        show_progress: bool = True,
        show_cache_events: bool = False,
        show_page_events: bool = False,
        log_file: Path | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        console_output: bool = True,
    ) -> None:
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)
        self.verbose = verbose
        self.trace_browser = trace_browser
        self.browser_network = browser_network
        self.stream_llm = stream_llm
        self.show_cache_events = show_cache_events
        self.show_page_events = show_page_events
        self.show_progress = show_progress and sys.stderr.isatty()
        self.log_file = Path(log_file) if log_file else None
        self.event_sink = event_sink
        self.console_output = console_output
        self.console = Console(stderr=True, highlight=False, soft_wrap=True)
        self.timings: list[TimingRecord] = []
        self.run_started_monotonic = time.perf_counter()
        self.run_started_at = self.now_iso()
        self._stream_open = False
        self._stream_buffer: list[str] = []
        self._progress_paused_for_stream = False
        self._log_handle: TextIO | None = None
        self._task_meta: dict[int, dict[str, Any]] = {}
        self._task_counter = 0
        self._lock = threading.RLock()

        self.progress: Progress | None = None
        if self.show_progress:
            self.progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(bar_width=None),
                TextColumn("{task.fields[status]}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=self.console,
                transient=True,
                refresh_per_second=6,
            )
            self.progress.start()

        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.log_file.open("a", encoding="utf-8")

    def now(self) -> datetime:
        return datetime.now(self.timezone)

    def now_iso(self) -> str:
        return self.now().isoformat(timespec="milliseconds")

    def clock(self) -> str:
        return self.now().strftime("%H:%M:%S.%f")[:-3]

    def _publish(self, payload: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(payload)
        except Exception:
            # Observability must never break scraping. GUI consumers are best-effort.
            return

    def _write_file(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if self._log_handle is None:
                return
            self._log_handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self._log_handle.flush()

    def _should_print(
        self,
        *,
        stage: str,
        message: str,
        level: str,
        always: bool,
    ) -> bool:
        if not self.console_output:
            return False
        if always or level in {"WARNING", "ERROR"}:
            return True
        if not self.verbose:
            return False
        if stage == "cache" and not self.show_cache_events:
            return False
        if stage == "browser" and message in {"network response", "network request failed"}:
            return self.browser_network
        if stage == "extract" and message == "PDF page extracted" and not self.show_page_events:
            return False
        return True

    def event(
        self,
        stage: str,
        message: str,
        *,
        level: str = "INFO",
        always: bool = False,
        **fields: Any,
    ) -> None:
        with self._lock:
            timestamp = self.now_iso()
            level_upper = level.upper()
            payload = {
                "type": "event",
                "timestamp": timestamp,
                "level": level_upper,
                "stage": stage,
                "message": message,
                "fields": fields,
            }
            self._write_file({key: value for key, value in payload.items() if key != "type"})
            self._publish(payload)
            if not self._should_print(
                stage=stage,
                message=message,
                level=level_upper,
                always=always,
            ):
                return
            field_text = " ".join(
                f"{key}={value}" for key, value in fields.items() if value is not None
            )
            line = f"[{timestamp}] {level_upper:<7} {stage}: {message}"
            if field_text:
                line += f" | {field_text}"
            if self.progress:
                self.progress.console.print(line)
            else:
                self.console.print(line)

    def browser(self, message: str, **fields: Any) -> None:
        if self.trace_browser or self.browser_network:
            self.event("browser", message, **fields)

    @contextmanager
    def timed(self, stage: str, label: str, **fields: Any) -> Iterator[None]:
        started = time.perf_counter()
        self.event(stage, f"START {label}", **fields)
        try:
            yield
        except Exception as exc:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.timings.append(TimingRecord(stage, label, elapsed, dict(fields)))
            self.event(
                stage,
                f"FAILED {label}",
                level="ERROR",
                always=True,
                elapsed_seconds=f"{elapsed:.3f}",
                error=str(exc),
                **fields,
            )
            raise
        else:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.timings.append(TimingRecord(stage, label, elapsed, dict(fields)))
            self.event(
                stage,
                f"DONE {label}",
                elapsed_seconds=f"{elapsed:.3f}",
                **fields,
            )

    def start_task(
        self,
        description: str,
        *,
        total: float | None = None,
        kind: str = "items",
    ) -> int | None:
        if not self.progress and self.event_sink is None:
            return None
        with self._lock:
            self._task_counter += 1
            task_id = self._task_counter
            rich_task_id = None
            if self.progress:
                rich_task_id = self.progress.add_task(
                    f"[{self.clock()}] {description}",
                    total=total,
                    status=self._task_status(kind=kind, completed=0, total=total, elapsed=0),
                )
            self._task_meta[task_id] = {
                "kind": kind,
                "description": description,
                "started": time.perf_counter(),
                "completed": 0.0,
                "total": total,
                "rich_task_id": rich_task_id,
            }
            self._publish({
                "type": "task",
                "action": "start",
                "task_id": task_id,
                "timestamp": self.now_iso(),
                "description": description,
                "kind": kind,
                "completed": 0.0,
                "total": total,
            })
            return task_id

    def _task_status(
        self,
        *,
        kind: str,
        completed: float,
        total: float | None,
        elapsed: float,
    ) -> str:
        if kind == "bytes":
            total_text = _format_bytes(total) if total is not None else "?"
            speed = completed / elapsed if elapsed > 0 else 0
            return f"{_format_bytes(completed)}/{total_text} • {_format_bytes(speed)}/s"
        if total is None:
            return f"{int(completed)}"
        return f"{int(completed)}/{int(total)}"

    def update_task(
        self,
        task_id: int | None,
        *,
        advance: float = 0,
        completed: float | None = None,
        total: float | None = None,
        description: str | None = None,
    ) -> None:
        if task_id is None:
            return
        meta = self._task_meta.get(task_id)
        if meta is None:
            return
        if completed is None:
            meta["completed"] = float(meta["completed"]) + advance
        else:
            meta["completed"] = float(completed)
        if total is not None:
            meta["total"] = float(total)
        elapsed = time.perf_counter() - float(meta["started"])
        kwargs: dict[str, Any] = {
            "completed": meta["completed"],
            "total": meta["total"],
            "status": self._task_status(
                kind=str(meta["kind"]),
                completed=float(meta["completed"]),
                total=meta["total"],
                elapsed=elapsed,
            ),
        }
        if description:
            meta["description"] = description
            kwargs["description"] = f"[{self.clock()}] {description}"
        rich_task_id = meta.get("rich_task_id")
        if self.progress is not None and rich_task_id is not None:
            self.progress.update(rich_task_id, **kwargs)
        self._publish({
            "type": "task",
            "action": "update",
            "task_id": task_id,
            "timestamp": self.now_iso(),
            "description": meta.get("description"),
            "kind": meta.get("kind"),
            "completed": meta.get("completed"),
            "total": meta.get("total"),
            "elapsed_seconds": round(elapsed, 3),
        })

    def finish_task(self, task_id: int | None, *, completed: float | None = None) -> None:
        if task_id is None:
            return
        meta = self._task_meta.get(task_id)
        if not meta:
            return
        if completed is not None:
            meta["completed"] = float(completed)
        if meta["total"] is None:
            meta["total"] = max(float(meta["completed"]), 1.0)
        self.update_task(
            task_id,
            completed=float(meta["completed"]),
            total=float(meta["total"]),
        )
        rich_task_id = meta.get("rich_task_id")
        if self.progress is not None and rich_task_id is not None:
            self.progress.update(rich_task_id, completed=float(meta["total"]))
            self.progress.remove_task(rich_task_id)
        elapsed = time.perf_counter() - float(meta["started"])
        self._publish({
            "type": "task",
            "action": "finish",
            "task_id": task_id,
            "timestamp": self.now_iso(),
            "description": meta.get("description"),
            "kind": meta.get("kind"),
            "completed": meta.get("total"),
            "total": meta.get("total"),
            "elapsed_seconds": round(elapsed, 3),
        })
        self._task_meta.pop(task_id, None)

    def begin_stream(self, label: str, **fields: Any) -> None:
        if not self.stream_llm:
            return
        self._stream_open = True
        self._stream_buffer = []
        if self.progress:
            self.progress.stop()
            self._progress_paused_for_stream = True
        timestamp = self.now_iso()
        line = f"[{timestamp}] STREAM  llm: {label}"
        if fields:
            line += " | " + " ".join(f"{key}={value}" for key, value in fields.items())
        if self.progress:
            self.progress.console.print(line)
        else:
            self.console.print(line)
        self._write_file(
            {
                "timestamp": timestamp,
                "level": "STREAM_START",
                "stage": "llm",
                "message": label,
                "fields": fields,
            }
        )

    def stream_chunk(self, chunk: str) -> None:
        if not self.stream_llm or not chunk:
            return
        target = self.progress.console if self.progress else self.console
        target.print(chunk, end="", markup=False, highlight=False, soft_wrap=True)
        self._stream_buffer.append(chunk)

    def end_stream(self, *, elapsed_seconds: float, characters: int) -> None:
        if not self.stream_llm:
            return
        target = self.progress.console if self.progress else self.console
        target.print()
        self._stream_open = False
        self._write_file(
            {
                "timestamp": self.now_iso(),
                "level": "STREAM_CONTENT",
                "stage": "llm",
                "content": "".join(self._stream_buffer),
            }
        )
        self._stream_buffer = []
        if self.progress and self._progress_paused_for_stream:
            self.progress.start()
            self._progress_paused_for_stream = False
        self.event(
            "llm",
            "stream completed",
            always=True,
            elapsed_seconds=f"{elapsed_seconds:.3f}",
            characters=characters,
        )

    def stage_timing_summary(self) -> dict[str, Any]:
        with self._lock:
            grouped: dict[str, list[float]] = {}
            for record in self.timings:
                grouped.setdefault(record.stage, []).append(float(record.elapsed_seconds))
        summary: dict[str, Any] = {}
        for stage, values in grouped.items():
            if not values:
                continue
            total = sum(values)
            summary[stage] = {
                "count": len(values),
                "total_seconds": round(total, 3),
                "average_seconds": round(total / len(values), 3),
                "max_seconds": round(max(values), 3),
            }
        return summary

    def slowdown_report(self, *, top_n: int = 5) -> dict[str, Any]:
        total_elapsed = time.perf_counter() - self.run_started_monotonic
        slowest = sorted(self.timings, key=lambda item: item.elapsed_seconds, reverse=True)[:top_n]
        if self.verbose:
            self.event(
                "timing",
                "SLOWEST STAGES",
                always=True,
                total_elapsed_seconds=f"{total_elapsed:.3f}",
            )
            for rank, record in enumerate(slowest, start=1):
                self.event(
                    "timing",
                    f"#{rank} {record.stage}: {record.label}",
                    always=True,
                    elapsed_seconds=f"{record.elapsed_seconds:.3f}",
                    **record.fields,
                )
        return {
            "total_elapsed_seconds": round(total_elapsed, 3),
            "slowest_stages": [
                {
                    "stage": record.stage,
                    "label": record.label,
                    "elapsed_seconds": round(record.elapsed_seconds, 3),
                    "fields": record.fields,
                }
                for record in slowest
            ],
        }

    def close(self) -> None:
        if self._stream_open:
            target = self.progress.console if self.progress else self.console
            target.print()
            self._stream_open = False
        if self.progress:
            self.progress.stop()
            self.progress = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
