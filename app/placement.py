"""
Job placement with automatic failover.

Walks the router's ranking one provider at a time: if a provider rejects the
job, the next-best provider is tried and a FailoverEvent is recorded.
"""
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import FailoverEvent, Job
from app.providers.base import JobSpec, Offer, ProviderAdapter, ProviderError


@dataclass
class Placement:
    provider_job_id: str
    offer: Offer


def best_offer_per_provider(ranked_offers: list[Offer], exclude: Collection[str] = ()) -> list[Offer]:
    """
    Collapse ranked offers to each provider's best-ranked one, keeping rank order.

    An adapter's submit() picks its own best instance, so retrying a second
    offer from the same provider would just repeat the attempt that failed.
    """
    seen = set(exclude)
    candidates: list[Offer] = []
    for offer in ranked_offers:
        if offer.provider not in seen:
            seen.add(offer.provider)
            candidates.append(offer)
    return candidates


def place_with_failover(
    ranked_offers: list[Offer],
    job_spec: JobSpec,
    adapters: dict[str, ProviderAdapter],
    db: Session,
    job_id: uuid.UUID,
    *,
    max_attempts: int,
    exclude: Collection[str] = (),
    failed_provider: str | None = None,
    failure_reason: str = "",
) -> Placement:
    """
    Submit to the best-ranked provider, advancing to the next on failure.

    `exclude` skips providers already tried for this job. Pass
    `failed_provider` / `failure_reason` when re-placing a job that died after
    launch, so the first attempt here is logged as a failover from it.
    Raises ProviderError if no provider accepts the job.
    """
    last_provider, last_error = failed_provider, failure_reason
    attempts = 0

    for offer in best_offer_per_provider(ranked_offers, exclude):
        if attempts >= max_attempts:
            break
        adapter = adapters.get(offer.provider)
        if adapter is None:
            continue

        if last_provider is not None:
            db.add(
                FailoverEvent(
                    job_id=job_id,
                    from_provider=last_provider,
                    to_provider=offer.provider,
                    reason=last_error,
                )
            )
            db.flush()

        attempts += 1
        try:
            return Placement(provider_job_id=adapter.submit(job_spec), offer=offer)
        except ProviderError as exc:
            last_provider, last_error = offer.provider, str(exc)

    if attempts == 0:
        raise ProviderError("No untried provider is available for this job")
    raise ProviderError(f"All providers exhausted after {attempts} attempt(s). Last error: {last_error}")


def record_placement(job: Job, placement: Placement) -> None:
    """Point the job at the provider that accepted it."""
    job.chosen_provider = placement.offer.provider
    job.provider_job_id = placement.provider_job_id
    job.price_per_hr = placement.offer.price_per_hr
    job.placed_at = datetime.now(timezone.utc)
    job.status = "pending"
