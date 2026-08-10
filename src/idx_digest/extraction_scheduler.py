from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Deque

from .observability import RunObserver


@dataclass(frozen=True)
class ExtractionJob:
    job_id: str
    ticker: str
    announcement_id: str
    func: Callable[[], Any]
    on_complete: Callable[[Any | None, BaseException | None], None] | None = None


class BoundedExtractionScheduler:
    """Bounded, ticker-fair background extraction pool with queue telemetry."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_inflight: int,
        max_per_ticker: int = 2,
        observer: RunObserver | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if max_inflight < max_workers:
            raise ValueError("max_inflight must be >= max_workers")
        if max_per_ticker < 1:
            raise ValueError("max_per_ticker must be >= 1")
        self.max_workers = int(max_workers)
        self.max_inflight = int(max_inflight)
        self.max_per_ticker = min(int(max_per_ticker), self.max_workers)
        self.observer = observer
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="idx-extract")
        self._condition = threading.Condition(threading.RLock())
        self._pending: OrderedDict[str, Deque[ExtractionJob]] = OrderedDict()
        self._active_total = 0
        self._active_by_ticker: dict[str, int] = {}
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._callbacks_inflight = 0
        self._closed = False
        self._max_observed_inflight = 0
        self._max_observed_active = 0
        self._max_observed_pending = 0
        self._queued_at: dict[str, float] = {}
        self._queue_wait_total = 0.0
        self._queue_wait_samples = 0
        self._queue_wait_max = 0.0
        self._backpressure_events = 0
        self._backpressure_wait_seconds = 0.0

    @property
    def metrics(self) -> dict[str, Any]:
        with self._condition:
            pending = sum(len(q) for q in self._pending.values())
            average_wait = self._queue_wait_total / self._queue_wait_samples if self._queue_wait_samples else 0.0
            return {
                "max_workers": self.max_workers,
                "max_inflight": self.max_inflight,
                "max_per_ticker": self.max_per_ticker,
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "active": self._active_total,
                "pending": pending,
                "callbacks_inflight": self._callbacks_inflight,
                "active_by_ticker": dict(self._active_by_ticker),
                "pending_by_ticker": {ticker: len(queue) for ticker, queue in self._pending.items() if queue},
                "max_observed_inflight": self._max_observed_inflight,
                "max_observed_active": self._max_observed_active,
                "max_observed_pending": self._max_observed_pending,
                "queue_wait_samples": self._queue_wait_samples,
                "average_queue_wait_seconds": round(average_wait, 3),
                "max_queue_wait_seconds": round(self._queue_wait_max, 3),
                "backpressure_events": self._backpressure_events,
                "backpressure_wait_seconds": round(self._backpressure_wait_seconds, 3),
                "peak_utilization": round(self._max_observed_active / self.max_workers, 3),
            }

    def _inflight_locked(self) -> int:
        return self._active_total + sum(len(q) for q in self._pending.values())

    def submit(self, job: ExtractionJob) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("extraction scheduler is closed")
            backpressure_started: float | None = None
            while self._inflight_locked() >= self.max_inflight:
                if backpressure_started is None:
                    backpressure_started = time.perf_counter()
                    self._backpressure_events += 1
                if self.observer:
                    self.observer.event(
                        "extract-queue",
                        "extraction backlog full; applying producer backpressure",
                        ticker=job.ticker,
                        announcement_id=job.announcement_id,
                        inflight=self._inflight_locked(),
                        limit=self.max_inflight,
                        active=self._active_total,
                        queue_depth=sum(len(q) for q in self._pending.values()),
                    )
                self._condition.wait(timeout=0.2)
                if self._closed:
                    raise RuntimeError("extraction scheduler closed while waiting for capacity")
            if backpressure_started is not None:
                self._backpressure_wait_seconds += time.perf_counter() - backpressure_started
            self._pending.setdefault(job.ticker, deque()).append(job)
            self._queued_at[job.job_id] = time.perf_counter()
            self._submitted += 1
            inflight = self._inflight_locked()
            pending = sum(len(q) for q in self._pending.values())
            self._max_observed_inflight = max(self._max_observed_inflight, inflight)
            self._max_observed_pending = max(self._max_observed_pending, pending)
            if self.observer:
                self.observer.event(
                    "extract-queue",
                    "extraction job queued",
                    ticker=job.ticker,
                    announcement_id=job.announcement_id,
                    job_id=job.job_id,
                    queue_depth=pending,
                    active=self._active_total,
                    workers=self.max_workers,
                    backlog_limit=self.max_inflight,
                    max_observed_inflight=self._max_observed_inflight,
                )
            self._dispatch_locked()
            self._condition.notify_all()

    def _next_ticker_locked(self) -> str | None:
        if not self._pending:
            return None
        for _ in range(len(self._pending)):
            ticker, queue = next(iter(self._pending.items()))
            self._pending.move_to_end(ticker)
            if not queue:
                self._pending.pop(ticker, None)
                continue
            if self._active_by_ticker.get(ticker, 0) < self.max_per_ticker:
                return ticker
        return None

    def _dispatch_locked(self) -> None:
        while self._active_total < self.max_workers:
            ticker = self._next_ticker_locked()
            if ticker is None:
                return
            queue = self._pending[ticker]
            job = queue.popleft()
            if not queue:
                self._pending.pop(ticker, None)
            queued_at = self._queued_at.pop(job.job_id, None)
            queue_wait = max(0.0, time.perf_counter() - queued_at) if queued_at is not None else 0.0
            self._queue_wait_total += queue_wait
            self._queue_wait_samples += 1
            self._queue_wait_max = max(self._queue_wait_max, queue_wait)
            self._active_total += 1
            self._max_observed_active = max(self._max_observed_active, self._active_total)
            self._active_by_ticker[ticker] = self._active_by_ticker.get(ticker, 0) + 1
            if self.observer:
                self.observer.event(
                    "extract-queue",
                    "extraction job dispatched",
                    ticker=ticker,
                    announcement_id=job.announcement_id,
                    job_id=job.job_id,
                    active=self._active_total,
                    queue_depth=sum(len(q) for q in self._pending.values()),
                    queue_wait_seconds=f"{queue_wait:.3f}",
                    ticker_active=self._active_by_ticker[ticker],
                    workers=self.max_workers,
                    backlog_limit=self.max_inflight,
                    max_observed_active=self._max_observed_active,
                )
            future = self._executor.submit(job.func)
            future.add_done_callback(lambda done, scheduled=job: self._done(scheduled, done))

    def _done(self, job: ExtractionJob, future: Future[Any]) -> None:
        value: Any | None = None
        error: BaseException | None = None
        try:
            value = future.result()
        except BaseException as exc:
            error = exc

        with self._condition:
            self._active_total -= 1
            remaining = self._active_by_ticker.get(job.ticker, 0) - 1
            if remaining > 0:
                self._active_by_ticker[job.ticker] = remaining
            else:
                self._active_by_ticker.pop(job.ticker, None)
            self._completed += 1
            if error is not None:
                self._failed += 1
            self._callbacks_inflight += 1
            if self.observer:
                self.observer.event(
                    "extract-queue",
                    "extraction job finished",
                    level="ERROR" if error else "INFO",
                    ticker=job.ticker,
                    announcement_id=job.announcement_id,
                    job_id=job.job_id,
                    error=str(error) if error else None,
                    active=self._active_total,
                    queue_depth=sum(len(q) for q in self._pending.values()),
                    completed=self._completed,
                    failed=self._failed,
                    workers=self.max_workers,
                    backlog_limit=self.max_inflight,
                )

        if job.on_complete is not None:
            try:
                job.on_complete(value, error)
            except BaseException as callback_exc:
                if self.observer:
                    self.observer.event(
                        "extract-queue",
                        "extraction completion hook failed",
                        level="ERROR",
                        always=True,
                        ticker=job.ticker,
                        announcement_id=job.announcement_id,
                        job_id=job.job_id,
                        error=str(callback_exc),
                    )

        with self._condition:
            self._callbacks_inflight -= 1
            self._dispatch_locked()
            self._condition.notify_all()

    def wait(self) -> dict[str, Any]:
        with self._condition:
            while self._active_total or self._callbacks_inflight or any(self._pending.values()):
                self._condition.wait(timeout=0.2)
            return self.metrics

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
        self.wait()
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._executor.shutdown(wait=True, cancel_futures=False)
