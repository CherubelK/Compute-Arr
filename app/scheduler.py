"""
Background scheduler: runs the price poller and the job monitor.

Both run in a daemon thread (APScheduler BackgroundScheduler) so they don't
block the FastAPI event loop.
"""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.cache import offer_cache
from app.config import settings
from app.monitor import sync_jobs_once
from app.poller import poll_once
from app.providers.base import ProviderAdapter

logger = logging.getLogger(__name__)


def start_scheduler(adapters: dict[str, ProviderAdapter]) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        poll_once,
        trigger="interval",
        seconds=settings.poll_interval_seconds,
        args=[adapters, offer_cache, settings.gpu_classes],
        id="price_poller",
        next_run_time=datetime.now(timezone.utc),  # run immediately on startup
    )
    scheduler.add_job(
        sync_jobs_once,
        trigger="interval",
        seconds=settings.job_sync_interval_seconds,
        args=[adapters],
        id="job_monitor",
    )
    scheduler.start()
    logger.info(
        "Scheduler started — poll=%ds, job sync=%ds, providers=%s, gpu_classes=%s",
        settings.poll_interval_seconds,
        settings.job_sync_interval_seconds,
        list(adapters),
        settings.gpu_classes,
    )
    return scheduler
