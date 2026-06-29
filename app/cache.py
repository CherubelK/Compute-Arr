"""
In-memory offer cache. Populated by the poller; read by the router and GET /providers.
Thread-safe: the poller writes from a background thread; FastAPI reads from async workers.
"""
import threading
from dataclasses import dataclass, field
from datetime import datetime

from app.providers.base import Offer


@dataclass
class ProviderSnapshot:
    provider_id: str
    healthy: bool
    offers: list[Offer]
    polled_at: datetime


class OfferCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[str, ProviderSnapshot] = {}

    def update(self, snapshot: ProviderSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.provider_id] = snapshot

    def get_all(self) -> list[ProviderSnapshot]:
        with self._lock:
            return list(self._snapshots.values())

    def get_offers_for_gpu(self, gpu_class: str) -> list[Offer]:
        """Return all healthy-provider offers for a given GPU class."""
        with self._lock:
            result: list[Offer] = []
            for snap in self._snapshots.values():
                if snap.healthy:
                    result.extend(o for o in snap.offers if o.gpu_class == gpu_class)
            return result


# Module-level singleton shared across the app.
offer_cache = OfferCache()
