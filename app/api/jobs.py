import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import settings
from app.database import get_db
from app.dependencies import get_adapters
from app.models import FailoverEvent, Job, RoutingDecision
from app.providers.base import JobSpec, Offer, ProviderAdapter, ProviderError
from app.router import route

router = APIRouter(prefix="/jobs")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class JobRequest(BaseModel):
    gpu_class: str
    preference: str = "cheapest"
    image: str
    command: Optional[str] = None
    env_vars: Optional[dict[str, str]] = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    chosen_provider: str
    price_per_hr: float
    reasoning: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _submit_with_failover(
    ranked_offers: list[Offer],
    job_spec: JobSpec,
    adapters: dict[str, ProviderAdapter],
    db: Session,
    job_id: uuid.UUID,
) -> tuple[str, Offer]:
    """
    Try each ranked offer in order (up to 3). On failure, log a FailoverEvent
    and advance to the next candidate. Raises ProviderError if all fail.
    """
    last_attempted: Optional[str] = None
    last_error = ""

    for offer in ranked_offers[:3]:
        adapter = adapters.get(offer.provider)
        if adapter is None:
            last_error = f"No adapter configured for provider '{offer.provider}'"
            continue

        if last_attempted is not None:
            db.add(
                FailoverEvent(
                    job_id=job_id,
                    from_provider=last_attempted,
                    to_provider=offer.provider,
                    reason=last_error,
                )
            )
            db.flush()

        last_attempted = offer.provider
        try:
            provider_job_id = adapter.submit(job_spec)
            return provider_job_id, offer
        except ProviderError as exc:
            last_error = str(exc)

    raise ProviderError(f"All providers exhausted. Last error: {last_error}")


def _offer_to_dict(o: Offer) -> dict[str, Any]:
    return {
        "provider": o.provider,
        "gpu_class": o.gpu_class,
        "price_per_hr": o.price_per_hr,
        "available": o.available,
        "region": o.region,
        "raw_offer_id": o.raw_offer_id,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=201, response_model=JobResponse)
def submit_job(
    body: JobRequest,
    db: Session = Depends(get_db),
    adapters: dict = Depends(get_adapters),
    _: None = Depends(require_api_key),
):
    # 1. Route
    result = route(body.gpu_class, body.preference)
    if result is None:
        raise HTTPException(
            status_code=503,
            detail=f"No available providers for GPU class '{body.gpu_class}'. "
                   "Check GET /providers or wait for the next poll cycle.",
        )

    # 2. Create a pending job row so we have an ID for failover logging
    job = Job(
        id=uuid.uuid4(),
        gpu_class=body.gpu_class,
        preference=body.preference,
        image=body.image,
        command=body.command,
        env_vars=body.env_vars,
        baseline_provider=settings.baseline_provider,
        status="pending",
    )
    db.add(job)
    db.flush()

    # 3. Submit with automatic failover
    job_spec = JobSpec(
        gpu_class=body.gpu_class,
        preference=body.preference,
        image=body.image,
        command=body.command,
        env_vars=body.env_vars or {},
    )
    try:
        provider_job_id, winning_offer = _submit_with_failover(
            result.ranked_offers, job_spec, adapters, db, job.id
        )
    except ProviderError as exc:
        job.status = "failed"
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc))

    # 4. Persist final job state + routing decision
    job.chosen_provider = winning_offer.provider
    job.provider_job_id = provider_job_id
    job.status = "running"

    db.add(
        RoutingDecision(
            job_id=job.id,
            ranked_offers=[_offer_to_dict(o) for o in result.ranked_offers],
            chosen_provider=winning_offer.provider,
            reason=result.reason,
        )
    )
    db.commit()

    return JobResponse(
        job_id=str(job.id),
        status=job.status,
        chosen_provider=winning_offer.provider,
        price_per_hr=winning_offer.price_per_hr,
        reasoning=result.reason,
    )


@router.get("/{job_id}")
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    adapters: dict = Depends(get_adapters),
    _: None = Depends(require_api_key),
):
    job = db.get(Job, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Poll provider for live status if the job is still active
    if job.status in ("pending", "running") and job.provider_job_id and job.chosen_provider:
        adapter = adapters.get(job.chosen_provider)
        if adapter:
            try:
                s = adapter.status(job.provider_job_id)
                job.status = s.state
                if s.cost_so_far is not None:
                    job.actual_cost = s.cost_so_far
                db.commit()
            except ProviderError:
                pass  # stale data is better than a 503

    return {
        "job_id": str(job.id),
        "gpu_class": job.gpu_class,
        "preference": job.preference,
        "image": job.image,
        "status": job.status,
        "chosen_provider": job.chosen_provider,
        "provider_job_id": job.provider_job_id,
        "actual_cost": float(job.actual_cost) if job.actual_cost is not None else None,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


@router.get("/{job_id}/logs")
def get_job_logs(
    job_id: str,
    db: Session = Depends(get_db),
    adapters: dict = Depends(get_adapters),
    _: None = Depends(require_api_key),
):
    job = db.get(Job, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.provider_job_id or not job.chosen_provider:
        return {"logs": ""}

    adapter = adapters.get(job.chosen_provider)
    if adapter is None:
        raise HTTPException(status_code=503, detail="Provider adapter not configured")

    try:
        logs = adapter.logs(job.provider_job_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {"logs": logs}
