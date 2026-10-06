"""
Router v1 — rules-based provider selection.

Preferences:
  cheapest  → lowest price/hr
  reliable  → highest availability rate (from poller stats), then price
  fastest   → lowest recent provision time (from the job monitor), then price
"""
from dataclasses import dataclass

from app.cache import offer_cache
from app.providers.base import Offer
from app.stats import stats_tracker


@dataclass
class RoutingResult:
    chosen_offer: Offer
    ranked_offers: list[Offer]
    reason: str


def route(gpu_class: str, preference: str) -> RoutingResult | None:
    """
    Return a RoutingResult for the best available provider, or None if the
    cache has no available offers for the requested GPU class.
    """
    offers = offer_cache.get_offers_for_gpu(gpu_class)
    available = [o for o in offers if o.available]
    if not available:
        return None

    ranked = _rank(available, preference)
    chosen = ranked[0]
    return RoutingResult(
        chosen_offer=chosen,
        ranked_offers=ranked,
        reason=(
            f"preference={preference}; "
            f"selected {chosen.provider} at ${chosen.price_per_hr}/hr "
            f"from {len(ranked)} candidate(s)"
        ),
    )


def _rank(offers: list[Offer], preference: str) -> list[Offer]:
    if preference == "cheapest":
        return sorted(offers, key=lambda o: o.price_per_hr)

    if preference == "reliable":
        return sorted(
            offers,
            key=lambda o: (
                -stats_tracker.availability_rate(o.provider, o.gpu_class),
                o.price_per_hr,
            ),
        )

    if preference == "fastest":
        # Providers with no observed provision time yet rank after those with
        # one; with no history at all this degrades to cheapest.
        def provision_key(o: Offer) -> tuple[bool, float, float]:
            avg = stats_tracker.avg_provision_seconds(o.provider, o.gpu_class)
            return (avg is None, avg or 0.0, o.price_per_hr)

        return sorted(offers, key=provision_key)

    # unknown preference: fall back to cheapest
    return sorted(offers, key=lambda o: o.price_per_hr)
