"""
Background job monitor.

Runs on a fixed interval (JOB_SYNC_INTERVAL_SECONDS). For each active job it
asks the provider for the current state and cost, so job rows and GET /usage
stay current without a client having to poll.

It also completes failover: a job that dies within EARLY_DEATH_WINDOW_SECONDS
of being placed is moved to the next-best provider that hasn't been tried.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Job
from app.placement import place_with_failover, record_placement
from app.providers.base import JobSpec, JobStatus, ProviderAdapter, ProviderError
from app.router import route
from app.stats import stats_tracker

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("pending", "running")


def _seconds_since_placement(job: Job) -> float | None:
    if job.placed_at is None:
        return None
    placed_at = job.placed_at
    if placed_at.tzinfo is None:  # SQLite (tests) drops the timezone
        placed_at = placed_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - placed_at).total_seconds()


def job_cost(job: Job, reported_cost: float | None) -> float | None:
    """
    Cost so far: the provider's figure when it reports one, otherwise an
    estimate from the hourly price the job was placed at.
    """
    if reported_cost is not None:
        return reported_cost
    elapsed = _seconds_since_placement(job)
    if elapsed is None or job.price_per_hr is None:
        return None
    return round(float(job.price_per_hr) * elapsed / 3600, 4)


def _died_early(job: Job) -> bool:
    elapsed = _seconds_since_placement(job)
    return elapsed is not None and elapsed <= settings.early_death_window_seconds


def _fail_over(db: Session, job: Job, adapters: dict[str, ProviderAdapter]) -> bool:
    """Re-place a job that died early. Returns False if it could not be moved."""
    tried = {event.from_provider for event in job.failover_events} | {job.chosen_provider}
    attempts_left = settings.max_placement_attempts - len(tried)
    if attempts_left <= 0:
        return False

    result = route(job.gpu_class, job.preference)
    if result is None:
        return False

    dead_provider, dead_job_id = job.chosen_provider, job.provider_job_id
    job_spec = JobSpec(
        gpu_class=job.gpu_class,
        preference=job.preference,
        image=job.image,
        command=job.command,
        env_vars=job.env_vars or {},
    )
    try:
        placement = place_with_failover(
            result.ranked_offers,
            job_spec,
            adapters,
            db,
            job.id,
            max_attempts=attempts_left,
            exclude=tried,
            failed_provider=dead_provider,
            failure_reason=f"job died within {settings.early_death_window_seconds}s of placement",
        )
    except ProviderError as exc:
        logger.warning("Job %s died on %s and could not be re-placed: %s", job.id, dead_provider, exc)
        return False

    # Best effort: a dead instance can still be billed until it is released.
    try:
        adapters[dead_provider].cancel(dead_job_id)
    except ProviderError as exc:
        logger.warning("Could not release dead job %s on %s: %s", dead_job_id, dead_provider, exc)

    record_placement(job, placement)
    logger.info("Job %s failed over %s -> %s", job.id, dead_provider, placement.offer.provider)
    return True


def _apply_status(db: Session, job: Job, status: JobStatus, adapters: dict[str, ProviderAdapter]) -> None:
    cost = job_cost(job, status.cost_so_far)
    if cost is not None:
        job.actual_cost = cost

    if status.state == "failed" and _died_early(job) and _fail_over(db, job, adapters):
        return

    if job.status == "pending" and status.state == "running":
        elapsed = _seconds_since_placement(job)
        if elapsed is not None:
            stats_tracker.record_provision(job.chosen_provider, job.gpu_class, elapsed)

    job.status = status.state


def _sync_job(db: Session, job_id: uuid.UUID, adapters: dict[str, ProviderAdapter]) -> None:
    job = db.get(Job, job_id)
    adapter = adapters.get(job.chosen_provider) if job else None
    if adapter is None:
        return

    provider_job_id = job.provider_job_id
    try:
        status = adapter.status(provider_job_id)
    except ProviderError as exc:
        logger.warning("Status check failed for job %s on %s: %s", job_id, job.chosen_provider, exc)
        return

    # The provider call above ran without a lock. Re-read the row under one so
    # a cancel that landed in the meantime is not overwritten or failed over.
    db.refresh(job, with_for_update=True)
    if job.status not in ACTIVE_STATES or job.provider_job_id != provider_job_id:
        return

    _apply_status(db, job, status, adapters)


def sync_jobs_once(adapters: dict[str, ProviderAdapter]) -> None:
    """Single monitor cycle. Called by the scheduler; safe to call directly in tests."""
    db = SessionLocal()
    try:
        job_ids = db.scalars(
            select(Job.id).where(Job.status.in_(ACTIVE_STATES), Job.provider_job_id.is_not(None))
        ).all()
        for job_id in job_ids:
            try:
                _sync_job(db, job_id, adapters)
                db.commit()
            except Exception:
                logger.exception("Job monitor failed on job %s", job_id)
                db.rollback()
    finally:
        db.close()
