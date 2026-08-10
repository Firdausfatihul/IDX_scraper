from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Deque, Generic, TypeVar

from .observability import RunObserver

T = TypeVar("T")


@dataclass(frozen=True)
class ScheduledJob(Generic[T]):
    job_id: str
    group_key: str
    stage: str
    ticker: str
    func: Callable[[], T]
    on_complete: Callable[[T | None, BaseException | None], None] | None = None
    announcement_id: str | None = None


class GlobalLLMScheduler:
    """Market-wide LLM scheduler with fairness, priority, and telemetry."""

    STAGE_WEIGHTS = {"document": 3, "announcement": 2, "company": 1}

    def __init__(self, *, max_workers: int, max_per_group: int, max_per_ticker: int | None = None, observer: RunObserver | None = None) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if max_per_group < 1:
            raise ValueError("max_per_group must be >= 1")
        self.max_workers = int(max_workers)
        self.max_per_group = min(int(max_per_group), self.max_workers)
        default_ticker_cap = min(self.max_workers, max(2, self.max_per_group))
        self.max_per_ticker = min(int(max_per_ticker or default_ticker_cap), self.max_workers)
        self.observer = observer
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="idx-global-llm")
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._pending: OrderedDict[str, OrderedDict[str, Deque[ScheduledJob[Any]]]] = OrderedDict()
        self._active_by_group: dict[str, int] = {}
        self._active_by_ticker: dict[str, int] = {}
        self._active_by_stage: dict[str, int] = {}
        self._active_total = 0
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._callbacks_inflight = 0
        self._closed = False
        self._dispatch_by_stage: dict[str, int] = {}
        self._stage_cycle = [stage for stage, weight in self.STAGE_WEIGHTS.items() for _ in range(weight)]
        self._stage_cursor = 0
        self._queued_at: dict[str, float] = {}
        self._queue_wait_total = 0.0
        self._queue_wait_samples = 0
        self._queue_wait_max = 0.0
        self._max_observed_active = 0
        self._max_observed_pending = 0

    def _pending_count_locked(self) -> int:
        return sum(len(queue) for groups in self._pending.values() for queue in groups.values())

    def _pending_by_stage_locked(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for groups in self._pending.values():
            for queue in groups.values():
                for job in queue:
                    counts[job.stage] = counts.get(job.stage, 0) + 1
        return counts

    def _pending_by_ticker_locked(self) -> dict[str, int]:
        return {
            ticker: sum(len(queue) for queue in groups.values())
            for ticker, groups in self._pending.items()
            if groups
        }

    @property
    def metrics(self) -> dict[str, Any]:
        with self._lock:
            average_wait = self._queue_wait_total / self._queue_wait_samples if self._queue_wait_samples else 0.0
            return {
                "max_workers": self.max_workers,
                "max_per_group": self.max_per_group,
                "max_per_ticker": self.max_per_ticker,
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "active": self._active_total,
                "pending": self._pending_count_locked(),
                "callbacks_inflight": self._callbacks_inflight,
                "active_tickers": len(self._active_by_ticker),
                "pending_by_stage": self._pending_by_stage_locked(),
                "active_by_stage": dict(self._active_by_stage),
                "dispatch_by_stage": dict(self._dispatch_by_stage),
                "active_by_ticker": dict(self._active_by_ticker),
                "pending_by_ticker": self._pending_by_ticker_locked(),
                "max_observed_active": self._max_observed_active,
                "max_observed_pending": self._max_observed_pending,
                "queue_wait_samples": self._queue_wait_samples,
                "average_queue_wait_seconds": round(average_wait, 3),
                "max_queue_wait_seconds": round(self._queue_wait_max, 3),
                "peak_utilization": round(self._max_observed_active / self.max_workers, 3),
                "policy": "ticker-round-robin+weighted-stage-priority",
            }

    def submit(self, job: ScheduledJob[Any]) -> None:
        ticker = (job.ticker or "UNKNOWN").strip().upper()
        with self._condition:
            if self._closed:
                raise RuntimeError("scheduler is closed")
            groups = self._pending.setdefault(ticker, OrderedDict())
            groups.setdefault(job.group_key, deque()).append(job)
            self._queued_at[job.job_id] = time.perf_counter()
            self._submitted += 1
            pending = self._pending_count_locked()
            self._max_observed_pending = max(self._max_observed_pending, pending)
            if self.observer:
                self.observer.event(
                    "scheduler", "LLM job queued",
                    stage_name=job.stage, job_id=job.job_id, ticker=ticker,
                    announcement_id=job.announcement_id, group_key=job.group_key,
                    queue_depth=pending, active=self._active_total,
                    global_limit=self.max_workers, per_group_limit=self.max_per_group,
                    per_ticker_limit=self.max_per_ticker,
                    max_observed_pending=self._max_observed_pending,
                    scheduling_policy="ticker-round-robin+weighted-stage-priority",
                )
            self._dispatch_locked()
            self._condition.notify_all()

    def _find_job_for_stage_locked(self, stage: str) -> tuple[str, str, ScheduledJob[Any]] | None:
        if not self._pending:
            return None
        for _ in range(len(self._pending)):
            ticker, groups = next(iter(self._pending.items()))
            self._pending.move_to_end(ticker)
            if not groups:
                self._pending.pop(ticker, None)
                continue
            if self._active_by_ticker.get(ticker, 0) >= self.max_per_ticker:
                continue
            for _ in range(len(groups)):
                group_key, queue = next(iter(groups.items()))
                groups.move_to_end(group_key)
                if not queue:
                    groups.pop(group_key, None)
                    continue
                if self._active_by_group.get(group_key, 0) >= self.max_per_group:
                    continue
                if queue[0].stage != stage:
                    continue
                job = queue.popleft()
                if not queue:
                    groups.pop(group_key, None)
                if not groups:
                    self._pending.pop(ticker, None)
                return ticker, group_key, job
        return None

    def _next_job_locked(self) -> tuple[str, str, ScheduledJob[Any]] | None:
        if not self._pending:
            return None
        cycle_len = len(self._stage_cycle)
        for offset in range(cycle_len):
            idx = (self._stage_cursor + offset) % cycle_len
            stage = self._stage_cycle[idx]
            found = self._find_job_for_stage_locked(stage)
            if found is not None:
                self._stage_cursor = (idx + 1) % cycle_len
                return found
        seen_stages = set(self._stage_cycle)
        for ticker, groups in list(self._pending.items()):
            if self._active_by_ticker.get(ticker, 0) >= self.max_per_ticker:
                continue
            for group_key, queue in list(groups.items()):
                if queue and queue[0].stage not in seen_stages and self._active_by_group.get(group_key, 0) < self.max_per_group:
                    job = queue.popleft()
                    if not queue:
                        groups.pop(group_key, None)
                    if not groups:
                        self._pending.pop(ticker, None)
                    return ticker, group_key, job
        return None

    def _dispatch_locked(self) -> None:
        while self._active_total < self.max_workers:
            selected = self._next_job_locked()
            if selected is None:
                return
            ticker, group_key, job = selected
            queued_at = self._queued_at.pop(job.job_id, None)
            queue_wait = max(0.0, time.perf_counter() - queued_at) if queued_at is not None else 0.0
            self._queue_wait_total += queue_wait
            self._queue_wait_samples += 1
            self._queue_wait_max = max(self._queue_wait_max, queue_wait)
            self._active_total += 1
            self._max_observed_active = max(self._max_observed_active, self._active_total)
            self._active_by_group[group_key] = self._active_by_group.get(group_key, 0) + 1
            self._active_by_ticker[ticker] = self._active_by_ticker.get(ticker, 0) + 1
            self._active_by_stage[job.stage] = self._active_by_stage.get(job.stage, 0) + 1
            self._dispatch_by_stage[job.stage] = self._dispatch_by_stage.get(job.stage, 0) + 1
            if self.observer:
                self.observer.event(
                    "scheduler", "LLM job dispatched",
                    stage_name=job.stage, job_id=job.job_id, ticker=ticker,
                    announcement_id=job.announcement_id, active=self._active_total,
                    queue_depth=self._pending_count_locked(),
                    queue_wait_seconds=f"{queue_wait:.3f}",
                    ticker_active=self._active_by_ticker[ticker],
                    group_active=self._active_by_group[group_key], global_limit=self.max_workers,
                    per_group_limit=self.max_per_group, per_ticker_limit=self.max_per_ticker,
                    max_observed_active=self._max_observed_active,
                )
            future = self._executor.submit(job.func)
            future.add_done_callback(lambda done, scheduled=job, tick=ticker: self._done(scheduled, tick, done))

    def _done(self, job: ScheduledJob[Any], ticker: str, future: Future[Any]) -> None:
        value: Any | None = None
        error: BaseException | None = None
        try:
            value = future.result()
        except BaseException as exc:
            error = exc

        with self._condition:
            self._active_total -= 1
            group_remaining = self._active_by_group.get(job.group_key, 0) - 1
            if group_remaining > 0:
                self._active_by_group[job.group_key] = group_remaining
            else:
                self._active_by_group.pop(job.group_key, None)
            ticker_remaining = self._active_by_ticker.get(ticker, 0) - 1
            if ticker_remaining > 0:
                self._active_by_ticker[ticker] = ticker_remaining
            else:
                self._active_by_ticker.pop(ticker, None)
            stage_remaining = self._active_by_stage.get(job.stage, 0) - 1
            if stage_remaining > 0:
                self._active_by_stage[job.stage] = stage_remaining
            else:
                self._active_by_stage.pop(job.stage, None)
            self._completed += 1
            if error is not None:
                self._failed += 1
            self._callbacks_inflight += 1
            if self.observer:
                self.observer.event(
                    "scheduler", "LLM job finished",
                    level="ERROR" if error is not None else "INFO",
                    stage_name=job.stage, job_id=job.job_id, ticker=ticker,
                    announcement_id=job.announcement_id,
                    error=str(error) if error is not None else None,
                    active=self._active_total, queue_depth=self._pending_count_locked(),
                    completed=self._completed, failed=self._failed,
                    global_limit=self.max_workers,
                )

        if job.on_complete is not None:
            try:
                job.on_complete(value, error)
            except BaseException as callback_exc:
                if self.observer:
                    self.observer.event(
                        "scheduler", "LLM completion hook failed", level="ERROR", always=True,
                        stage_name=job.stage, job_id=job.job_id, ticker=ticker,
                        announcement_id=job.announcement_id, error=str(callback_exc),
                    )

        with self._condition:
            self._callbacks_inflight -= 1
            self._dispatch_locked()
            self._condition.notify_all()

    def wait(self) -> dict[str, Any]:
        with self._condition:
            while self._active_total or self._callbacks_inflight or self._pending_count_locked():
                self._condition.wait(timeout=0.2)
            return self.metrics

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
        self.wait()
        with self._condition:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def __enter__(self) -> "GlobalLLMScheduler":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
