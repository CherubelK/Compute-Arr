from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app.models import Job, RoutingDecision

router = APIRouter()


@router.get("/usage")
def usage(
    db: Session = Depends(get_db),
    _: None = Depends(require_api_key),
):
    jobs = db.query(Job).all()

    total_jobs = len(jobs)
    completed = [j for j in jobs if j.status == "succeeded"]
    failed = [j for j in jobs if j.status == "failed"]
    total_cost = sum(float(j.actual_cost or 0) for j in completed)

    by_provider: dict[str, int] = {}
    for j in jobs:
        if j.chosen_provider:
            by_provider[j.chosen_provider] = by_provider.get(j.chosen_provider, 0) + 1

    estimated_savings = _compute_savings(completed, db)
    savings_pct = (estimated_savings / (total_cost + estimated_savings) * 100) if (total_cost + estimated_savings) > 0 else 0.0

    return {
        "total_jobs": total_jobs,
        "completed_jobs": len(completed),
        "failed_jobs": len(failed),
        "total_cost_usd": round(total_cost, 4),
        "by_provider": by_provider,
        "estimated_savings_usd": round(estimated_savings, 4),
        "savings_pct": round(savings_pct, 2),
    }


def _compute_savings(completed_jobs: list[Job], db: Session) -> float:
    """
    Estimate savings vs. the baseline provider for each completed job.

    For each job we look up the routing decision's ranked_offers, find what
    the baseline provider would have charged at that moment, and compute:
        savings = (baseline_price - chosen_price) * duration_hours
    where duration_hours is back-calculated from actual_cost / chosen_price.
    """
    total = 0.0
    for job in completed_jobs:
        if not job.actual_cost or not job.chosen_provider:
            continue

        rd = db.query(RoutingDecision).filter(RoutingDecision.job_id == job.id).first()
        if not rd:
            continue

        chosen_offer = next(
            (o for o in rd.ranked_offers if o["provider"] == job.chosen_provider), None
        )
        baseline_offer = next(
            (o for o in rd.ranked_offers if o["provider"] == job.baseline_provider), None
        )

        if not chosen_offer or not baseline_offer:
            continue
        if chosen_offer["price_per_hr"] == 0:
            continue

        duration_hours = float(job.actual_cost) / chosen_offer["price_per_hr"]
        baseline_cost = baseline_offer["price_per_hr"] * duration_hours
        total += baseline_cost - float(job.actual_cost)

    return total
