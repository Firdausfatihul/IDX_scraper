from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any


@dataclass(frozen=True)
class NetworkSnapshot:
    offline: bool
    offline_since_monotonic: float | None
    outage_count: int
    recovery_count: int
    probe_failures: int


class NetworkWatchdog:
    """Small connectivity gate used only after a transport-looking failure.

    It does not reduce LLM token/output limits. It prevents retry storms while the
    laptop is offline and makes Wi-Fi loss/recovery explicit in the run stream.
    """

    def __init__(self, base_url: str, *, enabled: bool = True, observer: Any = None, probe_interval: float = 3.0, probe_timeout: float = 2.0):
        parsed = urlparse(base_url)
        self.host = parsed.hostname or "openrouter.ai"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.enabled = enabled
        self.observer = observer
        self.probe_interval = max(0.5, float(probe_interval))
        self.probe_timeout = max(0.2, float(probe_timeout))
        self._condition = threading.Condition()
        self._offline = False
        self._offline_since: float | None = None
        self._outage_count = 0
        self._recovery_count = 0
        self._probe_failures = 0
        self._last_activity_wall = time.time()

    @staticmethod
    def _looks_transport_failure(exc: BaseException) -> bool:
        text = str(exc).lower()
        return any(token in text for token in (
            "timed out", "timeout", "network", "connection", "connecterror",
            "readerror", "writeerror", "name or service", "temporary failure",
            "nodename nor servname", "network is unreachable",
        ))

    def _event(self, message: str, **fields: Any) -> None:
        if self.observer:
            self.observer.event("network", message, always=True, **fields)

    def record_failure(self, exc: BaseException) -> None:
        if not self.enabled or not self._looks_transport_failure(exc):
            return
        with self._condition:
            if not self._offline:
                self._offline = True
                self._offline_since = time.monotonic()
                self._outage_count += 1
                self._event("network connectivity degraded; retries will wait for a successful probe", level="WARNING", error=str(exc), host=self.host)
            self._condition.notify_all()

    def record_success(self) -> None:
        with self._condition:
            self._last_activity_wall = time.time()
            if self._offline:
                elapsed = time.monotonic() - (self._offline_since or time.monotonic())
                self._offline = False
                self._offline_since = None
                self._recovery_count += 1
                self._event("network connectivity restored", outage_seconds=round(max(0.0, elapsed), 3), host=self.host)
            self._condition.notify_all()

    def _probe(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.probe_timeout):
                return True
        except OSError:
            self._probe_failures += 1
            return False

    def before_request(self) -> None:
        if not self.enabled:
            return
        # A long wall-clock gap often means laptop sleep. Probe before the next
        # provider request instead of assuming pre-sleep sockets are healthy.
        wall_gap = time.time() - self._last_activity_wall
        with self._condition:
            should_probe = self._offline or wall_gap >= 120
        if wall_gap >= 120:
            self._event("long client inactivity detected; checking connectivity before provider request", inactivity_seconds=round(wall_gap, 1))
        if not should_probe:
            self._last_activity_wall = time.time()
            return

        announced_wait = False
        while True:
            if self._probe():
                self.record_success()
                return
            with self._condition:
                if not self._offline:
                    self._offline = True
                    self._offline_since = time.monotonic()
                    self._outage_count += 1
                if not announced_wait:
                    announced_wait = True
                    self._event("NETWORK OFFLINE; provider requests paused until connectivity returns", level="WARNING", host=self.host)
            time.sleep(self.probe_interval)

    @property
    def metrics(self) -> dict[str, Any]:
        with self._condition:
            return {
                "offline": self._offline,
                "outage_count": self._outage_count,
                "recovery_count": self._recovery_count,
                "probe_failures": self._probe_failures,
            }
