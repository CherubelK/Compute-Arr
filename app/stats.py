"""
In-memory rolling stats per (provider, gpu_class).
Fed by the poller (availability) and the job monitor (provision times); used by
the router for reliable/fastest ranking.
Resets on restart — acceptable for Phase 1.
"""
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field

# Provision-time samples kept per (provider, gpu_class).
_PROVISION_WINDOW = 20


@dataclass
class _ProviderGpuStats:
    poll_available: int = 0
    poll_total: int = 0
    provision_seconds: deque[float] = field(default_factory=lambda: deque(maxlen=_PROVISION_WINDOW))

    @property
    def availability_rate(self) -> float:
        return self.poll_available / self.poll_total if self.poll_total else 1.0


class StatsTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], _ProviderGpuStats] = defaultdict(_ProviderGpuStats)

    def record_poll(self, provider_id: str, gpu_class: str, available: bool) -> None:
        with self._lock:
            s = self._data[(provider_id, gpu_class)]
            s.poll_total += 1
            if available:
                s.poll_available += 1

    def availability_rate(self, provider_id: str, gpu_class: str) -> float:
        with self._lock:
            return self._data[(provider_id, gpu_class)].availability_rate

    def record_provision(self, provider_id: str, gpu_class: str, seconds: float) -> None:
        """Record how long a job took to go from placed to running."""
        with self._lock:
            self._data[(provider_id, gpu_class)].provision_seconds.append(seconds)

    def avg_provision_seconds(self, provider_id: str, gpu_class: str) -> float | None:
        """Mean of the recent provision times, or None if none have been observed."""
        with self._lock:
            samples = self._data[(provider_id, gpu_class)].provision_seconds
            return sum(samples) / len(samples) if samples else None


stats_tracker = StatsTracker()
