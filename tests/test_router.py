"""Unit tests for the router ranking logic (no DB, no HTTP)."""
from datetime import datetime, timezone

from app.cache import OfferCache, ProviderSnapshot
from app.providers.base import Offer
from app.router import _rank, route
from app.stats import StatsTracker


def _snap(provider_id: str, offers: list[Offer]) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider_id=provider_id,
        healthy=True,
        offers=offers,
        polled_at=datetime.now(timezone.utc),
    )


def _offer(provider: str, price: float, available: bool = True) -> Offer:
    return Offer(provider=provider, gpu_class="H100", price_per_hr=price, available=available)


# ---------------------------------------------------------------------------
# _rank (unit)
# ---------------------------------------------------------------------------

def test_cheapest_picks_lowest_price():
    offers = [_offer("a", 3.0), _offer("b", 1.5), _offer("c", 2.0)]
    ranked = _rank(offers, "cheapest")
    assert ranked[0].provider == "b"
    assert ranked[0].price_per_hr == 1.5


def test_reliable_sorts_by_availability_rate_then_price(monkeypatch):
    tracker = StatsTracker()
    tracker.record_poll("good", "H100", available=True)
    tracker.record_poll("good", "H100", available=True)
    tracker.record_poll("flaky", "H100", available=True)
    tracker.record_poll("flaky", "H100", available=False)

    monkeypatch.setattr("app.router.stats_tracker", tracker)

    offers = [
        Offer(provider="flaky", gpu_class="H100", price_per_hr=1.0, available=True),
        Offer(provider="good", gpu_class="H100", price_per_hr=2.0, available=True),
    ]
    ranked = _rank(offers, "reliable")
    assert ranked[0].provider == "good"


def test_fastest_sorts_by_provision_time_then_price(monkeypatch):
    tracker = StatsTracker()
    tracker.record_provision("fast", "H100", 20.0)
    tracker.record_provision("fast", "H100", 40.0)
    tracker.record_provision("slow", "H100", 240.0)

    monkeypatch.setattr("app.router.stats_tracker", tracker)

    offers = [
        Offer(provider="slow", gpu_class="H100", price_per_hr=0.5, available=True),
        Offer(provider="fast", gpu_class="H100", price_per_hr=1.0, available=True),
    ]
    ranked = _rank(offers, "fastest")
    assert ranked[0].provider == "fast"


def test_fastest_ranks_providers_without_history_last(monkeypatch):
    tracker = StatsTracker()
    tracker.record_provision("measured", "H100", 300.0)

    monkeypatch.setattr("app.router.stats_tracker", tracker)

    offers = [
        Offer(provider="unknown", gpu_class="H100", price_per_hr=0.5, available=True),
        Offer(provider="measured", gpu_class="H100", price_per_hr=1.0, available=True),
    ]
    ranked = _rank(offers, "fastest")
    assert [o.provider for o in ranked] == ["measured", "unknown"]


def test_fastest_without_any_history_falls_back_to_price(monkeypatch):
    monkeypatch.setattr("app.router.stats_tracker", StatsTracker())

    ranked = _rank([_offer("a", 3.0), _offer("b", 1.5)], "fastest")
    assert ranked[0].provider == "b"


def test_provision_average_only_covers_recent_samples():
    tracker = StatsTracker()
    for _ in range(20):
        tracker.record_provision("p", "H100", 600.0)
    for _ in range(20):
        tracker.record_provision("p", "H100", 30.0)

    assert tracker.avg_provision_seconds("p", "H100") == 30.0
    assert tracker.avg_provision_seconds("p", "A100") is None


def test_unknown_preference_falls_back_to_cheapest():
    offers = [_offer("a", 5.0), _offer("b", 1.0)]
    ranked = _rank(offers, "foobar")
    assert ranked[0].provider == "b"


# ---------------------------------------------------------------------------
# route (integration with cache)
# ---------------------------------------------------------------------------

def test_route_returns_none_when_cache_empty(monkeypatch):
    cache = OfferCache()
    monkeypatch.setattr("app.router.offer_cache", cache)
    assert route("H100", "cheapest") is None


def test_route_filters_unavailable_offers(monkeypatch):
    cache = OfferCache()
    cache.update(
        _snap("p1", [_offer("p1", 1.0, available=False), _offer("p1", 2.0, available=True)])
    )
    monkeypatch.setattr("app.router.offer_cache", cache)

    result = route("H100", "cheapest")
    assert result is not None
    assert result.chosen_offer.price_per_hr == 2.0


def test_route_picks_cheapest_across_providers(monkeypatch):
    cache = OfferCache()
    cache.update(_snap("runpod", [_offer("runpod", 3.0)]))
    cache.update(_snap("vast", [_offer("vast", 1.5)]))
    monkeypatch.setattr("app.router.offer_cache", cache)

    result = route("H100", "cheapest")
    assert result is not None
    assert result.chosen_offer.provider == "vast"
    assert len(result.ranked_offers) == 2
