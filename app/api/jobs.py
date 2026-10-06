import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import settings
from app.database import get_db
from app.dependencies import get_adapters
from app.models import Job, RoutingDecision
from app.monitor import ACTIVE_STATES, job_cost
from app.placement import place_with_failover, record_placement
from app.providers.base import JobSpec, Offer, ProviderError
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

def _offer_to_dict(o: Offer) -> dict[str, Any]:
    return {
        "provider": o.provider,
        "gpu_class": o.gpu_class,
        "price_per_hr": o.price_per_hr,
        "available": o.available,
        "region": o.region,
        "raw_offer_id": o.raw_offer_id,
    }


def _job_view(job: Job) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "gpu_class": job.gpu_class,
        "preference": job.preference,
        "image": job.image,
        "status": job.status,
        "chosen_provider": job.chosen_provider,
        "provider_job_id": job.provider_job_id,
        "price_per_hr": float(job.price_per_hr) if job.price_per_hr is not None else None,
        "actual_cost": float(job.actual_cost) if job.actual_cost is not None else None,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _get_job_or_404(db: Session, job_id: uuid.UUID, for_update: bool = False) -> Job:
    job = db.get(Job, job_id, with_for_update=for_update)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


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

    # 2. Persist the job and the router's decision before placing anything,
    #    so the decision is kept even if every provider turns the job down.
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
    db.add(
        RoutingDecision(
            job_id=job.id,
            ranked_offers=[_offer_to_dict(o) for o in result.ranked_offers],
            chosen_provider=result.chosen_offer.provider,
            reason=result.reason,
        )
    )
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
        placement = place_with_failover(
            result.ranked_offers,
            job_spec,
            adapters,
            db,
            job.id,
            max_attempts=settings.max_placement_attempts,
        )
    except ProviderError as exc:
        job.status = "failed"
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc))

    # 4. Persist where the job landed
    record_placement(job, placement)
    db.commit()

    winner = placement.offer
    reasoning = result.reason
    if winner.provider != result.chosen_offer.provider:
        reasoning += f"; failed over to {winner.provider} at ${winner.price_per_hr}/hr"

    return JobResponse(
        job_id=str(job.id),
        status=job.status,
        chosen_provider=winner.provider,
        price_per_hr=winner.price_per_hr,
        reasoning=reasoning,
    )


@router.get("/{job_id}")
def get_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: None = Depends(require_api_key),
):
    # Status and cost are kept current by the background job monitor.
    return _job_view(_get_job_or_404(db, job_id))


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    adapters: dict = Depends(get_adapters),
    _: None = Depends(require_api_key),
):
    # Lock the row so the job monitor can't act on this job mid-cancel.
    job = _get_job_or_404(db, job_id, for_update=True)
    if job.status not in ACTIVE_STATES:
        raise HTTPException(status_code=409, detail=f"Job is already {job.status}")

    adapter = adapters.get(job.chosen_provider)
    if adapter is None:
        raise HTTPException(status_code=503, detail="Provider adapter not configured")

    # Capture the final cost first; some providers drop the record on cancel.
    try:
        reported_cost = adapter.status(job.provider_job_id).cost_so_far
    except ProviderError:
        reported_cost = None

    try:
        adapter.cancel(job.provider_job_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    cost = job_cost(job, reported_cost)
    if cost is not None:
        job.actual_cost = cost
    job.status = "cancelled"
    db.commit()

    return _job_view(job)


@router.get("/{job_id}/logs")
def get_job_logs(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    adapters: dict = Depends(get_adapters),
    _: None = Depends(require_api_key),
):
    job = _get_job_or_404(db, job_id)
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
