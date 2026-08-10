from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .observability import RunObserver


@dataclass(frozen=True)
class ProviderLease:
    waited_seconds: float
    limit_at_acquire: int
    request_class: str = "priority"


class AdaptiveProviderGate:
    """Adaptive cap around actual OpenRouter HTTP requests with live telemetry."""

    def __init__(
        self,
        *,
        configured_max: int,
        enabled: bool = True,
        observer: RunObserver | None = None,
        healthy_successes_per_step: int = 6,
    ) -> None:
        if configured_max < 1:
            raise ValueError("configured_max must be >= 1")
        self.configured_max = int(configured_max)
        self.enabled = bool(enabled)
        self.observer = observer
        self.healthy_successes_per_step = max(2, int(healthy_successes_per_step))
        self._condition = threading.Condition(threading.RLock())
        self._current_limit = self.configured_max
        self._active = 0
        self._max_observed_active = 0
        self._healthy_streak = 0
        self._throttle_events = 0
        self._transient_failure_events = 0
        self._ramp_events = 0
        self._waited_requests = 0
        self._total_wait_seconds = 0.0
        self._success_count = 0
        self._failure_count = 0
        self._latency_total = 0.0
        self._latency_max = 0.0
        self._latency_last = 0.0
        self._latency_ewma = 0.0
        self._waiting_priority = 0
        self._waiting_bulk = 0

    @property
    def metrics(self) -> dict[str, Any]:
        with self._condition:
            average_latency = self._latency_total / self._success_count if self._success_count else 0.0
            return {
                "enabled": self.enabled,
                "configured_max": self.configured_max,
                "current_limit": self._current_limit,
                "active": self._active,
                "max_observed_active": self._max_observed_active,
                "healthy_streak": self._healthy_streak,
                "throttle_events": self._throttle_events,
                "transient_failure_events": self._transient_failure_events,
                "ramp_events": self._ramp_events,
                "waited_requests": self._waited_requests,
                "total_wait_seconds": round(self._total_wait_seconds, 3),
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "average_latency_seconds": round(average_latency, 3),
                "ewma_latency_seconds": round(self._latency_ewma, 3),
                "max_latency_seconds": round(self._latency_max, 3),
                "last_latency_seconds": round(self._latency_last, 3),
                "peak_utilization": round(self._max_observed_active / self.configured_max, 3),
                "waiting_priority": self._waiting_priority,
                "waiting_bulk": self._waiting_bulk,
                "policy": "adaptive-provider-gate+priority-yield" if self.enabled else "fixed-provider-gate+priority-yield",
            }

    def acquire(self, *, request_class: str = "priority") -> ProviderLease:
        """Acquire a provider slot while preventing bulk chunk starvation.

        ``bulk_chunk`` requests may use every idle slot when nobody else is
        waiting. Once foreground work (single documents, reducers, combines) is
        queued, bulk chunks yield newly released slots until that foreground
        waiter has entered the provider. Active requests are never preempted.
        """
        request_class = "bulk_chunk" if request_class == "bulk_chunk" else "priority"
        is_bulk = request_class == "bulk_chunk"
        started = time.perf_counter()
        with self._condition:
            if is_bulk:
                self._waiting_bulk += 1
            else:
                self._waiting_priority += 1
            try:
                while True:
                    limit = self._current_limit if self.enabled else self.configured_max
                    slot_available = self._active < limit
                    priority_has_precedence = is_bulk and self._waiting_priority > 0
                    if slot_available and not priority_has_precedence:
                        break
                    self._condition.wait(timeout=0.5)
                waited = time.perf_counter() - started
                if is_bulk:
                    self._waiting_bulk = max(0, self._waiting_bulk - 1)
                else:
                    self._waiting_priority = max(0, self._waiting_priority - 1)
                self._active += 1
                self._max_observed_active = max(self._max_observed_active, self._active)
                if waited >= 0.01:
                    self._waited_requests += 1
                    self._total_wait_seconds += waited
                lease = ProviderLease(waited_seconds=waited, limit_at_acquire=limit, request_class=request_class)
                if self.observer:
                    self.observer.event(
                        "provider-gate",
                        "provider request slot acquired",
                        active=self._active,
                        current_limit=limit,
                        configured_max=self.configured_max,
                        waited_seconds=f"{waited:.3f}",
                        request_class=request_class,
                        waiting_priority=self._waiting_priority,
                        waiting_bulk=self._waiting_bulk,
                        max_observed_active=self._max_observed_active,
                    )
                return lease
            except BaseException:
                if is_bulk:
                    self._waiting_bulk = max(0, self._waiting_bulk - 1)
                else:
                    self._waiting_priority = max(0, self._waiting_priority - 1)
                self._condition.notify_all()
                raise

    def _finish_active_locked(self) -> None:
        self._active = max(0, self._active - 1)
        self._condition.notify_all()

    def _record_latency_locked(self, elapsed_seconds: float) -> None:
        latency = max(0.0, float(elapsed_seconds))
        self._latency_last = latency
        self._latency_total += latency
        self._latency_max = max(self._latency_max, latency)
        self._latency_ewma = latency if self._latency_ewma <= 0 else (0.22 * latency + 0.78 * self._latency_ewma)

    def record_success(self, lease: ProviderLease, *, elapsed_seconds: float) -> None:
        with self._condition:
            self._finish_active_locked()
            self._success_count += 1
            self._record_latency_locked(elapsed_seconds)
            if self.enabled:
                self._healthy_streak += 1
                threshold = self.healthy_successes_per_step * max(1, self._current_limit)
                if self._current_limit < self.configured_max and self._healthy_streak >= threshold:
                    old = self._current_limit
                    self._current_limit += 1
                    self._healthy_streak = 0
                    self._ramp_events += 1
                    if self.observer:
                        self.observer.event(
                            "provider-gate",
                            "provider concurrency ramped up after healthy responses",
                            always=True,
                            old_limit=old,
                            new_limit=self._current_limit,
                            configured_max=self.configured_max,
                            elapsed_seconds=f"{elapsed_seconds:.3f}",
                        )
            if self.observer:
                average = self._latency_total / self._success_count if self._success_count else 0.0
                self.observer.event(
                    "provider-gate",
                    "provider request slot released",
                    active=self._active,
                    current_limit=self._current_limit if self.enabled else self.configured_max,
                    configured_max=self.configured_max,
                    elapsed_seconds=f"{elapsed_seconds:.3f}",
                    average_latency_seconds=f"{average:.3f}",
                    ewma_latency_seconds=f"{self._latency_ewma:.3f}",
                    success_count=self._success_count,
                    failure_count=self._failure_count,
                    waiting_priority=self._waiting_priority,
                    waiting_bulk=self._waiting_bulk,
                )
            self._condition.notify_all()

    def record_failure(
        self,
        lease: ProviderLease,
        *,
        status_code: int | None = None,
        transient: bool = False,
        error: str | None = None,
    ) -> None:
        with self._condition:
            self._finish_active_locked()
            self._failure_count += 1
            old = self._current_limit
            self._healthy_streak = 0
            reason = "request failure"
            if self.enabled and status_code == 429:
                self._throttle_events += 1
                self._current_limit = max(1, self._current_limit // 2)
                reason = "HTTP 429 throttling"
            elif self.enabled and (transient or (status_code is not None and status_code >= 500)):
                self._transient_failure_events += 1
                self._current_limit = max(1, self._current_limit - 1)
                reason = f"HTTP {status_code}" if status_code is not None else "transport/timeout failure"
            if self._current_limit != old and self.observer:
                self.observer.event(
                    "provider-gate",
                    "provider concurrency reduced",
                    level="WARNING",
                    always=True,
                    reason=reason,
                    old_limit=old,
                    new_limit=self._current_limit,
                    configured_max=self.configured_max,
                    error=(error or "")[:300] or None,
                )
            if self.observer:
                self.observer.event(
                    "provider-gate",
                    "provider request slot released after failure",
                    level="WARNING",
                    active=self._active,
                    current_limit=self._current_limit if self.enabled else self.configured_max,
                    configured_max=self.configured_max,
                    status_code=status_code,
                    transient=transient,
                    success_count=self._success_count,
                    failure_count=self._failure_count,
                    waiting_priority=self._waiting_priority,
                    waiting_bulk=self._waiting_bulk,
                )
            self._condition.notify_all()
