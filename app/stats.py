"""
In-memory rolling stats per (provider, gpu_class).
Updated by the poller on every poll cycle; used by the router for reliable/fastest ranking.
Resets on restart — acceptable for Phase 1.
"""
import threading
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class _ProviderGpuStats:
    poll_available: int = 0
    poll_total: int = 0

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


stats_tracker = StatsTracker()
