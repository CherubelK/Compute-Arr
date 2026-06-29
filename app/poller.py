"""
Background price/health poller.

Runs on a fixed interval (POLL_INTERVAL_SECONDS). For each enabled provider
and each tracked GPU class, it calls get_offers(), persists a PriceSnapshot
row (append-only), and refreshes the in-memory OfferCache.

The scheduler runs in a daemon thread (APScheduler BackgroundScheduler) so it
doesn't block the FastAPI event loop.
"""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.cache import OfferCache, ProviderSnapshot, offer_cache
from app.config import settings
from app.database import SessionLocal
from app.models import PriceSnapshot, Provider
from app.providers.base import ProviderAdapter, ProviderError
from app.stats import stats_tracker

logger = logging.getLogger(__name__)


def _ensure_provider_row(db, provider_id: str) -> None:
    """Insert a provider row if one doesn't exist yet (idempotent)."""
    if db.get(Provider, provider_id) is None:
        db.add(Provider(id=provider_id, display_name=provider_id.title(), enabled=True, healthy=True))
        db.flush()


def poll_once(
    adapters: dict[str, ProviderAdapter],
    cache: OfferCache,
    gpu_classes: list[str],
) -> None:
    """Single poll cycle. Called by the scheduler; safe to call directly in tests."""
    db = SessionLocal()
    try:
        for provider_id, adapter in adapters.items():
            all_offers = []
            healthy = True

            _ensure_provider_row(db, provider_id)

            for gpu_class in gpu_classes:
                try:
                    offers = adapter.get_offers(gpu_class)
                    all_offers.extend(offers)
                    for offer in offers:
                        stats_tracker.record_poll(provider_id, gpu_class, offer.available)
                        db.add(
                            PriceSnapshot(
                                provider_id=provider_id,
                                gpu_class=gpu_class,
                                price_per_hr=float(offer.price_per_hr),
                                available=offer.available,
                                region=offer.region,
                                raw_offer_id=offer.raw_offer_id,
                            )
                        )
                except ProviderError as exc:
                    logger.warning("Poll failed %s/%s: %s", provider_id, gpu_class, exc)
                    healthy = False

            provider = db.get(Provider, provider_id)
            if provider:
                provider.healthy = healthy
                provider.last_checked_at = datetime.now(timezone.utc)

            db.commit()

            cache.update(
                ProviderSnapshot(
                    provider_id=provider_id,
                    healthy=healthy,
                    offers=all_offers,
                    polled_at=datetime.now(timezone.utc),
                )
            )
            logger.info(
                "Polled %s — %d offers, healthy=%s",
                provider_id,
                len(all_offers),
                healthy,
            )

    except Exception as exc:
        logger.error("Poller unexpected error: %s", exc, exc_info=True)
        db.rollback()
    finally:
        db.close()


def start_poller(adapters: dict[str, ProviderAdapter]) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        poll_once,
        trigger="interval",
        seconds=settings.poll_interval_seconds,
        args=[adapters, offer_cache, settings.gpu_classes],
        id="price_poller",
        next_run_time=datetime.now(timezone.utc),  # run immediately on startup
    )
    scheduler.start()
    logger.info(
        "Poller started — interval=%ds, providers=%s, gpu_classes=%s",
        settings.poll_interval_seconds,
        list(adapters),
        settings.gpu_classes,
    )
    return scheduler
