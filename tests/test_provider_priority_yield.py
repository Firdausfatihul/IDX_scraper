from __future__ import annotations

import threading
import time

from idx_digest.provider_gate import AdaptiveProviderGate


def _wait_until(predicate, timeout: float = 1.5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def test_bulk_chunks_yield_newly_freed_slot_to_foreground_request() -> None:
    gate = AdaptiveProviderGate(configured_max=2, enabled=False)
    first = gate.acquire(request_class="bulk_chunk")
    second = gate.acquire(request_class="bulk_chunk")

    acquired: list[tuple[str, object]] = []
    lock = threading.Lock()
    priority_ready = threading.Event()
    bulk_ready = threading.Event()

    def priority_waiter() -> None:
        lease = gate.acquire(request_class="priority")
        with lock:
            acquired.append(("priority", lease))
        priority_ready.set()

    def bulk_waiter() -> None:
        lease = gate.acquire(request_class="bulk_chunk")
        with lock:
            acquired.append(("bulk", lease))
        bulk_ready.set()

    bulk_thread = threading.Thread(target=bulk_waiter)
    priority_thread = threading.Thread(target=priority_waiter)
    bulk_thread.start()
    priority_thread.start()
    _wait_until(lambda: gate.metrics["waiting_bulk"] == 1 and gate.metrics["waiting_priority"] == 1)

    gate.record_success(first, elapsed_seconds=0.01)
    assert priority_ready.wait(0.5)
    assert not bulk_ready.is_set()
    assert acquired[0][0] == "priority"

    priority_lease = acquired[0][1]
    gate.record_success(priority_lease, elapsed_seconds=0.01)  # type: ignore[arg-type]
    assert bulk_ready.wait(0.5)
    bulk_lease = next(lease for name, lease in acquired if name == "bulk")
    gate.record_success(bulk_lease, elapsed_seconds=0.01)  # type: ignore[arg-type]
    gate.record_success(second, elapsed_seconds=0.01)
    bulk_thread.join(timeout=1)
    priority_thread.join(timeout=1)

    assert gate.metrics["active"] == 0
    assert gate.metrics["waiting_priority"] == 0
    assert gate.metrics["waiting_bulk"] == 0
